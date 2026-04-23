"""Tests for src.callback_map."""

import json
from pathlib import Path

from src import callback_map


def test_url_hash_is_stable_and_16_chars():
    h = callback_map.url_hash("https://example.com/article")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)
    assert callback_map.url_hash("https://example.com/article") == h


def test_url_hash_differs_per_url():
    assert callback_map.url_hash("https://a") != callback_map.url_hash("https://b")


def test_merge_urls_writes_file_and_returns_additions(tmp_path: Path):
    path = tmp_path / "map.json"
    added = callback_map.merge_urls(path, ["https://a", "https://b"])
    assert len(added) == 2
    assert callback_map.url_hash("https://a") in added

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk[callback_map.url_hash("https://a")] == "https://a"
    assert on_disk[callback_map.url_hash("https://b")] == "https://b"


def test_merge_urls_preserves_existing_entries(tmp_path: Path):
    path = tmp_path / "map.json"
    callback_map.merge_urls(path, ["https://a"])
    callback_map.merge_urls(path, ["https://b"])

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk.values()) == {"https://a", "https://b"}


def test_merge_urls_is_idempotent(tmp_path: Path):
    path = tmp_path / "map.json"
    callback_map.merge_urls(path, ["https://a"])
    callback_map.merge_urls(path, ["https://a"])

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk) == 1


def test_merge_urls_enforces_fifo_cap(tmp_path: Path, monkeypatch):
    # Shrink the cap so the test stays fast.
    monkeypatch.setattr(callback_map, "MAP_CAP", 3)
    path = tmp_path / "map.json"

    callback_map.merge_urls(path, [f"https://{i}" for i in range(5)])
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert len(on_disk) == 3
    # Oldest two ("0" and "1") are evicted; newest three remain.
    assert callback_map.url_hash("https://0") not in on_disk
    assert callback_map.url_hash("https://4") in on_disk


def test_load_map_returns_empty_on_missing_file(tmp_path: Path):
    assert callback_map.load_map(tmp_path / "nope.json") == {}


def test_load_map_ignores_malformed_content(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert callback_map.load_map(path) == {}
