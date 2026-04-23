"""
One-shot Telegram bot poller.

Invoked by .github/workflows/bot.yml every few minutes. Reads every new
update since the last run via /getUpdates, handles slash commands coming
from the configured chat id (everything else is silently dropped), replies
and, if any command mutated user prefs, persists them. The workflow then
commits config/user_prefs.yaml and data/bot_state.json back to main.

Commands:
    /help                 — list commands
    /topics               — list all topics with active/inactive markers
    /add <slug>           — add a topic to active_topics
    /remove <slug>        — remove a topic from active_topics
    /sources [slug]       — list feeds (all or for a specific topic)
    /status               — digest schedule + current filter summary
    /reset                — clear active_topics (every profile runs in full)
    /digests              — list digest profiles from config/digests.yaml

Unauthorized senders never get a reply (don't confirm the bot exists).
Unknown commands get a short help hint.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable

import requests
import yaml

from scripts.digest_pipeline import env_required
from src.telegram import send_message

logger = logging.getLogger("bot_poll")

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPICS_PATH = REPO_ROOT / "config" / "topics.yaml"
SOURCES_PATH = REPO_ROOT / "config" / "sources.yaml"
DIGESTS_PATH = REPO_ROOT / "config" / "digests.yaml"
USER_PREFS_PATH = REPO_ROOT / "config" / "user_prefs.yaml"
BOT_STATE_PATH = REPO_ROOT / "data" / "bot_state.json"

TELEGRAM_API = "https://api.telegram.org"

# getUpdates long-poll timeout. Runner keeps the HTTP connection open that long
# waiting for updates; anything beyond ~25s starts conflicting with short runs.
POLL_TIMEOUT_S = 20


# ── State I/O ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not BOT_STATE_PATH.exists():
        return {"last_update_id": 0}
    return json.loads(BOT_STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    BOT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOT_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def load_user_prefs() -> dict:
    if not USER_PREFS_PATH.exists():
        return {"active_topics": []}
    data = yaml.safe_load(USER_PREFS_PATH.read_text(encoding="utf-8")) or {}
    return {"active_topics": list(data.get("active_topics") or [])}


def save_user_prefs(prefs: dict) -> None:
    # Keep a stable, commented format so git diffs are readable.
    content = (
        "# User preferences, managed by scripts/bot_poll.py via Telegram commands.\n"
        "# Edit manually only when the bot is offline — otherwise your changes may be\n"
        "# overwritten on the next poll.\n"
        "#\n"
        "# active_topics — global filter applied on top of config/digests.yaml profiles.\n"
        "# Empty list = no filter. Non-empty list narrows every profile to this set.\n"
        "\nactive_topics:"
    )
    topics = prefs.get("active_topics") or []
    if not topics:
        content += " []\n"
    else:
        content += "\n" + "\n".join(f"  - {t}" for t in topics) + "\n"
    USER_PREFS_PATH.write_text(content, encoding="utf-8")


# ── Config loaders ────────────────────────────────────────────────────────────

def load_topics_registry() -> dict[str, dict]:
    if not TOPICS_PATH.exists():
        return {}
    data = yaml.safe_load(TOPICS_PATH.read_text(encoding="utf-8")) or {}
    return {t["slug"]: t for t in data.get("topics", [])}


def load_sources() -> list[dict]:
    data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    return list(data.get("feeds", []))


def load_digests() -> list[dict]:
    if not DIGESTS_PATH.exists():
        return []
    data = yaml.safe_load(DIGESTS_PATH.read_text(encoding="utf-8")) or {}
    return list(data.get("digests", []))


# ── Telegram I/O ──────────────────────────────────────────────────────────────

def fetch_updates(token: str, offset: int) -> list[dict]:
    """One-shot long poll. `offset` acks every update up to offset-1."""
    r = requests.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params={
            "offset": offset,
            "timeout": POLL_TIMEOUT_S,
            "allowed_updates": json.dumps(["message"]),
        },
        timeout=POLL_TIMEOUT_S + 10,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"getUpdates failed: {body}")
    return body.get("result", [])


# ── Command handlers ──────────────────────────────────────────────────────────

def _format_topic_line(slug: str, registry: dict, active: set[str]) -> str:
    entry = registry.get(slug, {})
    emoji = entry.get("emoji", "•")
    name = entry.get("name", slug)
    mark = "✅" if slug in active else "◻️"
    return f"{mark} {emoji} <b>{slug}</b> — {name}"


def cmd_help() -> str:
    return (
        "<b>Команди:</b>\n"
        "/topics — список топіків з поточним фільтром\n"
        "/add <code>slug</code> — додати топік у фільтр\n"
        "/remove <code>slug</code> — прибрати топік з фільтра\n"
        "/reset — зняти фільтр (дайджести повертаються до дефолтів)\n"
        "/sources [<code>slug</code>] — показати feed'и (всі або для топіка)\n"
        "/digests — поточні дайджест-профілі\n"
        "/status — стан системи + активний фільтр\n"
        "/help — цей список"
    )


def cmd_topics(registry: dict, prefs: dict) -> str:
    if not registry:
        return "⚠️ topics.yaml порожній."
    active = set(prefs.get("active_topics") or [])
    lines = [_format_topic_line(slug, registry, active) for slug in sorted(registry)]
    header = "<b>Топіки</b> (✅ = в активному фільтрі):\n"
    footer = "\n\n<i>Активних: {}</i>".format(len(active) if active else "усі")
    return header + "\n".join(lines) + footer


def cmd_add(slug: str, registry: dict, prefs: dict) -> tuple[str, bool]:
    if slug not in registry:
        known = ", ".join(sorted(registry)[:6])
        return (f"❌ невідомий slug <code>{slug}</code>. Доступні: {known}…", False)
    active = list(prefs.get("active_topics") or [])
    if slug in active:
        return (f"ℹ️ <code>{slug}</code> уже в фільтрі.", False)
    active.append(slug)
    prefs["active_topics"] = active
    return (f"✅ додав <code>{slug}</code>. У фільтрі: {', '.join(active)}", True)


def cmd_remove(slug: str, prefs: dict) -> tuple[str, bool]:
    active = list(prefs.get("active_topics") or [])
    if slug not in active:
        return (f"ℹ️ <code>{slug}</code> і так немає у фільтрі.", False)
    active.remove(slug)
    prefs["active_topics"] = active
    remaining = ", ".join(active) if active else "усі (фільтр порожній)"
    return (f"🗑 прибрав <code>{slug}</code>. Активні: {remaining}", True)


def cmd_reset(prefs: dict) -> tuple[str, bool]:
    if not prefs.get("active_topics"):
        return ("ℹ️ фільтр і так порожній.", False)
    prefs["active_topics"] = []
    return ("♻️ фільтр очищено — дайджести повертаються до дефолтів.", True)


def cmd_sources(slug: str | None, sources: list[dict], registry: dict) -> str:
    if slug and slug not in registry:
        return f"❌ невідомий slug <code>{slug}</code>."

    if slug:
        matching = [s for s in sources if slug in s.get("topics", [])]
        if not matching:
            return f"ℹ️ жоден feed не тегнуто як <code>{slug}</code>."
        lines = [f"• <b>{s['name']}</b> — <i>{s['url']}</i>" for s in matching]
        return f"<b>Feeds у топіку {slug}</b> ({len(matching)}):\n" + "\n".join(lines)

    # Aggregate count per topic
    counts: dict[str, int] = {}
    for s in sources:
        for t in s.get("topics", []):
            counts[t] = counts.get(t, 0) + 1
    lines = [f"• <b>{t}</b> — {counts.get(t, 0)} feeds" for t in sorted(registry)]
    return f"<b>Feeds всього: {len(sources)}</b>\n" + "\n".join(lines)


def cmd_digests(digests: list[dict], prefs: dict) -> str:
    if not digests:
        return "ℹ️ config/digests.yaml порожній — активний один дефолтний профіль."
    active = set(prefs.get("active_topics") or [])
    lines = []
    for d in digests:
        topics = d.get("topics", [])
        overlap = set(topics) & active if active else set(topics)
        marker = "✅" if overlap or not active else "⚠️ (фільтр виключає)"
        lines.append(
            f"{marker} {d.get('emoji', '📰')} <b>{d.get('name', '?')}</b>"
            f" — topics: {', '.join(topics) if topics else 'all'}"
        )
    return "<b>Дайджест-профілі:</b>\n" + "\n".join(lines)


def cmd_status(prefs: dict, digests: list[dict]) -> str:
    active = prefs.get("active_topics") or []
    filt = ", ".join(active) if active else "<i>порожній (повні дайджести)</i>"
    return (
        "<b>Стан бота:</b>\n"
        f"Розклад: щодня 10:00 Kyiv (GitHub Actions)\n"
        f"Дайджест-профілі: {len(digests)}\n"
        f"Активний фільтр: {filt}"
    )


# ── Dispatch ──────────────────────────────────────────────────────────────────

def handle_command(
    text: str,
    *,
    registry: dict,
    sources: list[dict],
    digests: list[dict],
    prefs: dict,
) -> tuple[str, bool]:
    """
    Return (reply_text, prefs_mutated).

    Mutates `prefs` in-place on /add /remove /reset.
    """
    text = text.strip()
    if not text.startswith("/"):
        return ("", False)

    parts = text.split()
    command = parts[0].split("@")[0].lower()  # strip @botname suffix if any
    arg = parts[1] if len(parts) > 1 else None

    if command in ("/start", "/help"):
        return (cmd_help(), False)
    if command == "/topics":
        return (cmd_topics(registry, prefs), False)
    if command == "/add":
        if not arg:
            return ("❌ <code>/add &lt;slug&gt;</code> — вкажи slug топіка.", False)
        return cmd_add(arg, registry, prefs)
    if command == "/remove":
        if not arg:
            return ("❌ <code>/remove &lt;slug&gt;</code> — вкажи slug топіка.", False)
        return cmd_remove(arg, prefs)
    if command == "/reset":
        return cmd_reset(prefs)
    if command == "/sources":
        return (cmd_sources(arg, sources, registry), False)
    if command == "/digests":
        return (cmd_digests(digests, prefs), False)
    if command == "/status":
        return (cmd_status(prefs, digests), False)

    return (f"🤷 невідома команда {command}. Спробуй /help.", False)


def is_authorised(update: dict, allowed_chat_id: str) -> bool:
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    return chat_id == str(allowed_chat_id)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    tg_token = env_required("TELEGRAM_BOT_TOKEN")
    tg_chat = env_required("TELEGRAM_CHAT_ID")

    registry = load_topics_registry()
    sources = load_sources()
    digests = load_digests()
    prefs = load_user_prefs()
    state = load_state()

    offset = int(state.get("last_update_id", 0)) + 1
    logger.info("Polling getUpdates offset=%d", offset)
    updates = fetch_updates(tg_token, offset)
    if not updates:
        logger.info("No new updates")
        return 0

    prefs_mutated = False
    max_update_id = state.get("last_update_id", 0)

    for upd in updates:
        max_update_id = max(max_update_id, upd.get("update_id", 0))
        if not is_authorised(upd, tg_chat):
            logger.info("Dropping unauthorised update %s", upd.get("update_id"))
            continue

        msg = upd.get("message") or {}
        text = msg.get("text", "") or ""
        if not text:
            continue

        reply, mutated = handle_command(
            text,
            registry=registry,
            sources=sources,
            digests=digests,
            prefs=prefs,
        )
        prefs_mutated = prefs_mutated or mutated
        if reply:
            send_message(reply, tg_token, tg_chat)

    # Always persist the last-seen update id so we don't reprocess on next run.
    state["last_update_id"] = max_update_id
    save_state(state)

    if prefs_mutated:
        save_user_prefs(prefs)
        logger.info("user_prefs.yaml updated")

    return 0


if __name__ == "__main__":
    sys.exit(main())
