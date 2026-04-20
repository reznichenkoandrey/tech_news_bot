"""
Deduplication helpers: load/save seen URLs with FIFO cap and atomic writes.
"""

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.models import FeedItem

logger = logging.getLogger(__name__)

# Maximum number of URLs to keep in seen.json (FIFO eviction)
SEEN_CAP = 5000


def load_seen(path: Path) -> set[str]:
    """
    Read seen URLs from JSON file.

    Returns empty set if file does not exist or is malformed.
    """
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return set(data)
        logger.warning("seen.json має неочікуваний формат, скидаю")
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Не вдалося прочитати seen.json: %s", exc)
        return set()


def filter_new(
    items: list[FeedItem],
    seen: set[str],
    max_age_hours: int,
) -> list[FeedItem]:
    """
    Return only items whose URL is not in seen AND published within max_age_hours.

    Published timestamp must be timezone-aware UTC.
    """
    now_utc = datetime.now(tz=timezone.utc)
    cutoff = now_utc.timestamp() - max_age_hours * 3600

    result: list[FeedItem] = []
    for item in items:
        if item.url in seen:
            continue
        if item.published.timestamp() < cutoff:
            continue
        result.append(item)

    return result


def save_seen(path: Path, seen: set[str], new_urls: list[str]) -> None:
    """
    Add new_urls to seen set and persist atomically via tmpfile + os.replace.

    Enforces SEEN_CAP by evicting oldest entries (FIFO).
    seen.json stores an ordered list so insertion order is preserved.
    """
    # Load current ordered list to preserve insertion order for FIFO eviction
    existing_ordered: list[str] = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                existing_ordered = data
        except (json.JSONDecodeError, OSError):
            existing_ordered = []

    # Build ordered list: existing + new (deduplicated, preserving order)
    existing_set = set(existing_ordered)
    for url in new_urls:
        if url not in existing_set:
            existing_ordered.append(url)
            existing_set.add(url)

    # Enforce FIFO cap — remove oldest entries from the front
    if len(existing_ordered) > SEEN_CAP:
        existing_ordered = existing_ordered[-SEEN_CAP:]

    # Atomic write: write to temp file in same directory, then rename
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(existing_ordered, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file if rename failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info("seen.json оновлено: %d URLs (додано %d)", len(existing_ordered), len(new_urls))


def _items_from_json(raw: str) -> list[FeedItem]:
    """Deserialize JSON list of dicts back into FeedItem list."""
    from datetime import datetime
    records = json.loads(raw)
    items: list[FeedItem] = []
    for r in records:
        published = datetime.fromisoformat(r["published"])
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        items.append(FeedItem(
            url=r["url"],
            title=r["title"],
            published=published,
            source=r["source"],
            category=r["category"],
            raw_summary=r.get("raw_summary", ""),
        ))
    return items


def _items_to_json(items: list[FeedItem]) -> str:
    """Serialize FeedItem list to JSON."""
    return json.dumps(
        [
            {
                "url": item.url,
                "title": item.title,
                "published": item.published.isoformat(),
                "source": item.source,
                "category": item.category,
                "raw_summary": item.raw_summary,
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    import os as _os

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    seen_path = Path(__file__).parent.parent / "data" / "seen.json"
    max_age = int(_os.environ.get("DIGEST_WINDOW_HOURS", "24"))

    subcommand = sys.argv[1] if len(sys.argv) > 1 else ""

    if subcommand == "filter":
        raw_input = sys.stdin.read()
        all_items = _items_from_json(raw_input)
        seen = load_seen(seen_path)
        new_items = filter_new(all_items, seen, max_age)
        logger.info("Нових новин: %d з %d", len(new_items), len(all_items))
        print(_items_to_json(new_items))

    elif subcommand == "update":
        new_urls = sys.argv[2:]
        seen = load_seen(seen_path)
        save_seen(seen_path, seen, new_urls)

    else:
        print("Usage: python -m src.dedup filter | update <url1> <url2> ...", file=sys.stderr)
        sys.exit(1)
