from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class FeedItem:
    url: str
    title: str
    published: datetime  # UTC, timezone-aware
    source: str
    category: str
    raw_summary: str = ""


@dataclass
class DigestEntry:
    item: FeedItem
    importance: int  # 1-5, higher = more important
    summary_uk: str  # Ukrainian summary, max 2 sentences
