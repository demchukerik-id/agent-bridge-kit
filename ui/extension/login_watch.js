// login_watch.js — when a browser agent goes idle, look once for a password
// field in the tabs of its own tab group (agent-system plan A2, RQ5 (c)2).
//
// Presence only: the injected function returns true or false and never reads
// a field's value. Only groups Claude for Chrome has marked as working (⌛ or
// 🔔 title prefix) are watched, so the user's own tab groups are never touched.
// Pure functions, no chrome.* here, so tests/login-watch.test.js can load it.

export const IDLE_MS = 60000;      // idle this long after the last ⌛/🔔 before checking
export const MAX_TABS = 5;         // tabs checked per group, most recently used first
const WORKING_PREFIXES = ["⌛", "\u{1F514}"];  // ⌛ 🔔

// state: Map groupId -> {busyAt, checked}. Returns the group ids to check now.
export function nextChecks(state, groups, now) {
  const out = [];
  const seen = new Set();
  for (const g of groups) {
    seen.add(g.id);
    const title = g.title || "";
    if (WORKING_PREFIXES.some(p => title.startsWith(p))) {
      state.set(g.id, { busyAt: now, checked: false });
      continue;
    }
    const s = state.get(g.id);
    if (s && !s.checked && now - s.busyAt >= IDLE_MS) {
      s.checked = true;
      out.push(g.id);
    }
  }
  for (const id of [...state.keys()]) if (!seen.has(id)) state.delete(id);
  return out;
}

// Web pages of the group worth checking: http(s), not discarded, most
// recently used first.
export function pickTabs(tabs) {
  return tabs
    .filter(t => /^https?:\/\//.test(t.url || "") && !t.discarded)
    .sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))
    .slice(0, MAX_TABS);
}

// Injected with chrome.scripting.executeScript. Must stay self-contained.
export function hasVisiblePasswordField() {
  for (const el of document.querySelectorAll("input[type=password]")) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none") return true;
  }
  return false;
}
