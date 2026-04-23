# tech_news_bot — Cloudflare Worker webhook

Приймає webhook від Telegram і відповідає на slash-команди за <1 сек.
Стан бота живе в репо (`config/user_prefs.yaml`) — Worker читає через GitHub
raw і пише через Contents API. Якщо Worker впаде — polling-fallback
`.github/workflows/bot.yml` можна запустити вручну (`workflow_dispatch`).

## Одноразове розгортання

Потрібні:
- Cloudflare account (free tier достатньо: 100k req/day)
- Node 20+
- GitHub PAT (classic або fine-grained) з правом **Contents: write** на цей репо

### 1. Встановити залежності

```bash
cd cloudflare-worker
npm install
```

### 2. Залогінитись у Cloudflare

```bash
npx wrangler login
```

Відкриє браузер для OAuth.

### 3. Задати секрети

```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN       # з @BotFather
npx wrangler secret put TELEGRAM_CHAT_ID         # 34412475
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET  # згенеруй: openssl rand -hex 24
npx wrangler secret put GITHUB_TOKEN             # PAT з contents:write
npx wrangler secret put GITHUB_REPO              # reznichenkoandrey/tech_news_bot
```

Кожна команда запитає значення у stdin — вклей, натисни Enter.

### 4. Задеплоїти Worker

```bash
npx wrangler deploy
```

Виведе URL вигляду `https://tech-news-bot.<subdomain>.workers.dev`. Запам'ятай його.

### 5. Зареєструвати webhook у Telegram

```bash
export WORKER_URL="https://tech-news-bot.<subdomain>.workers.dev"
export BOT_TOKEN="<той що у TELEGRAM_BOT_TOKEN>"
export SECRET="<той що у TELEGRAM_WEBHOOK_SECRET>"

curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${WORKER_URL}" \
  -d "secret_token=${SECRET}" \
  -d 'allowed_updates=["message"]'
```

Має прийти `{"ok":true,"result":true,"description":"Webhook was set"}`.

Перевірка:
```bash
curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

Має показати `url` (Worker URL) і `pending_update_count: 0`.

### 6. Вимкнути GHA polling

Після деплою `webhook` і `getUpdates` конфліктують — Telegram надсилає updates
тільки одним зі способів. Polling вимкнено за замовчуванням у `.github/workflows/bot.yml`
(розкоментуй `schedule:` щоб повернути).

### 7. Тест

Напиши `/help` боту — має прийти відповідь за 1-2 секунди.

## Моніторинг

```bash
npx wrangler tail   # live logs
```

або Cloudflare dashboard → Workers → tech-news-bot → Logs.

## Оновлення коду

```bash
# правки у src/index.ts
npx wrangler deploy
```

## Знятие webhook (повернутися на polling)

```bash
curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook"
# далі — розкоментувати schedule у .github/workflows/bot.yml
```
