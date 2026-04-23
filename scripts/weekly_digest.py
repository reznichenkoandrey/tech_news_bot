"""
Weekly deep-reads digest.

Reads every URL the user starred via ⭐ Save (see #6 inline buttons), makes
sure each has a cached long-form summary (`scripts.summarize_article`), then
posts a single HTML digest with title + TL;DR + link for each item and
archives the list so next week's digest starts fresh.

Invocation:
    python3 -m scripts.weekly_digest

Env (required, mirrors digest_pipeline.py):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    CLAUDE_CODE_OAUTH_TOKEN  (only needed if any item has no cached summary)

The workflow `.github/workflows/weekly.yml` commits the mutated
`data/reading_list.json` and `data/reading_archive.json`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.digest_pipeline import env_required, tg_error
from scripts.post_article_summary import markdown_to_tg_html
from scripts.summarize_article import summarize
from src.telegram import TelegramError, send_message

logger = logging.getLogger("weekly_digest")

REPO_ROOT = Path(__file__).resolve().parent.parent
READING_LIST_PATH = REPO_ROOT / "data" / "reading_list.json"
READING_ARCHIVE_PATH = REPO_ROOT / "data" / "reading_archive.json"

KYIV_TZ = ZoneInfo("Europe/Kyiv")


@dataclass
class ReadingEntry:
    url: str
    saved_at: str


@dataclass
class RenderedItem:
    url: str
    title: str
    tldr_html: str  # already Telegram-HTML
    failure: str | None = None


# ── Reading list I/O ──────────────────────────────────────────────────────────

def load_reading_list(path: Path) -> list[ReadingEntry]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("reading_list.json unreadable: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("reading_list.json has unexpected shape, ignoring")
        return []
    out: list[ReadingEntry] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        out.append(ReadingEntry(url=url, saved_at=str(row.get("saved_at") or "")))
    return out


def load_archive(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data) if isinstance(data, list) else []


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── Summary parsing ───────────────────────────────────────────────────────────

def parse_front_matter(md: str) -> tuple[dict[str, str], str]:
    """Split a summarize_article artefact into (frontmatter_dict, body)."""
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 3)
    if end == -1:
        return {}, md
    header = md[3:end].strip()
    body = md[end + 4 :].lstrip()
    meta: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def extract_section(body: str, heading: str) -> str:
    """
    Pull the markdown body under `## {heading}` up to the next `## ` heading.
    Returns empty string if the heading isn't present.
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(body)
    if not match:
        return ""
    return match.group(1).strip()


def hostname(url: str) -> str:
    m = re.match(r"^https?://([^/?#]+)", url)
    return m.group(1) if m else url


# ── Rendering ─────────────────────────────────────────────────────────────────

def render_item(entry: ReadingEntry, *, allow_llm: bool) -> RenderedItem:
    """
    Summarize (cached-first) and render title + TL;DR HTML block for the weekly
    digest. If the LLM is not available (no OAuth token) and the item isn't
    cached yet, mark it as a partial failure instead of bailing out.
    """
    try:
        md, _ = summarize(entry.url) if allow_llm else _cached_only(entry.url)
    except Exception as exc:
        logger.warning("summarize failed for %s: %s", entry.url, exc)
        return RenderedItem(
            url=entry.url,
            title=hostname(entry.url),
            tldr_html="",
            failure=str(exc)[:200],
        )

    meta, body = parse_front_matter(md)
    title = meta.get("title") or hostname(entry.url)
    tldr = extract_section(body, "TL;DR")
    tldr_html = markdown_to_tg_html(tldr) if tldr else "<i>TL;DR відсутній</i>"

    return RenderedItem(url=entry.url, title=title, tldr_html=tldr_html)


def _cached_only(url: str) -> tuple[str, Path]:
    """summarize() path that raises if nothing is cached yet."""
    from scripts.summarize_article import cache_path

    cp = cache_path(url)
    if not cp.exists():
        raise RuntimeError("not cached and no LLM token available")
    return cp.read_text(encoding="utf-8"), cp


def render_digest_html(items: list[RenderedItem], total: int) -> str:
    date_uk = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
    summarised = sum(1 for i in items if not i.failure)

    blocks: list[str] = []
    for idx, item in enumerate(items, start=1):
        title = _escape(item.title)
        if item.failure:
            blocks.append(
                f"<b>{idx}. {title}</b>\n"
                f"<i>⚠️ не вдалось підсумувати: {_escape(item.failure)}</i>\n"
                f'<a href="{item.url}">Читати →</a>'
            )
            continue
        blocks.append(
            f"<b>{idx}. {title}</b>\n"
            f"{item.tldr_html}\n"
            f'<a href="{item.url}">Читати →</a>'
        )

    header = (
        f"<b>📚 Deep reads тижня — {date_uk}</b>\n"
        f"{total} збережено · {summarised} підсумовано\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    footer = "<i>Reading list очищено. Наступний збір — наступної неділі.</i>"
    return header + "\n\n" + "\n\n".join(blocks) + "\n\n" + footer


def _escape(text: str) -> str:
    import html as _html
    return _html.escape(text)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    tg_token = env_required("TELEGRAM_BOT_TOKEN")
    tg_chat = env_required("TELEGRAM_CHAT_ID")
    oauth_available = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    entries = load_reading_list(READING_LIST_PATH)
    if not entries:
        logger.info("reading_list empty — nothing to send")
        # Silent exit — no point spamming the chat with an empty digest.
        return 0

    logger.info("Rendering %d saved items (oauth=%s)", len(entries), oauth_available)

    rendered: list[RenderedItem] = []
    for entry in entries:
        rendered.append(render_item(entry, allow_llm=oauth_available))

    # If the LLM is down and nothing is cached, bail out visibly instead of
    # sending an all-failures digest that the user would have to re-trigger.
    if all(item.failure for item in rendered):
        msg = "❌ weekly_digest: жодний saved item не має кешованого саммарі і LLM недоступний."
        tg_error(msg, tg_token, tg_chat)
        return 1

    html = render_digest_html(rendered, total=len(entries))
    try:
        send_message(html, tg_token, tg_chat)
    except TelegramError as exc:
        tg_error(f"❌ weekly_digest: Telegram send failed: {exc}", tg_token, tg_chat)
        return 1

    # Archive successful run: move reading_list contents into the archive
    # (append) and clear the list. Archive keeps {url, saved_at, archived_at}
    # so a future stats pass can see history.
    archived_at = datetime.now(KYIV_TZ).isoformat()
    archive = load_archive(READING_ARCHIVE_PATH)
    archive.extend(
        {"url": e.url, "saved_at": e.saved_at, "archived_at": archived_at}
        for e in entries
    )
    write_json(READING_ARCHIVE_PATH, archive)
    write_json(READING_LIST_PATH, [])

    print(
        f"saved={len(entries)} summarised="
        f"{sum(1 for i in rendered if not i.failure)} "
        f"failed={sum(1 for i in rendered if i.failure)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
