"""Tests for src.blocklist — hostname-level matching and false-positive guards."""

import pytest

from src.blocklist import BLOCKED_DOMAINS, is_blocked


# ── Positive cases: these URLs must be blocked ────────────────────────────────

@pytest.mark.parametrize(
    "url, expected_reason",
    [
        ("https://rt.com/world/12345-article", "domain:rt.com"),
        ("https://news.rt.com/world/12345", "domain:rt.com"),
        ("https://yandex.ru/news/story/123", "domain:yandex.ru"),
        ("https://habr.com/ru/articles/123456/", "domain:habr.com"),
        ("https://www.sberbank.ru/investor", "domain:sberbank.ru"),
        ("https://vk.com/some_profile", "domain:vk.com"),
        ("http://KREMLIN.RU/en/events/123", "domain:kremlin.ru"),
        ("https://tass.com/politics/123", "domain:tass.com"),
    ],
)
def test_blocked_urls(url, expected_reason):
    blocked, reason = is_blocked(url)
    assert blocked is True, f"{url} should be blocked"
    assert reason == expected_reason


# ── Negative cases: these URLs must NOT be blocked ────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        # Western outlets reporting on Russian companies are kept — content-level
        # filtering is intentionally out of scope.
        "https://techcrunch.com/2026/04/01/yandex-acquired-by-western-consortium/",
        "https://arstechnica.com/security/2026/04/new-kaspersky-research/",
        "https://www.theverge.com/2026/03/15/sberbank-trading-halted",
        "https://www.reddit.com/r/russia/comments/abc123/",
        # Unrelated Western tech domains.
        "https://anthropic.com/news/claude-4-7",
        "https://github.com/openai/openai-python",
        "https://huggingface.co/blog/new-model",
        "https://news.ycombinator.com/item?id=12345",
    ],
)
def test_allowed_urls(url):
    blocked, reason = is_blocked(url)
    assert blocked is False, f"{url} should pass, got reason={reason!r}"
    assert reason == ""


# ── False-positive guards: substrings that used to slip through ───────────────

@pytest.mark.parametrize(
    "url",
    [
        # "rt.com" appears as a substring of "smart.company" — hostname match
        # must not trip on this.
        "https://smart.company/about",
        # "ria.ru" substring in "theria.ru.net" would false-positive with
        # substring matching; hostname match is stricter.
        "https://austrian-press.com/about/theria-article",
        # "vk.com" substring inside "stackoverflow.com" etc — guard anyway.
        "https://stackoverflow.com/questions/vk-api",
        # "my.com" belongs to Mail.ru Group so IS blocked — but "ximy.com"
        # (hypothetical unrelated company) must NOT be blocked.
        "https://academy.ximy.com/course",
        # Path containing a blocked domain name as a text fragment.
        "https://github.com/someone/yandex-maps-wrapper",
    ],
)
def test_substring_false_positives_are_not_blocked(url):
    blocked, _ = is_blocked(url)
    assert blocked is False, f"{url} tripped a substring false-positive"


# ── Edge cases ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["", "   ", "not a url", "javascript:alert(1)", "mailto:me@rt.com"])
def test_malformed_urls_are_not_blocked(url):
    blocked, reason = is_blocked(url)
    assert blocked is False
    assert reason == ""


def test_subdomain_is_matched():
    blocked, reason = is_blocked("https://press.rt.com/article")
    assert blocked is True
    assert reason == "domain:rt.com"


def test_trailing_dot_hostname_is_normalised():
    blocked, reason = is_blocked("https://rt.com./article")
    assert blocked is True
    assert reason == "domain:rt.com"


def test_uppercase_hostname_matches():
    blocked, reason = is_blocked("https://WWW.YANDEX.RU/news")
    assert blocked is True
    assert reason == "domain:yandex.ru"


# ── Sanity: blocklist is non-empty and uses lowercase ─────────────────────────

def test_blocklist_nonempty_and_all_lowercase():
    assert len(BLOCKED_DOMAINS) > 20
    for domain in BLOCKED_DOMAINS:
        assert domain == domain.lower(), f"{domain} must be lowercase"
        assert "." in domain, f"{domain} must look like a domain"
