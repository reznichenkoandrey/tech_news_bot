---
description: Generate AI/tech news digest and send to Telegram
---

# /tech-digest — AI/Tech News Digest Orchestrator

Ти — автоматичний двигун дайджесту AI/tech новин. Твоя задача: зібрати свіжі новини з RSS-фідів, відфільтрувати нові, коротко саммарізувати українською та надіслати в Telegram.

Працюй **мовчки й автономно** — без уточнюючих питань. Всі кроки детерміновані, інструкції нижче.

---

## Крок 1 — Завантаж environment

Виконай bash:
```bash
cd /Users/reznichenkoandrii/htdocs/tech_news_bot
set -a && source .env && set +a
echo "CHAT_ID=$TELEGRAM_CHAT_ID WINDOW=$DIGEST_WINDOW_HOURS MAX=$DIGEST_MAX_ITEMS"
```

Якщо будь-яка змінна порожня — зупинись і виведи помилку `.env missing credentials`.

---

## Крок 2 — Зберіть всі items

```bash
cd /Users/reznichenkoandrii/htdocs/tech_news_bot
python3 -m src.fetcher > /tmp/tech_news_all.json 2> /tmp/tech_news_fetch.log
```

Перевір `/tmp/tech_news_fetch.log` — якщо всі feeds failed (0 items) — надішли в Telegram "Помилка: всі RSS-фіди недоступні" і зупинись.

---

## Крок 3 — Відфільтруй нові items

```bash
cd /Users/reznichenkoandrii/htdocs/tech_news_bot
cat /tmp/tech_news_all.json | python3 -m src.dedup filter > /tmp/tech_news_new.json
```

Прочитай `/tmp/tech_news_new.json`. Якщо список порожній — надішли в Telegram повідомлення:
```
<b>AI/Tech дайджест — {DATE_UK}</b>

Нових новин за останні ${DIGEST_WINDOW_HOURS}г немає.
```
де `{DATE_UK}` — сьогоднішня дата у форматі `DD.MM.YYYY` (Kyiv timezone). Потім зупинись.

---

## Крок 4 — Саммарізація та оцінка важливості

Для кожного item з `/tmp/tech_news_new.json` обери **українську саммарізацію** (1-2 речення, 150-250 символів) та `importance` від 1 до 5.

### Критерії importance:
- **5** — Major реліз великої лабораторії: нова модель Claude/GPT/Gemini, великий реліз open-source (LLaMA, Mistral), критична вразливість, поглинання
- **4** — Нові фічі в існуючих продуктах OpenAI/Anthropic/Google, значний реліз фреймворку (vLLM, transformers 5.x)
- **3** — Цікаві research papers, benchmarks, технічні deep-dives, мінорні релізи популярних проєктів
- **2** — Industry news, commentary, tutorials, community threads
- **1** — Tangential mentions, дублікати ідей, low-effort posts

### Правила саммарізації:
- Мова — **українська**, термінологія в англійській якщо немає усталеного перекладу (LLM, inference, fine-tuning)
- Не додавай інтерпретації/думки — тільки фактичний зміст
- Якщо `raw_summary` занадто короткий/порожній — формулюй на основі `title` + `source`
- Не використовуй emoji
- Не повторюй source у summary (він вже в заголовку)

### Фільтрація:
- Викинь items з importance ≤ 2 ЯКЩО загалом більше 10 items (економимо увагу)
- Викинь явні дублікати за темою (різні джерела про один і той самий реліз — залиш авторитетне)
- Сортуй по importance DESC, потім по published DESC
- Обмеж top N = `$DIGEST_MAX_ITEMS` (зазвичай 15)

---

## Крок 5 — Сформуй markdown дайджесту

Формат повідомлення (HTML для Telegram, **не** Markdown):

```
<b>🤖 AI/Tech дайджест — {DATE_UK}</b>

{N} новин за останні {WINDOW}г

━━━━━━━━━━━━━━━━━━━━

<b>1. [{CATEGORY_EMOJI}] {TITLE}</b>
<i>{SOURCE}</i>
{SUMMARY_UK}
<a href="{URL}">Читати →</a>

<b>2. ...</b>
...
```

### Emoji по категоріях:
- `lab` → 🧪
- `release` → 📦
- `media` → 📰
- `community` → 💬

### Додаткові правила:
- `TITLE` — escape HTML: `<` → `&lt;`, `>` → `&gt;`, `&` → `&amp;`
- Між items — порожній рядок
- В кінці digest додай рядок: `<i>Наступний дайджест за 12 годин</i>`

---

## Крок 6 — Надішли в Telegram

```bash
cd /Users/reznichenkoandrii/htdocs/tech_news_bot
set -a && source .env && set +a
cat /tmp/tech_news_digest.html | python3 -m src.telegram send
```

Де `/tmp/tech_news_digest.html` — файл з повним текстом дайджесту, який ти згенерував на Кроці 5.

---

## Крок 7 — Оновити seen.json

Витягни всі URL з `/tmp/tech_news_new.json` (не тільки ті що потрапили в дайджест — щоб не показувати їх повторно) і додай у seen:

```bash
cd /Users/reznichenkoandrii/htdocs/tech_news_bot
python3 -c "import json; urls=[i['url'] for i in json.load(open('/tmp/tech_news_new.json'))]; print('\n'.join(urls))" | xargs python3 -m src.dedup update
```

---

## Крок 8 — Git commit

```bash
cd /Users/reznichenkoandrii/htdocs/tech_news_bot
git add data/seen.json
git commit -m "chore: digest $(date +%Y-%m-%d-%H%M)" --allow-empty
```

**НЕ** робити `git push` автоматично — лише локальний commit. Користувач пушить вручну за бажанням.

---

## Крок 9 — Фінальний звіт

Виведи коротко (2-3 рядки):
- Скільки items було всього
- Скільки нових
- Скільки в дайджесті
- Telegram status (ok / error)

---

## Обробка помилок

- `src.fetcher` failed всі feeds → Telegram повідомлення про помилку, зупинка
- `src.telegram` TelegramError → виведи stderr, не оновлюй seen.json (щоб наступний запуск спробував знову), завершись з exit 1
- Python import error → перевір що `cd` в правильну директорію перед викликом модулів

## Важливо

- **НЕ** додавай `--no-verify` чи інші bypass-и
- **НЕ** пропускай Крок 7 (оновлення seen.json) — інакше наступний запуск дубльне всі новини
- **НЕ** використовуй emoji в технічних термінах/назвах моделей у summary
- Якщо GitHub releases feed повертає pre-release/rc — познач у summary як "(pre-release)"
