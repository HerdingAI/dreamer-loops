// Pure sync logic for Dreamer Sync. No chrome.* calls in this file: the
// service worker (background.js) supplies I/O through the `io` object, and
// the tests supply fakes. See docs/claude-ai-sync.md.
//
// Cursor guarantee (the reason this file is shaped as it is):
//   * The list is sorted by updated_at, newest first. A run takes the chats
//     with updated_at > cursor, orders them OLDEST first, and delivers a
//     PREFIX of that order. The cursor becomes the newest delivered
//     updated_at. Everything not delivered (quiet window, run cap) is strictly
//     newer than that, so the next run sees it again.
//   * "Strictly": the cap never splits a group of chats sharing one
//     updated_at, because the next run asks for updated_at > cursor and a
//     split group would lose its tail.
//   * Any failure throws SyncError; the caller reports it and never commits,
//     so the cursor stays where it was.

export class SyncError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status; // needs_login | blocked | schema_changed | error
    this.detail = detail;
  }
}

const TS_RE = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/;

// Total order on ISO timestamps, to the microsecond, as a BigInt.
// Throws SyncError(schema_changed) for anything that is not an ISO timestamp.
export function tsKey(s) {
  const m = typeof s === "string" ? TS_RE.exec(s) : null;
  if (!m) throw new SyncError("schema_changed", `unparseable timestamp: ${JSON.stringify(s)}`);
  const [, y, mo, d, h, mi, se, frac = "", tz] = m;
  let sec = Date.UTC(+y, +mo - 1, +d, +h, +mi, +se) / 1000;
  if (tz !== "Z") {
    const sign = tz[0] === "-" ? -1 : 1;
    sec -= sign * (+tz.slice(1, 3) * 3600 + +tz.slice(4, 6) * 60);
  }
  const micro = frac.padEnd(6, "0").slice(0, 6);
  return BigInt(sec) * 1000000n + BigInt(micro);
}

// --- classification --------------------------------------------------------

// r: {status, contentType, isHtml, redirectedToLogin, cfMitigated, jsonOk}
// -> "ok" | "retry" | "blocked" | "needs_login" | "error"
export function classifyResponse(r) {
  if (r.redirectedToLogin) return "needs_login";
  // A Cloudflare challenge is HTML (or flagged by cf-mitigated) whatever the
  // status code. We report it and stop; we never try to get around it.
  if (r.cfMitigated || r.isHtml) return "blocked";
  if (r.status === 401 || r.status === 403) return "needs_login";
  if (r.status === 429 || (r.status >= 500 && r.status <= 599)) return "retry";
  if (r.status >= 200 && r.status < 300) return r.jsonOk ? "ok" : "error";
  return "error";
}

// Delay before retry number `attempt` (0-based): Retry-After when present
// (seconds or an HTTP date), else exponential. Always capped at maxMs.
export function backoffMs(attempt, retryAfter, { baseMs, maxMs }, nowMs = Date.now()) {
  let ms = baseMs * 2 ** attempt;
  if (retryAfter !== undefined && retryAfter !== null && retryAfter !== "") {
    const n = Number(retryAfter);
    if (Number.isFinite(n) && n >= 0) ms = Math.max(ms, n * 1000);
    else {
      const t = Date.parse(retryAfter);
      if (Number.isFinite(t)) ms = Math.max(ms, t - nowMs);
    }
  }
  return Math.min(Math.max(ms, 0), maxMs);
}

// --- schema checks ---------------------------------------------------------

function need(obj, key, type, where) {
  if (obj === null || typeof obj !== "object" || !(key in obj) || obj[key] === null) {
    throw new SyncError("schema_changed", `${where} is missing "${key}"`);
  }
  if (type === "array" ? !Array.isArray(obj[key]) : typeof obj[key] !== type) {
    throw new SyncError("schema_changed", `${where} "${key}" is not a ${type}`);
  }
}

export function validateListItem(item) {
  need(item, "uuid", "string", "list item");
  need(item, "updated_at", "string", "list item");
  need(item, "created_at", "string", "list item");
  tsKey(item.updated_at);
  return item;
}

export function validateChat(chat) {
  need(chat, "uuid", "string", "chat");
  need(chat, "updated_at", "string", "chat");
  need(chat, "created_at", "string", "chat");
  need(chat, "chat_messages", "array", "chat");
  tsKey(chat.updated_at);
  chat.chat_messages.forEach((m, i) => {
    const where = `chat message #${i}`;
    need(m, "uuid", "string", where);
    need(m, "sender", "string", where);
    need(m, "created_at", "string", where);
    // The prose lives in content[]; text is the older flat field. One of the
    // two must exist or the converter has nothing to read.
    if (!Array.isArray(m.content) && typeof m.text !== "string") {
      throw new SyncError("schema_changed", `${where} has neither content[] nor text`);
    }
  });
  return chat;
}

// --- selection -------------------------------------------------------------

// Newer than the cursor, not temporary, one entry per uuid. cursor may be null.
export function selectCandidates(items, cursor) {
  const cur = cursor ? tsKey(cursor) : null;
  const seen = new Map();
  for (const it of items) {
    if (it.is_temporary) continue;
    if (cur !== null && tsKey(it.updated_at) <= cur) continue;
    seen.set(it.uuid, it); // a re-listed uuid (list shifted while paging): last wins
  }
  return [...seen.values()];
}

// Split candidates into what to deliver now and what waits.
//   deliver      oldest-first prefix, at most maxPerRun (ties never split)
//   quiet        updated within quietHours: taken on a later run
//   capped       eligible but beyond the cap: taken on a later run
export function planRun(candidates, { nowMs, quietHours, maxPerRun }) {
  const threshold = BigInt(nowMs) * 1000n - BigInt(Math.round(quietHours * 3600)) * 1000000n;
  const asc = candidates
    .map((c) => ({ c, k: tsKey(c.updated_at) }))
    .sort((a, b) => (a.k < b.k ? -1 : a.k > b.k ? 1 : a.c.uuid < b.c.uuid ? -1 : 1));
  const eligible = asc.filter((e) => e.k <= threshold);
  const quiet = asc.filter((e) => e.k > threshold).map((e) => e.c);

  let take = Math.min(maxPerRun, eligible.length);
  if (take < eligible.length && take > 0) {
    // The cap would cut through a group sharing one updated_at: back off to
    // the group boundary so the cursor can never pass an undelivered member.
    const edge = eligible[take - 1].k;
    if (eligible[take].k === edge) {
      let cut = take;
      while (cut > 0 && eligible[cut - 1].k === edge) cut--;
      if (cut > 0) take = cut;
      else {
        // The whole first group is bigger than the cap: take all of it.
        take = eligible.findIndex((e) => e.k !== edge);
        if (take < 0) take = eligible.length;
      }
    }
  }
  return {
    deliver: eligible.slice(0, take).map((e) => e.c),
    capped: eligible.slice(take).map((e) => e.c),
    quiet,
  };
}

// New cursor: newest delivered updated_at (the list item's string), never
// moving backwards past the previous one. No delivery -> unchanged.
export function nextCursor(prev, delivered) {
  let best = prev ?? null;
  let bestKey = prev ? tsKey(prev) : null;
  for (const c of delivered) {
    const k = tsKey(c.updated_at);
    if (bestKey === null || k > bestKey) {
      best = c.updated_at;
      bestKey = k;
    }
  }
  return best;
}

// --- orchestration ---------------------------------------------------------

// io: {
//   getOrg(): Promise<string|null>,
//   fetchRaw(url): Promise<{status, isHtml, redirectedToLogin, cfMitigated, retryAfter, json}>,
//   host(msg): Promise<reply>,      // native host round trip; reply.ok must be true
//   sleep(ms): Promise<void>, now(): number (ms),
// }
// settings: the host's status reply `settings`. Returns a summary and commits
// through the host; throws SyncError on any failure (nothing committed).
export async function runSync(io, settings, cursor) {
  const s = settings;
  const org = await io.getOrg();
  if (!org) throw new SyncError("needs_login", "no lastActiveOrg cookie on claude.ai");
  const base = `https://claude.ai/api/organizations/${encodeURIComponent(org)}/chat_conversations`;
  let lastRequestAt = null;

  async function request(url) {
    // Spacing applies between every request, including list pages and retries.
    for (let attempt = 0; ; attempt++) {
      if (lastRequestAt !== null) {
        const wait = lastRequestAt + s.spacing_seconds * 1000 - io.now();
        if (wait > 0) await io.sleep(wait);
      }
      let raw;
      try {
        raw = await io.fetchRaw(url);
      } catch (e) {
        lastRequestAt = io.now();
        if (attempt >= s.max_retries) throw new SyncError("error", `network error: ${e.message}`);
        await io.sleep(backoffMs(attempt, null, backoffCfg(s)));
        continue;
      }
      lastRequestAt = io.now();
      const verdict = classifyResponse({ ...raw, jsonOk: raw.json !== undefined && raw.json !== null });
      if (verdict === "ok") return raw.json;
      if (verdict === "retry") {
        if (attempt >= s.max_retries) throw new SyncError("error", `HTTP ${raw.status} after ${attempt + 1} tries`);
        await io.sleep(backoffMs(attempt, raw.retryAfter, backoffCfg(s), io.now()));
        continue;
      }
      if (verdict === "blocked") throw new SyncError("blocked", `HTML/challenge response (HTTP ${raw.status})`);
      if (verdict === "needs_login") throw new SyncError("needs_login", `HTTP ${raw.status} or login redirect`);
      throw new SyncError("error", `HTTP ${raw.status}`);
    }
  }

  // 1. List pages (newest first) until we reach the cursor.
  const curKey = cursor ? tsKey(cursor) : null;
  const items = [];
  let offset = 0;
  for (let page = 0; ; page++) {
    if (page >= s.max_pages) throw new SyncError("error", `list paging exceeded ${s.max_pages} pages`);
    const rows = await request(`${base}?limit=${s.page_size}&offset=${offset}`);
    if (!Array.isArray(rows)) throw new SyncError("schema_changed", "chat list is not an array");
    rows.forEach(validateListItem);
    // The cursor logic leans on newest-first order; refuse a list that breaks it.
    for (let i = 1; i < rows.length; i++) {
      if (tsKey(rows[i].updated_at) > tsKey(rows[i - 1].updated_at)) {
        throw new SyncError("schema_changed", "chat list is not sorted by updated_at descending");
      }
    }
    items.push(...rows);
    // Not "short page means last page": the server may clamp `limit` below
    // what we asked for, and stopping early would skip chats. Only an empty
    // page, or reaching the cursor, ends paging. Advance by what we received.
    if (rows.length === 0) break;
    offset += rows.length;
    if (curKey !== null && tsKey(rows[rows.length - 1].updated_at) <= curKey) break;
  }

  // 2. Plan: skip temporary, quiet window, cap.
  const plan = planRun(selectCandidates(items, cursor), {
    nowMs: io.now(), quietHours: s.quiet_hours, maxPerRun: s.max_per_run,
  });

  // 3. Fetch and hand each chat to the host.
  let delivered = 0;
  for (const item of plan.deliver) {
    const chat = await request(`${base}/${encodeURIComponent(item.uuid)}?tree=True&rendering_mode=messages&render_all_tools=true`);
    validateChat(chat);
    if (chat.uuid !== item.uuid) throw new SyncError("schema_changed", "chat uuid differs from the list item");
    const reply = await io.host({ cmd: "conversation", chat });
    if (!reply || reply.ok !== true) throw new SyncError("error", `host rejected a chat: ${reply && reply.error}`);
    delivered++;
  }

  // 4. Commit. The cursor is computed from the LIST items' updated_at of the
  // chats actually delivered (the host cross-checks the count).
  const newCursor = nextCursor(cursor, plan.deliver);
  const reply = await io.host({
    cmd: "commit", cursor: newCursor, count: delivered, pending: plan.capped.length > 0,
    detail: `fetched ${delivered}; deferred ${plan.quiet.length} quiet, ${plan.capped.length} over cap`,
  });
  if (!reply || reply.ok !== true) throw new SyncError("error", `host commit failed: ${reply && reply.error}`);
  return { delivered, quiet: plan.quiet.length, capped: plan.capped.length, cursor: newCursor };
}

function backoffCfg(s) {
  return { baseMs: s.backoff_base_seconds * 1000, maxMs: s.backoff_max_seconds * 1000 };
}

// The login cookie's expiry for the host's re-login warning. Only the date
// leaves the extension, never the cookie value. null = Chrome holds no
// claude.ai login (reported, so the hourly tick catches a logout before the
// next due run); undefined = the read failed, so say nothing.
export function loginExpiryField(cookie) {
  if (cookie === undefined) return {};
  if (cookie === null) return { login_expires_at: null };
  const secs = cookie.expirationDate;
  if (typeof secs !== "number" || !Number.isFinite(secs)) return {};
  return { login_expires_at: new Date(secs * 1000).toISOString() };
}
