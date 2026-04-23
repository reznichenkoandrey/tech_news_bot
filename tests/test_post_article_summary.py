"""Tests for scripts.post_article_summary (rendering only — no network)."""

from scripts import post_article_summary as pas


def test_markdown_strips_front_matter():
    md = "---\nurl: https://x\ntitle: T\n---\n\n## TL;DR\nbody"
    html = pas.markdown_to_tg_html(md)
    assert "url:" not in html
    assert "<b>TL;DR</b>" in html
    assert "body" in html


def test_markdown_converts_headings_and_bullets():
    md = "## Ключові тези\n- first\n- second\n\n## Deep dive\n*inline* **bold** text."
    html = pas.markdown_to_tg_html(md)
    assert "<b>Ключові тези</b>" in html
    assert "<b>Deep dive</b>" in html
    assert "• first" in html and "• second" in html
    assert "<b>bold</b>" in html


def test_markdown_escapes_html_entities():
    md = "## Heading\nSome <script> & \"quotes\""
    html = pas.markdown_to_tg_html(md)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    assert "&quot;" in html


def test_truncate_preserves_paragraph_break():
    text = "para one\n\npara two\n\npara three"
    out = pas.truncate_html(text, 18)
    assert out.endswith("<i>…обрізано</i>")
    assert "para one" in out
    assert "para three" not in out


def test_truncate_noop_when_under_limit():
    assert pas.truncate_html("short", 100) == "short"
