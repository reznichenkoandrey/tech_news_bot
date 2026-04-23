"""Tests for the Telegram bot command parser and user_prefs overlay."""

import pytest
import yaml

from scripts import bot_poll, digest_pipeline


@pytest.fixture
def registry():
    # Minimal subset mirroring real topics.yaml
    return {
        "ai-lab": {"slug": "ai-lab", "name": "AI labs", "emoji": "🧪"},
        "design": {"slug": "design", "name": "Design / UX", "emoji": "🎨"},
        "media": {"slug": "media", "name": "Tech media", "emoji": "📰"},
    }


@pytest.fixture
def sources():
    return [
        {"name": "Anthropic", "url": "https://a", "topics": ["ai-lab"]},
        {"name": "Smashing", "url": "https://b", "topics": ["design"]},
        {"name": "TechCrunch", "url": "https://c", "topics": ["media"]},
    ]


@pytest.fixture
def digests():
    return [
        {"name": "AI/Tech", "emoji": "🤖", "topics": ["ai-lab", "media"]},
        {"name": "Design", "emoji": "🎨", "topics": ["design"]},
    ]


@pytest.fixture
def prefs():
    return {"active_topics": []}


# ── command parser ────────────────────────────────────────────────────────────

def test_non_slash_message_is_ignored(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "hello there", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert reply == ""
    assert not mutated


def test_help_lists_commands(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/help", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert "/topics" in reply and "/add" in reply
    assert not mutated


def test_topics_marks_active(registry, sources, digests):
    prefs = {"active_topics": ["design"]}
    reply, _ = bot_poll.handle_command(
        "/topics", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert "✅" in reply
    assert "design" in reply
    assert "ai-lab" in reply


def test_add_new_topic_mutates_prefs(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/add design", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is True
    assert prefs["active_topics"] == ["design"]
    assert "додав" in reply


def test_add_existing_topic_is_noop(registry, sources, digests):
    prefs = {"active_topics": ["design"]}
    reply, mutated = bot_poll.handle_command(
        "/add design", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is False
    assert prefs["active_topics"] == ["design"]
    assert "уже" in reply.lower()


def test_add_unknown_slug_is_rejected(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/add foobar", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is False
    assert "foobar" in reply
    assert "невідомий" in reply


def test_add_without_arg_shows_usage(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/add", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is False
    assert "slug" in reply


def test_remove_drops_topic(registry, sources, digests):
    prefs = {"active_topics": ["design", "ai-lab"]}
    reply, mutated = bot_poll.handle_command(
        "/remove design", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is True
    assert prefs["active_topics"] == ["ai-lab"]
    assert "ai-lab" in reply


def test_remove_missing_topic_is_noop(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/remove design", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is False
    assert "і так немає" in reply


def test_reset_empties_filter(registry, sources, digests):
    prefs = {"active_topics": ["design", "ai-lab"]}
    reply, mutated = bot_poll.handle_command(
        "/reset", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is True
    assert prefs["active_topics"] == []
    assert "очищено" in reply


def test_reset_on_empty_is_noop(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/reset", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is False


def test_sources_without_arg_summarises_by_topic(registry, sources, digests, prefs):
    reply, _ = bot_poll.handle_command(
        "/sources", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert "Feeds всього: 3" in reply
    assert "design" in reply
    assert "ai-lab" in reply


def test_sources_with_slug_lists_matching_feeds(registry, sources, digests, prefs):
    reply, _ = bot_poll.handle_command(
        "/sources design", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert "Smashing" in reply
    assert "Anthropic" not in reply


def test_status_reports_filter(registry, sources, digests):
    prefs = {"active_topics": ["design"]}
    reply, _ = bot_poll.handle_command(
        "/status", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert "design" in reply
    assert "2" in reply  # digest profile count


def test_bot_username_suffix_stripped(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/add@scr1be_tech_bot design",
        registry=registry, sources=sources, digests=digests, prefs=prefs,
    )
    assert mutated is True
    assert prefs["active_topics"] == ["design"]


def test_unknown_command_replies_with_hint(registry, sources, digests, prefs):
    reply, mutated = bot_poll.handle_command(
        "/foobar", registry=registry, sources=sources, digests=digests, prefs=prefs
    )
    assert mutated is False
    assert "/help" in reply


# ── authorisation ─────────────────────────────────────────────────────────────

def test_authorised_when_chat_id_matches():
    upd = {"message": {"chat": {"id": 34412475}, "text": "/topics"}}
    assert bot_poll.is_authorised(upd, "34412475") is True


def test_unauthorised_when_chat_id_differs():
    upd = {"message": {"chat": {"id": 999}, "text": "/topics"}}
    assert bot_poll.is_authorised(upd, "34412475") is False


def test_unauthorised_on_malformed_update():
    assert bot_poll.is_authorised({}, "34412475") is False


# ── user_prefs persistence round trip ─────────────────────────────────────────

def test_save_and_load_user_prefs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_poll, "USER_PREFS_PATH", tmp_path / "user_prefs.yaml")
    bot_poll.save_user_prefs({"active_topics": ["design", "ai-lab"]})
    loaded = bot_poll.load_user_prefs()
    assert loaded["active_topics"] == ["design", "ai-lab"]


def test_save_user_prefs_empty_list_is_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_poll, "USER_PREFS_PATH", tmp_path / "user_prefs.yaml")
    bot_poll.save_user_prefs({"active_topics": []})
    data = yaml.safe_load((tmp_path / "user_prefs.yaml").read_text())
    assert data["active_topics"] == [] or data["active_topics"] is None


# ── resolve_digest_profiles overlay ──────────────────────────────────────────

def test_resolve_with_empty_user_prefs_returns_full_list(monkeypatch, tmp_path):
    monkeypatch.delenv("DIGEST_TOPICS", raising=False)
    monkeypatch.setattr(digest_pipeline, "USER_PREFS_PATH", tmp_path / "np.yaml")
    profiles = digest_pipeline.resolve_digest_profiles()
    names = [p["name"] for p in profiles]
    assert "AI/Tech" in names and "Design" in names


def test_resolve_narrows_profiles_by_user_active_topics(monkeypatch, tmp_path):
    monkeypatch.delenv("DIGEST_TOPICS", raising=False)
    prefs_file = tmp_path / "user_prefs.yaml"
    prefs_file.write_text("active_topics:\n  - design\n")
    monkeypatch.setattr(digest_pipeline, "USER_PREFS_PATH", prefs_file)

    profiles = digest_pipeline.resolve_digest_profiles()
    # AI/Tech profile topics = [ai-lab, ai-tools, ai-local, ai-infra, ai-research,
    #                           community, media]; no overlap with {design} → dropped.
    names = [p["name"] for p in profiles]
    assert names == ["Design"]
    assert profiles[0]["topics"] == ["design"]


def test_resolve_with_env_override_ignores_user_prefs(monkeypatch, tmp_path):
    prefs_file = tmp_path / "user_prefs.yaml"
    prefs_file.write_text("active_topics:\n  - design\n")
    monkeypatch.setattr(digest_pipeline, "USER_PREFS_PATH", prefs_file)
    monkeypatch.setenv("DIGEST_TOPICS", "ai-lab")

    profiles = digest_pipeline.resolve_digest_profiles()
    assert len(profiles) == 1
    assert profiles[0]["topics"] == ["ai-lab"]


def test_overlay_keeps_empty_topics_profile_as_user_filter(monkeypatch):
    from scripts.digest_pipeline import _overlay_with_user_filter

    base = [{"name": "All", "emoji": "📰", "topics": []}]
    out = _overlay_with_user_filter(base, {"design", "ai-lab"})
    assert out[0]["topics"] == ["ai-lab", "design"]
