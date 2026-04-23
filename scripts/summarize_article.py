"""
CLI: summarize a single article URL into a structured Ukrainian markdown
blob (TL;DR → Ключові тези → Deep dive), cache it on disk.

Uses the same Claude Max OAuth path as scripts/digest_pipeline so it stays
inside the Max subscription (no API billing).

Usage:
    python3 -m scripts.summarize_article <url> [--force]

The output is written to stdout and cached at
`data/summaries/<hash>.md`. Re-running with the same URL reads from cache
unless --force is passed. Network / paywall failures exit 1.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path

# Reuse the pipeline's LLM helpers (same OAuth headers, same Claude-Code system
# prompt).
from scripts.digest_pipeline import CLAUDE_CODE_SYSTEM, call_llm, env_required
from src.reader import Article, fetch_article

logger = logging.getLogger("summarize_article")

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "summaries"

# Max chars of article body fed to the LLM. 16k keeps us well inside the
# context budget for Sonnet 4.5 while covering virtually every long-form post.
MAX_BODY_CHARS = 16000


def cache_key(url: str) -> str:
    """Short, filesystem-safe deterministic key for a URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def cache_path(url: str) -> Path:
    return CACHE_DIR / f"{cache_key(url)}.md"


def build_prompt(article: Article) -> str:
    body = article.text
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n…[truncated]"

    meta_lines = [f"URL: {article.url}"]
    if article.title:
        meta_lines.append(f"Title: {article.title}")
    if article.author:
        meta_lines.append(f"Author: {article.author}")
    if article.date:
        meta_lines.append(f"Date: {article.date}")
    meta_block = "\n".join(meta_lines)

    return f"""Підсумуй статтю українською у форматі:

## TL;DR
1-2 речення, що це взагалі.

## Ключові тези
3-5 bullet-ів, найважливіші факти/ідеї.

## Deep dive
200-400 слів: контекст, чому це має значення, нюанси, можливі практичні висновки.

Правила:
- Мова — українська. Англійську термінологію залиш (LLM, inference, fine-tuning тощо).
- Без емодзі, без води, без "стаття каже".
- Факти зі статті. Якщо щось не ясно — не вигадуй.
- Не вставляй посилання — вони вже в кеші разом з URL.

{meta_block}

ТЕКСТ СТАТТІ:
{body}
"""


def render_markdown(article: Article, summary_body: str) -> str:
    """Assemble the cached artefact: YAML-like front matter + LLM output."""
    fm_lines = ["---", f"url: {article.url}"]
    if article.title:
        fm_lines.append(f"title: {_yaml_escape(article.title)}")
    if article.author:
        fm_lines.append(f"author: {_yaml_escape(article.author)}")
    if article.date:
        fm_lines.append(f"date: {_yaml_escape(article.date)}")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n" + summary_body.strip() + "\n"


def _yaml_escape(value: str) -> str:
    # Quote if the value contains characters that would make it ambiguous YAML.
    if any(ch in value for ch in (":", "#", "\"", "'", "\n")):
        escaped = value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ")
        return f'"{escaped}"'
    return value


def summarize(url: str, *, force: bool = False) -> tuple[str, Path]:
    """
    Produce the markdown summary for `url` and return (markdown, cache_path).

    Reads from cache unless `force=True`. Raises RuntimeError on fetch/paywall
    failure so the CLI can surface a clear exit code.
    """
    cp = cache_path(url)
    if cp.exists() and not force:
        logger.info("cache hit: %s", cp.name)
        return cp.read_text(encoding="utf-8"), cp

    article = fetch_article(url)
    if article is None:
        raise RuntimeError(f"Could not download {url}")
    if article.is_empty:
        raise RuntimeError(
            f"Extracted body too short ({len(article.text)} chars) — "
            "likely a paywall or JS-only page"
        )

    oauth = env_required("CLAUDE_CODE_OAUTH_TOKEN")
    summary_body = call_llm(build_prompt(article), oauth)

    md = render_markdown(article, summary_body)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp.write_text(md, encoding="utf-8")
    return md, cp


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Summarize a single article URL")
    parser.add_argument("url", help="Article URL")
    parser.add_argument("--force", action="store_true", help="Bypass cache")
    args = parser.parse_args(argv)

    try:
        md, cp = summarize(args.url, force=args.force)
    except RuntimeError as exc:
        print(f"⚠️  {exc}", file=sys.stderr)
        return 1

    print(md)
    logger.info("cached at %s", cp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
