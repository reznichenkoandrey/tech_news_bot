"""
Domain blocklist: drop items whose URL originates from Russian sources.

Intentionally source-level only. Items from Western outlets (HN, TechCrunch,
Reddit, etc) that happen to mention Russian companies are kept — covering
them is part of legitimate Western reporting.
"""

from __future__ import annotations


# URL host / path substrings. Case-insensitive match against the full URL.
BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        # State / propaganda media
        "rt.com",
        "tass.ru",
        "tass.com",
        "ria.ru",
        "sputniknews.com",
        "kremlin.ru",
        "interfax.ru",
        "iz.ru",
        "kommersant.ru",
        "rg.ru",
        "rbc.ru",
        "lenta.ru",
        "gazeta.ru",
        "vedomosti.ru",
        "1tv.ru",
        "ntv.ru",
        # Russian tech platforms / publishers
        "habr.com",
        "habrahabr.ru",
        "vc.ru",
        "cnews.ru",
        "3dnews.ru",
        # Russian search / mail / social
        "yandex.com",
        "yandex.ru",
        "ya.ru",
        "vk.com",
        "vk.ru",
        "mail.ru",
        "ok.ru",
        "rutube.ru",
        # Russian corporate / state-linked
        "kaspersky.com",
        "kaspersky.ru",
        "sberbank.com",
        "sberbank.ru",
        "sber.ru",
        "sberdevices.ru",
        "rostelecom.ru",
        "mts.ru",
        "beeline.ru",
        "megafon.ru",
        "rostec.ru",
        "rosatom.ru",
        "gazprom.ru",
        "roscosmos.ru",
    }
)

def is_blocked(url: str) -> tuple[bool, str]:
    """
    Return (blocked, reason). `reason` is a short tag like 'domain:yandex.ru'
    suitable for a log line.
    """
    url_lower = (url or "").lower()
    for domain in BLOCKED_DOMAINS:
        if domain in url_lower:
            return True, f"domain:{domain}"
    return False, ""
