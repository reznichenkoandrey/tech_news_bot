"""Tests for DIGEST_TOPICS filter plumbing in scripts/digest_pipeline."""

from datetime import datetime, timezone

from scripts.digest_pipeline import (
    filter_by_topics,
    parse_active_topics,
    render_topics_header_suffix,
)
from src.models import FeedItem


def _item(url, topics):
    return FeedItem(
        url=url,
        title="t",
        published=datetime(2026, 4, 23, tzinfo=timezone.utc),
        source="s",
        category="lab",
        raw_summary="",
        topics=tuple(topics),
    )


def test_filter_passthrough_when_no_active_topics():
    items = [_item("a", ["ai-lab"]), _item("b", ["design"])]
    assert filter_by_topics(items, set()) == items


def test_filter_keeps_items_matching_any_active_topic():
    items = [
        _item("a", ["ai-lab", "ai-tools"]),
        _item("b", ["design"]),
        _item("c", ["media"]),
    ]
    kept = filter_by_topics(items, {"design", "media"})
    urls = {i.url for i in kept}
    assert urls == {"b", "c"}


def test_filter_drops_items_with_empty_topics_when_filter_active():
    items = [_item("a", []), _item("b", ["design"])]
    assert [i.url for i in filter_by_topics(items, {"design"})] == ["b"]


def test_parse_active_topics_empty_and_whitespace_disable_filter():
    registry = {"design": {}, "ai-lab": {}}
    assert parse_active_topics("", registry) == set()
    assert parse_active_topics("   ", registry) == set()


def test_parse_active_topics_splits_and_trims():
    registry = {"design": {}, "ai-lab": {}, "media": {}}
    assert parse_active_topics("design, ai-lab", registry) == {"design", "ai-lab"}
    assert parse_active_topics("media,design,,", registry) == {"media", "design"}


def test_parse_active_topics_silently_drops_unknown_slugs():
    registry = {"design": {}}
    # typo 'desing' is dropped but 'design' still makes it through
    assert parse_active_topics("desing,design", registry) == {"design"}


def test_header_suffix_empty_when_no_filter():
    assert render_topics_header_suffix(set(), {"design": {}}) == ""


def test_header_suffix_formats_single_topic():
    registry = {"design": {"emoji": "🎨", "name": "Design / UX"}}
    assert render_topics_header_suffix({"design"}, registry) == " [🎨 Design / UX]"


def test_header_suffix_formats_multi_topic_sorted_by_slug():
    registry = {
        "design": {"emoji": "🎨", "name": "Design"},
        "ai-lab": {"emoji": "🧪", "name": "AI labs"},
    }
    # sorted by slug: ai-lab < design
    assert (
        render_topics_header_suffix({"design", "ai-lab"}, registry)
        == " [🧪 AI labs · 🎨 Design]"
    )
