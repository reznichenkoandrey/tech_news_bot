"""
Tests for src/dedup.py — filter_new, load_seen, save_seen atomicity and FIFO cap.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.dedup import SEEN_CAP, filter_new, load_seen, save_seen
from src.models import FeedItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_item(
    url: str = "https://example.com/article",
    hours_ago: float = 1.0,
    source: str = "Test Source",
    category: str = "lab",
) -> FeedItem:
    """Build a FeedItem published `hours_ago` hours in the past."""
    published = datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)
    return FeedItem(
        url=url,
        title=f"Article at {url}",
        published=published,
        source=source,
        category=category,
        raw_summary="",
    )


# ---------------------------------------------------------------------------
# filter_new
# ---------------------------------------------------------------------------

class TestFilterNew:
    def test_new_item_within_window_passes(self):
        item = _make_item(hours_ago=1.0)
        result = filter_new([item], seen=set(), max_age_hours=24)
        assert len(result) == 1
        assert result[0].url == item.url

    def test_item_older_than_window_excluded(self):
        item = _make_item(hours_ago=25.0)
        result = filter_new([item], seen=set(), max_age_hours=24)
        assert len(result) == 0

    def test_exactly_at_boundary_excluded(self):
        # Published exactly max_age_hours ago — should be excluded (strictly older)
        item = _make_item(hours_ago=24.001)
        result = filter_new([item], seen=set(), max_age_hours=24)
        assert len(result) == 0

    def test_url_in_seen_excluded(self):
        item = _make_item(url="https://example.com/seen", hours_ago=1.0)
        result = filter_new([item], seen={"https://example.com/seen"}, max_age_hours=24)
        assert len(result) == 0

    def test_url_not_in_seen_passes(self):
        item = _make_item(url="https://example.com/new", hours_ago=1.0)
        result = filter_new([item], seen={"https://example.com/other"}, max_age_hours=24)
        assert len(result) == 1

    def test_mixed_items(self):
        items = [
            _make_item(url="https://example.com/old", hours_ago=30.0),   # too old
            _make_item(url="https://example.com/seen", hours_ago=1.0),   # in seen
            _make_item(url="https://example.com/new", hours_ago=2.0),    # valid
        ]
        seen = {"https://example.com/seen"}
        result = filter_new(items, seen=seen, max_age_hours=24)
        assert len(result) == 1
        assert result[0].url == "https://example.com/new"

    def test_empty_inputs(self):
        assert filter_new([], seen=set(), max_age_hours=24) == []


# ---------------------------------------------------------------------------
# load_seen
# ---------------------------------------------------------------------------

class TestLoadSeen:
    def test_returns_empty_set_if_file_missing(self, tmp_path: Path):
        result = load_seen(tmp_path / "seen.json")
        assert result == set()

    def test_loads_existing_urls(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        urls = ["https://a.com", "https://b.com"]
        seen_file.write_text(json.dumps(urls), encoding="utf-8")
        result = load_seen(seen_file)
        assert result == set(urls)

    def test_returns_empty_set_on_malformed_json(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        seen_file.write_text("not json", encoding="utf-8")
        result = load_seen(seen_file)
        assert result == set()

    def test_returns_empty_set_on_unexpected_format(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        seen_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        result = load_seen(seen_file)
        assert result == set()


# ---------------------------------------------------------------------------
# save_seen
# ---------------------------------------------------------------------------

class TestSaveSeen:
    def test_creates_file_if_missing(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        save_seen(seen_file, set(), ["https://new.com"])
        assert seen_file.exists()
        data = json.loads(seen_file.read_text(encoding="utf-8"))
        assert "https://new.com" in data

    def test_appends_new_urls_to_existing(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        existing = ["https://a.com", "https://b.com"]
        seen_file.write_text(json.dumps(existing), encoding="utf-8")

        save_seen(seen_file, set(existing), ["https://c.com"])
        data = json.loads(seen_file.read_text(encoding="utf-8"))
        assert "https://a.com" in data
        assert "https://c.com" in data

    def test_no_duplicates_added(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        url = "https://duplicate.com"
        seen_file.write_text(json.dumps([url]), encoding="utf-8")

        save_seen(seen_file, {url}, [url])
        data = json.loads(seen_file.read_text(encoding="utf-8"))
        assert data.count(url) == 1

    def test_fifo_cap_enforced(self, tmp_path: Path):
        seen_file = tmp_path / "seen.json"
        # Pre-fill with SEEN_CAP URLs
        existing = [f"https://example.com/{i}" for i in range(SEEN_CAP)]
        seen_file.write_text(json.dumps(existing), encoding="utf-8")

        new_url = "https://example.com/new"
        save_seen(seen_file, set(existing), [new_url])

        data = json.loads(seen_file.read_text(encoding="utf-8"))
        assert len(data) == SEEN_CAP
        # Newest URL should be present
        assert new_url in data
        # Oldest URL should have been evicted (FIFO)
        assert "https://example.com/0" not in data

    def test_atomic_write_no_temp_file_left(self, tmp_path: Path):
        """Temp file should not remain after successful save."""
        seen_file = tmp_path / "seen.json"
        save_seen(seen_file, set(), ["https://a.com"])

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_creates_parent_directories(self, tmp_path: Path):
        nested = tmp_path / "subdir" / "deeper" / "seen.json"
        save_seen(nested, set(), ["https://a.com"])
        assert nested.exists()
