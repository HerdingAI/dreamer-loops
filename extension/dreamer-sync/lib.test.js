import test from "node:test";
import assert from "node:assert/strict";
import {
  SyncError, tsKey, classifyResponse, backoffMs, validateListItem, validateChat,
  selectCandidates, planRun, nextCursor, runSync, loginExpiryField,
} from "./lib.js";

const H = 3600 * 1000;
const NOW = Date.parse("2030-01-14T12:00:00Z");
const iso = (hoursAgo, extra = "") =>
  new Date(NOW - hoursAgo * H).toISOString().replace(".000Z", `${extra}Z`);
const item = (uuid, hoursAgo, extra = {}) => ({
  uuid, name: uuid, created_at: iso(hoursAgo + 1), updated_at: iso(hoursAgo), is_temporary: false, ...extra,
});
const chatOf = (it) => ({
  ...it, chat_messages: [{ uuid: "m1", sender: "human", created_at: it.created_at, content: [{ type: "text", text: "hi" }], text: "" }],
});
const SETTINGS = {
  quiet_hours: 6, max_per_run: 3, spacing_seconds: 3, page_size: 2, max_pages: 50,
  max_retries: 3, backoff_base_seconds: 5, backoff_max_seconds: 120,
};

test("tsKey orders to the microsecond and honours offsets", () => {
  assert.ok(tsKey("2030-01-14T10:00:00.000002Z") > tsKey("2030-01-14T10:00:00.000001Z"));
  assert.equal(tsKey("2030-01-14T10:00:00Z"), tsKey("2030-01-14T12:00:00+02:00"));
  assert.throws(() => tsKey("yesterday"), (e) => e.status === "schema_changed");
  assert.throws(() => tsKey(undefined), (e) => e.status === "schema_changed");
});

test("classifyResponse", () => {
  const c = (o) => classifyResponse({ status: 200, jsonOk: true, ...o });
  assert.equal(c({}), "ok");
  assert.equal(c({ status: 200, isHtml: true, jsonOk: false }), "blocked");
  assert.equal(c({ status: 403, isHtml: true, jsonOk: false }), "blocked");
  assert.equal(c({ status: 403, cfMitigated: true, jsonOk: false }), "blocked");
  assert.equal(c({ status: 403, jsonOk: true }), "needs_login");
  assert.equal(c({ status: 401 }), "needs_login");
  assert.equal(c({ status: 200, redirectedToLogin: true, isHtml: true }), "needs_login");
  assert.equal(c({ status: 429 }), "retry");
  assert.equal(c({ status: 503 }), "retry");
  assert.equal(c({ status: 404 }), "error");
  assert.equal(c({ status: 200, jsonOk: false }), "error");
});

test("backoffMs: exponential, Retry-After honoured, capped", () => {
  const cfg = { baseMs: 5000, maxMs: 120000 };
  assert.equal(backoffMs(0, null, cfg), 5000);
  assert.equal(backoffMs(2, null, cfg), 20000);
  assert.equal(backoffMs(0, "30", cfg), 30000);
  assert.equal(backoffMs(0, "9999", cfg), 120000);
  assert.equal(backoffMs(0, new Date(NOW + 40000).toUTCString(), cfg, NOW), 40000);
  assert.equal(backoffMs(10, null, cfg), 120000);
});

test("schema checks name what is missing", () => {
  assert.throws(() => validateListItem({ uuid: "a", created_at: iso(1) }), /updated_at/);
  assert.throws(() => validateListItem(null), (e) => e.status === "schema_changed");
  assert.throws(() => validateChat({ ...chatOf(item("a", 8)), chat_messages: undefined }), /chat_messages/);
  const bad = chatOf(item("a", 8));
  delete bad.chat_messages[0].sender;
  assert.throws(() => validateChat(bad), /sender/);
  const noBody = chatOf(item("a", 8));
  delete noBody.chat_messages[0].content;
  delete noBody.chat_messages[0].text;
  assert.throws(() => validateChat(noBody), /content/);
  assert.ok(validateChat(chatOf(item("a", 8))));
});

test("selectCandidates drops temporary chats, old chats and duplicates", () => {
  const items = [item("n", 8), item("t", 9, { is_temporary: true }), item("o", 30), item("n", 8)];
  const got = selectCandidates(items, iso(20));
  assert.deepEqual(got.map((x) => x.uuid), ["n"]);
  assert.equal(selectCandidates(items, null).length, 2); // n and o: temporary skipped, duplicate merged
});

test("planRun: quiet window defers, cap takes the OLDEST, ties are never split", () => {
  const a = item("a", 40), b = item("b", 30), c = item("c", 20), d = item("d", 10), q = item("q", 2);
  const p = planRun([q, d, c, b, a], { nowMs: NOW, quietHours: 6, maxPerRun: 3 });
  assert.deepEqual(p.deliver.map((x) => x.uuid), ["a", "b", "c"]);
  assert.deepEqual(p.capped.map((x) => x.uuid), ["d"]);
  assert.deepEqual(p.quiet.map((x) => x.uuid), ["q"]);

  // b and c share an updated_at: a cap of 2 would split them, so only a goes.
  const c2 = { ...item("c2", 30) };
  const t = planRun([a, b, c2, d], { nowMs: NOW, quietHours: 6, maxPerRun: 2 });
  assert.deepEqual(t.deliver.map((x) => x.uuid), ["a"]);
  assert.deepEqual(t.capped.map((x) => x.uuid).sort(), ["b", "c2", "d"]);

  // A first group larger than the cap is taken whole rather than split.
  const g = planRun([item("x", 30), item("y", 30), item("z", 30), d], { nowMs: NOW, quietHours: 6, maxPerRun: 2 });
  assert.deepEqual(g.deliver.map((x) => x.uuid), ["x", "y", "z"]);
  assert.deepEqual(g.capped.map((x) => x.uuid), ["d"]);
});

test("cursor never passes anything undelivered (property check)", () => {
  let seed = 7;
  const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  for (let n = 0; n < 300; n++) {
    const items = Array.from({ length: 1 + Math.floor(rnd() * 12) }, (_, i) =>
      item(`u${i}`, Math.floor(rnd() * 12) * 4 + 0.0)); // many ties, some quiet
    const cap = 1 + Math.floor(rnd() * 5);
    const p = planRun(items, { nowMs: NOW, quietHours: 6, maxPerRun: cap });
    const cur = nextCursor(null, p.deliver);
    const waiting = [...p.capped, ...p.quiet];
    for (const w of waiting) {
      if (cur !== null) assert.ok(tsKey(w.updated_at) > tsKey(cur), "undelivered chat is not past the cursor");
    }
    // Every chat is delivered or waits; none vanishes.
    assert.equal(p.deliver.length + waiting.length, items.length);
  }
});

test("nextCursor: unchanged with no delivery, never regresses", () => {
  assert.equal(nextCursor(iso(5), []), iso(5));
  assert.equal(nextCursor(null, []), null);
  assert.equal(nextCursor(iso(5), [item("a", 9)]), iso(5));
  assert.equal(nextCursor(iso(5), [item("a", 9), item("b", 3)]), iso(3));
});

// --- runSync with a fake world ----------------------------------------------
function world({ org = "org1", list, chats = {}, script = {} } = {}) {
  const w = { calls: [], hostMsgs: [], sleeps: [], t: NOW, org };
  w.io = {
    getOrg: async () => w.org,
    now: () => w.t,
    sleep: async (ms) => { w.sleeps.push(ms); w.t += ms; },
    host: async (m) => { w.hostMsgs.push(m); return { ok: true }; },
    fetchRaw: async (url) => {
      w.calls.push({ url, t: w.t });
      const hook = script[w.calls.length];
      if (hook) return hook;
      const u = new URL(url);
      if (u.pathname.endsWith("/chat_conversations")) {
        const off = +u.searchParams.get("offset"), lim = +u.searchParams.get("limit");
        return { status: 200, json: list.slice(off, off + lim) };
      }
      const id = decodeURIComponent(u.pathname.split("/").pop());
      return { status: 200, json: chats[id] };
    },
  };
  return w;
}
const mkChats = (items) => Object.fromEntries(items.map((i) => [i.uuid, chatOf(i)]));

test("runSync: pages until the cursor, fetches oldest first, commits max delivered", async () => {
  const items = [item("e", 2), item("d", 10), item("c", 20), item("b", 30), item("a", 40)]; // newest first
  const w = world({ list: items, chats: mkChats(items) });
  const out = await runSync(w.io, SETTINGS, iso(25));
  assert.deepEqual(out, { delivered: 2, quiet: 1, capped: 0, cursor: iso(10) });
  const convs = w.hostMsgs.filter((m) => m.cmd === "conversation").map((m) => m.chat.uuid);
  assert.deepEqual(convs, ["c", "d"]);
  const commit = w.hostMsgs.at(-1);
  assert.equal(commit.cmd, "commit");
  assert.equal(commit.cursor, iso(10));
  assert.equal(commit.count, 2);
  // listing stopped once a page reached the cursor: pages of 2 -> [e,d] [c,b] and no third
  assert.equal(w.calls.filter((c) => !c.url.includes("tree=True")).length, 2);
  // chat fetches are spaced by at least spacing_seconds
  const ts = w.calls.map((c) => c.t);
  for (let i = 1; i < ts.length; i++) assert.ok(ts[i] - ts[i - 1] >= 3000);
});

test("runSync: cap leaves the rest for the next run and reports pending", async () => {
  const items = [d4(), d3(), d2(), d1(), d0()];
  function d0() { return item("a", 50); } function d1() { return item("b", 40); }
  function d2() { return item("c", 30); } function d3() { return item("d", 20); } function d4() { return item("e", 10); }
  const w = world({ list: items, chats: mkChats(items) });
  const first = await runSync(w.io, SETTINGS, null);
  assert.equal(first.delivered, 3);
  assert.equal(first.capped, 2);
  assert.equal(first.cursor, iso(30));
  assert.equal(w.hostMsgs.at(-1).pending, true);
  // The next run, from that cursor, picks up exactly the remaining two.
  const w2 = world({ list: items, chats: mkChats(items) });
  const second = await runSync(w2.io, SETTINGS, first.cursor);
  assert.deepEqual(w2.hostMsgs.filter((m) => m.cmd === "conversation").map((m) => m.chat.uuid), ["d", "e"]);
  assert.equal(second.cursor, iso(10));
});

test("runSync: nothing new commits ok with the cursor unchanged", async () => {
  const items = [item("a", 40)];
  const w = world({ list: items, chats: mkChats(items) });
  const out = await runSync(w.io, SETTINGS, iso(40));
  assert.equal(out.delivered, 0);
  assert.equal(out.cursor, iso(40));
});

test("runSync: failure classes throw and never commit", async () => {
  const items = [item("a", 40)];
  const cases = [
    ["no org", { org: null }, "needs_login"],
    ["html", { script: { 1: { status: 200, isHtml: true } } }, "blocked"],
    ["cf 403", { script: { 1: { status: 403, isHtml: true, cfMitigated: true } } }, "blocked"],
    ["json 403", { script: { 1: { status: 403, json: { error: "x" } } } }, "needs_login"],
    ["redirect", { script: { 1: { status: 200, redirectedToLogin: true, isHtml: true } } }, "needs_login"],
    ["list not array", { script: { 1: { status: 200, json: { oops: 1 } } } }, "schema_changed"],
    ["list item bad", { script: { 1: { status: 200, json: [{ uuid: "a" }] } } }, "schema_changed"],
    ["unsorted list", { script: { 1: { status: 200, json: [item("a", 40), item("b", 20)] } } }, "schema_changed"],
    ["500 forever", { script: Object.fromEntries([1, 2, 3, 4, 5].map((i) => [i, { status: 500 }])) }, "error"],
    ["teapot", { script: { 1: { status: 418, json: {} } } }, "error"],
  ];
  for (const [name, opts, want] of cases) {
    const w = world({ list: items, chats: mkChats(items), ...opts });
    await assert.rejects(runSync(w.io, SETTINGS, null), (e) => e instanceof SyncError && e.status === want, name);
    assert.equal(w.hostMsgs.filter((m) => m.cmd === "commit").length, 0, `${name}: no commit`);
  }
});

test("runSync: chat with a missing key is schema_changed, nothing committed", async () => {
  const items = [item("a", 40)];
  const chats = mkChats(items);
  delete chats.a.chat_messages;
  const w = world({ list: items, chats });
  await assert.rejects(runSync(w.io, SETTINGS, null), (e) => e.status === "schema_changed");
  assert.equal(w.hostMsgs.some((m) => m.cmd === "commit"), false);
});

test("runSync: 429 is retried with Retry-After honoured, then succeeds", async () => {
  const items = [item("a", 40)];
  const w = world({ list: items, chats: mkChats(items), script: { 1: { status: 429, retryAfter: "30" } } });
  const out = await runSync(w.io, SETTINGS, null);
  assert.equal(out.delivered, 1);
  assert.ok(w.sleeps.some((ms) => ms >= 30000));
  assert.ok(w.calls[1].t - w.calls[0].t >= 30000);
});

test("runSync: a host that rejects a chat aborts without committing", async () => {
  const items = [item("a", 40)];
  const w = world({ list: items, chats: mkChats(items) });
  w.io.host = async (m) => { w.hostMsgs.push(m); return { ok: false, error: "disk full" }; };
  await assert.rejects(runSync(w.io, SETTINGS, null), (e) => e.status === "error" && /disk full/.test(e.detail));
  assert.equal(w.hostMsgs.some((m) => m.cmd === "commit"), false);
});

test("runSync: list never ends is bounded by max_pages", async () => {
  let n = 0;
  const w = world({ list: [] });
  w.io.fetchRaw = async () => ({ status: 200, json: [item(`u${n++}`, 40 - n * 0.001)] });
  await assert.rejects(runSync(w.io, { ...SETTINGS, max_pages: 5 }, null), (e) => e.status === "error");
});

test("loginExpiryField reports the cookie's expiry as UTC ISO, never its value", () => {
  const f = loginExpiryField({ name: "sessionKey", value: "SECRET", expirationDate: 1898154900.5 });
  assert.deepEqual(f, { login_expires_at: "2030-02-24T09:15:00.500Z" });
  assert.ok(!JSON.stringify(f).includes("SECRET"));
});

test("loginExpiryField: no cookie means no login (null); unknown means omit", () => {
  assert.deepEqual(loginExpiryField(null), { login_expires_at: null });
  // A session-only cookie has no expiry to warn about; omit rather than guess.
  assert.deepEqual(loginExpiryField({ name: "sessionKey", session: true }), {});
  assert.deepEqual(loginExpiryField({ name: "sessionKey", expirationDate: "x" }), {});
  assert.deepEqual(loginExpiryField(undefined), {});  // the cookie read itself failed
});
