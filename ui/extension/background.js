// background.js — service worker. Polls the bridge server, dispatches to a
// chosen claude.ai tab (tab mode) or to the Claude side-panel extension via
// chrome.debugger / CDP (sidepanel mode), collects replies, posts them back.

import { nextChecks, pickTabs, hasVisiblePasswordField } from "./login_watch.js";

const BRIDGE_URL = "http://127.0.0.1:7777";
const POLL_INTERVAL_MS = 1000;
const POLL_BACKOFF_MS = 5000;
const TABS_PUSH_INTERVAL_MS = 3000;

const CLAUDE_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"; // Anthropic's "Claude in Chrome (Beta)"
const SIDEPANEL_PROTO = "1.3";

// ────────────────────────────────────────────────────────────────────────
// chrome.debugger helpers (for sidepanel mode)
// ────────────────────────────────────────────────────────────────────────

async function findClaudeSidePanelTarget() {
  const targets = await chrome.debugger.getTargets();
  return targets.find(t =>
    t.extensionId === CLAUDE_EXTENSION_ID &&
    (t.url || "").includes("sidepanel.html")
  ) || null;
}

async function attachIfNeeded(target) {
  // target = { targetId } object passed back to chrome.debugger.* calls
  try {
    await chrome.debugger.attach(target, SIDEPANEL_PROTO);
  } catch (e) {
    // "Another debugger is already attached" — fine, reuse.
    if (!String(e?.message || e).includes("Another debugger")) throw e;
  }
}

async function evalInTarget(target, expression, { awaitPromise = false } = {}) {
  const r = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
    expression,
    awaitPromise,
    returnByValue: true,
  });
  if (r?.exceptionDetails) {
    throw new Error("eval threw: " + (r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails)));
  }
  return r?.result?.value;
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(`timeout after ${ms}ms: ${label}`)), ms)),
  ]);
}

async function discoverSidePanel() {
  let allTargets = [];
  try {
    allTargets = await withTimeout(chrome.debugger.getTargets(), 3000, "getTargets");
  } catch (e) {
    return { ok: false, error: `getTargets failed: ${e.message}` };
  }
  // Always include a snapshot of every target so the user can see what's visible.
  const summary = allTargets.map(t => ({
    type: t.type,
    title: (t.title || "").slice(0, 80),
    url: (t.url || "").slice(0, 120),
    extensionId: t.extensionId || null,
    targetId: t.id,
    attached: !!t.attached,
  }));

  const claudeTargets = allTargets.filter(t => t.extensionId === CLAUDE_EXTENSION_ID);
  const sp = claudeTargets.find(t => (t.url || "").includes("sidepanel.html"));
  if (!sp) {
    return {
      ok: false,
      error: "Claude side panel is not exposed as a debugger target. " +
             "Make sure the side panel is OPEN (orange Claude icon clicked, panel visible). " +
             "If it is open, the panel may be hibernating — type something in it once and retry.",
      claude_targets_found: claudeTargets.length,
      all_targets_summary: summary,
    };
  }
  const target = { targetId: sp.id };
  try {
    await withTimeout(attachIfNeeded(target), 5000, "debugger.attach");
  } catch (e) {
    return { ok: false, error: `attach failed: ${e.message}`, target: sp };
  }
  try {
    const dump = await withTimeout(evalInTarget(target, `
      (() => {
        const summarize = (el, max=240) => {
          const attrs = {};
          for (const a of Array.from(el.attributes || [])) attrs[a.name] = (a.value || '').slice(0, 120);
          return {
            tag: el.tagName?.toLowerCase?.(),
            id: el.id || undefined,
            classes: (el.className?.toString?.() || '').slice(0, 200),
            attrs,
            text_preview: (el.innerText || el.textContent || '').slice(0, max).trim(),
          };
        };
        const out = { url: location.href, title: document.title, body_html_len: document.body?.innerHTML?.length || 0 };
        out.inputs = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')).slice(0, 10).map(summarize);
        out.buttons = Array.from(document.querySelectorAll('button')).slice(0, 30).map(b => ({
          text: (b.innerText || '').slice(0, 60),
          ariaLabel: b.getAttribute('aria-label') || '',
          type: b.type, disabled: b.disabled,
          classes: (b.className?.toString?.() || '').slice(0, 120),
        }));
        out.message_like = Array.from(document.querySelectorAll('[role="article"], [data-testid*="message" i], [class*="message" i], [class*="conversation" i]')).slice(0, 12).map(el => summarize(el, 120));
        return out;
      })()
    `), 8000, "Runtime.evaluate dump");
    return { ok: true, dump, target: sp };
  } catch (e) {
    return { ok: false, error: `evaluate failed: ${e.message}`, target: sp };
  } finally {
    try { await chrome.debugger.detach(target); } catch (_) {}
  }
}

// Context-window usage is NOT read here. Chrome refuses to let one extension
// attach the debugger to another extension's page ("Cannot access a
// chrome-extension:// URL of different extension"), so the bridge extension
// cannot read the Claude side panel. daemon/ctx_agent.js does it from the
// Windows side over CDP and posts to /panels/ctx; the popup reads it there.

let pollTimer = null;
let tabsPushTimer = null;
let controlPollTimer = null;

async function getToken() {
  const { token } = await chrome.storage.local.get("token");
  return token || "";
}
async function setToken(token) { await chrome.storage.local.set({ token }); }

async function bridgeFetch(path, init = {}) {
  const token = await getToken();
  const headers = new Headers(init.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${BRIDGE_URL}${path}`, { ...init, headers });
}

async function setBadge(text, color = "#888") {
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
}

async function listClaudeTabs() {
  // Despite the name (kept for compat), this lists all https:// tabs we *could*
  // inject into. http:// is excluded to avoid noise; chrome:// is unreachable.
  const tabs = await chrome.tabs.query({ url: "https://*/*" });
  return tabs.map(t => ({
    id: t.id,
    title: t.title || "",
    url: t.url || "",
    active: !!t.active,
    windowId: t.windowId,
  }));
}

async function chooseTabForItem(item) {
  const tabs = await listClaudeTabs();
  if (!tabs.length) return null;
  if (item.tab_id != null) {
    const t = tabs.find(t => t.id === item.tab_id);
    if (t) return t;
    return null; // explicit id requested but missing — fail loudly
  }
  if (item.tab_match) {
    const needle = item.tab_match.toLowerCase();
    const t = tabs.find(t =>
      (t.url || "").toLowerCase().includes(needle) ||
      (t.title || "").toLowerCase().includes(needle));
    if (t) return t;
    return null;
  }
  // Fallback: prefer active claude.ai tab in focused window
  return tabs.find(t => t.active) || tabs[0];
}

async function pushTabs() {
  try {
    const tabs = await listClaudeTabs();
    await bridgeFetch("/tabs", { method: "POST", body: JSON.stringify({ tabs }) });
  } catch (_) { /* ignore — server may be down */ }
}

async function pushDiscoveredPanels() {
  try {
    const [groups, tabs, targets] = await Promise.all([
      chrome.tabGroups.query({}),
      chrome.tabs.query({}),
      // CDP target ids, so a popup's Open can call /json/activate/<id> on the
      // exact tab (RQ4 K5). getTargets does not attach, so no debug infobar.
      chrome.debugger.getTargets().catch(() => []),
    ]);
    const targetByTab = {};
    for (const t of targets) if (t.type === "page" && t.tabId != null) targetByTab[t.tabId] = t.id;
    const tabsByGroup = {};
    for (const t of tabs) {
      if (t.groupId != null && t.groupId !== -1) (tabsByGroup[t.groupId] ||= []).push(t);
    }
    checkLoginWalls(groups, tabsByGroup, targetByTab);
    const items = groups.map(g => {
      const gt = (tabsByGroup[g.id] || [])[0];
      return {
        group_title: g.title || "",
        group_id: g.id,
        tab_id: gt?.id,
        target_id: gt ? targetByTab[gt.id] || null : null,
        // Every tab of the group, in strip order, with its CDP target id. The
        // first tab is not always the panel's own tab: an agent can open a
        // tab before it. The server picks the tab that hosts the panel.
        tab_ids: (tabsByGroup[g.id] || []).map(t => t.id),
        target_ids: Object.fromEntries((tabsByGroup[g.id] || [])
          .filter(t => targetByTab[t.id]).map(t => [t.id, targetByTab[t.id]])),
        tab_title: gt?.title || "",
        // chrome.tabGroups is extension-only; forwarding the colour lets the
        // localhost dashboard tint its dots the same way the popup does.
        group_color: g.color || null,
        // Collapsed groups are hidden in the tab strip. Forwarding this lets
        // both surfaces show whether a chat is currently open.
        group_collapsed: !!g.collapsed,
      };
    }).filter(x => x.tab_id != null);
    await bridgeFetch("/panels/discovered", { method: "POST", body: JSON.stringify({ items }) });
  } catch (_) { /* server may be down */ }
}

async function poll() {
  let nextDelay = POLL_INTERVAL_MS;
  try {
    const r = await bridgeFetch("/poll?mode=tab");
    if (r.status === 204) {
      await setBadge("", "#888");
    } else if (r.status === 401) {
      await setBadge("AUTH", "#c33");
    } else if (r.ok) {
      const item = await r.json();
      await setBadge("•", "#3a3");
      await dispatchToClaude(item);
    } else {
      await setBadge("ERR", "#c33");
    }
  } catch (_) {
    await setBadge("OFF", "#888");
    nextDelay = POLL_BACKOFF_MS;
  }
  pollTimer = setTimeout(poll, nextDelay);
}

async function dispatchToClaude(item) {
  const tab = await chooseTabForItem(item);
  if (!tab) {
    const reason = item.tab_id != null
      ? `tab_id ${item.tab_id} not found`
      : item.tab_match
        ? `no claude.ai tab matches "${item.tab_match}"`
        : "no claude.ai tab open";
    await postReply(item.id, item.from_pane, `[bridge error] ${reason}. Open https://claude.ai or use 'claude-web tabs' to list.`);
    return;
  }
  try {
    // Ensure the content script is loaded (idempotent: content.js has a guard).
    // Necessary for tabs that were open before the extension was loaded/reloaded.
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
    } catch (injErr) {
      // Some pages (chrome://, login, etc.) refuse injection — surface clearly.
      await postReply(item.id, item.from_pane,
        `[bridge error] cannot inject into tab ${tab.id} "${tab.title}": ${injErr.message}`);
      return;
    }

    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (msg) => window.__claudeBridge?.send?.(msg) ?? { ok: false, error: "content script not ready" },
      args: [item],
    });
    if (!result?.ok) {
      await postReply(item.id, item.from_pane,
        `[bridge error] content script (tab ${tab.id} "${tab.title}"): ${result?.error || "unknown"}`);
    }
    // success path: content script will sendMessage 'reply' when done
  } catch (e) {
    await postReply(item.id, item.from_pane, `[bridge error] dispatch to tab ${tab.id} failed: ${e.message}`);
  }
}

async function renameBookmarkFolder(fromTitle, toTitle) {
  const matches = (await chrome.bookmarks.search({ title: fromTitle }))
    .filter(b => b.title === fromTitle);
  if (matches.length === 0) {
    return { ok: false, error: `no bookmark or folder titled "${fromTitle}"` };
  }
  if (matches.length > 1) {
    return {
      ok: false,
      error: `${matches.length} items match "${fromTitle}" — be more specific`,
      candidates: matches.map(b => ({ id: b.id, isFolder: !b.url, url: b.url || null, parentId: b.parentId })),
    };
  }
  const b = matches[0];
  await chrome.bookmarks.update(b.id, { title: toTitle });
  return {
    ok: true,
    id: b.id,
    parentId: b.parentId,
    isFolder: !b.url,
    url: b.url || null,
    oldTitle: fromTitle,
    newTitle: toTitle,
  };
}

async function searchBookmarks(query) {
  const matches = await chrome.bookmarks.search(query || "");
  return matches.slice(0, 50).map(b => ({
    id: b.id,
    title: b.title,
    url: b.url || null,
    isFolder: !b.url,
    parentId: b.parentId,
  }));
}

async function listTabGroups() {
  const groups = await chrome.tabGroups.query({});
  return groups.map(g => ({
    id: g.id,
    title: g.title || "",
    color: g.color,
    collapsed: g.collapsed,
    windowId: g.windowId,
  }));
}

async function renameTabGroup(fromTitle, toTitle, tabId) {
  // tab_id (when provided) trumps title matching. Resolves the tab's
  // current tabGroupId and renames THAT specific group, side-stepping
  // ambiguity when multiple groups share the same title.
  if (tabId != null && Number.isFinite(Number(tabId))) {
    let tab;
    try { tab = await chrome.tabs.get(Number(tabId)); }
    catch (e) { return { ok: false, error: `tab_id ${tabId} not found: ${e.message}` }; }
    const gid = tab.groupId;
    if (gid == null || gid === -1) {
      return { ok: false, error: `tab ${tabId} is not in any tab group — add it to a group first` };
    }
    const before = await chrome.tabGroups.get(gid);
    await chrome.tabGroups.update(gid, { title: toTitle });
    return { ok: true, id: gid, oldTitle: before.title || "", newTitle: toTitle, via: "tab_id" };
  }
  const groups = await chrome.tabGroups.query({});
  const matches = groups.filter(g => (g.title || "") === fromTitle);
  if (matches.length === 0) {
    return { ok: false, error: `no tab group titled "${fromTitle}"`, available: groups.map(g => g.title) };
  }
  if (matches.length > 1) {
    return {
      ok: false,
      error: `${matches.length} tab groups match "${fromTitle}" — pass tab_id to disambiguate`,
      candidates: matches.map(g => g.id),
    };
  }
  const g = matches[0];
  await chrome.tabGroups.update(g.id, { title: toTitle });
  return { ok: true, id: g.id, oldTitle: fromTitle, newTitle: toTitle, via: "title" };
}

async function closeTabs(tabIds) {
  // Filter to ones that still exist so a partially-stale list still closes
  // what's closable without aborting the whole batch.
  const ids = Array.from(new Set((tabIds || []).map(Number).filter(n => Number.isFinite(n))));
  const stillOpen = [];
  for (const id of ids) {
    try { await chrome.tabs.get(id); stillOpen.push(id); } catch (_) { /* gone */ }
  }
  if (stillOpen.length === 0) {
    return { ok: true, closed: 0, requested: ids.length, note: "no matching tabs were open" };
  }
  await chrome.tabs.remove(stillOpen);
  return { ok: true, closed: stillOpen.length, requested: ids.length };
}

async function closeAllWindows() {
  const wins = await chrome.windows.getAll();
  for (const w of wins) {
    try { await chrome.windows.remove(w.id); } catch (_) { /* ignore */ }
  }
  return { ok: true, closed: wins.length };
}

async function dispatchControl(item) {
  let result;
  try {
    if (item.kind === "bookmark.rename") {
      result = await renameBookmarkFolder(item.from_title, item.to_title);
    } else if (item.kind === "bookmark.search") {
      const items = await searchBookmarks(item.query || "");
      result = { ok: true, count: items.length, items };
    } else if (item.kind === "tabGroup.rename") {
      // Two forms:
      //   1. legacy (from_title, to_title[, tab_id]) — used by `browser-bridge name`
      //   2. new   (tab_id, to_title, color?)        — used by rename-last-tab,
      //      which knows the tab_id directly and never had a from_title.
      // Distinguish by whether a from_title was supplied. If absent, do an
      // in-place chrome.tabGroups.update on the tab's current group (and
      // optionally recolor it) — no title search needed.
      if (item.tab_id != null && (item.from_title == null || item.from_title === "")) {
        try {
          const tab = await chrome.tabs.get(Number(item.tab_id));
          if (tab.groupId == null || tab.groupId === -1) {
            result = { ok: false, error: `tab ${item.tab_id} is not in a group yet (no chat created?)` };
          } else {
            const updateOpts = { title: item.to_title };
            if (item.color) updateOpts.color = item.color;
            await chrome.tabGroups.update(tab.groupId, updateOpts);
            result = { ok: true, id: tab.groupId };
          }
        } catch (e) {
          result = { ok: false, error: e.message };
        }
      } else {
        result = await renameTabGroup(item.from_title, item.to_title, item.tab_id);
      }
    } else if (item.kind === "tabGroup.list") {
      const items = await listTabGroups();
      result = { ok: true, count: items.length, items };
    } else if (item.kind === "tabs.close") {
      result = await closeTabs(item.tab_ids || []);
    } else if (item.kind === "windows.closeAll") {
      result = await closeAllWindows();
    } else if (item.kind === "tabs.create") {
      // Open a fresh tab. Default = blank new-tab page (chrome://newtab/) so
      // the operator gets the look in the design mockup. If item.url is set,
      // honour it. The operator then clicks the Claude for Chrome extension
      // icon to open the side panel — we can't open another extension's panel
      // programmatically (Chrome security boundary).
      //
      // RULE: bring Chrome to the foreground after creating the tab so the
      // operator sees the new tab immediately. Without this, Chrome may stay
      // hidden behind the terminal that issued `tmux-new-tab` and the
      // operator has to alt-tab to find the new tab — defeats the purpose.
      try {
        const opts = { active: true };
        if (item.url) opts.url = item.url;
        const t = await chrome.tabs.create(opts);
        // Force Chrome to the foreground. A bare {focused:true} is silently
        // refused by Windows' focus-stealing prevention. The reliable
        // workaround is minimize → restore: Windows treats the restore as
        // a legitimate foreground request and brings the window forward.
        try {
          await chrome.windows.update(t.windowId, { state: "minimized" });
          await chrome.windows.update(t.windowId, { state: "normal", focused: true, drawAttention: true });
        } catch (_focusErr) {
          // Platforms that refuse both still get the tab opened — just no
          // foreground change. Drop a drawAttention hint so the taskbar
          // icon flashes at minimum.
          try { await chrome.windows.update(t.windowId, { drawAttention: true }); } catch (_) {}
        }
        result = { ok: true, tab_id: t.id, window_id: t.windowId };
      } catch (e) {
        result = { ok: false, error: e.message };
      }
    } else if (item.kind === "tabs.focus") {
      // Bring an existing tab to the foreground. Smoothness rule:
      //
      //   1. If the target tab's window IS the currently-focused window
      //      (operator is already in bridge Chrome viewing the dashboard
      //      in another tab of the same window), JUST activate the tab.
      //      Chrome's native tab-switch is silky smooth, no flicker.
      //
      //   2. If the target window is NOT focused but EXISTS, try a soft
      //      focus first: chrome.windows.update({focused:true,
      //      drawAttention:true}). Windows may refuse the foreground
      //      change due to focus-stealing prevention; the drawAttention
      //      flash gives a taskbar hint either way.
      //
      //   3. Only if BOTH (a) cross-window AND (b) the soft focus was
      //      refused (we can't detect refusal reliably, so this is
      //      best-effort), fall back to the minimize→restore "trick".
      //      That's the choppy flicker — we now skip it in the common
      //      same-window case.
      try {
        const tab = await chrome.tabs.update(item.tab_id, { active: true });
        const targetWindowId = tab.windowId;
        const lastFocused = await chrome.windows.getLastFocused({ populate: false });
        if (lastFocused && lastFocused.id === targetWindowId && lastFocused.focused) {
          // Same window, already focused — pure tab switch, no flicker.
          result = { ok: true, tab_id: tab.id, window_id: targetWindowId, mode: "same-window" };
        } else {
          // Cross-window or window minimized — soft focus only.
          try {
            await chrome.windows.update(targetWindowId, { focused: true, drawAttention: true });
            result = { ok: true, tab_id: tab.id, window_id: targetWindowId, mode: "soft-focus" };
          } catch (e) {
            // Last resort: drawAttention only (taskbar flash).
            try { await chrome.windows.update(targetWindowId, { drawAttention: true }); } catch (_) {}
            result = { ok: true, tab_id: tab.id, window_id: targetWindowId, mode: "draw-attention" };
          }
        }
      } catch (e) {
        result = { ok: false, error: e.message };
      }
    } else {
      result = { ok: false, error: `unknown control kind: ${item.kind}` };
    }
  } catch (e) {
    result = { ok: false, error: e.message };
  }
  let text;
  if (item.kind === "bookmark.rename") {
    text = result.ok
      ? `[control:ok] bookmark.rename "${item.from_title}" -> "${item.to_title}" (id=${result.id}, folder=${result.isFolder})`
      : `[control:err] bookmark.rename: ${result.error}`;
  } else if (item.kind === "bookmark.search") {
    text = result.ok
      ? `[control:ok] bookmark.search "${item.query || ''}" (${result.count} results): ` +
        result.items.map(i => `[${i.isFolder ? "F" : "B"}] "${i.title}"${i.url ? " " + i.url : ""}`).join(" | ")
      : `[control:err] bookmark.search: ${result.error}`;
  } else if (item.kind === "tabGroup.rename") {
    text = result.ok
      ? `[control:ok] tabGroup.rename ${item.tab_id && (item.from_title == null || item.from_title === "") ? `tab_id=${item.tab_id} ` : `"${item.from_title}" -> `}"${item.to_title}" (id=${result.id})${item.color ? ` color=${item.color}` : ""}`
      : `[control:err] tabGroup.rename: ${result.error}` +
        (result.available ? ` (available: ${result.available.map(t => `"${t}"`).join(", ")})` : "");
  } else if (item.kind === "tabGroup.list") {
    text = result.ok
      ? `[control:ok] tabGroup.list (${result.count}): ` +
        result.items.map(g => `id=${g.id} "${g.title}" (${g.color})`).join(" | ")
      : `[control:err] tabGroup.list: ${result.error}`;
  } else if (item.kind === "tabs.close") {
    text = result.ok
      ? `[control:ok] tabs.close closed=${result.closed} requested=${result.requested}` + (result.note ? ` (${result.note})` : "")
      : `[control:err] tabs.close: ${result.error}`;
  } else if (item.kind === "windows.closeAll") {
    text = result.ok
      ? `[control:ok] windows.closeAll closed=${result.closed}`
      : `[control:err] windows.closeAll: ${result.error}`;
  } else if (item.kind === "tabs.create") {
    text = result.ok
      ? `[control:ok] tabs.create tab_id=${result.tab_id} url=${item.url || "(blank)"}`
      : `[control:err] tabs.create: ${result.error}`;
  } else if (item.kind === "tabs.focus") {
    text = result.ok
      ? `[control:ok] tabs.focus tab_id=${result.tab_id} window_id=${result.window_id}`
      : `[control:err] tabs.focus: ${result.error}`;
  } else {
    text = result.ok ? `[control:ok] ${item.kind}` : `[control:err] ${item.kind}: ${result.error}`;
  }
  await postReply(item.id, item.from_pane, text);
}

// groupId -> {busyAt, checked}; see login_watch.js.
const loginWatch = new Map();

// When an agent's group has been idle a minute after working, check its own
// tabs once for a visible password field and tell the server which site.
// Presence only, never a value. Runs in the background of the 3 s push.
async function checkLoginWalls(groups, tabsByGroup, targetByTab) {
  for (const gid of nextChecks(loginWatch, groups, Date.now())) {
    const groupTabs = tabsByGroup[gid] || [];
    for (const t of pickTabs(groupTabs)) {
      let found = false;
      try {
        const res = await chrome.scripting.executeScript({
          target: { tabId: t.id, allFrames: true },
          func: hasVisiblePasswordField,
        });
        found = res.some(r => r.result === true);
      } catch (_) {
        continue;  // error page, blocked host, tab closed meanwhile
      }
      if (found) {
        try {
          await bridgeFetch("/panels/password-field", {
            method: "POST",
            // The server resolves which of the group's tabs hosts the panel.
            // target_id: the page with the field, so the popup's Open lands on it.
            body: JSON.stringify({ tab_id: groupTabs[0].id, tab_ids: groupTabs.map(x => x.id),
                                   group_id: gid, host: new URL(t.url).hostname,
                                   target_id: targetByTab[t.id] || null }),
          });
        } catch (_) { /* server may be down */ }
        break;
      }
    }
  }
}

async function pollControl() {
  let nextDelay = POLL_INTERVAL_MS;
  try {
    const r = await bridgeFetch("/poll?mode=control");
    if (r.status === 204) {
      // nothing
    } else if (r.ok) {
      const item = await r.json();
      await dispatchControl(item);
    }
  } catch (_) {
    nextDelay = POLL_BACKOFF_MS;
  }
  controlPollTimer = setTimeout(pollControl, nextDelay);
}

async function postReply(id, from_pane, text) {
  try {
    await bridgeFetch("/reply", {
      method: "POST",
      body: JSON.stringify({ id, from_pane, text }),
    });
  } catch (e) { console.error("[bridge] postReply failed:", e); }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "reply") {
    postReply(msg.id, msg.from_pane, msg.text).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg?.type === "set-token") {
    setToken(msg.token).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg?.type === "list-tabs") {
    listClaudeTabs().then(tabs => sendResponse({ tabs }));
    return true;
  }
  if (msg?.type === "discover-sidepanel") {
    discoverSidePanel().then(r => sendResponse(r));
    return true;
  }
  return false;
});

function startLoops() {
  if (!pollTimer) poll();
  if (!controlPollTimer) pollControl();
  if (!tabsPushTimer) {
    const tick = () => { pushTabs(); pushDiscoveredPanels(); tabsPushTimer = setTimeout(tick, TABS_PUSH_INTERVAL_MS); };
    tick();
  }
}

chrome.runtime.onInstalled.addListener(startLoops);
chrome.runtime.onStartup.addListener(startLoops);
chrome.tabs.onUpdated.addListener(() => pushTabs());
chrome.tabs.onRemoved.addListener((tabId) => {
  pushTabs();
  // Auto-forget: if the closed tab was a registered panel, drop it from
  // panels.json so the popup doesn't keep showing it as a red stale row.
  // Best effort — server returns 404 for non-panel tabs, which is fine.
  bridgeFetch("/panels/forget", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab_id: tabId }),
  }).catch(() => { /* ignore — tab wasn't a registered panel */ });
});
chrome.tabs.onActivated.addListener(() => pushTabs());
startLoops();


// Media fade (media_fade.js, media_fade_state.js). Keeps videos on these
// sites playing while Chrome is in the background and lowers their volume.
// The popup's FADE button turns it on or off (storage key mediaFade, on by
// default) and open tabs follow at once. The scripts must run before the
// site's own code, so they are registered here instead of in manifest.json.
const MEDIA_FADE_SITES = ["*://*.tiktok.com/*", "*://*.youtube.com/*"];

async function registerMediaFade() {
  // Media fade is the only script this extension registers at runtime.
  const registered = await chrome.scripting.getRegisteredContentScripts();
  if (registered.length) {
    await chrome.scripting.unregisterContentScripts({ ids: registered.map(s => s.id) });
  }
  const base = { matches: MEDIA_FADE_SITES, runAt: "document_start", allFrames: true };
  await chrome.scripting.registerContentScripts([
    { ...base, id: "media-fade", js: ["media_fade.js"], world: "MAIN" },
    { ...base, id: "media-fade-state", js: ["media_fade_state.js"], world: "ISOLATED" },
  ]);
}

// on: the FADE switch. bg: no Chrome window has focus.
async function mediaFadeState() {
  const { mediaFade = true } = await chrome.storage.local.get("mediaFade");
  const wins = await chrome.windows.getAll();
  return { on: mediaFade, bg: !wins.some(w => w.focused) };
}

async function sendMediaFadeState() {
  const msg = { type: "media-fade-state", ...(await mediaFadeState()) };
  for (const t of await chrome.tabs.query({ url: MEDIA_FADE_SITES })) {
    chrome.tabs.sendMessage(t.id, msg).catch(() => {});
  }
}

chrome.windows.onFocusChanged.addListener(() => sendMediaFadeState());

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "mediaFade" in changes) sendMediaFadeState();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type !== "media-fade-state?") return false;
  mediaFadeState().then(sendResponse);
  return true;
});

registerMediaFade().catch(console.error);
