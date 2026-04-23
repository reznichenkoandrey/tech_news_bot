"""Tests for the topics field on FeedItem and its propagation through fetch/dedup."""

from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from src.dedup import _items_from_json, _items_to_json
from src.fetcher import _parse_atom_items, _parse_rss_items
from src.models import FeedItem

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_item(topics=()):
    return FeedItem(
        url="https://example.com/post",
        title="Example",
        published=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        source="Example",
        category="lab",
        raw_summary="",
        topics=tuple(topics),
    )


def test_feeditem_default_topics_is_empty_tuple():
    item = _make_item()
    assert item.topics == ()


def test_feeditem_topics_round_trip_through_json():
    item = _make_item(topics=["ai-lab", "design"])
    roundtripped = _items_from_json(_items_to_json([item]))
    assert len(roundtripped) == 1
    assert roundtripped[0].topics == ("ai-lab", "design")


def test_items_from_json_handles_legacy_records_without_topics():
    raw = """[
        {
            "url": "https://example.com/a",
            "title": "Legacy",
            "published": "2026-04-23T10:00:00+00:00",
            "source": "Legacy",
            "category": "lab",
            "raw_summary": ""
        }
    ]"""
    items = _items_from_json(raw)
    assert items[0].topics == ()


def test_parse_rss_items_applies_topics_to_every_entry():
    rss = b"""<?xml version="1.0"?>
    <rss version="2.0">
        <channel>
            <title>T</title>
            <item>
                <link>https://example.com/a</link>
                <title>A</title>
                <pubDate>Thu, 23 Apr 2026 10:00:00 GMT</pubDate>
                <description>x</description>
            </item>
            <item>
                <link>https://example.com/b</link>
                <title>B</title>
                <pubDate>Thu, 23 Apr 2026 11:00:00 GMT</pubDate>
                <description>y</description>
            </item>
        </channel>
    </rss>"""
    root = ElementTree.fromstring(rss)
    items = _parse_rss_items(root, "Src", "lab", topics=("ai-lab", "ai-tools"))
    assert len(items) == 2
    assert all(i.topics == ("ai-lab", "ai-tools") for i in items)


def test_parse_atom_items_applies_topics():
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <link href="https://example.com/x"/>
            <title>X</title>
            <published>2026-04-23T10:00:00Z</published>
            <summary>s</summary>
        </entry>
    </feed>"""
    root = ElementTree.fromstring(atom)
    items = _parse_atom_items(root, "Src", "release", topics=("design",))
    assert len(items) == 1
    assert items[0].topics == ("design",)


def test_every_source_in_sources_yaml_has_valid_topics():
    """Guard: every feed must reference only slugs defined in topics.yaml."""
    topics_cfg = yaml.safe_load((REPO_ROOT / "config" / "topics.yaml").read_text())
    known_slugs = {t["slug"] for t in topics_cfg["topics"]}

    sources_cfg = yaml.safe_load((REPO_ROOT / "config" / "sources.yaml").read_text())
    unknown = []
    for feed in sources_cfg["feeds"]:
        feed_topics = feed.get("topics", [])
        if not feed_topics:
            pytest.fail(f"Feed {feed['name']!r} has no topics")
        for slug in feed_topics:
            if slug not in known_slugs:
                unknown.append((feed["name"], slug))
    assert not unknown, f"Unknown topic slugs: {unknown}"
