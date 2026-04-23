# tech_news_bot

Щоденний AI/tech/design дайджест у приватний Telegram-чат. Без платних LLM API — саммаризація йде через підписку Claude Max (OAuth).

## Архітектура

```
┌─────────────────────────────────────────────────────────────────────┐
│                    GitHub Actions (schedule)                        │
│  digest.yml        — щодня 10:00 Kyiv   (DST-safe двома cron)       │
│  weekly.yml        — неділя 09:00 Kyiv  (deep-reads з reading_list) │
│  summarize.yml     — on-demand (repository_dispatch від Worker'а)   │
└─────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
     ┌──────────────────────────────────────────────────────┐
     │  scripts/digest_pipeline.py                          │
     │  ─ src.fetcher     → RSS/Atom (parallel, 40+ feeds)  │
     │  ─ src.blocklist   → RU-source filter (hostname)     │
     │  ─ src.dedup       → seen.json + 24h window          │
     │  ─ topic filter    → topics.yaml × user_prefs.yaml   │
     │  ─ Claude Sonnet   → рейтинг + UA-саммарі (per item) │
     │  ─ src.telegram    → sendMessage(reply_markup=...)   │
     │                      [header] + N item messages      │
     │                      each with 📖 ⭐ 🗑 buttons        │
     └──────────────────────────────────────────────────────┘
                                 │
                                 ▼
                          Telegram (DM)
                                 │
  ┌──────────────────────────────┴─────────────────────────┐
  │   inline buttons     /команди                          │
  │         │                 │                            │
  │         ▼                 ▼                            │
  │   Cloudflare Worker (tech-news-bot.scr1be.workers.dev) │
  │   ─ /help /topics /add /remove /reset /sources ...     │
  │   ─ callback_query:                                    │
  │       save  → PUT data/reading_list.json (GitHub API)  │
  │       hide  → PUT data/seen.json + deleteMessage       │
  │       expand→ repository_dispatch summarize.yml →      │
  │               scripts.post_article_summary → reply      │
  └────────────────────────────────────────────────────────┘
```

## Можливості

- **Topic-based дайджести**: `config/digests.yaml` дозволяє кілька профілів (AI/Tech, Design) з власними темами; `config/user_prefs.yaml` звужує до активних topics через /команди
- **Inline buttons** на кожній новині: 📖 розширене саммарі, ⭐ у reading list, 🗑 сховати
- **Weekly deep-reads** з reading_list щонеділі 09:00 Kyiv (TL;DR per item)
- **Cloudflare Worker webhook** — /команди і кнопки відповідають миттєво, без 5-хвилинного polling'у
- **Failsafe**: heartbeat message + error-to-Telegram notify на кожний GHA job
- **RU source blocklist** на рівні hostname (не substring)

## Передумови

- macOS / Linux, Python 3.12+
- Telegram акаунт
- Claude Max OAuth token (для LLM-саммарі) — [див. нижче](#llm-claude-max-oauth)
- (опційно) Cloudflare акаунт + GitHub PAT — якщо хочеш webhook для миттєвих реакцій на кнопки/команди. Без Worker'а /команди працюють через `bot_poll.py` fallback workflow.

## Швидкий старт

### 1. Telegram-бот
У `@BotFather`: `/newbot`, скопіюй TOKEN. Напиши `/start` своєму боту, потім візьми `chat.id` з `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 2. Репо + secrets
```bash
git clone https://github.com/reznichenkoandrey/tech_news_bot.git
cd tech_news_bot
```

Додай GitHub secrets у репо (Settings → Secrets and variables → Actions):
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CLAUDE_CODE_OAUTH_TOKEN` ([як отримати](#llm-claude-max-oauth))

### 3. Ручний тест локально
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -q          # 172 passed
```

Для dry-run дайджесту локально:
```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... CLAUDE_CODE_OAUTH_TOKEN=...
export DIGEST_WINDOW_HOURS=24 DIGEST_MAX_ITEMS=5
python3 -m scripts.digest_pipeline
```

### 4. Перший GHA запуск
Зайди в Actions → "Daily tech digest" → Run workflow. Через 30-60с прийде дайджест у Telegram.

### 5. (опційно) Cloudflare Worker
```bash
cd cloudflare-worker
npm install
npx wrangler login
# постав три secrets:
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET      # будь-яка довга строка
npx wrangler secret put GITHUB_TOKEN                 # fine-grained PAT
# TELEGRAM_CHAT_ID + GITHUB_REPO — через wrangler.toml [vars]
npx wrangler deploy
# зареєструй webhook у Telegram:
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://tech-news-bot.<worker-subdomain>.workers.dev" \
  -d "secret_token=<той самий WEBHOOK_SECRET>"
```

`GITHUB_TOKEN` має мати permissions: Contents (write), Actions (write) — перший для callback'ів save/hide, другий для `repository_dispatch` у експанд-flow.

## Bot commands

Команди працюють у твоєму DM з ботом. ACL: тільки `TELEGRAM_CHAT_ID` може слати команди; інші дропаються тихо.

| Команда | Що робить |
|---|---|
| `/help` або `/start` | Список команд |
| `/topics` | Усі topics з ✅/◻️ маркером активного фільтра |
| `/add <slug>` | Додати topic у фільтр (`/add design`) |
| `/remove <slug>` | Прибрати topic з фільтра |
| `/reset` | Очистити фільтр (дайджести повертаються до дефолтів у `digests.yaml`) |
| `/sources` | Скільки feeds у кожному topic |
| `/sources <slug>` | Перелік feeds у конкретному topic |
| `/digests` | Перелік дайджест-профілів з маркером, чи перетинаються з фільтром |
| `/status` | Розклад + Worker/polling mode + активний фільтр |

Активний фільтр зберігається у `config/user_prefs.yaml` і читається `digest_pipeline.py` на кожному run'і. При встановленому Worker'і зміни комітяться в репо миттєво (через GitHub Contents API); без Worker'а — `bot_poll.py` polling workflow коміт їх раз на 5 хв.

## Scripts cheatsheet

| Скрипт | Коли запускати | Що робить |
|---|---|---|
| `python3 -m scripts.digest_pipeline` | GHA `digest.yml` щодня 10:00 | End-to-end дайджест (fetch → dedup → LLM → send per item with buttons) |
| `python3 -m scripts.weekly_digest` | GHA `weekly.yml` неділя 09:00 | Deep-reads з `reading_list.json`: title+TL;DR per saved item, архів у `reading_archive.json` |
| `python3 -m scripts.summarize_article <url> [--force]` | CLI / імпортом | Fetch + LLM long-form саммарі одного URL. Кеш: `data/summaries/<hash>.md`. `--force` ігнорує кеш. |
| `python3 -m scripts.post_article_summary <url> <chat_id> <message_id>` | GHA `summarize.yml` (trigger від Worker'а) | Викликає `summarize_article` і постить результат як reply до item'а + знімає кнопки |
| `python3 -m scripts.bot_poll` | GHA `bot.yml` (fallback, вимкнено коли Worker активний) | Polls /getUpdates, виконує ті самі /команди що й Worker |
| `python3 -m src.fetcher` | CLI | JSON усіх items з `config/sources.yaml` (stdout) |
| `python3 -m src.dedup filter` | CLI | stdin-based filter: items JSON → "new only" JSON |

## Sources & topics

### Як додати новий feed

У `config/sources.yaml`:
```yaml
feeds:
  - name: "Source Name"
    url: "https://example.com/rss.xml"
    category: "lab"                # lab | community | media | release
    topics: [ai-lab, ai-tools]     # один+ slug з topics.yaml
```

Перевір що feed валідний:
```bash
curl -sI https://example.com/rss.xml | grep -i content-type   # має бути application/(rss|atom|xml)
python3 -c "from src.fetcher import _fetch_feed; print(len(_fetch_feed({'name':'X','url':'<URL>','category':'media','topics':[]})))"
```

### Як додати новий topic

1. Додай запис у `config/topics.yaml`:
   ```yaml
   - slug: devops
     name: "DevOps / SRE"
     emoji: "🛠️"
     default_active: true
     description: "SRE, observability, K8s, CI/CD"
   ```
2. Проставий slug у `sources.yaml` для релевантних feeds.
3. (опційно) Додай digest-профіль у `config/digests.yaml`, якщо хочеш окремий дайджест для topic'а.
4. `pytest tests/test_topics.py` — guard, що всі feeds посилаються на існуючі slugs.

Доступні зараз: `ai-lab`, `ai-tools`, `ai-local`, `ai-infra`, `ai-research`, `community`, `media`, `design`, `design-tools`, `ai-design`, `frontend`. Опис кожного — у `topics.yaml`.

### Digest-профілі

`config/digests.yaml` описує ЯКІ topics збираються разом в один дайджест:
```yaml
digests:
  - name: "AI/Tech"
    emoji: "🤖"
    topics: [ai-lab, ai-tools, ai-local, ai-infra, ai-research, community, media]
  - name: "Design"
    emoji: "🎨"
    topics: [design, design-tools, ai-design, frontend]
```

`digest_pipeline.py` запускає кожен профіль окремо (окремий LLM-call + окреме повідомлення з префіксом). `user_prefs.active_topics` звужує кожен профіль до перетину; якщо перетин порожній — профіль пропускається.

## Source policy

**Принцип:** блокування на рівні джерела, не змісту. Жоден item із домену, зареєстрованого/працюючого в РФ чи прив'язаного до російських держструктур/санкційних компаній, не потрапляє в дайджест (`src/blocklist.py` → `is_blocked()` → фільтр у `src/fetcher.py::fetch_all`). Західні outlets (TechCrunch, Ars Technica, Reddit, HN), які пишуть про Yandex/Sberbank/Kaspersky, **залишаються** — це нормальне західне покриття.

**Матчинг:** hostname-suffix (`news.rt.com` → блок по `rt.com`; `smart.company` → НЕ блокується). Case-insensitive. Повний список — у `src/blocklist.py::BLOCKED_DOMAINS`.

**Додати джерело у блок:** lowercase-домен у `BLOCKED_DOMAINS` → `pytest tests/test_blocklist.py` → commit з `security:` або `feat(blocklist):` префіксом.

**Аудит, що нічого не проскакує:**
```bash
python3 -c "
import json
from urllib.parse import urlparse
seen = json.loads(open('data/seen.json').read())
for url in seen:
    h = (urlparse(url).hostname or '').lower()
    if h.endswith('.ru'):
        print('LEAK:', url)
"
```

## Inline buttons

Кожна новина у дайджесті — окреме повідомлення з трьома кнопками:

| Кнопка | Flow |
|---|---|
| 📖 **Deep** | Worker → `answerCallbackQuery("⏳ готую саммарі…")` → `repository_dispatch` `summarize-article` → `scripts/post_article_summary.py` → Telegram reply з TL;DR + Ключові тези + Deep dive + знімає кнопки з оригіналу |
| ⭐ **Save** | Worker → GitHub Contents API PUT `data/reading_list.json` → `answerCallbackQuery("⭐ збережено")` |
| 🗑 **Hide** | Worker → PUT `data/seen.json` + Telegram `deleteMessage` → `answerCallbackQuery("🗑 сховано")` |

Telegram `callback_data` має ліміт 64 байти, тому URL замінюється sha256[:16] хешем. Мапа хеш→URL лежить у `data/callback_map.json` (FIFO cap 1000, комітиться `digest.yml`). Worker читає її через GitHub raw.

## Weekly deep reads (неділя 09:00 Kyiv)

Все що ти ⭐ Save-нув протягом тижня, `.github/workflows/weekly.yml` збирає в один дайджест "📚 Deep reads тижня": title + TL;DR + лінк для кожного item. Реалізація — `scripts/weekly_digest.py`. Після успішного send `data/reading_list.json` очищається, а URL'и переносяться в `data/reading_archive.json` (з `archived_at`). Якщо reading list порожній — workflow тихо виходить. Якщо весь тиждень — paywall'и і жоден item не вдалось підсумувати — workflow шле помилку в чат і лишає reading list недоторканим для ручного розгрібання.

## Design tools RSS (станом на 2026-04-23)

| Бренд | Feed | Стан |
|---|---|---|
| Linear | `https://linear.app/rss/blog.xml` | ✅ first-party, 50 items, активний |
| Linear | `https://linear.app/rss/changelog.xml` | ✅ first-party, 234 items, активний |
| Figma | — | ❌ no public RSS; `/blog/` — SPA без `<link rel="alternate">`. Medium `figma-design` мертвий з 2018. |
| Framer | — | ❌ `/updates` — Framer-hosted SPA, нічого з feed-side не віддає. |
| Adobe Design | `blog.adobe.com/feed.xml` | ⚠️ існує, але заморожений у 2022-07. Поточний AEM-блог не серв'ує RSS. |
| UX Tools | — | ❌ Framer-hosted як і Framer'івський блог. |

Як Linear feeds знайшлися: `curl ... | grep 'rel="alternate"'` на `https://linear.app/now`. Для решти — перепробувано `/feed.xml` / `/rss.xml` / `/atom.xml` на кореневих і `/blog` шляхах, RSSHub.app (403 Cloudflare), feeds.pub (404), Adobe subsites, форуми. Sitemap.xml знайдений на всіх — sitemap-based derived feeds можливі, але це окремий follow-up.

## LLM: Claude Max OAuth

`digest_pipeline.py` та `summarize_article.py` б'ють Anthropic `/v1/messages` напряму з bearer'ом Claude Max підписки. Ключові нюанси:

- Header `anthropic-beta: oauth-2025-04-20` + `Authorization: Bearer <OAUTH>` (НЕ `x-api-key`)
- `system` повинен починатися з `"You are Claude Code, Anthropic's official CLI for Claude."` — інакше 401
- User-Agent `claude-cli/...`

Token — з `/login` у Claude Code CLI, grep `access_token` у конфізі (`~/Library/Application Support/claude-cli/...` на macOS).

## Project structure

```
tech_news_bot/
├── .claude/commands/tech-digest.md    # (legacy) manual slash command
├── .github/workflows/
│   ├── digest.yml                     # daily 10:00 Kyiv
│   ├── weekly.yml                     # Sunday 09:00 Kyiv
│   ├── summarize.yml                  # on-demand from Worker
│   └── bot.yml                        # polling fallback (disabled)
├── cloudflare-worker/
│   ├── src/index.ts                   # webhook: /commands + callback_query
│   ├── wrangler.toml
│   └── package.json
├── config/
│   ├── sources.yaml                   # 40+ RSS/Atom feeds
│   ├── topics.yaml                    # topic registry
│   ├── digests.yaml                   # digest profiles (AI/Tech, Design, …)
│   └── user_prefs.yaml                # active_topics, writable via bot
├── data/
│   ├── seen.json                      # dedup (5000-cap FIFO)
│   ├── callback_map.json              # hash → URL (1000-cap FIFO)
│   ├── reading_list.json              # ⭐ Save destination
│   ├── reading_archive.json           # weekly_digest archive
│   ├── bot_state.json                 # bot_poll last_update_id
│   └── summaries/<hash>.md            # cached long-form summaries
├── src/
│   ├── models.py                      # FeedItem, DigestEntry
│   ├── fetcher.py                     # parallel RSS/Atom parser
│   ├── dedup.py                       # seen.json + age filter
│   ├── blocklist.py                   # RU-source hostname blocklist
│   ├── reader.py                      # article fetch + HTML → text
│   ├── telegram.py                    # sendMessage/editMessage/send_reply
│   └── callback_map.py                # hash ↔ URL map IO
├── scripts/
│   ├── digest_pipeline.py             # daily orchestrator
│   ├── summarize_article.py           # on-demand summary CLI
│   ├── post_article_summary.py        # GHA entrypoint for 📖 Deep
│   ├── weekly_digest.py               # Sunday deep-reads
│   ├── bot_poll.py                    # polling fallback
│   └── run.sh                         # env validator
├── tests/                             # pytest (172 passed)
└── README.md
```

## Troubleshooting

**Команди / кнопки не відповідають**
- `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"` — перевір що webhook зареєстрований
- Логи Worker'а: `npx wrangler tail`
- Fallback: `Actions → Telegram bot poll (fallback) → Run workflow`

**Дубльовані новини**
- Перевір `data/seen.json` (має містити URL). Скинути: `echo '[]' > data/seen.json && git commit -am "reset seen"`
- Або тимчасово знизь `DIGEST_WINDOW_HOURS`

**Всі feeds failed**
- GitHub Actions IP може бути заблокований для окремих CDN — перевір логи `digest.yml` на специфічні 403/timeout
- `curl -sI` локально і з runner'а покаже, чи справа в IP-fencing

**Digest приходить, але Telegram message cut off**
- `DIGEST_MAX_ITEMS=10` (зменшити) або перевір `src/telegram.py::_chunk_text` — chunks > 4000 символів має ламати по `\n\n`

**Claude API повертає 401**
- Бекет OAuth: `anthropic-beta: oauth-2025-04-20` відсутній
- `system:` не починається з обов'язкової префіксної фрази (див. `CLAUDE_CODE_SYSTEM` у `digest_pipeline.py`)

## Ліцензія

MIT
