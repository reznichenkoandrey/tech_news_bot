"""Tests for multi-digest config loader + resolve_digest_profiles + run_digest."""

from datetime import datetime, timezone

import pytest

from scripts import digest_pipeline as pipeline
from src.models import FeedItem


def _item(url, topics):
    return FeedItem(
        url=url,
        title=f"title-{url}",
        published=datetime(2026, 4, 23, tzinfo=timezone.utc),
        source="src",
        category="lab",
        raw_summary="",
        topics=tuple(topics),
    )


# ── load_digest_configs ───────────────────────────────────────────────────────

def test_load_digest_configs_reads_real_file():
    """The real config/digests.yaml must parse and have at least one profile."""
    profiles = pipeline.load_digest_configs()
    assert len(profiles) >= 1
    # Every profile has the three required keys after normalisation.
    for p in profiles:
        assert set(p.keys()) == {"name", "emoji", "topics"}
        assert isinstance(p["topics"], list)


def test_load_digest_configs_returns_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "DIGESTS_PATH", tmp_path / "nope.yaml")
    assert pipeline.load_digest_configs() == []


# ── resolve_digest_profiles priority ──────────────────────────────────────────

def test_resolve_uses_env_when_DIGEST_TOPICS_set(monkeypatch):
    monkeypatch.setenv("DIGEST_TOPICS", "design,ai-lab")
    profiles = pipeline.resolve_digest_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "AI/Tech"
    assert set(profiles[0]["topics"]) == {"design", "ai-lab"}


def test_resolve_falls_back_to_file_when_env_empty(monkeypatch):
    monkeypatch.delenv("DIGEST_TOPICS", raising=False)
    profiles = pipeline.resolve_digest_profiles()
    # Real digests.yaml has at least "AI/Tech" and "Design"
    names = [p["name"] for p in profiles]
    assert "AI/Tech" in names and "Design" in names


def test_resolve_fallback_default_when_no_env_no_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DIGEST_TOPICS", raising=False)
    monkeypatch.setattr(pipeline, "DIGESTS_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(pipeline, "USER_PREFS_PATH", tmp_path / "user_prefs.yaml")
    profiles = pipeline.resolve_digest_profiles()
    assert profiles == [{"name": "AI/Tech", "emoji": "🤖", "topics": []}]


# ── run_digest ────────────────────────────────────────────────────────────────

def test_run_digest_skips_when_no_items_after_filter(monkeypatch):
    monkeypatch.setattr(pipeline, "call_llm", lambda *a, **k: pytest.fail("LLM should not be called"))
    monkeypatch.setattr(pipeline, "send_message", lambda *a, **k: pytest.fail("send should not be called"))
    sent, urls, entries = pipeline.run_digest(
        {"name": "Design", "emoji": "🎨", "topics": ["design"]},
        [_item("a", ["ai-lab"])],
        oauth_token="x",
        tg_token="y",
        tg_chat="z",
        window=24,
        max_items=15,
    )
    assert sent == 0
    assert urls == []
    assert entries == []


def test_run_digest_happy_path_renders_single_blob(monkeypatch):
    captured: dict[str, list] = {"texts": []}

    def fake_llm(prompt, _oauth):
        return '[{"url":"http://a","title":"T","source":"S","category":"lab","summary_uk":"uk","importance":5}]'

    def fake_send(text, _token, _chat, **_kwargs):
        captured["texts"].append(text)

    monkeypatch.setattr(pipeline, "call_llm", fake_llm)
    monkeypatch.setattr(pipeline, "send_message", fake_send)

    sent, urls, entries = pipeline.run_digest(
        {"name": "Design", "emoji": "🎨", "topics": ["design"]},
        [_item("http://a", ["design"]), _item("http://b", ["ai-lab"])],
        oauth_token="x",
        tg_token="y",
        tg_chat="z",
        window=24,
        max_items=15,
        start_idx=7,
    )
    assert sent == 1
    assert urls == ["http://a"]
    assert entries == [
        {"url": "http://a", "title": "T", "source": "S", "category": "lab", "summary_uk": "uk", "importance": 5},
    ]
    # Single blob send: header + items + footer concatenated into one text.
    assert len(captured["texts"]) == 1
    blob = captured["texts"][0]
    assert "🎨 Design дайджест" in blob
    # Numbering respects start_idx (so /save N stays globally unique).
    assert "<b>7. " in blob
    # Footer with action hints lives in the same blob.
    assert "/save N" in blob


def test_run_digest_considered_urls_include_everything_matched_even_if_llm_drops(monkeypatch):
    """Dedup must cover all filtered URLs, not just the top-N the LLM kept."""
    monkeypatch.setattr(
        pipeline,
        "call_llm",
        lambda *a, **k: '[{"url":"http://a","title":"T","source":"S","category":"lab","summary_uk":"uk","importance":5}]',
    )
    monkeypatch.setattr(pipeline, "send_message", lambda *a, **k: None)

    items = [_item("http://a", ["design"]), _item("http://b", ["design"]), _item("http://c", ["design"])]
    sent, urls, entries = pipeline.run_digest(
        {"name": "Design", "emoji": "🎨", "topics": ["design"]},
        items,
        oauth_token="x",
        tg_token="y",
        tg_chat="z",
        window=24,
        max_items=15,
    )
    assert sent == 1  # LLM only returned one
    assert set(urls) == {"http://a", "http://b", "http://c"}  # dedup covers all filtered
    assert len(entries) == 1


def test_save_last_digest_writes_numbered_map(monkeypatch, tmp_path):
    target = tmp_path / "last_digest.json"
    monkeypatch.setattr(pipeline, "LAST_DIGEST_PATH", target)
    items = [
        {"n": 1, "url": "http://a", "title": "T1", "profile": "AI/Tech"},
        {"n": 2, "url": "http://b", "title": "T2", "profile": "Design"},
    ]
    pipeline.save_last_digest(items)

    import json
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["items"] == items
    assert "generated_at" in payload
