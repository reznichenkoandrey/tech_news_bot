# tech_news_bot

Щоденний дайджест AI/tech новин у Telegram. Без платних API — працює через підписку Claude Max та безкоштовний Telegram Bot API.

## Як це працює

1. **Claude Code** по розкладу (`/schedule`) запускає slash-команду `/tech-digest`
2. Python-модуль `src.fetcher` паралельно тягне 17 RSS/Atom фідів (Anthropic, OpenAI, DeepMind, HuggingFace, HN, Reddit, GitHub releases тощо)
3. `src.dedup` відфільтровує вже надіслані та старші за 12 годин
4. Claude саммарізує українською, оцінює важливість, формує дайджест
5. `src.telegram` шле повідомлення в твій приватний чат з ботом

## Передумови

- macOS / Linux
- Python 3.11+
- Claude Code CLI з активною підпискою Claude Max
- Telegram-акаунт

## Налаштування

### 1. Створи Telegram-бота

Відкрий `@BotFather` в Telegram:
```
/newbot
My AI Digest        ← назва (будь-яка)
my_ai_digest_bot    ← username (має закінчуватись на _bot)
```

BotFather віддасть TOKEN — це рядок типу `1234567890:AAA...`.

### 2. Отримай свій chat_id

Напиши `/start` своєму новому боту, потім відкрий у браузері:
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Знайди `"chat":{"id":123456789}` — це твій `TELEGRAM_CHAT_ID`.

### 3. Клонуй і налаштуй

```bash
git clone https://github.com/reznichenkoandrey/tech_news_bot.git
cd tech_news_bot
cp .env.example .env
```

Відкрий `.env` і встав TOKEN + CHAT_ID:
```
TELEGRAM_BOT_TOKEN=1234567890:AAA...
TELEGRAM_CHAT_ID=123456789
DIGEST_WINDOW_HOURS=12
DIGEST_MAX_ITEMS=15
```

### 4. Встанови залежності

```bash
python3 -m pip install -r requirements.txt --user
```

Або через venv:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Перевір тести

```bash
pytest tests/ -v
```

Має бути `50 passed`.

## Ручний запуск

Відкрий Claude Code в директорії проєкту:
```bash
cd tech_news_bot
claude
```

Потім всередині Claude:
```
/tech-digest
```

Через 30-60 секунд отримаєш повідомлення в Telegram.

## Автоматизація (schedule)

У Claude Code:
```
/schedule create "tech-digest-morning" "0 9 * * *" "Europe/Kyiv" "/tech-digest"
/schedule create "tech-digest-evening" "0 18 * * *" "Europe/Kyiv" "/tech-digest"
```

Переглянути активні:
```
/schedule list
```

Видалити:
```
/schedule delete tech-digest-morning
```

## Структура проєкту

```
tech_news_bot/
├── .claude/commands/tech-digest.md   # Orchestrator slash command
├── config/sources.yaml               # 17 RSS/Atom feeds
├── data/seen.json                    # Dedup state (git-committed)
├── src/
│   ├── models.py                     # FeedItem, DigestEntry dataclasses
│   ├── fetcher.py                    # Parallel RSS/Atom parser
│   ├── dedup.py                      # Dedup + age filter
│   └── telegram.py                   # sendMessage with retry + chunking
├── scripts/run.sh                    # Env validator
├── tests/                            # pytest (50 tests)
├── .env.example
├── requirements.txt
├── PLAN.md
└── README.md
```

## Troubleshooting

**Бот не відповідає:**
- Перевір що написав `/start` боту в Telegram
- Перевір що TOKEN скопійовано без пробілів
- Перевір через `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"`

**"Дубльовані новини":**
- Видали `data/seen.json` — наступний запуск буде зі свіжою історією
- Або відредагуй `DIGEST_WINDOW_HOURS` в `.env`

**"Всі фіди failed":**
- Перевір інтернет
- Деякі фіди можуть тимчасово бути недоступні — нормально, якщо failed < 50%
- Логи: `/tmp/tech_news_fetch.log`

**"Python 3.11+ required":**
```bash
brew install python@3.12
```

**Telegram 429 rate limit:**
- Бот робить 1 запит на запуск. 429 буває лише якщо ти руками запускаєш /tech-digest дуже часто — просто почекай.

## Додавання нових джерел

Відредагуй `config/sources.yaml`:
```yaml
feeds:
  - name: "New Source Name"
    url: "https://example.com/rss.xml"
    category: "lab"  # lab | community | media | release
```

Перевір що фід валідний:
```bash
curl -s https://example.com/rss.xml | head -20
```

## Ліцензія

MIT
