# PLAN: tech_news_bot — Daily AI/Tech Digest to Telegram
**Date:** 2026-04-20
**Status:** Draft

---

## Goal

Щоденний автоматичний дайджест AI/tech новин у Telegram-канал українською мовою. Claude Code (підписка Claude Max) є "двигуном": сам тягне RSS/Atom фіди через WebFetch, саммарізує контекстом моделі, шле результат через Telegram Bot API. Жодних платних LLM API — лише підписка Claude Max.

---

## Stack

| Шар | Технологія |
|---|---|
| Orchestrator | Claude Code slash command `.claude/commands/tech-digest.md` |
| Fetcher | Python 3.12 stdlib (`urllib`, `xml.etree`) + `feedparser` (опційно) |
| State store | `data/seen.json` (JSON файл, git-committed) |
| Telegram API | `curl` або `requests` (HTTP POST) |
| Scheduler | Claude Code `schedule` skill (remote cron agents) |
| Config | `config/sources.yaml` (PyYAML) |
| Secrets | `.env` (gitignored) + `.env.example` |

---

## File Structure

```
tech_news_bot/
├── .claude/
│   └── commands/
│       └── tech-digest.md          # Main slash command orchestrator
├── config/
│   └── sources.yaml                # RSS/Atom/GitHub release feeds
├── data/
│   └── seen.json                   # Dedup store (git-committed, empty on init)
├── src/
│   ├── fetcher.py                  # RSS/Atom parser, returns list[FeedItem]
│   ├── dedup.py                    # seen.json read/write helpers
│   ├── telegram.py                 # Telegram sendMessage (HTML, chunk split)
│   └── models.py                   # Dataclasses: FeedItem, DigestEntry
├── scripts/
│   └── run.sh                      # Manual one-shot run wrapper (bash)
├── tests/
│   ├── test_fetcher.py
│   ├── test_dedup.py
│   └── test_telegram.py
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── PLAN.md
```

---

## Architecture

```
Claude Code (/tech-digest)
        │
        ├── Read config/sources.yaml
        │
        ├── src/fetcher.py ──► WebFetch (parallel RSS/Atom/GH releases)
        │        │
        │        └── list[FeedItem] (title, url, published, source, summary)
        │
        ├── src/dedup.py ──► Filter: not in data/seen.json AND published < 24h ago
        │
        ├── Claude context ──► Summarize + rank top-N items in Ukrainian
        │        │              (prompt engineering in slash command)
        │        └── Markdown digest string
        │
        ├── src/telegram.py ──► POST /sendMessage (HTML parse_mode, chunked)
        │
        ├── src/dedup.py ──► Update seen.json with new URLs
        │
        └── git commit "chore: update seen.json YYYY-MM-DD"
```

---

## Data Model

```python
# src/models.py

@dataclass
class FeedItem:
    url: str
    title: str
    published: datetime      # UTC
    source: str              # e.g. "Anthropic Blog"
    raw_summary: str         # first 500 chars from feed

@dataclass
class DigestEntry:
    item: FeedItem
    importance: int          # 1–5, assigned by Claude during summarization
    summary_uk: str          # Ukrainian summary, max 2 sentences
```

---

## Implementation Phases

### Phase 1: Project Setup (3 files)
- [ ] `.gitignore` — Python, .env, data/seen.json excluded from reset, __pycache__
- [ ] `.env.example` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- [ ] `requirements.txt` — `PyYAML>=6.0`, `requests>=2.31`, `feedparser>=6.0` (feedparser — fallback for broken XML)

### Phase 2: Sources Config (1 file)
- [ ] `config/sources.yaml` — повний список фідів:
  - Anthropic: `https://www.anthropic.com/news/rss.xml`
  - OpenAI Blog: `https://openai.com/blog/rss.xml`
  - Google DeepMind: `https://deepmind.google/blog/rss/`
  - HuggingFace: `https://huggingface.co/blog/feed.xml`
  - Meta AI: `https://ai.meta.com/blog/feed/`
  - HackerNews AI filter: `https://hnrss.org/frontpage?q=AI+LLM+Claude+GPT&points=50`
  - Reddit r/LocalLLaMA: `https://www.reddit.com/r/LocalLLaMA/.rss`
  - Reddit r/MachineLearning: `https://www.reddit.com/r/MachineLearning/.rss`
  - TechCrunch AI: `https://techcrunch.com/category/artificial-intelligence/feed/`
  - The Verge AI: `https://www.theverge.com/ai-artificial-intelligence/rss/index.xml`
  - Ars Technica AI: `https://feeds.arstechnica.com/arstechnica/technology-lab`
  - GitHub releases (via Atom):
    - `https://github.com/anthropics/anthropic-sdk-python/releases.atom`
    - `https://github.com/openai/openai-python/releases.atom`
    - `https://github.com/ollama/ollama/releases.atom`
    - `https://github.com/huggingface/transformers/releases.atom`
    - `https://github.com/vllm-project/vllm/releases.atom`
    - `https://github.com/ggml-org/llama.cpp/releases.atom`

### Phase 3: Core Python Modules (4 files)
- [ ] `src/models.py` — `FeedItem`, `DigestEntry` dataclasses з type hints
- [ ] `src/fetcher.py` — функція `fetch_all_feeds(sources_path: Path) -> list[FeedItem]`:
  - Читає `sources.yaml`
  - Для кожного URL: `urllib.request.urlopen` з timeout=10s
  - XML парсинг через `xml.etree.ElementTree` (RSS 2.0 + Atom 1.0)
  - Fallback на `feedparser` якщо etree fails
  - Повертає `FeedItem` з `published` в UTC, обрізає `raw_summary` до 500 chars
  - User-Agent header щоб не блокували
- [ ] `src/dedup.py` — функції:
  - `load_seen(path: Path) -> set[str]` — читає seen.json
  - `filter_new(items: list[FeedItem], seen: set[str], max_age_hours: int = 24) -> list[FeedItem]`
  - `save_seen(path: Path, seen: set[str], new_urls: list[str]) -> None` — атомарний запис
- [ ] `src/telegram.py` — функція `send_digest(text: str, token: str, chat_id: str) -> None`:
  - Chunks: розбиває текст на частини по 4000 chars (не 4096 — буфер для HTML тегів)
  - POST `/sendMessage` з `parse_mode=HTML`
  - Retry 3 рази з exp backoff на 429/5xx
  - Raises `TelegramError` з деталями при failure

### Phase 4: Slash Command Orchestrator (1 file)
- [ ] `.claude/commands/tech-digest.md` — повний промпт-сценарій для Claude Code:
  ```
  Структура файлу:
  1. Мета та контекст (що робить команда)
  2. Step-by-step інструкції для Claude:
     a. Load .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
     b. Run: python src/fetcher.py → отримати JSON список items
     c. Run: python src/dedup.py filter → отримати нові items
     d. Якщо items == 0: надіслати "Нових новин за 24г немає" і завершити
     e. Summarize: для кожного item — 1-2 речення українською + importance 1-5
     f. Sort by importance desc, взяти top-15
     g. Compose digest markdown (заголовок, дата, пронумерований список)
     h. Run: python src/telegram.py send → надіслати
     i. Run: python src/dedup.py update → оновити seen.json
     j. git add data/seen.json && git commit -m "chore: digest YYYY-MM-DD"
  3. Формат дайджесту (шаблон)
  4. Правила важливості (importance scoring criteria)
  ```

  Формат digest:
  ```
  <b>AI/Tech дайджест — {date}</b>

  {N} новин за останні 24 години

  <b>1. {title}</b> [{source}]
  {summary_uk}
  <a href="{url}">Читати</a>

  ...
  ```

### Phase 5: Runner Script (1 file)
- [ ] `scripts/run.sh` — bash-обгортка для ручного запуску:
  ```bash
  #!/usr/bin/env bash
  # Manual trigger for tech-digest slash command
  set -euo pipefail
  cd "$(dirname "$0")/.."
  source .env
  claude --slash tech-digest
  ```

### Phase 6: Tests (3 files)
- [ ] `tests/test_fetcher.py` — mock urllib, перевірка парсингу RSS 2.0 і Atom 1.0
- [ ] `tests/test_dedup.py` — перевірка filter_new (вікно 24h, дублікати, атомарний запис)
- [ ] `tests/test_telegram.py` — mock requests.post, перевірка chunking і retry logic

### Phase 7: README (1 file)
- [ ] `README.md` — покроково:
  1. Передумови (Python 3.12+, Claude Code, Telegram акаунт)
  2. Створення бота через @BotFather (`/newbot`)
  3. Отримання `TELEGRAM_CHAT_ID` через @userinfobot або `getUpdates`
  4. Клонування репо + `pip install -r requirements.txt`
  5. Копіювання `.env.example` → `.env`, заповнення токенів
  6. Ручний тест: `bash scripts/run.sh` або `/tech-digest` у Claude Code
  7. Активація schedule: інструкція з `claude schedule` або `claude /schedule`
  8. Troubleshooting (бот не відповідає, дублікати, GitHub rate limit)

### Phase 8: End-to-End Manual Test
- [ ] Запустити `/tech-digest` вручну в Claude Code
- [ ] Перевірити seen.json оновився
- [ ] Перевірити повідомлення в Telegram
- [ ] Перевірити git commit з'явився
- [ ] Запустити вдруге — переконатись що дублікатів немає

---

## Agents Needed

| Агент | Фази | Що робить |
|---|---|---|
| DEV | 1, 2, 3, 5, 6 | Python модулі, конфіги, тести, run.sh |
| AI/ML | 4 | Slash command промпт, importance scoring, digest format |
| SCRIBE | 7 | README покроково |
| QA | 8 | End-to-end test, edge cases |

---

## Risks & Decisions

| Рішення | Варіанти | Вибір | Причина |
|---|---|---|---|
| RSS парсер | `feedparser` vs `xml.etree` | `xml.etree` primary + `feedparser` fallback | stdlib = нуль залежностей для основного шляху; feedparser для broken feeds |
| State store | SQLite vs JSON | `seen.json` | Простота, git-trackable, достатньо для ~1000 URLs/день |
| Telegram sender | `curl` bash vs `requests` Python | `requests` Python | Кращий retry/error handling, вже в requirements |
| Scheduler | cron macOS vs Claude schedule skill | Claude schedule skill | Не потребує окремого процесу, вбудовано в Claude Code workflow |
| LLM | Anthropic API vs Claude Code context | Claude Code context (Claude Max) | Безкоштовно для користувача, ключова вимога |
| GitHub releases | RSS API vs GitHub REST API | GitHub Atom releases feed | Без токена, без rate limit issues для публічних репо |
| Reddit | API vs old.reddit RSS | old.reddit.com .rss | Без OAuth токена |

---

## Out of Scope

- Веб-інтерфейс або dashboard — непотрібно, Telegram достатньо
- Docker контейнер — немає сенсу, Claude Code запускається локально
- База даних (PostgreSQL/MySQL) — overkill для seen.json
- Антропік API / OpenAI API — принципово не використовуємо (платне)
- Multi-channel (кілька Telegram чатів) — фаза 1 = один чат
- Image/media в повідомленнях — text-only digest
- Переклад новин з інших мов — Claude саммарізує напряму українською
- Web scraping (non-RSS джерела) — лише RSS/Atom фіди

---

## Acceptance Criteria

- [ ] `/tech-digest` запускається в Claude Code без помилок
- [ ] Дайджест містить 5–15 новин за останні 24 години
- [ ] Всі новини написані українською мовою
- [ ] Telegram отримує повідомлення з HTML форматуванням і клікабельними посиланнями
- [ ] Повторний запуск протягом 24h не надсилає дублікатів
- [ ] seen.json оновлюється і commit з'являється в git
- [ ] Якщо новин немає — надсилається коротке повідомлення "Нових новин немає"
- [ ] Telegram message split працює коректно якщо digest > 4000 chars
- [ ] Всі тести проходять: `pytest tests/`

---

## Progress

| Фаза | Файли | Статус |
|---|---|---|
| Phase 1: Setup | `.gitignore`, `.env.example`, `requirements.txt` | [ ] Not started |
| Phase 2: Sources | `config/sources.yaml` | [ ] Not started |
| Phase 3: Core modules | `src/models.py`, `src/fetcher.py`, `src/dedup.py`, `src/telegram.py` | [ ] Not started |
| Phase 4: Slash command | `.claude/commands/tech-digest.md` | [ ] Not started |
| Phase 5: Runner | `scripts/run.sh` | [ ] Not started |
| Phase 6: Tests | `tests/test_*.py` (3 files) | [ ] Not started |
| Phase 7: README | `README.md` | [ ] Not started |
| Phase 8: E2E test | Manual run validation | [ ] Not started |
