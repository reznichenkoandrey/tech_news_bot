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

from src.banner import render_digest_banner
from src.dedup import _items_from_json, filter_new, load_seen, save_seen
from src.models import DigestEntry, FeedItem
from src.telegram import TelegramError, send_message, send_photo

logger = logging.getLogger("digest_pipeline")

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEN_PATH = REPO_ROOT / "data" / "seen.json"
LAST_DIGEST_PATH = REPO_ROOT / "data" / "last_digest.json"
TOPICS_PATH = REPO_ROOT / "config" / "topics.yaml"
DIGESTS_PATH = REPO_ROOT / "config" / "digests.yaml"
USER_PREFS_PATH = REPO_ROOT / "config" / "user_prefs.yaml"

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
- `summary_uk` — рівно 2 речення українською (220-340 символів РАЗОМ), без emoji, без markdown:
  - **1-е речення** — суто фактичний зміст (що саме сталось: який реліз, яка фіча, які цифри, який вердикт). Без оцінок.
  - **2-е речення** — короткий тверезий інсайт: **чому це важливо ЗАРАЗ або що це змінить у найближчий тиждень-місяць** для розробника/команди/ринку. Не передбачення на роки, не маркетинг, не "це революція". Якщо інсайт справді не вартий згадки (минорний bugfix, чийсь твіт без наслідків) — друге речення може бути просто "Для більшості команд — без негайних дій." або аналогічне чесне.
  Англійську термінологію залиши (LLM, inference, fine-tuning) де немає усталеного перекладу. Не повторюй `source` у summary (він в заголовку). Якщо GitHub releases feed pre-release/rc — познач "(pre-release)" у 1-му реченні.
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

КРИТИЧНО про JSON:
- Кожен подвійний лапка всередині `title` чи `summary_uk` має бути екранована як \\".
  Приклад: "title": "The \\"Bug-Free\\" Workforce" — НЕ "The "Bug-Free" Workforce".
- Не використовуй raw newlines всередині рядків — заміни на пробіл або \\n.
- Перевір що відповідь розбирається `json.loads()` ДО надсилання.

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
            "temperature": 0,
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
    snippet = match.group()
    try:
        return json.loads(snippet)
    except json.JSONDecodeError as exc:
        # Surface the offending region so we can see what the model produced
        # without dumping the whole 8KB into Telegram.
        pos = exc.pos
        window = snippet[max(0, pos - 200) : pos + 200]
        logger.error("LLM raw response (first 2000 chars):\n%s", snippet[:2000])
        raise RuntimeError(
            f"LLM returned invalid JSON ({exc.msg} at char {pos}). "
            f"Around offset: …{window}…"
        ) from exc


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


def render_digest_header(
    count: int,
    window_hours: int,
    title: str = "🤖 AI/Tech дайджест",
) -> str:
    date_uk = render_date_uk()
    return (
        f"<b>{title} — {date_uk}</b>\n"
        f"{count} новин за останні {window_hours}г"
    )


def render_digest_item(idx: int, entry: dict) -> str:
    emoji = CATEGORY_EMOJI.get(entry.get("category", ""), "•")
    title_e = html.escape(entry.get("title", "").strip())
    source = html.escape(entry.get("source", "").strip())
    summary = html.escape(entry.get("summary_uk", "").strip())
    url = entry.get("url", "").strip()
    return (
        f"<b>{idx}. [{emoji}] {title_e}</b>\n"
        f"<i>{source}</i>\n"
        f"{summary}\n"
        f'<a href="{url}">Читати →</a>'
    )


def render_digest_footer() -> str:
    # Hint at the numbered text commands the Worker handles since we no longer
    # ship per-item inline buttons (one big post = no callback surface).
    return (
        "<i>Дії: <code>/save N</code> · <code>/hide N</code> · <code>/deep N</code></i>\n"
        "<i>Наступний дайджест завтра о 10:00</i>"
    )


def render_digest_html(
    entries: list[dict],
    *,
    header_text: str | None,
    start_idx: int = 1,
    include_footer: bool = True,
) -> str:
    """
    Render the digest body as a single HTML blob.

    `header_text` is the fully-formed HTML header (or None to omit, e.g. when
    a banner photo already carries the header). When provided, a divider rule
    is inserted between header and items.

    `start_idx` lets callers continue numbering across profiles so /save N
    references a globally unique item.
    """
    blocks = [render_digest_item(start_idx + i, e) for i, e in enumerate(entries)]
    parts: list[str] = []
    if header_text:
        parts.append(header_text)
        parts.append("━━━━━━━━━━━━━━━━━━━━")
    parts.append("\n\n".join(blocks))
    if include_footer:
        parts.append(render_digest_footer())
    return "\n\n".join(parts)


def render_profile_subheader(profile: dict) -> str:
    """Compact one-line header used when the banner already shows the umbrella title."""
    return f"<b>{profile['emoji']} {profile['name']}</b>"


def load_digest_configs() -> list[dict]:
    """
    Read config/digests.yaml and return list of {name, emoji, topics} dicts.
    Returns [] if the file doesn't exist (caller falls back to env/legacy behaviour).
    """
    if not DIGESTS_PATH.exists():
        return []
    data = yaml.safe_load(DIGESTS_PATH.read_text(encoding="utf-8")) or {}
    out: list[dict] = []
    for entry in data.get("digests", []):
        out.append(
            {
                "name": entry.get("name", "Digest"),
                "emoji": entry.get("emoji", "📰"),
                "topics": list(entry.get("topics", [])),
            }
        )
    return out


def run_digest(
    config: dict,
    new_items: list[FeedItem],
    *,
    oauth_token: str,
    tg_token: str,
    tg_chat: str,
    window: int,
    max_items: int,
    start_idx: int = 1,
    header_text: str | None,
    include_footer: bool = True,
) -> tuple[int, list[str], list[dict]]:
    """
    Execute one digest profile end-to-end (filter → LLM → send).

    Returns (items_sent, considered_urls, sent_entries). `considered_urls` is
    every URL matched by the topic filter (used for dedup). `sent_entries` is
    the LLM-curated top-N — main() folds it into data/last_digest.json so the
    Worker can resolve /save N, /hide N, /deep N to the correct URL.

    `header_text` is the HTML header for this profile's post (or None to
    omit when a preceding banner already carries the title). `start_idx` is
    the global counter offset so /save N stays unique when multiple profiles
    run back-to-back.

    Raises on LLM/network failure so caller can notify and continue with
    the next profile.
    """
    name = config["name"]

    filtered = filter_by_topics(new_items, set(config["topics"]))
    considered = [i.url for i in filtered]
    logger.info("[%s] %d items after topic filter", name, len(filtered))

    if not filtered:
        return (0, considered, [])

    prompt = build_llm_prompt(filtered, window, max_items)
    raw = call_llm(prompt, oauth_token)
    entries = parse_llm_json(raw)

    if not entries:
        logger.warning("[%s] LLM returned empty list", name)
        return (0, considered, [])

    text = render_digest_html(
        entries,
        header_text=header_text,
        start_idx=start_idx,
        include_footer=include_footer,
    )
    send_message(text, tg_token, tg_chat, disable_web_page_preview=True)

    logger.info("[%s] sent %d entries (one post)", name, len(entries))
    return (len(entries), considered, entries)


def save_last_digest(items: list[dict]) -> None:
    """
    Persist the per-number → url mapping the Worker reads to resolve
    /save N, /hide N, /deep N. Each entry is the minimum the Worker needs:
    n (1-based global index), url, title, profile.
    """
    LAST_DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
    }
    LAST_DIGEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_user_prefs_active_topics() -> set[str]:
    """User-configured global topic filter (via Telegram /add /remove)."""
    if not USER_PREFS_PATH.exists():
        return set()
    data = yaml.safe_load(USER_PREFS_PATH.read_text(encoding="utf-8")) or {}
    return {t for t in (data.get("active_topics") or []) if t}


def _overlay_with_user_filter(profiles: list[dict], user_active: set[str]) -> list[dict]:
    """
    Narrow every profile's topic list to the intersection with user_active.
    Profiles whose intersection is empty are dropped — the user doesn't want
    that stream right now. Returns the profile list unchanged if user_active
    is empty.
    """
    if not user_active:
        return profiles

    narrowed = []
    for p in profiles:
        profile_topics = set(p.get("topics") or [])
        if not profile_topics:
            # Empty means "all" in digest config; user filter becomes the list.
            narrowed.append({**p, "topics": sorted(user_active)})
            continue
        overlap = profile_topics & user_active
        if not overlap:
            continue
        narrowed.append({**p, "topics": sorted(overlap)})
    return narrowed


def resolve_digest_profiles() -> list[dict]:
    """
    Decide which digest profiles to run, in priority order:
    1. DIGEST_TOPICS env is set → single ad-hoc profile (backward compat)
    2. config/digests.yaml exists → its list, narrowed by user_prefs if any
    3. Fallback → single "AI/Tech" profile with no topic filter (also
       narrowed by user_prefs if any)
    """
    env_topics = os.environ.get("DIGEST_TOPICS", "").strip()
    if env_topics:
        topics_registry = load_topics_registry()
        active = parse_active_topics(env_topics, topics_registry)
        return [{"name": "AI/Tech", "emoji": "🤖", "topics": list(active)}]

    base = load_digest_configs() or [{"name": "AI/Tech", "emoji": "🤖", "topics": []}]
    return _overlay_with_user_filter(base, load_user_prefs_active_topics())


def _try_send_banner(
    *,
    new_items_count: int,
    window_hours: int,
    tg_token: str,
    tg_chat: str,
    date_uk: str,
) -> bool:
    """
    Render the digest banner to a temp file and send it as a Telegram photo
    with a short HTML caption. Returns True on success, False on any failure
    (caller falls back to text-only header).

    Never raises — banner is a visual nice-to-have, not a blocker.
    """
    banner_path = REPO_ROOT / "data" / "_banner.png"
    try:
        render_digest_banner(
            banner_path,
            count=new_items_count,
            profile_name="AI/Tech дайджест",
        )
    except Exception as exc:
        logger.warning("Banner render failed, falling back to text header: %s", exc)
        return False

    caption = (
        f"<b>🤖 AI/Tech дайджест</b>\n"
        f"<i>{date_uk} · {new_items_count} нових за {window_hours}г</i>"
    )
    try:
        send_photo(str(banner_path), tg_token, tg_chat, caption=caption, parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("Banner send failed, falling back to text header: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Unexpected banner send error: %s", exc)
        return False

    return True


def _compose_header_text(
    *,
    profile: dict,
    window_hours: int,
    banner_sent: bool,
    multi_profile: bool,
) -> str | None:
    """
    Decide the per-profile text header based on whether the banner photo went
    out and whether we're running multiple profiles.

    - banner sent + single profile → no text header (banner is enough)
    - banner sent + multi profile → compact one-line sub-header per profile
    - banner failed → full legacy header per profile (backward compatible)
    """
    if banner_sent and not multi_profile:
        return None
    if banner_sent and multi_profile:
        return render_profile_subheader(profile)
    # Legacy path (banner failed): plain text header per profile. We don't
    # know item count yet, so subtitle is just date + window — items render
    # right below, so the count is visible by scrolling.
    title = f"{profile['emoji']} {profile['name']} дайджест"
    date_uk = render_date_uk()
    return f"<b>{title} — {date_uk}</b>\n<i>за останні {window_hours}г</i>"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    tg_token = env_required("TELEGRAM_BOT_TOKEN")
    tg_chat = env_required("TELEGRAM_CHAT_ID")
    oauth = env_required("CLAUDE_CODE_OAUTH_TOKEN")
    window = int(os.environ.get("DIGEST_WINDOW_HOURS", "24"))
    max_items = int(os.environ.get("DIGEST_MAX_ITEMS", "15"))

    profiles = resolve_digest_profiles()
    logger.info("Running %d digest profile(s): %s", len(profiles), [p["name"] for p in profiles])

    try:
        all_items = fetch_items()
    except Exception as exc:
        tg_error(f"❌ fetcher failed: {exc}", tg_token, tg_chat)
        return 1

    logger.info("Fetched %d items", len(all_items))

    seen = load_seen(SEEN_PATH)
    new_items = filter_new(all_items, seen, window)
    logger.info("New items: %d of %d", len(new_items), len(all_items))

    date_uk = render_date_uk()

    # Try to render and send the header banner once (best-effort). If it works,
    # individual profile posts skip the full text header — banner already carries
    # the umbrella title. If anything goes wrong (PIL missing, font I/O, network),
    # we fall back to the legacy text-only header inside each profile.
    banner_sent = False
    if new_items:
        banner_sent = _try_send_banner(
            new_items_count=len(new_items),
            window_hours=window,
            tg_token=tg_token,
            tg_chat=tg_chat,
            date_uk=date_uk,
        )

    total_sent = 0
    urls_to_persist: set[str] = set()
    failures: list[str] = []
    last_digest_items: list[dict] = []
    next_n = 1

    for profile_idx, profile in enumerate(profiles):
        is_last = profile_idx == len(profiles) - 1
        header_text = _compose_header_text(
            profile=profile,
            window_hours=window,
            banner_sent=banner_sent,
            multi_profile=len(profiles) > 1,
        )
        try:
            sent, considered, entries = run_digest(
                profile,
                new_items,
                oauth_token=oauth,
                tg_token=tg_token,
                tg_chat=tg_chat,
                window=window,
                max_items=max_items,
                start_idx=next_n,
                header_text=header_text,
                # Footer (with /save N hint) lives only on the final post so
                # it doesn't repeat between profile blocks.
                include_footer=is_last,
            )
            for offset, e in enumerate(entries):
                url = (e.get("url") or "").strip()
                if not url:
                    continue
                last_digest_items.append({
                    "n": next_n + offset,
                    "url": url,
                    "title": (e.get("title") or "").strip(),
                    "profile": profile["name"],
                })
            next_n += len(entries)
            total_sent += sent
            urls_to_persist.update(considered)
        except TelegramError as exc:
            logger.error("[%s] Telegram send failed: %s", profile["name"], exc)
            failures.append(profile["name"])
            tg_error(f"❌ [{profile['name']}] Telegram send failed: {exc}", tg_token, tg_chat)
        except Exception as exc:
            logger.exception("[%s] digest failed", profile["name"])
            failures.append(profile["name"])
            tg_error(f"❌ [{profile['name']}] digest failed: {exc}", tg_token, tg_chat)

    # If every profile ran cleanly but nothing was sent, send a single
    # "nothing new" message so the user sees the pipeline is alive.
    if total_sent == 0 and not failures:
        send_message(
            f"<b>AI/Tech дайджест — {date_uk}</b>\n\n"
            f"Нових новин за останні {window}г немає.",
            tg_token,
            tg_chat,
        )

    # Always overwrite last_digest.json with whatever shipped this run, even
    # an empty list — that way /save N from a prior digest can't accidentally
    # resolve to something the user hasn't seen today.
    save_last_digest(last_digest_items)

    # Persist every URL that was filtered to any profile, even if its LLM
    # pass dropped it from the top-N — so it won't resurface tomorrow.
    # Only skip this when a profile failed outright, so the next run retries.
    if urls_to_persist and not failures:
        save_seen(SEEN_PATH, seen, list(urls_to_persist))

    print(
        f"total={len(all_items)} new={len(new_items)} "
        f"sent={total_sent} profiles={len(profiles)} "
        f"failures={len(failures)} tg_status={'ok' if not failures else 'partial'}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
