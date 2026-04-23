"""
Tests for scripts/weekly_digest.py — parsing, rendering, and main() flow.

No network: we monkeypatch summarize() / send_message() and redirect the
reading_list / archive paths to tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import weekly_digest as wd


# ── parse_front_matter / extract_section / hostname ───────────────────────────

def test_parse_front_matter_extracts_fields():
    md = '---\nurl: https://x\ntitle: "Hello: world"\nauthor: Alice\n---\n\n## TL;DR\nbody'
    meta, body = wd.parse_front_matter(md)
    assert meta["url"] == "https://x"
    assert meta["title"] == "Hello: world"
    assert meta["author"] == "Alice"
    assert body.startswith("## TL;DR")


def test_parse_front_matter_no_marker_returns_empty_meta():
    meta, body = wd.parse_front_matter("## TL;DR\nhi")
    assert meta == {}
    assert body == "## TL;DR\nhi"


def test_extract_section_pulls_body_until_next_heading():
    md = "## TL;DR\none sentence\nsecond line\n\n## Ключові тези\n- a\n"
    assert wd.extract_section(md, "TL;DR") == "one sentence\nsecond line"


def test_extract_section_returns_empty_when_missing():
    assert wd.extract_section("## Other\nx", "TL;DR") == ""


def test_hostname_strips_scheme_and_path():
    assert wd.hostname("https://example.com/a/b") == "example.com"
    assert wd.hostname("http://sub.ex.com?q=1") == "sub.ex.com"
    assert wd.hostname("not a url") == "not a url"


# ── render_item ───────────────────────────────────────────────────────────────

def test_render_item_happy_path(monkeypatch):
    def fake_summarize(url, *, force=False):
        md = (
            "---\nurl: https://example.com\ntitle: Sample Post\n---\n\n"
            "## TL;DR\nOne-line takeaway.\n\n"
            "## Deep dive\nA lot of text.\n"
        )
        return md, Path("/tmp/fake")

    monkeypatch.setattr(wd, "summarize", fake_summarize)
    entry = wd.ReadingEntry(url="https://example.com", saved_at="now")
    item = wd.render_item(entry, allow_llm=True)

    assert item.failure is None
    assert item.title == "Sample Post"
    assert "One-line takeaway." in item.tldr_html


def test_render_item_falls_back_to_hostname_when_no_title(monkeypatch):
    def fake_summarize(url, *, force=False):
        return "## TL;DR\nbody\n", Path("/tmp/fake")

    monkeypatch.setattr(wd, "summarize", fake_summarize)
    item = wd.render_item(wd.ReadingEntry(url="https://foo.bar/x", saved_at=""), allow_llm=True)
    assert item.title == "foo.bar"


def test_render_item_marks_failure_when_summarize_raises(monkeypatch):
    def fake_summarize(url, *, force=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(wd, "summarize", fake_summarize)
    item = wd.render_item(wd.ReadingEntry(url="https://z.com", saved_at=""), allow_llm=True)
    assert item.failure == "boom"
    assert item.title == "z.com"


def test_render_item_without_llm_uses_cache_only(monkeypatch, tmp_path):
    cache_file = tmp_path / "hash.md"
    cache_file.write_text("---\ntitle: Cached\n---\n\n## TL;DR\nfrom cache\n", encoding="utf-8")
    monkeypatch.setattr(wd, "summarize", lambda *a, **k: pytest.fail("LLM path must not run"))
    monkeypatch.setattr("scripts.summarize_article.cache_path", lambda url: cache_file)

    item = wd.render_item(wd.ReadingEntry(url="https://x", saved_at=""), allow_llm=False)
    assert item.failure is None
    assert item.title == "Cached"
    assert "from cache" in item.tldr_html


def test_render_item_without_llm_and_no_cache_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "summarize", lambda *a, **k: pytest.fail("LLM path must not run"))
    monkeypatch.setattr("scripts.summarize_article.cache_path", lambda url: tmp_path / "missing.md")

    item = wd.render_item(wd.ReadingEntry(url="https://x", saved_at=""), allow_llm=False)
    assert item.failure is not None


# ── render_digest_html ────────────────────────────────────────────────────────

def test_render_digest_html_counts_success_and_failure():
    items = [
        wd.RenderedItem(url="https://a", title="A", tldr_html="short"),
        wd.RenderedItem(url="https://b", title="B", tldr_html="", failure="nope"),
    ]
    html = wd.render_digest_html(items, total=2)
    assert "Deep reads тижня" in html
    assert "2 збережено · 1 підсумовано" in html
    assert "Читати →" in html
    assert "⚠️ не вдалось підсумувати: nope" in html


# ── main() ────────────────────────────────────────────────────────────────────

def test_main_exits_quietly_when_reading_list_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "READING_LIST_PATH", tmp_path / "reading_list.json")
    monkeypatch.setattr(wd, "READING_ARCHIVE_PATH", tmp_path / "archive.json")
    (tmp_path / "reading_list.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setattr(wd, "send_message", lambda *a, **k: pytest.fail("should not send when empty"))

    assert wd.main() == 0


def test_main_happy_path_sends_and_archives(monkeypatch, tmp_path):
    rl_path = tmp_path / "reading_list.json"
    arch_path = tmp_path / "archive.json"
    rl_path.write_text(
        json.dumps([{"url": "https://a", "saved_at": "t1"}, {"url": "https://b", "saved_at": "t2"}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(wd, "READING_LIST_PATH", rl_path)
    monkeypatch.setattr(wd, "READING_ARCHIVE_PATH", arch_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "z")

    def fake_summarize(url, *, force=False):
        return f"---\nurl: {url}\ntitle: T-{url[-1]}\n---\n\n## TL;DR\ntl;dr for {url}\n", Path("/tmp")

    monkeypatch.setattr(wd, "summarize", fake_summarize)

    sent = []
    monkeypatch.setattr(wd, "send_message", lambda text, *a, **k: sent.append(text))

    assert wd.main() == 0
    assert len(sent) == 1
    assert "tl;dr for https://a" in sent[0]

    # reading_list cleared, archive populated.
    assert json.loads(rl_path.read_text()) == []
    archived = json.loads(arch_path.read_text())
    assert len(archived) == 2
    assert {row["url"] for row in archived} == {"https://a", "https://b"}
    assert all(row["archived_at"] for row in archived)


def test_main_bails_when_all_items_fail(monkeypatch, tmp_path):
    rl_path = tmp_path / "reading_list.json"
    arch_path = tmp_path / "archive.json"
    rl_path.write_text(json.dumps([{"url": "https://a", "saved_at": "t1"}]), encoding="utf-8")

    monkeypatch.setattr(wd, "READING_LIST_PATH", rl_path)
    monkeypatch.setattr(wd, "READING_ARCHIVE_PATH", arch_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "z")

    def failing_summarize(url, *, force=False):
        raise RuntimeError("paywall")

    monkeypatch.setattr(wd, "summarize", failing_summarize)

    errors = []
    monkeypatch.setattr(wd, "tg_error", lambda msg, *a, **k: errors.append(msg))
    monkeypatch.setattr(wd, "send_message", lambda *a, **k: pytest.fail("digest should not send"))

    assert wd.main() == 1
    assert errors and "жодний saved item" in errors[0]

    # Reading list untouched so next run retries.
    assert json.loads(rl_path.read_text()) == [{"url": "https://a", "saved_at": "t1"}]
