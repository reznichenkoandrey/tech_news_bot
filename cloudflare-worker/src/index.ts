/**
 * Telegram webhook for tech_news_bot.
 *
 * Flow:
 *   Telegram → POST → this Worker
 *     ↓ verify x-telegram-bot-api-secret-token header
 *     ↓ ACL (chat.id must match TELEGRAM_CHAT_ID)
 *     ↓ dispatch slash command
 *     ↓ read configs from GitHub raw, maybe write user_prefs.yaml via Contents API
 *     ↓ reply via sendMessage
 *
 * All configuration lives in the repo (single source of truth for the digest
 * pipeline AND this Worker). The Worker never keeps local state.
 */

import yaml from "js-yaml";

export interface Env {
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  GITHUB_TOKEN: string;
  GITHUB_REPO: string; // "owner/repo"
  OAUTH_STATE: KVNamespace;
}

// Anthropic OAuth client_id is the public id baked into the official Claude
// Code CLI — same value the local CLI uses to refresh user tokens.
const ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
const ANTHROPIC_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token";

interface TgMessage {
  message_id?: number;
  chat?: { id?: number };
  text?: string;
  from?: { id?: number; username?: string };
}

interface TgCallbackQuery {
  id: string;
  from?: { id?: number; username?: string };
  message?: TgMessage;
  data?: string;
}

interface TgUpdate {
  update_id: number;
  message?: TgMessage;
  callback_query?: TgCallbackQuery;
}

interface Topic {
  slug: string;
  name: string;
  emoji: string;
  default_active?: boolean;
  description?: string;
}

interface Source {
  name: string;
  url: string;
  topics?: string[];
}

interface DigestProfile {
  name: string;
  emoji: string;
  topics?: string[];
}

interface UserPrefs {
  active_topics: string[];
}

// ── Worker entry ──────────────────────────────────────────────────────────────

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    const secret = request.headers.get("x-telegram-bot-api-secret-token");
    if (secret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update: TgUpdate;
    try {
      update = await request.json<TgUpdate>();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    // Inline-button callbacks were retired with the single-message digest;
    // any leftover taps from old messages are silently swallowed.
    if (update.callback_query) {
      return jsonOk();
    }

    const msg = update.message;
    if (!msg) {
      return jsonOk();
    }

    // ACL — silent drop for anyone else.
    if (String(msg.chat?.id ?? "") !== String(env.TELEGRAM_CHAT_ID)) {
      return jsonOk();
    }

    const text = (msg.text ?? "").trim();
    if (!text.startsWith("/")) {
      return jsonOk();
    }

    try {
      const reply = await handleCommand(text, env);
      if (reply) {
        await sendMessage(env, reply);
      }
    } catch (err) {
      console.error("handler failed", err);
      await sendMessage(env, `⚠️ bot error: ${(err as Error).message}`);
    }
    return jsonOk();
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    // Cron fires at :05 in both EEST (07:05 UTC) and EET (08:05 UTC) slots.
    // Exactly one of those is 10:xx Kyiv on any given day — gate decides.
    ctx.waitUntil(maybeTriggerDigest(env));
  },
} satisfies ExportedHandler<Env>;

async function maybeTriggerDigest(env: Env): Promise<void> {
  const hourKyiv = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Kyiv",
    hour: "2-digit",
    hour12: false,
  }).format(new Date());

  if (hourKyiv !== "10") {
    console.log(`scheduled: Kyiv hour=${hourKyiv}, skipping (waiting for 10)`);
    return;
  }

  let accessToken: string | null = null;
  try {
    accessToken = await rotateAnthropicAccessToken(env);
  } catch (err) {
    console.error("scheduled: oauth refresh failed", err);
    await sendMessage(
      env,
      `⚠️ <b>OAuth refresh failed</b>: ${(err as Error).message.slice(0, 200)}\n` +
        `Дайджест запущено з GitHub Secret як fallback — оновіть refresh_token у KV.`,
    );
  }

  const body: Record<string, unknown> = { event_type: "daily-digest" };
  if (accessToken) {
    body.client_payload = { access_token: accessToken };
  }

  const res = await fetch(`${API_BASE}/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: githubHeaders(env),
    body: JSON.stringify(body),
  });
  if (res.ok) {
    console.log(`scheduled: dispatch queued (oauth=${accessToken ? "fresh" : "fallback"})`);
    return;
  }
  const errText = await res.text();
  console.error(`scheduled: dispatch ${res.status}: ${errText.slice(0, 200)}`);
  await sendMessage(
    env,
    `⚠️ <b>Cron failed</b>: не вдалось запустити щоденний дайджест (${res.status}).`,
  );
}

interface AnthropicTokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
}

// Refreshes the Anthropic OAuth access token using the rotating refresh_token
// stored in KV. Writes the new refresh_token back atomically — the old one
// becomes invalid the moment Anthropic returns a new pair, so a write failure
// here means the *next* run will fail until the KV value is fixed manually.
async function rotateAnthropicAccessToken(env: Env): Promise<string> {
  const refreshToken = await env.OAUTH_STATE.get("refresh_token");
  if (!refreshToken) {
    throw new Error('KV key "refresh_token" is empty — seed it first');
  }

  const res = await fetch(ANTHROPIC_OAUTH_TOKEN_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "user-agent": "tech-news-bot-worker",
    },
    body: JSON.stringify({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: ANTHROPIC_OAUTH_CLIENT_ID,
    }),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`oauth refresh ${res.status}: ${text.slice(0, 200)}`);
  }
  let data: AnthropicTokenResponse;
  try {
    data = JSON.parse(text) as AnthropicTokenResponse;
  } catch {
    throw new Error(`oauth refresh: non-JSON response: ${text.slice(0, 200)}`);
  }
  if (!data.access_token || !data.refresh_token) {
    throw new Error("oauth refresh: response missing tokens");
  }
  await env.OAUTH_STATE.put("refresh_token", data.refresh_token);
  return data.access_token;
}

function jsonOk(): Response {
  return new Response(JSON.stringify({ ok: true }), {
    headers: { "content-type": "application/json" },
  });
}

// ── Numbered action dispatch (/save N, /hide N, /deep N) ─────────────────────

interface LastDigestEntry {
  n: number;
  url: string;
  title?: string;
  profile?: string;
}

async function loadLastDigest(env: Env): Promise<LastDigestEntry[]> {
  // Contents API instead of raw CDN — same reason as user_prefs: the digest
  // commit happens at 10:0X and the user may type /save 5 within seconds.
  const res = await fetch(
    `${API_BASE}/repos/${env.GITHUB_REPO}/contents/data/last_digest.json?ref=main`,
    { headers: githubHeaders(env) },
  );
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`last_digest get: ${res.status}`);
  const body = (await res.json()) as { content: string; encoding: string };
  const decoded = body.encoding === "base64" ? atobUtf8(body.content) : body.content;
  const parsed = JSON.parse(decoded) as { items?: LastDigestEntry[] };
  return parsed.items ?? [];
}

function resolveItemNumber(items: LastDigestEntry[], arg: string | undefined): LastDigestEntry | string {
  if (!arg) return "❌ Вкажи номер: <code>/save 3</code>";
  const n = Number.parseInt(arg, 10);
  if (!Number.isFinite(n) || n <= 0) return `❌ <code>${escapeHtml(arg)}</code> не схоже на номер.`;
  const entry = items.find((e) => e.n === n);
  if (!entry) {
    const max = items.length > 0 ? Math.max(...items.map((e) => e.n)) : 0;
    return `❌ Номер ${n} відсутній у останньому дайджесті (доступно 1..${max}).`;
  }
  return entry;
}

async function cmdSave(env: Env, arg: string | undefined): Promise<string> {
  const items = await loadLastDigest(env);
  const resolved = resolveItemNumber(items, arg);
  if (typeof resolved === "string") return resolved;

  const { list, sha } = await loadReadingListWithSha(env);
  if (list.some((e) => e.url === resolved.url)) {
    return `ℹ️ <b>${resolved.n}.</b> вже у reading list.`;
  }
  list.push({ url: resolved.url, saved_at: new Date().toISOString() });
  await saveReadingList(env, list, sha);
  return `⭐ збережено <b>${resolved.n}.</b> ${escapeHtml(resolved.title ?? resolved.url)}`;
}

async function cmdHide(env: Env, arg: string | undefined): Promise<string> {
  const items = await loadLastDigest(env);
  const resolved = resolveItemNumber(items, arg);
  if (typeof resolved === "string") return resolved;

  const { list, sha } = await loadSeenWithSha(env);
  if (list.includes(resolved.url)) {
    return `ℹ️ <b>${resolved.n}.</b> вже сховано.`;
  }
  list.push(resolved.url);
  await saveSeenList(env, list, sha);
  return `🗑 сховано <b>${resolved.n}.</b> більше не з'явиться у дайджестах.`;
}

async function cmdDeep(env: Env, arg: string | undefined): Promise<string> {
  const items = await loadLastDigest(env);
  const resolved = resolveItemNumber(items, arg);
  if (typeof resolved === "string") return resolved;

  // Hand the heavy summarize job to GitHub Actions; the workflow will reply
  // in-chat when it's ready. We pass chat_id but no message_id (no per-item
  // message exists anymore) — the summarize workflow posts a fresh message.
  const res = await fetch(`${API_BASE}/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: githubHeaders(env),
    body: JSON.stringify({
      event_type: "summarize-article",
      client_payload: {
        url: resolved.url,
        chat_id: Number(env.TELEGRAM_CHAT_ID),
      },
    }),
  });
  if (!res.ok) {
    const errText = await res.text();
    console.error(`/deep dispatch ${res.status}: ${errText.slice(0, 200)}`);
    return `⚠️ не вдалось запустити summarize: ${res.status}`;
  }
  return `⏳ готую <b>${resolved.n}.</b> ${escapeHtml(resolved.title ?? resolved.url)} — пришлю окремим повідомленням.`;
}

// ── Command dispatch ──────────────────────────────────────────────────────────

async function handleCommand(text: string, env: Env): Promise<string> {
  const parts = text.split(/\s+/);
  const command = parts[0].split("@")[0].toLowerCase();
  const arg = parts[1];

  const registry = await loadTopicsRegistry(env);

  switch (command) {
    case "/start":
    case "/help":
      return cmdHelp();
    case "/topics": {
      const prefs = await loadUserPrefs(env);
      return cmdTopics(registry, prefs);
    }
    case "/add": {
      if (!arg) return "❌ <code>/add &lt;slug&gt;</code> — вкажи slug топіка.";
      return await cmdMutate(env, registry, (prefs) => addTopic(arg, registry, prefs));
    }
    case "/remove": {
      if (!arg) return "❌ <code>/remove &lt;slug&gt;</code> — вкажи slug топіка.";
      return await cmdMutate(env, registry, (prefs) => removeTopic(arg, prefs));
    }
    case "/reset":
      return await cmdMutate(env, registry, resetTopics);
    case "/sources": {
      const sources = await loadSources(env);
      return cmdSources(arg, sources, registry);
    }
    case "/digests": {
      const [digests, prefs] = await Promise.all([loadDigests(env), loadUserPrefs(env)]);
      return cmdDigests(digests, prefs);
    }
    case "/status": {
      const [digests, prefs] = await Promise.all([loadDigests(env), loadUserPrefs(env)]);
      return cmdStatus(digests, prefs);
    }
    case "/save":
      return await cmdSave(env, arg);
    case "/hide":
      return await cmdHide(env, arg);
    case "/deep":
      return await cmdDeep(env, arg);
    default:
      return `🤷 невідома команда ${command}. Спробуй /help.`;
  }
}

// ── Command implementations ───────────────────────────────────────────────────

function cmdHelp(): string {
  return (
    "<b>Дії над останнім дайджестом:</b>\n" +
    "/save <code>N</code> — зберегти item N у reading list\n" +
    "/hide <code>N</code> — сховати item N з майбутніх дайджестів\n" +
    "/deep <code>N</code> — згенерувати розширене саммарі для item N\n" +
    "\n" +
    "<b>Налаштування:</b>\n" +
    "/topics — список топіків з поточним фільтром\n" +
    "/add <code>slug</code> — додати топік у фільтр\n" +
    "/remove <code>slug</code> — прибрати топік з фільтра\n" +
    "/reset — зняти фільтр (повні дайджести)\n" +
    "/sources [<code>slug</code>] — показати feed'и\n" +
    "/digests — поточні дайджест-профілі\n" +
    "/status — стан системи + активний фільтр\n" +
    "/help — цей список"
  );
}

function cmdTopics(registry: Record<string, Topic>, prefs: UserPrefs): string {
  const slugs = Object.keys(registry).sort();
  if (slugs.length === 0) return "⚠️ topics.yaml порожній.";
  const active = new Set(prefs.active_topics);
  const lines = slugs.map((slug) => formatTopicLine(slug, registry, active));
  const footer = active.size > 0 ? `\n\n<i>Активних: ${active.size}</i>` : "\n\n<i>Активних: усі</i>";
  return "<b>Топіки</b> (✅ = в активному фільтрі):\n" + lines.join("\n") + footer;
}

function formatTopicLine(slug: string, registry: Record<string, Topic>, active: Set<string>): string {
  const entry = registry[slug];
  const emoji = entry?.emoji ?? "•";
  const name = entry?.name ?? slug;
  const mark = active.has(slug) ? "✅" : "◻️";
  return `${mark} ${emoji} <b>${slug}</b> — ${escapeHtml(name)}`;
}

type PrefMutator = (prefs: UserPrefs) => { reply: string; mutated: boolean };

function addTopic(slug: string, registry: Record<string, Topic>, prefs: UserPrefs): { reply: string; mutated: boolean } {
  if (!(slug in registry)) {
    const known = Object.keys(registry).sort().slice(0, 6).join(", ");
    return { reply: `❌ невідомий slug <code>${escapeHtml(slug)}</code>. Доступні: ${known}…`, mutated: false };
  }
  if (prefs.active_topics.includes(slug)) {
    return { reply: `ℹ️ <code>${slug}</code> уже в фільтрі.`, mutated: false };
  }
  prefs.active_topics.push(slug);
  return { reply: `✅ додав <code>${slug}</code>. У фільтрі: ${prefs.active_topics.join(", ")}`, mutated: true };
}

function removeTopic(slug: string, prefs: UserPrefs): { reply: string; mutated: boolean } {
  const idx = prefs.active_topics.indexOf(slug);
  if (idx === -1) {
    return { reply: `ℹ️ <code>${escapeHtml(slug)}</code> і так немає у фільтрі.`, mutated: false };
  }
  prefs.active_topics.splice(idx, 1);
  const remaining = prefs.active_topics.length > 0 ? prefs.active_topics.join(", ") : "усі (фільтр порожній)";
  return { reply: `🗑 прибрав <code>${slug}</code>. Активні: ${remaining}`, mutated: true };
}

function resetTopics(prefs: UserPrefs): { reply: string; mutated: boolean } {
  if (prefs.active_topics.length === 0) {
    return { reply: "ℹ️ фільтр і так порожній.", mutated: false };
  }
  prefs.active_topics = [];
  return { reply: "♻️ фільтр очищено — дайджести повертаються до дефолтів.", mutated: true };
}

function cmdSources(slug: string | undefined, sources: Source[], registry: Record<string, Topic>): string {
  if (slug && !(slug in registry)) {
    return `❌ невідомий slug <code>${escapeHtml(slug)}</code>.`;
  }
  if (slug) {
    const matching = sources.filter((s) => (s.topics ?? []).includes(slug));
    if (matching.length === 0) {
      return `ℹ️ жоден feed не тегнуто як <code>${slug}</code>.`;
    }
    const lines = matching.map((s) => `• <b>${escapeHtml(s.name)}</b> — <i>${escapeHtml(s.url)}</i>`);
    return `<b>Feeds у топіку ${slug}</b> (${matching.length}):\n` + lines.join("\n");
  }
  const counts: Record<string, number> = {};
  for (const s of sources) {
    for (const t of s.topics ?? []) {
      counts[t] = (counts[t] ?? 0) + 1;
    }
  }
  const slugs = Object.keys(registry).sort();
  const lines = slugs.map((t) => `• <b>${t}</b> — ${counts[t] ?? 0} feeds`);
  return `<b>Feeds всього: ${sources.length}</b>\n` + lines.join("\n");
}

function cmdDigests(digests: DigestProfile[], prefs: UserPrefs): string {
  if (digests.length === 0) {
    return "ℹ️ config/digests.yaml порожній — активний один дефолтний профіль.";
  }
  const active = new Set(prefs.active_topics);
  const lines = digests.map((d) => {
    const topics = d.topics ?? [];
    const overlap = active.size > 0 ? topics.filter((t) => active.has(t)) : topics;
    const marker = overlap.length > 0 || active.size === 0 ? "✅" : "⚠️ (фільтр виключає)";
    const list = topics.length > 0 ? topics.join(", ") : "all";
    return `${marker} ${d.emoji ?? "📰"} <b>${escapeHtml(d.name ?? "?")}</b> — topics: ${list}`;
  });
  return "<b>Дайджест-профілі:</b>\n" + lines.join("\n");
}

function cmdStatus(digests: DigestProfile[], prefs: UserPrefs): string {
  const filt = prefs.active_topics.length > 0 ? prefs.active_topics.join(", ") : "<i>порожній (повні дайджести)</i>";
  return (
    "<b>Стан бота:</b>\n" +
    "Розклад: щодня 10:00 Kyiv (GitHub Actions)\n" +
    "Webhook: Cloudflare Worker (миттєві відповіді)\n" +
    `Дайджест-профілі: ${digests.length}\n` +
    `Активний фільтр: ${filt}`
  );
}

// ── Mutating commands: load prefs, mutate, save back via GitHub ───────────────

async function cmdMutate(env: Env, registry: Record<string, Topic>, mutator: PrefMutator): Promise<string> {
  const { prefs, sha } = await loadUserPrefsWithSha(env);
  const { reply, mutated } = mutator(prefs);
  if (mutated) {
    await saveUserPrefs(env, prefs, sha);
  }
  return reply;
}

// ── GitHub I/O ────────────────────────────────────────────────────────────────

const RAW_BASE = "https://raw.githubusercontent.com";
const API_BASE = "https://api.github.com";

async function githubRaw(env: Env, path: string): Promise<string> {
  const res = await fetch(`${RAW_BASE}/${env.GITHUB_REPO}/main/${path}`, {
    headers: { "cache-control": "no-cache" },
  });
  if (!res.ok) throw new Error(`github raw ${path}: ${res.status}`);
  return await res.text();
}

async function loadTopicsRegistry(env: Env): Promise<Record<string, Topic>> {
  const text = await githubRaw(env, "config/topics.yaml");
  const data = yaml.load(text) as { topics?: Topic[] } | null;
  const registry: Record<string, Topic> = {};
  for (const t of data?.topics ?? []) {
    registry[t.slug] = t;
  }
  return registry;
}

async function loadSources(env: Env): Promise<Source[]> {
  const text = await githubRaw(env, "config/sources.yaml");
  const data = yaml.load(text) as { feeds?: Source[] } | null;
  return data?.feeds ?? [];
}

async function loadDigests(env: Env): Promise<DigestProfile[]> {
  const text = await githubRaw(env, "config/digests.yaml");
  const data = yaml.load(text) as { digests?: DigestProfile[] } | null;
  return data?.digests ?? [];
}

async function loadUserPrefs(env: Env): Promise<UserPrefs> {
  // Must go through the Contents API, not raw.githubusercontent.com: the
  // raw CDN caches for up to ~5 minutes, which means a /topics call seconds
  // after /add would show a stale "empty filter" state. Contents API with
  // ref=main serves the committed tree immediately.
  const { prefs } = await loadUserPrefsWithSha(env);
  return prefs;
}

interface ReadingListEntry {
  url: string;
  saved_at: string;
}

interface ReadingListWithSha {
  list: ReadingListEntry[];
  sha: string | null;
}

async function loadReadingListWithSha(env: Env): Promise<ReadingListWithSha> {
  const res = await fetch(
    `${API_BASE}/repos/${env.GITHUB_REPO}/contents/data/reading_list.json?ref=main`,
    { headers: githubHeaders(env) },
  );
  if (res.status === 404) {
    return { list: [], sha: null };
  }
  if (!res.ok) throw new Error(`reading_list get: ${res.status}`);
  const body = (await res.json()) as { sha: string; content: string; encoding: string };
  const decoded = body.encoding === "base64" ? atobUtf8(body.content) : body.content;
  let parsed: unknown;
  try {
    parsed = JSON.parse(decoded);
  } catch {
    parsed = [];
  }
  const list = Array.isArray(parsed) ? (parsed as ReadingListEntry[]) : [];
  return { list, sha: body.sha };
}

async function saveReadingList(env: Env, list: ReadingListEntry[], sha: string | null): Promise<void> {
  await githubPutJson(env, "data/reading_list.json", list, sha, "chore(bot): save to reading list");
}

interface SeenWithSha {
  list: string[];
  sha: string | null;
}

async function loadSeenWithSha(env: Env): Promise<SeenWithSha> {
  const res = await fetch(
    `${API_BASE}/repos/${env.GITHUB_REPO}/contents/data/seen.json?ref=main`,
    { headers: githubHeaders(env) },
  );
  if (res.status === 404) {
    return { list: [], sha: null };
  }
  if (!res.ok) throw new Error(`seen get: ${res.status}`);
  const body = (await res.json()) as { sha: string; content: string; encoding: string };
  const decoded = body.encoding === "base64" ? atobUtf8(body.content) : body.content;
  let parsed: unknown;
  try {
    parsed = JSON.parse(decoded);
  } catch {
    parsed = [];
  }
  const list = Array.isArray(parsed) ? (parsed as string[]) : [];
  return { list, sha: body.sha };
}

async function saveSeenList(env: Env, list: string[], sha: string | null): Promise<void> {
  await githubPutJson(env, "data/seen.json", list, sha, "chore(bot): hide digest item");
}

async function githubPutJson(
  env: Env,
  path: string,
  payload: unknown,
  sha: string | null,
  commitMessage: string,
): Promise<void> {
  const content = JSON.stringify(payload, null, 2) + "\n";
  const encoded = btoaUtf8(content);

  let currentSha = sha;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const body: Record<string, unknown> = {
      message: commitMessage,
      content: encoded,
      branch: "main",
      committer: { name: "tech-digest-bot", email: "bot@users.noreply.github.com" },
    };
    if (currentSha) body.sha = currentSha;

    const res = await fetch(`${API_BASE}/repos/${env.GITHUB_REPO}/contents/${path}`, {
      method: "PUT",
      headers: githubHeaders(env),
      body: JSON.stringify(body),
    });
    if (res.ok) return;

    if (res.status === 409 || res.status === 422) {
      // Refetch sha and retry — another workflow wrote in-between.
      const latest = await fetch(
        `${API_BASE}/repos/${env.GITHUB_REPO}/contents/${path}?ref=main`,
        { headers: githubHeaders(env) },
      );
      if (latest.ok) {
        const body = (await latest.json()) as { sha: string };
        currentSha = body.sha;
        continue;
      }
    }
    const errText = await res.text();
    throw new Error(`github PUT ${path} ${res.status}: ${errText.slice(0, 200)}`);
  }
  throw new Error(`github PUT ${path}: 3 attempts failed`);
}

interface PrefsWithSha {
  prefs: UserPrefs;
  sha: string;
}

async function loadUserPrefsWithSha(env: Env): Promise<PrefsWithSha> {
  const res = await fetch(`${API_BASE}/repos/${env.GITHUB_REPO}/contents/config/user_prefs.yaml?ref=main`, {
    headers: githubHeaders(env),
  });
  if (!res.ok) throw new Error(`github contents get: ${res.status}`);
  const body = (await res.json()) as { sha: string; content: string; encoding: string };
  const decoded = body.encoding === "base64" ? atobUtf8(body.content) : body.content;
  return { prefs: parseUserPrefs(decoded), sha: body.sha };
}

async function saveUserPrefs(env: Env, prefs: UserPrefs, sha: string): Promise<void> {
  const content = renderUserPrefs(prefs);
  const encoded = btoaUtf8(content);
  // en-CA gives "YYYY-MM-DD, HH:MM:SS" — collapse runs of separator chars so
  // ", " between date and time doesn't leak into the message as "--".
  const tzKyiv = new Date().toLocaleString("en-CA", { timeZone: "Europe/Kyiv", hour12: false })
    .replace(/[,\s:]+/g, "-").slice(0, 16); // YYYY-MM-DD-HH-MM

  // Retry up to 3 times on SHA conflict (another workflow wrote in-between).
  let currentSha = sha;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const res = await fetch(`${API_BASE}/repos/${env.GITHUB_REPO}/contents/config/user_prefs.yaml`, {
      method: "PUT",
      headers: githubHeaders(env),
      body: JSON.stringify({
        message: `chore(bot): state ${tzKyiv}`,
        content: encoded,
        sha: currentSha,
        branch: "main",
        committer: { name: "tech-digest-bot", email: "bot@users.noreply.github.com" },
      }),
    });
    if (res.ok) return;

    if (res.status === 409 || res.status === 422) {
      // Conflict — refetch sha and retry.
      const latest = await loadUserPrefsWithSha(env);
      currentSha = latest.sha;
      continue;
    }
    const errText = await res.text();
    throw new Error(`github contents put ${res.status}: ${errText.slice(0, 200)}`);
  }
  throw new Error("github contents put: 3 attempts failed");
}

function githubHeaders(env: Env): HeadersInit {
  return {
    "authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "accept": "application/vnd.github+json",
    "user-agent": "tech-news-bot-worker",
    "x-github-api-version": "2022-11-28",
    "content-type": "application/json",
  };
}

// ── user_prefs.yaml parse / render (keep format stable for readable diffs) ────

function parseUserPrefs(text: string): UserPrefs {
  const data = yaml.load(text) as { active_topics?: string[] } | null;
  return { active_topics: data?.active_topics ?? [] };
}

function renderUserPrefs(prefs: UserPrefs): string {
  let content =
    "# User preferences, managed by the Cloudflare Worker (and bot_poll.py as fallback).\n" +
    "# Edit manually only when the bot is offline — otherwise your changes may be\n" +
    "# overwritten on the next webhook.\n" +
    "#\n" +
    "# active_topics — global filter applied on top of config/digests.yaml profiles.\n" +
    "# Empty list = no filter. Non-empty list narrows every profile to this set.\n" +
    "\nactive_topics:";
  if (prefs.active_topics.length === 0) {
    content += " []\n";
  } else {
    content += "\n" + prefs.active_topics.map((t) => `  - ${t}`).join("\n") + "\n";
  }
  return content;
}

// ── Telegram ──────────────────────────────────────────────────────────────────

async function sendMessage(env: Env, text: string): Promise<void> {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_CHAT_ID,
      parse_mode: "HTML",
      disable_web_page_preview: true,
      text,
    }),
  });
  if (!res.ok) {
    const errText = await res.text();
    console.error(`telegram sendMessage ${res.status}: ${errText.slice(0, 200)}`);
  }
}

// ── tiny utils ────────────────────────────────────────────────────────────────

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function atobUtf8(b64: string): string {
  // atob gives latin-1 bytes; convert to UTF-8 string properly.
  const bin = atob(b64.replace(/\n/g, ""));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder("utf-8").decode(bytes);
}

function btoaUtf8(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const byte of bytes) bin += String.fromCharCode(byte);
  return btoa(bin);
}
