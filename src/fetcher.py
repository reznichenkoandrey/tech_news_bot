"""
RSS/Atom feed fetcher using stdlib urllib + xml.etree.
Falls back to feedparser for malformed XML.
"""

import json
import logging
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError

import yaml

from src.models import FeedItem

logger = logging.getLogger(__name__)

USER_AGENT = "tech_news_bot/1.0 (+github.com/reznichenkoandrey/tech_news_bot)"
MAX_SUMMARY_CHARS = 500
MAX_WORKERS = 8

# XML namespaces used in Atom feeds
ATOM_NS = "http://www.w3.org/2005/Atom"
ATOM_CONTENT_NS = "http://www.w3.org/2005/Atom"


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    clean = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", clean).strip()


def _truncate(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Truncate to max_chars without cutting inside a word."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


def _parse_rfc2822(value: str) -> datetime:
    """Parse RFC 2822 pubDate (RSS 2.0) → UTC-aware datetime."""
    dt = parsedate_to_datetime(value)
    return dt.astimezone(timezone.utc)


def _parse_iso8601(value: str) -> datetime:
    """Parse ISO 8601 datetime (Atom) → UTC-aware datetime."""
    # Python 3.11+ fromisoformat handles Z; older versions need manual replace
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_rss_items(
    root: ElementTree.Element,
    source_name: str,
    category: str,
    topics: tuple[str, ...] = (),
) -> list[FeedItem]:
    """Extract FeedItem list from an RSS 2.0 ElementTree root."""
    items: list[FeedItem] = []
    channel = root.find("channel")
    if channel is None:
        return items

    for item in channel.findall("item"):
        url_el = item.find("link")
        title_el = item.find("title")
        pub_el = item.find("pubDate")
        summary_el = item.find("description")

        if url_el is None or title_el is None or pub_el is None:
            continue

        url = (url_el.text or "").strip()
        title = _strip_html(title_el.text or "").strip()
        raw_summary = _truncate(_strip_html(summary_el.text or "")) if summary_el is not None else ""

        try:
            published = _parse_rfc2822(pub_el.text or "")
        except Exception:
            continue

        if not url or not title:
            continue

        items.append(FeedItem(
            url=url,
            title=title,
            published=published,
            source=source_name,
            category=category,
            raw_summary=raw_summary,
            topics=topics,
        ))

    return items


def _parse_atom_items(
    root: ElementTree.Element,
    source_name: str,
    category: str,
    topics: tuple[str, ...] = (),
) -> list[FeedItem]:
    """Extract FeedItem list from an Atom 1.0 ElementTree root."""
    items: list[FeedItem] = []
    ns = ATOM_NS

    for entry in root.findall(f"{{{ns}}}entry"):
        # Atom link has rel="alternate" or is the first <link>
        url = ""
        for link in entry.findall(f"{{{ns}}}link"):
            rel = link.get("rel", "alternate")
            if rel == "alternate" or rel == "":
                url = link.get("href", "")
                break
        if not url:
            link_el = entry.find(f"{{{ns}}}link")
            if link_el is not None:
                url = link_el.get("href", "")

        title_el = entry.find(f"{{{ns}}}title")
        # Prefer <published> over <updated>
        pub_el = entry.find(f"{{{ns}}}published")
        if pub_el is None:
            pub_el = entry.find(f"{{{ns}}}updated")

        summary_el = entry.find(f"{{{ns}}}summary")
        if summary_el is None:
            summary_el = entry.find(f"{{{ns}}}content")

        if not url or title_el is None or pub_el is None:
            continue

        title = _strip_html(title_el.text or "").strip()
        raw_summary = _truncate(_strip_html(summary_el.text or "")) if summary_el is not None else ""

        try:
            published = _parse_iso8601(pub_el.text or "")
        except Exception:
            continue

        if not title:
            continue

        items.append(FeedItem(
            url=url,
            title=title,
            published=published,
            source=source_name,
            category=category,
            raw_summary=raw_summary,
            topics=topics,
        ))

    return items


def _fetch_via_feedparser(
    url: str,
    source_name: str,
    category: str,
    topics: tuple[str, ...] = (),
) -> list[FeedItem]:
    """Fallback parser using feedparser library."""
    import feedparser  # imported lazily — only used as fallback

    feed = feedparser.parse(url)
    items: list[FeedItem] = []

    for entry in feed.entries:
        item_url = getattr(entry, "link", "")
        title = _strip_html(getattr(entry, "title", "")).strip()

        # Resolve published time
        published_parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if published_parsed is None:
            continue
        published = datetime(*published_parsed[:6], tzinfo=timezone.utc)

        summary_raw = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or ""
        )
        raw_summary = _truncate(_strip_html(summary_raw))

        if not item_url or not title:
            continue

        items.append(FeedItem(
            url=item_url,
            title=title,
            published=published,
            source=source_name,
            category=category,
            raw_summary=raw_summary,
            topics=topics,
        ))

    return items


def _fetch_feed(feed_config: dict) -> list[FeedItem]:
    """Fetch and parse a single feed. Returns empty list on unrecoverable error."""
    name: str = feed_config["name"]
    url: str = feed_config["url"]
    category: str = feed_config["category"]
    topics: tuple[str, ...] = tuple(feed_config.get("topics", ()))

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw_bytes: bytes = response.read()
    except Exception as exc:
        logger.warning("Не вдалося завантажити %s (%s): %s", name, url, exc)
        return []

    try:
        root = ElementTree.fromstring(raw_bytes)
    except ParseError as exc:
        logger.warning("XML помилка для %s, використовую feedparser: %s", name, exc)
        return _fetch_via_feedparser(url, name, category, topics)

    # Detect feed type by root tag.
    # Atom feeds have a namespaced root: {http://www.w3.org/2005/Atom}feed
    # RSS feeds have a plain root: rss or rdf
    tag = root.tag
    tag_local = tag.split("}")[-1].lower() if "}" in tag else tag.lower()

    if tag == f"{{{ATOM_NS}}}feed" or (tag_local == "feed" and ATOM_NS in tag):
        return _parse_atom_items(root, name, category, topics)
    elif tag_local in ("rss", "rdf"):
        return _parse_rss_items(root, name, category, topics)
    else:
        # Ambiguous root — try Atom first (namespaced), then RSS
        atom_items = _parse_atom_items(root, name, category, topics)
        if atom_items:
            return atom_items
        return _parse_rss_items(root, name, category, topics)


def fetch_all(sources_path: Path, timeout: int = 10) -> list[FeedItem]:
    """
    Load sources.yaml and fetch all feeds in parallel.

    Args:
        sources_path: Path to config/sources.yaml
        timeout: per-request timeout in seconds (passed via closure to _fetch_feed)

    Returns:
        Flat list of FeedItem across all feeds, deduplicated by URL.
    """
    with open(sources_path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    feeds: list[dict] = config.get("feeds", [])
    logger.info("Завантажую %d фідів...", len(feeds))

    from src.blocklist import is_blocked

    all_items: list[FeedItem] = []
    failed_count = 0
    blocked_count = 0
    seen_urls: set[str] = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_feed = {executor.submit(_fetch_feed, feed): feed for feed in feeds}

        for future in as_completed(future_to_feed):
            feed = future_to_feed[future]
            try:
                items = future.result()
            except Exception as exc:
                logger.error("Критична помилка для %s: %s", feed["name"], exc)
                failed_count += 1
                continue

            if not items:
                failed_count += 1

            for item in items:
                if item.url in seen_urls:
                    continue
                blocked, reason = is_blocked(item.url)
                if blocked:
                    blocked_count += 1
                    logger.info("Blocked (%s): %s", reason, item.title[:80])
                    continue
                seen_urls.add(item.url)
                all_items.append(item)

    logger.info(
        "Отримано %d унікальних новин з %d фідів (%d з помилками, %d відфільтровано)",
        len(all_items),
        len(feeds),
        failed_count,
        blocked_count,
    )

    return all_items


def _items_to_json(items: list[FeedItem]) -> str:
    """Serialize FeedItem list to JSON with ISO 8601 datetimes."""
    return json.dumps(
        [
            {
                "url": item.url,
                "title": item.title,
                "published": item.published.isoformat(),
                "source": item.source,
                "category": item.category,
                "raw_summary": item.raw_summary,
                "topics": list(item.topics),
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    sources = Path(__file__).parent.parent / "config" / "sources.yaml"
    items = fetch_all(sources)
    print(_items_to_json(items))
