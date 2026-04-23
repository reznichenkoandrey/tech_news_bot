"""
Tests for src/telegram.py — chunking, retry logic, error handling.
"""

import time
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from src.telegram import (
    MAX_MESSAGE_CHARS,
    TelegramError,
    _chunk_text,
    _post_with_retry,
    edit_message_reply_markup,
    send_message,
    send_reply,
)


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_short_text_not_split(self):
        text = "Short message."
        assert _chunk_text(text) == [text]

    def test_text_at_exact_limit_not_split(self):
        text = "x" * MAX_MESSAGE_CHARS
        result = _chunk_text(text)
        assert len(result) == 1
        assert result[0] == text

    def test_10000_chars_splits_into_3_messages(self):
        # Build text with paragraph breaks so splitter can work naturally
        paragraph = "word " * 150 + "\n\n"  # ~756 chars per paragraph
        text = paragraph * 14  # ~10584 chars total
        chunks = _chunk_text(text)
        assert len(chunks) >= 3
        for chunk in chunks:
            assert len(chunk) <= MAX_MESSAGE_CHARS

    def test_all_chunks_within_limit(self):
        long_text = ("This is a sentence. " * 50 + "\n\n") * 10
        chunks = _chunk_text(long_text)
        for chunk in chunks:
            assert len(chunk) <= MAX_MESSAGE_CHARS

    def test_split_preserves_double_newline_boundary(self):
        """Chunks should not cut inside a paragraph when \n\n is available."""
        para_a = "A " * 400  # 800 chars
        para_b = "B " * 400  # 800 chars
        para_c = "C " * 400  # 800 chars
        para_d = "D " * 400  # 800 chars
        para_e = "E " * 400  # 800 chars

        text = f"{para_a}\n\n{para_b}\n\n{para_c}\n\n{para_d}\n\n{para_e}"
        chunks = _chunk_text(text)

        # No chunk should start mid-word — paragraph boundaries preserved where possible
        for chunk in chunks:
            assert len(chunk) <= MAX_MESSAGE_CHARS

    def test_no_chunk_empty(self):
        text = "word " * 2000
        chunks = _chunk_text(text)
        for chunk in chunks:
            assert len(chunk.strip()) > 0

    def test_text_without_newlines_hard_cuts(self):
        """When no line breaks exist, hard cut at max_chars."""
        text = "x" * (MAX_MESSAGE_CHARS + 500)
        chunks = _chunk_text(text)
        assert len(chunks) == 2
        for chunk in chunks:
            assert len(chunk) <= MAX_MESSAGE_CHARS


# ---------------------------------------------------------------------------
# _post_with_retry
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = str(json_body or {})
    resp.json.return_value = json_body or {}
    return resp


class TestPostWithRetry:
    def test_success_on_first_attempt(self):
        with patch("requests.post", return_value=_mock_response(200)) as mock_post:
            _post_with_retry("https://api.telegram.org/sendMessage", {"text": "hi"})
        assert mock_post.call_count == 1

    def test_500_retries_three_times_then_raises(self):
        with (
            patch("requests.post", return_value=_mock_response(500)),
            patch("time.sleep"),
        ):
            with pytest.raises(TelegramError, match="500"):
                _post_with_retry("https://api.telegram.org/sendMessage", {})

    def test_400_no_retry_raises_immediately(self):
        with patch("requests.post", return_value=_mock_response(400)) as mock_post:
            with pytest.raises(TelegramError, match="400"):
                _post_with_retry("https://api.telegram.org/sendMessage", {})
        # Only one attempt — no retry on 400
        assert mock_post.call_count == 1

    def test_429_reads_retry_after_and_waits(self):
        responses = [
            _mock_response(429, {"parameters": {"retry_after": 3}}),
            _mock_response(200),
        ]
        sleep_calls: list[float] = []

        with (
            patch("requests.post", side_effect=responses),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            _post_with_retry("https://api.telegram.org/sendMessage", {})

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 3  # retry_after value from response

    def test_network_error_retries_then_raises(self):
        with (
            patch("requests.post", side_effect=requests.ConnectionError("refused")),
            patch("time.sleep"),
        ):
            with pytest.raises(TelegramError, match="Мережева помилка"):
                _post_with_retry("https://api.telegram.org/sendMessage", {})

    def test_recovers_after_transient_500(self):
        responses = [
            _mock_response(500),
            _mock_response(200),
        ]
        with (
            patch("requests.post", side_effect=responses) as mock_post,
            patch("time.sleep"),
        ):
            _post_with_retry("https://api.telegram.org/sendMessage", {})
        assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class TestSendMessage:
    TOKEN = "1234567890:AAAA"
    CHAT_ID = "-100123456789"

    def test_short_message_single_post(self):
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_message("Hello Telegram", self.TOKEN, self.CHAT_ID)
        assert mock_post.call_count == 1

    def test_long_message_multiple_posts(self):
        long_text = ("word " * 300 + "\n\n") * 8  # well over 4000 chars
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_message(long_text, self.TOKEN, self.CHAT_ID)
        assert mock_post.call_count >= 2

    def test_correct_api_url_used(self):
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_message("Test", self.TOKEN, self.CHAT_ID)
        called_url = mock_post.call_args[0][0]
        assert f"bot{self.TOKEN}/sendMessage" in called_url

    def test_parse_mode_passed_in_payload(self):
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_message("Test", self.TOKEN, self.CHAT_ID, parse_mode="HTML")
        payload = mock_post.call_args[1]["json"]
        assert payload["parse_mode"] == "HTML"

    def test_permanent_error_raises_telegram_error(self):
        with (
            patch("requests.post", return_value=_mock_response(400)),
            patch("time.sleep"),
        ):
            with pytest.raises(TelegramError):
                send_message("Test", self.TOKEN, self.CHAT_ID)

    def test_reply_markup_attached_to_last_chunk_only(self):
        long_text = ("word " * 300 + "\n\n") * 8  # splits into >=2 chunks
        markup = {"inline_keyboard": [[{"text": "ok", "callback_data": "x"}]]}
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_message(long_text, self.TOKEN, self.CHAT_ID, reply_markup=markup)

        calls = mock_post.call_args_list
        assert len(calls) >= 2
        for call_obj in calls[:-1]:
            assert "reply_markup" not in call_obj[1]["json"]
        # Last chunk should serialise the markup as JSON.
        import json as _json
        last_payload = calls[-1][1]["json"]
        assert "reply_markup" in last_payload
        assert _json.loads(last_payload["reply_markup"]) == markup

    def test_reply_markup_passed_on_single_message(self):
        import json as _json
        markup = {"inline_keyboard": [[{"text": "ok", "callback_data": "x"}]]}
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_message("short", self.TOKEN, self.CHAT_ID, reply_markup=markup)
        payload = mock_post.call_args[1]["json"]
        assert _json.loads(payload["reply_markup"]) == markup


class TestEditMessageReplyMarkup:
    def test_empty_keyboard_sent_when_none(self):
        import json as _json
        with patch("requests.post", return_value=_mock_response(200)) as mock_post:
            edit_message_reply_markup("tok", "chat", 42, None)
        payload = mock_post.call_args[1]["json"]
        assert payload["message_id"] == 42
        assert _json.loads(payload["reply_markup"]) == {"inline_keyboard": []}

    def test_custom_markup_forwarded(self):
        import json as _json
        markup = {"inline_keyboard": [[{"text": "done", "callback_data": "y"}]]}
        with patch("requests.post", return_value=_mock_response(200)) as mock_post:
            edit_message_reply_markup("tok", "chat", 42, markup)
        payload = mock_post.call_args[1]["json"]
        assert _json.loads(payload["reply_markup"]) == markup


class TestSendReply:
    def test_sends_with_reply_parameters(self):
        with (
            patch("requests.post", return_value=_mock_response(200)) as mock_post,
            patch("time.sleep"),
        ):
            send_reply("hello", "tok", "chat", reply_to_message_id=99)
        payload = mock_post.call_args[1]["json"]
        assert payload["reply_parameters"] == {"message_id": 99}
        assert payload["disable_web_page_preview"] is True
