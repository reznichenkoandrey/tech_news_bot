"""
Tests for src/fetcher.py — RSS 2.0 and Atom 1.0 parsing, fallback, summary truncation.
"""

import io
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import ParseError

import pytest
import yaml

from src.fetcher import (
    MAX_SUMMARY_CHARS,
    _parse_atom_items,
    _parse_rss_items,
    _strip_html,
    _truncate,
    fetch_all,
)
from src.models import FeedItem

# ---------------------------------------------------------------------------
# Fixtures — minimal valid XML payloads
# ---------------------------------------------------------------------------

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Anthropic News</title>
    <item>
      <title>Claude 4 released</title>
      <link>https://anthropic.com/news/claude-4</link>
      <pubDate>Mon, 20 Apr 2026 08:00:00 +0000</pubDate>
      <description>&lt;p&gt;Big announcement&lt;/p&gt;</description>
    </item>
    <item>
      <title>Safety research update</title>
      <link>https://anthropic.com/news/safety</link>
      <pubDate>Sun, 19 Apr 2026 12:00:00 +0000</pubDate>
      <description>Plain text summary</description>
    </item>
    <item>
      <title>Third item</title>
      <link>https://anthropic.com/news/third</link>
      <pubDate>Sat, 18 Apr 2026 10:00:00 +0000</pubDate>
      <description>Another item</description>
    </item>
  </channel>
</rss>"""

ATOM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>GitHub releases</title>
  <entry>
    <title>v1.0.0</title>
    <link href="https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.0.0" rel="alternate"/>
    <published>2026-04-20T08:00:00Z</published>
    <summary>Initial stable release</summary>
  </entry>
  <entry>
    <title>v0.9.0</title>
    <link href="https://github.com/anthropics/anthropic-sdk-python/releases/tag/v0.9.0" rel="alternate"/>
    <published>2026-04-19T12:00:00+00:00</published>
    <summary>Beta release</summary>
  </entry>
  <entry>
    <title>v0.8.0</title>
    <link href="https://github.com/anthropics/anthropic-sdk-python/releases/tag/v0.8.0" rel="alternate"/>
    <published>2026-04-18T10:00:00Z</published>
    <summary>Alpha release</summary>
  </entry>
</feed>"""

SOURCES_YAML_CONTENT = {
    "feeds": [
        {"name": "Anthropic News", "url": "https://www.anthropic.com/news/rss.xml", "category": "lab"},
        {"name": "SDK releases", "url": "https://github.com/anthropics/anthropic-sdk-python/releases.atom", "category": "release"},
    ]
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(content: bytes) -> MagicMock:
    """Create a context-manager mock for urllib.request.urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = content
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# RSS 2.0 parsing
# ---------------------------------------------------------------------------

class TestRssParser:
    def test_parses_three_items(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(RSS_XML)
        items = _parse_rss_items(root, "Anthropic News", "lab")
        assert len(items) == 3

    def test_correct_url_and_title(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(RSS_XML)
        items = _parse_rss_items(root, "Anthropic News", "lab")
        assert items[0].url == "https://anthropic.com/news/claude-4"
        assert items[0].title == "Claude 4 released"

    def test_published_is_utc_aware(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(RSS_XML)
        items = _parse_rss_items(root, "Anthropic News", "lab")
        for item in items:
            assert item.published.tzinfo is not None
            assert item.published.tzinfo == timezone.utc

    def test_html_stripped_from_description(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(RSS_XML)
        items = _parse_rss_items(root, "Anthropic News", "lab")
        assert "<p>" not in items[0].raw_summary
        assert "Big announcement" in items[0].raw_summary

    def test_pubdate_with_plus0000_offset(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(RSS_XML)
        items = _parse_rss_items(root, "Anthropic News", "lab")
        assert items[0].published == datetime(2026, 4, 20, 8, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Atom 1.0 parsing
# ---------------------------------------------------------------------------

class TestAtomParser:
    def test_parses_three_entries(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_XML)
        items = _parse_atom_items(root, "SDK releases", "release")
        assert len(items) == 3

    def test_correct_url_and_title(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_XML)
        items = _parse_atom_items(root, "SDK releases", "release")
        assert items[0].url == "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.0.0"
        assert items[0].title == "v1.0.0"

    def test_z_suffix_parsed_as_utc(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_XML)
        items = _parse_atom_items(root, "SDK releases", "release")
        assert items[0].published.tzinfo is not None
        assert items[0].published == datetime(2026, 4, 20, 8, 0, 0, tzinfo=timezone.utc)

    def test_plus00_00_offset_parsed_as_utc(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_XML)
        items = _parse_atom_items(root, "SDK releases", "release")
        assert items[1].published == datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fallback to feedparser
# ---------------------------------------------------------------------------

class TestFeedparserFallback:
    def test_fallback_called_on_parse_error(self, tmp_path: Path):
        """When etree.fromstring raises ParseError, feedparser is invoked."""
        sources_file = tmp_path / "sources.yaml"
        sources_file.write_text(yaml.dump(SOURCES_YAML_CONTENT), encoding="utf-8")

        mock_resp = _make_mock_response(b"<broken xml")

        feedparser_entry = MagicMock()
        feedparser_entry.link = "https://example.com/fallback"
        feedparser_entry.title = "Fallback item"
        feedparser_entry.published_parsed = (2026, 4, 20, 8, 0, 0, 0, 0, 0)
        feedparser_entry.updated_parsed = None
        feedparser_entry.summary = "Fallback summary"
        feedparser_entry.description = ""

        mock_feed = MagicMock()
        mock_feed.entries = [feedparser_entry]

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("feedparser.parse", return_value=mock_feed) as mock_fp,
        ):
            items = fetch_all(sources_file)

        # feedparser should have been called at least once (for broken XML feeds)
        assert mock_fp.called
        fallback_items = [i for i in items if i.url == "https://example.com/fallback"]
        assert len(fallback_items) == 1


# ---------------------------------------------------------------------------
# Summary truncation
# ---------------------------------------------------------------------------

class TestSummaryTruncation:
    def test_long_summary_truncated_to_max_chars(self):
        long_text = "word " * 200  # 1000 chars
        result = _truncate(long_text)
        assert len(result) <= MAX_SUMMARY_CHARS

    def test_short_summary_not_truncated(self):
        short_text = "Short summary."
        assert _truncate(short_text) == short_text

    def test_html_stripped_before_truncation(self):
        html = "<p>" + "word " * 200 + "</p>"
        clean = _strip_html(html)
        result = _truncate(clean)
        assert "<" not in result
        assert len(result) <= MAX_SUMMARY_CHARS


# ---------------------------------------------------------------------------
# fetch_all integration (mocked network)
# ---------------------------------------------------------------------------

class TestFetchAll:
    def test_returns_feed_items_from_multiple_feeds(self, tmp_path: Path):
        sources_file = tmp_path / "sources.yaml"
        sources_file.write_text(yaml.dump(SOURCES_YAML_CONTENT), encoding="utf-8")

        responses = [RSS_XML, ATOM_XML]
        call_count = 0

        def side_effect(request, timeout=10):
            nonlocal call_count
            content = responses[call_count % len(responses)]
            call_count += 1
            return _make_mock_response(content)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            items = fetch_all(sources_file)

        assert len(items) == 6  # 3 RSS + 3 Atom

    def test_failed_feed_does_not_crash_others(self, tmp_path: Path):
        sources_file = tmp_path / "sources.yaml"
        sources_file.write_text(yaml.dump(SOURCES_YAML_CONTENT), encoding="utf-8")

        call_count = 0

        def side_effect(request, timeout=10):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("connection refused")
            return _make_mock_response(ATOM_XML)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            items = fetch_all(sources_file)

        # Second feed (Atom) still parsed despite first failing
        assert any(i.category == "release" for i in items)
