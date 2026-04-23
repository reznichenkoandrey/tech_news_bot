"""
Readable-text extractor backed by trafilatura.

Handles the HTTP fetch and the boilerplate removal in one call. Returns a
light `Article` dataclass or None on network failure. Paywalled / JS-only
pages usually come back with near-empty text — check `Article.is_empty`
before feeding it to an LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Below this char count we treat extraction as failed (paywall, JS-only page,
# or a page that trafilatura couldn't parse). The LLM prompt wouldn't have
# enough signal anyway.
MIN_USEFUL_CHARS = 500


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    text: str
    author: str = ""
    date: str = ""  # ISO 8601 if trafilatura could parse it

    @property
    def is_empty(self) -> bool:
        return len(self.text.strip()) < MIN_USEFUL_CHARS


def fetch_article(url: str, timeout: int = 20) -> Article | None:
    """
    Download and extract readable text. Returns None on network failure or
    when trafilatura couldn't get any HTML back.
    """
    import trafilatura  # imported lazily so tests can monkeypatch

    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=False)
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None

    if not downloaded:
        logger.warning("Empty response for %s", url)
        return None

    text = trafilatura.extract(
        downloaded,
        favor_recall=True,
        include_comments=False,
        include_tables=False,
    ) or ""

    metadata = trafilatura.extract_metadata(downloaded)
    title = (getattr(metadata, "title", None) or "").strip()
    author = (getattr(metadata, "author", None) or "").strip()
    date = (getattr(metadata, "date", None) or "").strip()

    return Article(url=url, title=title, text=text.strip(), author=author, date=date)
