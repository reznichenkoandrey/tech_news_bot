"""
Summarize a digest item on demand and post the result as a Telegram reply
to the original message.

Triggered from GitHub Actions `.github/workflows/summarize.yml` (which in
turn fires on `repository_dispatch` sent by the Cloudflare Worker after
the user taps 📖 Deep on a digest item).

Usage:
    python3 -m scripts.post_article_summary <url> <chat_id> <message_id>

Emits best-effort notifications: if summarize() fails we still send an
apology reply and strip the inline keyboard from the original item so the
user isn't left staring at a silent message.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import sys

from scripts.digest_pipeline import env_required
from scripts.summarize_article import summarize
from src.telegram import TelegramError, edit_message_reply_markup, send_reply

logger = logging.getLogger("post_article_summary")

TELEGRAM_MAX_CHARS = 4000  # leave headroom for HTML tags, same as src.telegram


def markdown_to_tg_html(md: str) -> str:
    """
    Minimal markdown → Telegram-HTML pipeline for the summarize_article
    output. Handles YAML front-matter, `## Heading`, bullet lists and bold.
    We escape HTML first, then reapply a small whitelist of tags.
    """
    # Strip YAML front-matter if present.
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            md = md[end + 4 :].lstrip()

    # Extract bold markers before escaping so they survive.
    placeholders: list[tuple[str, str]] = []

    def _stash(match: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append((f"@@BOLD{idx}@@", match.group(1)))
        return f"@@BOLD{idx}@@"

    md = re.sub(r"\*\*(.+?)\*\*", _stash, md, flags=re.DOTALL)

    escaped = html_lib.escape(md)

    for token, inner in placeholders:
        escaped = escaped.replace(token, f"<b>{html_lib.escape(inner)}</b>")

    lines = []
    for raw_line in escaped.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            lines.append(f"<b>{line[3:].strip()}</b>")
        elif line.startswith("# "):
            lines.append(f"<b>{line[2:].strip()}</b>")
        elif line.startswith("- "):
            lines.append(f"• {line[2:].strip()}")
        elif line.startswith("* "):
            lines.append(f"• {line[2:].strip()}")
        else:
            lines.append(line)
    return "\n".join(lines).strip()


def truncate_html(text: str, max_chars: int) -> str:
    """Hard-cap the rendered HTML, preserving paragraph breaks when possible."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n\n", 0, max_chars - 20)
    if cut == -1:
        cut = max_chars - 20
    return text[:cut].rstrip() + "\n\n<i>…обрізано</i>"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 3:
        print("Usage: post_article_summary <url> <chat_id> <message_id>", file=sys.stderr)
        return 2

    url, chat_id, message_id_raw = args[0], args[1], args[2]
    try:
        message_id = int(message_id_raw)
    except ValueError:
        print(f"ERROR: message_id must be int, got {message_id_raw!r}", file=sys.stderr)
        return 2

    tg_token = env_required("TELEGRAM_BOT_TOKEN")

    try:
        md, _ = summarize(url)
        rendered = markdown_to_tg_html(md)
        body = truncate_html(rendered, TELEGRAM_MAX_CHARS)
        send_reply(body, tg_token, chat_id, reply_to_message_id=message_id)
    except Exception as exc:
        logger.exception("summarize failed for %s", url)
        try:
            send_reply(
                f"⚠️ не вдалось підсумувати: <code>{html_lib.escape(str(exc)[:200])}</code>",
                tg_token,
                chat_id,
                reply_to_message_id=message_id,
            )
        except TelegramError as notify_exc:
            logger.error("apology send failed: %s", notify_exc)
        # Strip buttons so the user doesn't retry into the same failure.
        try:
            edit_message_reply_markup(tg_token, chat_id, message_id, None)
        except TelegramError as edit_exc:
            logger.warning("edit reply_markup failed: %s", edit_exc)
        return 1

    # On success the Worker already stripped the keyboard; re-run is harmless.
    try:
        edit_message_reply_markup(tg_token, chat_id, message_id, None)
    except TelegramError as edit_exc:
        logger.warning("edit reply_markup failed: %s", edit_exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
