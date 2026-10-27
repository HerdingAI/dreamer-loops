// Dreamer Sync service worker: a thin shell around lib.js. All decisions live
// in lib.js (tested with node --test); this file only wires chrome.* APIs.
import { runSync, SyncError, loginExpiryField } from "./lib.js";

const HOST = "com.dreamer.claude_ai_sync";
const ALARM = "dreamer-sync-tick";
let running = false;

// --- native host: one persistent port per run (the host keeps a spool) ------
function openHost() {
  const port = chrome.runtime.connectNative(HOST);
  const pending = new Map();
  let seq = 0;
  let dead = null;
  port.onMessage.addListener((msg) => {
    const p = pending.get(msg && msg.id);
    if (p) { pending.delete(msg.id); p.resolve(msg); }
  });
  port.onDisconnect.addListener(() => {
    dead = (chrome.runtime.lastError && chrome.runtime.lastError.message) || "host disconnected";
    for (const p of pending.values()) p.reject(new Error(dead));
    pending.clear();
  });
  return {
    send(msg) {
      if (dead) return Promise.reject(new Error(dead));
      const id = ++seq;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        try { port.postMessage({ ...msg, id }); } catch (e) { pending.delete(id); reject(e); }
      });
    },
    close() { try { port.disconnect(); } catch (_) { /* already gone */ } },
  };
}

// --- claude.ai I/O -----------------------------------------------------------
async function getOrg() {
  const c = await chrome.cookies.get({ url: "https://claude.ai", name: "lastActiveOrg" });
  return c && c.value ? c.value : null;
}

// Reads only the cookie's metadata for its expiry; loginExpiryField drops the value.
async function getLoginCookie() {
  try {
    return await chrome.cookies.get({ url: "https://claude.ai", name: "sessionKey" });
  } catch (_) {
    return undefined;  // unknown: report nothing rather than "logged out"
  }
}

async function fetchRaw(url) {
  const res = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
  const contentType = res.headers.get("content-type") || "";
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch (_) { json = undefined; }
  const head = text.slice(0, 200).trimStart().toLowerCase();
  return {
    status: res.status,
    isHtml: contentType.includes("text/html") || head.startsWith("<!doctype") || head.startsWith("<html"),
    redirectedToLogin: res.redirected && /\/(login|logout)\b/.test(new URL(res.url).pathname),
    cfMitigated: res.headers.get("cf-mitigated") === "challenge",
    retryAfter: res.headers.get("retry-after"),
    json,
  };
}

// --- one run -----------------------------------------------------------------
async function run({ force }) {
  if (running) return;
  running = true;
  let host = null;
  try {
    host = openHost();
    const st = await host.send({ cmd: "status", ...loginExpiryField(await getLoginCookie()) });
    if (!st.ok) throw new Error(`status failed: ${st.error}`);
    if (!force && !st.due) { await note("idle", "not due"); return; }
    await note("running", "");
    try {
      const out = await runSync({
        getOrg, fetchRaw, host: (m) => host.send(m),
        sleep: (ms) => new Promise((r) => setTimeout(r, ms)), now: () => Date.now(),
      }, st.settings, st.cursor);
      await note("ok", `fetched ${out.delivered}`);
    } catch (e) {
      const status = e instanceof SyncError ? e.status : "error";
      const detail = e instanceof SyncError ? e.detail : String(e && e.message || e);
      await host.send({ cmd: "report", status, detail }).catch(() => {});
      await note(status, detail);
    }
  } catch (e) {
    // The host itself is unreachable (not installed, crashed): nowhere to report to.
    await note("host_error", String(e && e.message || e));
  } finally {
    if (host) host.close();
    running = false;
  }
}

async function note(status, detail) {
  const bad = !["ok", "idle", "running"].includes(status);
  await chrome.action.setBadgeText({ text: bad ? "!" : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#b3261e" });
  await chrome.storage.local.set({ lastResult: { status, detail, at: new Date().toISOString() } });
}

// --- triggers ----------------------------------------------------------------
function ensureAlarm() {
  chrome.alarms.get(ALARM, (a) => { if (!a) chrome.alarms.create(ALARM, { periodInMinutes: 60 }); });
}
ensureAlarm();
chrome.runtime.onInstalled.addListener(ensureAlarm);
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); run({ force: false }); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) run({ force: false }); });
chrome.action.onClicked.addListener(() => run({ force: true }));
