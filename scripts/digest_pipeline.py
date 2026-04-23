"""
End-to-end digest pipeline: fetch → dedup → LLM summarize → Telegram → persist.

Calls the Anthropic messages API directly with a Claude Max OAuth bearer so the
cost is covered by the subscription (no API billing). Replaces the Claude CLI
orchestration used by /tech-digest when running headless in CI.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from src.dedup import _items_from_json, filter_new, load_seen, save_seen
from src.models import DigestEntry, FeedItem
from src.telegram import TelegramError, send_message

logger = logging.getLogger("digest_pipeline")

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEN_PATH = REPO_ROOT / "data" / "seen.json"
TOPICS_PATH = REPO_ROOT / "config" / "topics.yaml"

KYIV_TZ = ZoneInfo("Europe/Kyiv")

CATEGORY_EMOJI = {
    "lab": "🧪",
    "release": "📦",
    "media": "📰",
    "community": "💬",
}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"
ANTHROPIC_BETA = "oauth-2025-04-20"
# Claude Max OAuth requires identifying as Claude Code at the system-prompt layer.
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def env_required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"ERROR: {name} is required")
    return val


def tg_error(text: str, token: str, chat_id: str) -> None:
    """Best-effort error notification — never raises."""
    try:
        send_message(text, token, chat_id)
    except TelegramError as exc:
        logger.error("Failed to notify Telegram of error: %s", exc)


def fetch_items() -> list[FeedItem]:
    """Run src.fetcher as subprocess and parse its JSON output."""
    result = subprocess.run(
        [sys.executable, "-m", "src.fetcher"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        logger.error("fetcher failed: rc=%d stderr=%s", result.returncode, result.stderr[-500:])
        raise RuntimeError("fetcher failed")
    return _items_from_json(result.stdout)


def render_date_uk() -> str:
    return datetime.now(KYIV_TZ).strftime("%d.%m.%Y")


def build_llm_prompt(items: list[FeedItem], window_hours: int, max_items: int) -> str:
    compact = [
        {
            "url": i.url,
            "title": i.title,
            "source": i.source,
            "category": i.category,
            "published": i.published.isoformat(),
            "raw_summary": (i.raw_summary or "")[:500],
        }
        for i in items
    ]
    return f"""Ти готуєш AI/tech дайджест українською. На вхід — список новин за останні {window_hours} годин у JSON.

Для кожного item обери:
- `summary_uk` — 1-2 речення українською (150-250 символів), суто фактичний зміст, без інтерпретацій, без emoji. Англійську термінологію залиши (LLM, inference, fine-tuning) де немає усталеного перекладу. Не повторюй `source` у summary (він в заголовку). Якщо GitHub releases feed pre-release/rc — познач "(pre-release)".
- `importance` 1-5:
  - 5: major реліз великої лабораторії (нова модель Claude/GPT/Gemini, великий open-source реліз LLaMA/Mistral, критична вразливість, поглинання)
  - 4: нові фічі в продуктах OpenAI/Anthropic/Google, значний реліз фреймворку (vLLM, transformers 5.x)
  - 3: research papers, benchmarks, технічні deep-dives, мінорні релізи популярних проєктів
  - 2: industry news, commentary, tutorials, community threads
  - 1: tangential mentions, low-effort posts

Правила фільтрації:
- Якщо items > 10 — викинь усе з importance ≤ 2
- Викинь явні дублікати за темою (різні джерела про один реліз — залиш авторитетне)
- Сортуй importance DESC, потім published DESC
- Залиш топ {max_items}

Поверни **лише** JSON-масив (без markdown fences, без коментарів) таких об'єктів:
{{"url": "...", "title": "...", "source": "...", "category": "...", "summary_uk": "...", "importance": 1-5}}

Порядок у відповіді = порядок у дайджесті.

INPUT:
{json.dumps(compact, ensure_ascii=False)}
"""


def call_llm(prompt: str, oauth_token: str) -> str:
    response = requests.post(
        ANTHROPIC_URL,
        headers={
            "Authorization": f"Bearer {oauth_token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": ANTHROPIC_BETA,
            "content-type": "application/json",
            "user-agent": "claude-cli/2.1.118 (external, tech_news_bot)",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 8000,
            "system": CLAUDE_CODE_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError(f"Empty LLM response: {json.dumps(data)[:500]}")
    return text


def parse_llm_json(text: str) -> list[dict]:
    # Strip possible markdown fences defensively.
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"No JSON array in LLM response: {text[:500]}")
    return json.loads(match.group())


def load_topics_registry() -> dict[str, dict]:
    """Return dict slug → topic entry (name, emoji, default_active, description)."""
    if not TOPICS_PATH.exists():
        return {}
    data = yaml.safe_load(TOPICS_PATH.read_text(encoding="utf-8")) or {}
    return {t["slug"]: t for t in data.get("topics", [])}


def parse_active_topics(env_value: str, registry: dict[str, dict]) -> set[str]:
    """
    Parse DIGEST_TOPICS env (comma-separated) into a set of slugs.
    Empty string / unset → empty set (= disable filter = all items pass).
    Unknown slugs are logged but not fatal (so a typo doesn't break the run).
    """
    if not env_value or not env_value.strip():
        return set()
    requested = {s.strip() for s in env_value.split(",") if s.strip()}
    unknown = requested - registry.keys()
    if unknown:
        logger.warning("DIGEST_TOPICS has unknown slugs (ignored): %s", sorted(unknown))
    return requested & registry.keys()


def filter_by_topics(items: list[FeedItem], active: set[str]) -> list[FeedItem]:
    """Keep items that have at least one topic in `active`. Empty active → pass-through."""
    if not active:
        return items
    return [i for i in items if any(t in active for t in i.topics)]


def render_topics_header_suffix(active: set[str], registry: dict[str, dict]) -> str:
    """Render '[🎨 Design · 🧪 AI labs]' suffix; empty string if no filter."""
    if not active:
        return ""
    pieces = []
    for slug in sorted(active):
        entry = registry.get(slug, {})
        pieces.append(f"{entry.get('emoji', '•')} {entry.get('name', slug)}")
    return f" [{' · '.join(pieces)}]"


def render_digest_html(
    entries: list[dict],
    window_hours: int,
    topics_suffix: str = "",
) -> str:
    date_uk = render_date_uk()
    header = (
        f"<b>🤖 AI/Tech дайджест{topics_suffix} — {date_uk}</b>\n\n"
        f"{len(entries)} новин за останні {window_hours}г\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    blocks: list[str] = []
    for idx, e in enumerate(entries, start=1):
        emoji = CATEGORY_EMOJI.get(e.get("category", ""), "•")
        title = html.escape(e.get("title", "").strip())
        source = html.escape(e.get("source", "").strip())
        summary = html.escape(e.get("summary_uk", "").strip())
        url = e.get("url", "").strip()
        blocks.append(
            f"<b>{idx}. [{emoji}] {title}</b>\n"
            f"<i>{source}</i>\n"
            f"{summary}\n"
            f'<a href="{url}">Читати →</a>'
        )

    footer = "<i>Наступний дайджест завтра о 10:00</i>"
    return header + "\n\n".join(blocks) + "\n\n" + footer


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    tg_token = env_required("TELEGRAM_BOT_TOKEN")
    tg_chat = env_required("TELEGRAM_CHAT_ID")
    oauth = env_required("CLAUDE_CODE_OAUTH_TOKEN")
    window = int(os.environ.get("DIGEST_WINDOW_HOURS", "24"))
    max_items = int(os.environ.get("DIGEST_MAX_ITEMS", "15"))

    topics_registry = load_topics_registry()
    active_topics = parse_active_topics(os.environ.get("DIGEST_TOPICS", ""), topics_registry)
    topics_suffix = render_topics_header_suffix(active_topics, topics_registry)
    if active_topics:
        logger.info("Active topics filter: %s", sorted(active_topics))

    try:
        all_items = fetch_items()
    except Exception as exc:
        tg_error(f"❌ fetcher failed: {exc}", tg_token, tg_chat)
        return 1

    logger.info("Fetched %d items", len(all_items))

    seen = load_seen(SEEN_PATH)
    new_items = filter_new(all_items, seen, window)
    logger.info("New items: %d of %d", len(new_items), len(all_items))

    before_topic_filter = len(new_items)
    new_items = filter_by_topics(new_items, active_topics)
    if active_topics:
        logger.info("After topic filter: %d of %d", len(new_items), before_topic_filter)

    date_uk = render_date_uk()
    if not new_items:
        send_message(
            f"<b>AI/Tech дайджест{topics_suffix} — {date_uk}</b>\n\n"
            f"Нових новин за останні {window}г немає.",
            tg_token,
            tg_chat,
        )
        print(f"total={len(all_items)} new=0 sent=0 tg_status=ok")
        return 0

    try:
        prompt = build_llm_prompt(new_items, window, max_items)
        raw = call_llm(prompt, oauth)
        entries = parse_llm_json(raw)
    except Exception as exc:
        tg_error(f"❌ LLM step failed: {exc}", tg_token, tg_chat)
        return 1

    if not entries:
        send_message(
            f"<b>AI/Tech дайджест{topics_suffix} — {date_uk}</b>\n\n"
            f"LLM повернув порожній список.",
            tg_token,
            tg_chat,
        )
        return 0

    digest_html = render_digest_html(entries, window, topics_suffix)
    try:
        send_message(digest_html, tg_token, tg_chat)
    except TelegramError as exc:
        logger.error("Telegram send failed: %s", exc)
        # Do NOT mark URLs seen so the next run retries.
        tg_error(f"❌ Telegram send failed: {exc}", tg_token, tg_chat)
        return 1

    # Mark all fetched-new URLs as seen, not only the digested ones, so they
    # don't reappear after the LLM filters them out.
    save_seen(SEEN_PATH, seen, [i.url for i in new_items])

    print(f"total={len(all_items)} new={len(new_items)} sent={len(entries)} tg_status=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
