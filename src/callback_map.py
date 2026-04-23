"""
Callback map: short URL hashes stored in data/callback_map.json so Telegram
inline-button callback_data (max 64 bytes) can reference long article URLs.

Format:
    {"<16-char sha256 prefix>": "<url>"}

FIFO-capped so the file doesn't grow forever; latest digests push oldest
entries out. Insertion order is preserved by round-tripping through a dict
that we rebuild in insertion order on every save.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAP_CAP = 1000


def url_hash(url: str) -> str:
    """Same scheme as scripts.summarize_article.cache_key — 16-hex-char SHA256 prefix."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def load_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("callback_map.json unreadable, resetting: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("callback_map.json has unexpected shape, resetting")
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_map(path: Path, mapping: dict[str, str]) -> None:
    """Atomic JSON write. Caller owns cap / ordering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def merge_urls(path: Path, urls: list[str]) -> dict[str, str]:
    """
    Add every URL in `urls` to the on-disk map (keyed by url_hash), enforce
    FIFO cap, persist, and return the freshly-computed hash→url dict for the
    new URLs (the digest renderer uses it to label each item's buttons).
    """
    existing = load_map(path)
    additions: dict[str, str] = {}
    for url in urls:
        h = url_hash(url)
        additions[h] = url
        if h not in existing:
            existing[h] = url

    # Enforce cap: keep the newest MAP_CAP entries by insertion order.
    if len(existing) > MAP_CAP:
        items = list(existing.items())[-MAP_CAP:]
        existing = dict(items)

    save_map(path, existing)
    logger.info("callback_map.json updated: %d entries (added %d)", len(existing), len(additions))
    return additions
