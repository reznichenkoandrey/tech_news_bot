"""
Telegram Bot API sender with chunking and exponential-backoff retry.
"""

import json
import logging
import sys
import time
from typing import Any, NoReturn

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # conservative limit (hard cap is 4096)
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1  # doubles each retry: 1s, 2s, 4s

# HTTP status codes that warrant a retry
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class TelegramError(Exception):
    """Raised on permanent Telegram API failure."""


def _chunk_text(text: str, max_chars: int = MAX_MESSAGE_CHARS) -> list[str]:
    """
    Split text into chunks of at most max_chars.

    Tries to split on paragraph boundaries (double newline) to avoid
    cutting mid-sentence. Falls back to line breaks, then hard cut.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_chars:
        # Try to split on the last \n\n before the limit
        split_at = remaining.rfind("\n\n", 0, max_chars)
        if split_at == -1:
            # No paragraph break found — try single newline
            split_at = remaining.rfind("\n", 0, max_chars)
        if split_at == -1:
            # No line break at all — hard cut at max_chars
            split_at = max_chars

        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def _post_with_retry(
    url: str,
    payload: dict,
) -> None:
    """
    POST payload to url with retry logic.

    Retries on RETRYABLE_STATUSES up to MAX_RETRIES times.
    Respects retry_after from Telegram 429 responses.
    Raises TelegramError on permanent failure or exhausted retries.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** attempt)
                logger.warning("Мережева помилка (спроба %d/%d), чекаю %ds: %s", attempt + 1, MAX_RETRIES, wait, exc)
                time.sleep(wait)
                continue
            raise TelegramError(f"Мережева помилка після {MAX_RETRIES} спроб: {exc}") from exc

        if response.status_code == 200:
            return

        if response.status_code not in RETRYABLE_STATUSES:
            # Permanent error — no point retrying
            raise TelegramError(
                f"Telegram API помилка {response.status_code}: {response.text[:200]}"
            )

        if attempt >= MAX_RETRIES:
            raise TelegramError(
                f"Telegram API повертає {response.status_code} після {MAX_RETRIES} спроб. "
                f"Відповідь: {response.text[:200]}"
            )

        # Determine wait time
        if response.status_code == 429:
            try:
                retry_after = int(response.json().get("parameters", {}).get("retry_after", BACKOFF_BASE_SECONDS * (2 ** attempt)))
            except (ValueError, AttributeError):
                retry_after = BACKOFF_BASE_SECONDS * (2 ** attempt)
            logger.warning("Rate limit 429, чекаю %ds (спроба %d/%d)", retry_after, attempt + 1, MAX_RETRIES)
            time.sleep(retry_after)
        else:
            wait = BACKOFF_BASE_SECONDS * (2 ** attempt)
            logger.warning("Помилка %d, чекаю %ds (спроба %d/%d)", response.status_code, wait, attempt + 1, MAX_RETRIES)
            time.sleep(wait)


def send_message(
    text: str,
    token: str,
    chat_id: str,
    parse_mode: str = "HTML",
    reply_markup: dict[str, Any] | None = None,
    disable_web_page_preview: bool = False,
) -> None:
    """
    Send text to a Telegram chat, splitting into chunks if needed.

    Args:
        text: Message text (HTML allowed when parse_mode="HTML")
        token: Telegram bot token from @BotFather
        chat_id: Target chat/channel ID
        parse_mode: "HTML" or "Markdown" (default "HTML")
        reply_markup: Optional Telegram reply_markup (inline_keyboard, etc.).
            Only attached to the LAST chunk when text is split — buttons on
            every chunk would confuse the UX.
        disable_web_page_preview: If True, suppress the URL preview card.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    chunks = _chunk_text(text)

    logger.info("Надсилаю %d повідомлень до чату %s", len(chunks), chat_id)

    for index, chunk in enumerate(chunks, start=1):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None and index == len(chunks):
            payload["reply_markup"] = json.dumps(reply_markup)
        _post_with_retry(url, payload)
        logger.info("Повідомлення %d/%d надіслано", index, len(chunks))

        # Brief pause between chunks to avoid local rate limiting
        if index < len(chunks):
            time.sleep(0.5)


def edit_message_reply_markup(
    token: str,
    chat_id: str,
    message_id: int,
    reply_markup: dict[str, Any] | None,
) -> None:
    """
    Replace (or remove) the inline keyboard of an existing message.

    Pass reply_markup=None or an empty keyboard dict to strip buttons.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/editMessageReplyMarkup"
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    else:
        payload["reply_markup"] = json.dumps({"inline_keyboard": []})
    _post_with_retry(url, payload)


def send_reply(
    text: str,
    token: str,
    chat_id: str,
    reply_to_message_id: int,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
) -> None:
    """Send a message threaded as a reply to an existing message (with chunking)."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    chunks = _chunk_text(text)

    for index, chunk in enumerate(chunks, start=1):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
            # Only thread the first chunk; subsequent chunks append naturally.
            "reply_parameters": {"message_id": reply_to_message_id} if index == 1 else None,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        _post_with_retry(url, payload)
        if index < len(chunks):
            time.sleep(0.5)


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    subcommand = sys.argv[1] if len(sys.argv) > 1 else ""

    if subcommand == "send":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        if not token or not chat_id:
            print("ERROR: TELEGRAM_BOT_TOKEN та TELEGRAM_CHAT_ID мають бути задані", file=sys.stderr)
            sys.exit(1)

        text = sys.stdin.read()
        if not text.strip():
            print("ERROR: текст повідомлення порожній", file=sys.stderr)
            sys.exit(1)

        send_message(text, token, chat_id)
    else:
        print("Usage: python -m src.telegram send", file=sys.stderr)
        sys.exit(1)
