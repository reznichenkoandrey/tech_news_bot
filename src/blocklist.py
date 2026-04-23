"""
Domain blocklist: drop items whose URL originates from Russian sources.

Intentionally source-level only. Items from Western outlets (HN, TechCrunch,
Reddit, etc) that happen to mention Russian companies are kept — covering
them is part of legitimate Western reporting.

Matching is hostname-based, not substring: a URL is blocked iff its parsed
hostname equals one of the listed domains OR is a subdomain of it. That
avoids false positives like `smart.company` accidentally hitting `rt.com`.
"""

from __future__ import annotations

from urllib.parse import urlparse


# Registered / state-linked Russian domains. Match is suffix-based, so
# `news.rt.com` and `rt.com` both trigger on "rt.com".
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
        "ixbt.com",
        "4pda.to",
        "4pda.ru",
        "dtf.ru",
        "ferra.ru",
        "tadviser.ru",
        "comnews.ru",
        # Russian search / mail / social
        "yandex.com",
        "yandex.ru",
        "ya.ru",
        "vk.com",
        "vk.ru",
        "mail.ru",
        "ok.ru",
        "rutube.ru",
        "my.com",
        # Russian corporate / state-linked
        "kaspersky.com",
        "kaspersky.ru",
        "sberbank.com",
        "sberbank.ru",
        "sber.ru",
        "sberdevices.ru",
        "sbercloud.ru",
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


def _extract_host(url: str) -> str:
    """Return the lowercased hostname of `url`, or "" if not parseable."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def is_blocked(url: str) -> tuple[bool, str]:
    """
    Return (blocked, reason). `reason` is a short tag like 'domain:yandex.ru'
    suitable for a log line. The match is hostname-suffix: `news.rt.com`
    matches `rt.com`, but `smart.company` does NOT match `rt.com`.
    """
    host = _extract_host(url)
    if not host:
        return False, ""
    for domain in BLOCKED_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return True, f"domain:{domain}"
    return False, ""
