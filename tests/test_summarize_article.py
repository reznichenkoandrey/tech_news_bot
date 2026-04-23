"""Tests for the article summarizer CLI + cache + prompt assembly."""

from pathlib import Path

import pytest

from scripts import summarize_article
from src.reader import Article


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(summarize_article, "CACHE_DIR", tmp_path)
    return tmp_path


def _article(url="https://example.com/post", text_len=5000, **kw):
    defaults = dict(
        url=url,
        title="Title",
        text="x" * text_len,
        author="Author",
        date="2026-04-23",
    )
    defaults.update(kw)
    return Article(**defaults)


# ── cache keys ────────────────────────────────────────────────────────────────

def test_cache_key_is_deterministic():
    assert summarize_article.cache_key("https://a") == summarize_article.cache_key("https://a")
    assert summarize_article.cache_key("https://a") != summarize_article.cache_key("https://b")


def test_cache_key_is_filesystem_safe():
    key = summarize_article.cache_key("https://example.com/path?q=x&y=1")
    assert key.isalnum() and len(key) == 16


# ── render_markdown / yaml escape ────────────────────────────────────────────

def test_render_markdown_wraps_front_matter_and_body():
    md = summarize_article.render_markdown(_article(), "## TL;DR\nHi\n")
    assert md.startswith("---\n")
    assert "url: https://example.com/post" in md
    assert "title: Title" in md
    assert md.strip().endswith("## TL;DR\nHi")


def test_render_markdown_quotes_colons_in_title():
    md = summarize_article.render_markdown(
        _article(title="Paper: subtitle"),
        "body",
    )
    assert 'title: "Paper: subtitle"' in md


def test_render_markdown_omits_empty_optional_fields():
    md = summarize_article.render_markdown(
        _article(author="", date=""),
        "body",
    )
    assert "author:" not in md
    assert "date:" not in md


# ── build_prompt ──────────────────────────────────────────────────────────────

def test_build_prompt_truncates_very_long_bodies():
    big = _article(text_len=summarize_article.MAX_BODY_CHARS + 5000)
    prompt = summarize_article.build_prompt(big)
    assert "[truncated]" in prompt


def test_build_prompt_includes_metadata_lines():
    prompt = summarize_article.build_prompt(_article())
    assert "URL: https://example.com/post" in prompt
    assert "Title: Title" in prompt
    assert "Author: Author" in prompt


# ── summarize() flow ──────────────────────────────────────────────────────────

def test_cache_hit_bypasses_fetch_and_llm(tmp_cache, monkeypatch):
    url = "https://example.com/cached"
    cached = "---\nurl: https://example.com/cached\n---\n\nCACHED BODY\n"
    (tmp_cache / f"{summarize_article.cache_key(url)}.md").write_text(cached)

    monkeypatch.setattr(
        summarize_article,
        "fetch_article",
        lambda *a, **k: pytest.fail("fetch should not run on cache hit"),
    )
    monkeypatch.setattr(
        summarize_article,
        "call_llm",
        lambda *a, **k: pytest.fail("LLM should not run on cache hit"),
    )

    md, _ = summarize_article.summarize(url)
    assert md == cached


def test_force_bypasses_cache(tmp_cache, monkeypatch):
    url = "https://example.com/force"
    (tmp_cache / f"{summarize_article.cache_key(url)}.md").write_text("STALE")

    monkeypatch.setattr(summarize_article, "fetch_article", lambda *a, **k: _article(url=url))
    monkeypatch.setattr(summarize_article, "call_llm", lambda *a, **k: "## TL;DR\nFresh\n")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy")

    md, _ = summarize_article.summarize(url, force=True)
    assert "Fresh" in md
    assert "STALE" not in md


def test_network_failure_raises_runtime_error(tmp_cache, monkeypatch):
    monkeypatch.setattr(summarize_article, "fetch_article", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="Could not download"):
        summarize_article.summarize("https://example.com/dead")


def test_empty_body_raises_runtime_error(tmp_cache, monkeypatch):
    monkeypatch.setattr(
        summarize_article,
        "fetch_article",
        lambda *a, **k: _article(text_len=100),  # under MIN_USEFUL_CHARS
    )
    with pytest.raises(RuntimeError, match="paywall or JS-only"):
        summarize_article.summarize("https://example.com/paywall")


def test_happy_path_writes_cache_file(tmp_cache, monkeypatch):
    url = "https://example.com/new"
    monkeypatch.setattr(summarize_article, "fetch_article", lambda *a, **k: _article(url=url))
    monkeypatch.setattr(summarize_article, "call_llm", lambda *a, **k: "## TL;DR\nok\n")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy")

    md, cp = summarize_article.summarize(url)
    assert cp.exists()
    assert cp.read_text() == md
    assert "## TL;DR" in md
