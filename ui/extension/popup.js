const BRIDGE_URL = "http://127.0.0.1:7777";
const $ = (id) => document.getElementById(id);
// The status line was removed from the panel; keep the calls harmless.
const setFooter = (text) => { const el = $("footer"); if (el) el.textContent = text; };

// Chrome tab-group palette → CSS color. Used by renderBrowserAgents() so the
// status dot for each row mirrors the Chrome tab-group color, making it easy
// to correlate a side-panel row with a tab in the strip.
const TG_COLOR_CSS = {
  // Chrome's own tab-group palette, sampled pixel-for-pixel from the
  // colour picker in Chrome's dark theme. These are the "300" tones Chrome
  // paints group chips with; the older 500-tone values (#4285f4 etc.) are
  // visibly darker and more saturated than what the tab strip shows.
  grey:   "#dadce0",
  blue:   "#8ab4f8",
  red:    "#f28b82",
  yellow: "#fdd663",
  green:  "#81c995",
  pink:   "#ff8bcb",
  purple: "#c58af9",
  cyan:   "#78d9ec",
  orange: "#fcad70",
};

async function token() {
  const { token } = await chrome.storage.local.get("token");
  return token || "";
}

async function bridgeFetch(path, init = {}) {
  const tok = await token();
  const headers = new Headers(init.headers || {});
  if (tok) headers.set("Authorization", `Bearer ${tok}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(`${BRIDGE_URL}${path}`, { ...init, headers });
}

// Reads storage directly and reports what is really there. If this says
// "none" right after a save, the write is being rejected — which is a very
// different problem from the field simply not being repopulated.
async function showTokenState() {
  const el = $("token-state");
  if (!el) return;
  try {
    const { token } = await chrome.storage.local.get("token");
    if (token) {
      el.textContent = `stored: ${String(token).slice(0, 10)}… (${String(token).length} chars)`;
      el.className = "token-state ok";
    } else {
      el.textContent = "stored: none";
      el.className = "token-state bad";
    }
  } catch (e) {
    el.textContent = `storage error: ${e.message}`;
    el.className = "token-state bad";
  }
}

async function load() {
  // Every step is isolated. This used to be a bare chain of awaits, so a
  // single rejection (or a hang on the first storage read) aborted the whole
  // init: the token field was never populated, the state line kept its "—"
  // placeholder, and the agent panel sat on "loading…" forever. That looked
  // exactly like the token had been lost when in fact it was never read.
  const step = async (name, fn) => {
    try { await fn(); }
    catch (e) {
      console.error(`[bridge] init step "${name}" failed:`, e);
      const el = $("token-state");
      if (el && name === "token") {
        el.textContent = `read failed: ${e.message}`;
        el.className = "token-state bad";
      }
    }
  };

  await step("token", async () => {
    const { token } = await chrome.storage.local.get("token");
    if (token) $("token").value = token;
    await showTokenState();
  });
  await step("inactivePref", loadInactiveOpenPref);
  await step("pinnedPanes", loadPinnedPanes);
  await step("refresh", refreshAll);
  setInterval(refreshAll, 3000);
}

async function save() {
  const tok = $("token").value.trim();
  // Persist from the popup itself FIRST. This used to delegate the only
  // write to the service worker via sendMessage; when that worker was
  // asleep or had thrown, the message was dropped and the token was never
  // stored. The panel still looked authenticated because refreshHealth
  // reads the input box rather than storage — so the loss only showed up
  // after the popup closed and the field came back empty.
  await chrome.storage.local.set({ token: tok });
  // Then tell the worker, best-effort: it keeps its own in-memory copy for
  // the polling loops. A failure here no longer costs us the token.
  try {
    await chrome.runtime.sendMessage({ type: "set-token", token: tok });
  } catch (e) {
    console.warn("[bridge] set-token message failed (token is stored anyway):", e);
  }
  const el = $("token");
  el.classList.add("saved");
  setTimeout(() => el.classList.remove("saved"), 700);
  await showTokenState();
  await refreshAll();
}

function setBadge(text, kind) {
  const b = $("status-badge");
  b.textContent = text;
  b.className = "badge" + (kind ? " " + kind : "");
}

async function refreshHealth() {
  try {
    const r = await fetch(`${BRIDGE_URL}/health`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const tok = $("token").value.trim();
    let authOk = !tok;
    if (tok) {
      const p = await fetch(`${BRIDGE_URL}/poll`, { headers: { Authorization: `Bearer ${tok}` } });
      authOk = p.status !== 401;
    }
    if (!authOk) {
      setBadge("auth", "err");
      setFooter("wrong token");
    } else {
      setBadge("ok", "ok");
      setFooter(`q:${j.queued} · pending:${j.pending_replies} · tabs:${j.tabs}`);
    }
  } catch (e) {
    setBadge("offline", "err");
    setFooter(`server: ${e.message}`);
  }
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

// %N short aliases come from the server's /panels/all (assign_short_names) so
// the popup and the `tmux-browser-bridge list` CLI agree on what %0/%1/...
// refer to. Previous implementation kept its own persistent map in
// chrome.storage, which produced popup %12 vs CLI %0 for the same panel
// (gap #16 in the 2026-05-11 onboarding test). The single source of truth
// is now the server.

const ACTIVE_WINDOW_S = 15;

async function refreshAgents() {
  const root = $("agents");

  // Local discovery — instant, doesn't depend on the server having received
  // an extension push yet. Server is consulted ONLY for the registered list
  // (panels.json) and live activity, then we merge with current tab groups locally.
  const [groups, tabs] = await Promise.all([
    chrome.tabGroups.query({}),
    chrome.tabs.query({}),
  ]);
  let registered = {};
  let activity = { streaming_tab_ids: [], working_tab_ids: [], last_active_tab: {}, now: Date.now() / 1000 };
  // serverShortByTabId: tab_id -> "%N" exactly as `tmux-browser-bridge list` shows it.
  // Used so popup and CLI never disagree (gap #16).
  let serverShortByTabId = {};
  try {
    const [rPanels, rAct, rAll] = await Promise.all([
      bridgeFetch("/panels"),
      bridgeFetch("/panels/activity"),
      bridgeFetch("/panels/all"),
    ]);
    if (rPanels.ok) registered = (await rPanels.json()).panels || {};
    if (rAct.ok) activity = await rAct.json();
    if (rAll.ok) {
      const all = await rAll.json();
      for (const p of (all.panels || [])) {
        if (p.tab_id != null && p.short) serverShortByTabId[p.tab_id] = p.short;
      }
    }
  } catch (_) { /* show discovered only if server is down */ }
  const collapsedSet = new Set(activity.collapsed_tab_ids || []);
  const streamingSet = new Set(activity.streaming_tab_ids || []);
  const workingSet = new Set(activity.working_tab_ids || []);
  const lastActive = activity.last_active_tab || {};
  const now = activity.now || Date.now() / 1000;

  const tabsByGroup = {};
  for (const t of tabs) {
    if (t.groupId != null && t.groupId !== -1) (tabsByGroup[t.groupId] ||= []).push(t);
  }
  const nameByTabId = {};
  for (const [name, p] of Object.entries(registered)) nameByTabId[p.tab_id] = name;

  const seenRegisteredNames = new Set();
  const registeredEntries = [];
  const discoveredEntries = [];

  for (const g of groups) {
    const groupTabs = tabsByGroup[g.id] || [];
    const tab = groupTabs[0];
    if (!tab) continue;
    let regName = null;
    for (const t of groupTabs) {
      if (nameByTabId[t.id]) { regName = nameByTabId[t.id]; seenRegisteredNames.add(regName); break; }
    }
    const entry = {
      kind: regName ? "registered" : "discovered",
      // Prefer the live Chrome tab-group title so the popup always matches
      // what the operator sees in the tab strip. Fall back to the registered
      // name only when Chrome reports an empty title (untitled group).
      name: g.title || regName || "(unnamed group)",
      tab_id: regName ? registered[regName].tab_id : tab.id,
      // Every tab in the group: the Claude side panel is bound to whichever
      // one it was opened on, which is not always the group's first tab.
      tab_ids: groupTabs.map(t => t.id),
      tab_title: tab.title || "",
      group_color: g.color,
    };
    (regName ? registeredEntries : discoveredEntries).push(entry);
  }

  // Stale: registered panels whose tab group is no longer present.
  const staleEntries = [];
  for (const [name, p] of Object.entries(registered)) {
    if (!seenRegisteredNames.has(name)) {
      staleEntries.push({ kind: "stale", name, tab_id: p.tab_id, tab_title: "tab not found" });
    }
  }

  registeredEntries.sort((a, b) => a.name.localeCompare(b.name));
  discoveredEntries.sort((a, b) => a.name.localeCompare(b.name));
  // Stale entries (registered panel whose Chrome tab is gone) are hidden from
  // the popup — they inflated the BROWSER AGENTS count even when zero tabs
  // were open. They still live in panels.json; `tmux-browser-bridge list`
  // surfaces them for cleanup.
  const unified = [...registeredEntries, ...discoveredEntries];

  // %N comes from the server's /panels/all so popup and CLI always agree.
  // Stale entries (registered but tab gone) won't be in the server map; show
  // a placeholder so the row still renders.
  unified.forEach(e => { e.short = serverShortByTabId[e.tab_id] || "%—"; });
  // Render in numeric short order so the list visually stays consistent.
  unified.sort((a, b) => {
    const ai = parseInt(String(a.short).slice(1));
    const bi = parseInt(String(b.short).slice(1));
    if (isNaN(ai) && isNaN(bi)) return 0;
    if (isNaN(ai)) return 1;
    if (isNaN(bi)) return -1;
    return ai - bi;
  });

  // Snapshot for the Tmux agents panel: which browser panels exist + their
  // tab_ids + names. We pass kind so renderTmuxAgents can filter to
  // registered only.
  panelsForTmuxRender = unified.map(e => ({ kind: e.kind, name: e.name, tab_id: e.tab_id, tab_ids: e.tab_ids || [] }));
  if (unified.length === 0) {
    root.innerHTML = `<div class="agent-row" style="color:var(--txt-dim2)">no chats open</div>`;
    renderTmuxAgents();
    return;
  }
  // Every claude.ai chat reports the same name, so a stack of them is
  // unreadable. Number duplicates (Claude 1, Claude 2, …) and cap how many
  // rows the panel will draw.
  const MAX_ROWS = 7;
  const nameCounts = {};
  unified.forEach(e => { nameCounts[e.name] = (nameCounts[e.name] || 0) + 1; });
  const nameSeen = {};
  unified.forEach(e => {
    if (nameCounts[e.name] > 1) {
      nameSeen[e.name] = (nameSeen[e.name] || 0) + 1;
      e.display_name = `${e.name} ${nameSeen[e.name]}`;
    } else {
      e.display_name = e.name;
    }
  });
  const overflow = Math.max(0, unified.length - MAX_ROWS);
  const shown = unified.slice(0, MAX_ROWS);

  root.innerHTML = shown.map(e => {
    let dotCls, tip;
    const everReplied = lastActive[e.tab_id] !== undefined;
    if (e.kind === "stale") {
      dotCls = "dead"; tip = "registered but tab closed";
    } else if (streamingSet.has(e.tab_id)) {
      dotCls = "responding"; tip = "responding (streaming reply)";
    } else if (workingSet.has(e.tab_id)) {
      dotCls = "active"; tip = "working (received task, waiting to respond)";
    } else if (e.kind === "discovered" && !everReplied) {
      // Yellow only while unproven. Once any reply has come back from this
      // tab, treat it like a regular idle agent — green dot.
      dotCls = "discovered"; tip = "discovered (not registered yet)";
    } else {
      dotCls = "idle"; tip = e.kind === "discovered" ? "discovered (proven — has replied at least once)" : "idle";
    }
    const title = e.tab_title || "—";

    // Per-row relay toggle: reflects the effective state for this tab.
    // The label adds "*" when the state comes from an explicit override
    // (vs. inheriting the default). Relay on/off is no longer a control in
    // this panel — it is folded into the pairing readout below: a browser
    // agent with relay off has no return path, so it renders as unlinked.
    const eff = effectiveEnabled(e.tab_id);

    // Pairing readout. The operator's question is "which terminal agent is
    // this browser agent talking back to?", so we render the effective
    // target as text instead of a dropdown. Routing itself is driven from
    // the terminal side; if nothing is bound, there is no back-relay and we
    // say so plainly.
    // Only a real, live tmux pane counts as a pairing. The server's default
    // target is a placeholder label, not a bound agent, so it is rendered as
    // nothing rather than as noise on every row.
    const curTarget = effectiveTarget(e.tab_id);
    const pane = curTarget ? availablePanes.find(p => p.pane_id === curTarget) : null;
    let linkText = "", linkCls = "agent-link off", linkTitle =
      "no terminal agent bound — replies are not relayed back";
    if (eff && pane) {
      const label = pane.label && pane.label !== "-" ? pane.label : "";
      linkText = label ? `${pane.pane_id} ${label}` : pane.pane_id;
      linkCls = "agent-link";
      linkTitle = `replies relay back to ${linkText}`;
    }

    // Context fill for this chat (see refreshContextUsage). Exact — it comes
    // from the usage the API reports for the turn, not from counting text.
    const ctx = ctxByTab[e.tab_id];
    const ctxText = (typeof ctx === "number") ? `${ctx}%` : "—";
    const ctxCls = "agent-ctx" +
      (typeof ctx === "number" && ctx >= 80 ? " hot" : (typeof ctx === "number" && ctx >= 55 ? " warm" : ""));
    const ctxTok = ctxTokensByTab[e.tab_id];
    const ctxTitle = (typeof ctx === "number")
      ? `${(ctxTok || 0).toLocaleString()} input tokens of ${CTX_WINDOW_TOKENS.toLocaleString()} on the last turn`
      : "no turn measured yet in this chat";

    // Tab-group color override: when Chrome assigned a group color, use it
    // for the dot background so the row visually matches the tab strip.
    // Status (responding/active/etc.) still drives the pulse animation via
    // dotCls; only the background flips. Stale rows have no group → keep red.
    const tgCss = TG_COLOR_CSS[e.group_color];
    const dotStyle = tgCss ? ` style="background:${tgCss}"` : "";
    // A collapsed tab group is hidden in the tab strip. Dim its dot so an
    // open chat reads as the live one; expanding the group brings it back
    // to full strength on the next tick.
    const collapsed = collapsedSet.has(e.tab_id);
    const openCls = collapsed ? " collapsed" : " expanded";
    return `
      <div class="agent-row ${e.kind}">
        <span class="dot ${dotCls}${openCls}"${dotStyle} title="${tip}${collapsed ? " · group collapsed" : " · group open"}"></span>
        <span class="agent-short" data-tab-id="${e.tab_id}"
              title="${escapeHtml(e.short)} — click to copy tab id ${e.tab_id}">\u29c9</span>
        <span class="agent-name" title="${escapeHtml(title)}">${escapeHtml(e.display_name || e.name)}</span>
        <span class="${linkCls}" title="${escapeHtml(linkTitle)}">${escapeHtml(linkText)}</span>
        <span class="${ctxCls}" title="${ctxTitle}">${ctxText}</span>
      </div>`;
  }).join("") + (overflow
    ? `<div class="agent-row" style="color:var(--txt-faint); font-style:italic;">+${overflow} more not shown</div>`
    : "");
}

// Per-tab relay state cache, fetched from the server on each refresh tick.
// Shape: { default: bool, overrides: { tab_id_str: bool } }
let relayState = { default: true, overrides: {} };
// Per-tab routing state cache. Shape: { default: "orchestrator", overrides: { tab_id_str: "label" } }
let targetState = { default: "orchestrator", overrides: {} };
// List of available tmux panes (with labels), refreshed each tick.
let availablePanes = [];   // [{pane_id: "%0", label: "orchestrator"}, ...]

async function refreshTmuxPanes() {
  try {
    const r = await bridgeFetch("/tmux-panes");
    if (!r.ok) return;
    const j = await r.json();
    availablePanes = Array.isArray(j.panes) ? j.panes : [];
  } catch (_) { /* leave previous list in place */ }
}

// Latest snapshot of browser-panel routings, used to build per-pane
// ownership rows in renderTmuxAgents. Kept in module scope so other
// refresh handlers can update it.
let panelsForTmuxRender = [];

// Format "Xd Yh ago" / "Xh Ym ago" / "Xm Ys ago" / "Xs ago". Always shows two
// units of precision when possible so the operator can see seconds tick on
// fresh rows AND get a useful day/hour readout on stale ones.
function fmtAgo(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m ago`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h ago`;
}

// Pinned panes are the operator's "Active" list. Auto-detecting which panes
// are an operator's working chats is unreliable (many panes can run
// claude.exe / codex simultaneously, all look equally alive to the OS), so
// the Active section is operator-curated via a star/pin per row.
// Keyed by pane_id ("%5") — stable until the tmux server restarts.
let pinnedPanes = {};

// Auto-active threshold based on the pane's pty access time. If a terminal
// emulator has rendered this pane within this window, the operator is
// considered to be working in it. Combined with has_agent_process to filter
// out idle bash/init panes.
const AUTO_ACTIVE_TTY_MAX_AGE_S = 2 * 60 * 60;  // 2 hours

// A pinned pane is demoted from Active to Inactive once its last bridge
// prompt/reply is older than this. The star stays filled, so as soon as a
// new prompt or reply flows the pane snaps back into Active. Null
// last_activity_at (server just restarted, no traffic yet) is treated as
// a grace period — pinned panes stay in Active until real data shows up.
const PIN_DEMOTE_AFTER_S = 5 * 60 * 60;  // 5 hours
async function loadPinnedPanes() {
  try {
    const { pinnedPanes: v } = await chrome.storage.local.get("pinnedPanes");
    if (v && typeof v === "object") pinnedPanes = v;
  } catch (_) {}
}
async function togglePinned(paneId) {
  if (pinnedPanes[paneId]) delete pinnedPanes[paneId];
  else pinnedPanes[paneId] = true;
  try { await chrome.storage.local.set({ pinnedPanes }); } catch (_) {}
  renderTmuxAgents();
}

async function renderTmuxAgents() {
  const root = document.getElementById("tmux-agents");
  if (!root) return;
  // If the operator is typing in a rename input, don't blow it away on
  // the 3-second poll. The next tick will rebuild once focus moves.
  const active = document.activeElement;
  if (active && active.classList && active.classList.contains("pane-rename")
      && root.contains(active)) {
    return;
  }
  if (!availablePanes || availablePanes.length === 0) {
    root.innerHTML = `<div class="agent-row" style="color:var(--txt-dim2)">no tmux panes (is tmux running?)</div>`;
    return;
  }
  // Build a map: pane label/%N -> list of browser panel names routing there.
  // Browser panels route via the relay-target effective value: an explicit
  // override beats the popup's Default Relay Target.
  const ownedByPane = {};
  for (const p of panelsForTmuxRender) {
    if (p.kind !== "registered" || p.tab_id == null) continue;
    const target = effectiveTarget(p.tab_id);
    if (!target) continue;
    (ownedByPane[target] ||= []).push(p.name || `%${p.tab_id}`);
  }
  // Count how many panes share each session UUID. A session that's "in
  // use" by 2+ panes can't be opened by a 3rd `claude --session-id` —
  // claude refuses with "Session ID ... is already in use" — so we
  // disable the 📋 button on those rows. Cached/exited panes (claude
  // gone) don't hold the session, so they don't count toward the cap.
  const sessionUseCount = {};
  for (const p of availablePanes) {
    if (p.session_id && !p.agent_exited) {
      sessionUseCount[p.session_id] = (sessionUseCount[p.session_id] || 0) + 1;
    }
  }

  // Bucket panes into Active / Inactive using "auto OR star" rule:
  //   Auto-active = has_agent_process AND tty_atime_age_s < 2h
  //                 (operator's terminal has rendered the pane recently,
  //                  AND a claude/codex process is alive inside it)
  //   Star-active = pinned AND fresh (within 5h of bridge activity, or
  //                 no bridge activity recorded yet)
  //   Active      = auto-active OR star-active
  //   Inactive    = everything else (pinned-but-quiet panes keep the
  //                 filled ★ so the operator can see they're still pinned).
  const nowSec = Date.now() / 1000;
  const activeList = [];
  const inactiveList = [];
  for (const p of availablePanes) {
    const autoActive =
      !!p.has_agent_process &&
      typeof p.tty_atime_age_s === "number" &&
      p.tty_atime_age_s < AUTO_ACTIVE_TTY_MAX_AGE_S;

    const pinned = !!pinnedPanes[p.pane_id];
    const ts = p.last_activity_at;
    const pinFresh = (ts == null) || ((nowSec - ts) < PIN_DEMOTE_AFTER_S);
    const starActive = pinned && pinFresh;

    if (autoActive || starActive) activeList.push(p);
    else inactiveList.push(p);
  }
  // Active: most-recently-active first. Inactive: most-recently-inactive
  // first; never-active panes sink to the bottom.
  activeList.sort((a, b) => (b.last_activity_at || 0) - (a.last_activity_at || 0));
  inactiveList.sort((a, b) => {
    const ax = a.last_activity_at, bx = b.last_activity_at;
    if (ax == null && bx == null) return 0;
    if (ax == null) return 1;
    if (bx == null) return -1;
    return bx - ax;
  });
  // state → CSS class for the terminal-agents status dot.
  //   new      = untouched (no bridge traffic ever) → no class, default grey
  //   active   = recent heartbeat / prompt outbound  → green (.dot.discovered)
  //   finished = reply received >= last prompt sent  → blue (.dot.active)
  //   idle     = finished and stale (>=10 min)       → yellow (.dot.idle)
  const stateToDot = { new: "", active: "discovered", finished: "active", idle: "idle" };
  const renderRow = (p, { showAgo } = {}) => {
    // Display: always prefix with the pane id, append the @name label when set.
    // e.g. `%0 orchestrator`, or just `%0` when no label is set.
    const hasLabel = p.label && p.label !== "-";
    const display = hasLabel ? `${p.pane_id} ${p.label}` : p.pane_id;
    // ownedByPane is keyed by the label (or pane_id) — keep matching by label
    // when present so the legacy relay-target inference still works.
    const owned = ownedByPane[hasLabel ? p.label : p.pane_id] || [];
    const count = owned.length;
    const ownedText = count === 0
      ? `<span style="color:var(--txt-faint); font-style:italic;">no browser agents</span>`
      : owned.map(n => `<span style="background:var(--cyan-a12); color:var(--cyan); border:1px solid var(--cyan-a30); padding:1px 5px; border-radius:8px; font-family:monospace; font-size:10px;">${escapeHtml(n)}</span>`).join(" ");
    // Connected browsers (new server): array of {short, name, tab_id}. Shown
    // as comma-separated `/brN`. Falls back to the legacy `ownedByPane`
    // computation (popup-side relay-target inference) when the field is absent.
    let browsersCell;
    if (Array.isArray(p.connected_browsers)) {
      if (p.connected_browsers.length === 0) {
        browsersCell = `<span style="color:var(--txt-faint); font-style:italic;">no browser agents</span>`;
      } else {
        browsersCell = p.connected_browsers
          .map(b => `<span style="background:var(--cyan-a12); color:var(--cyan); border:1px solid var(--cyan-a30); padding:1px 5px; border-radius:8px; font-family:monospace; font-size:10px;">${escapeHtml(b.short || ("%" + b.tab_id))}</span>`)
          .join(" ");
      }
    } else {
      browsersCell = ownedText;
    }
    const browsersCount = Array.isArray(p.connected_browsers) ? p.connected_browsers.length : count;
    const cmd = p.cmd ? `<span style="color:var(--txt-faint); font-size:10px;">${escapeHtml(p.cmd)}</span>` : "";

    // Status dot. Priority:
    //   agent_exited       → orange (claude gone, session resumable)
    //   agent_status=busy  → green pulsing (claude is processing)
    //   agent_status=idle/ready/... → solid grey (waiting at prompt)
    //   no agent info      → fall back to bridge state
    let dotCls, dotTitle, dotStyle;
    if (p.agent_exited) {
      dotCls = ""; dotTitle = "agent exited — resume via 📋"; dotStyle = "background:#ff8c42;";
    } else if (p.agent_status === "busy") {
      dotCls = "discovered"; dotTitle = "busy (responding)"; dotStyle = "";
    } else if (p.agent_status) {
      dotCls = ""; dotTitle = p.agent_status; dotStyle = "background:var(--txt-faint);";
    } else {
      dotCls = stateToDot[p.state] || "";
      dotTitle = p.state || "";
      dotStyle = dotCls ? "" : "visibility:hidden;";
    }
    const dot = `<span class="dot ${dotCls}" title="${escapeHtml(dotTitle)}" style="${dotStyle}"></span>`;

    // Rename input. Placeholder shows current @name if present, else "rename…".
    const curName = p.name || "";
    const placeholder = curName ? curName : "rename…";
    const renameInput = `<input type="text" class="pane-rename"
            data-pane-id="${escapeHtml(p.pane_id)}"
            maxlength="32"
            placeholder="${escapeHtml(placeholder)}"
            title="rename pane (max 32 chars, A-Z a-z 0-9 _ -). Press Enter to submit." />`;

    // Inactive section: "Xd Yh ago" hint sourced from the most recent
    // signal we have for this pane — min(tty_atime_age_s, tty_mtime_age_s).
    //   atime = terminal rendered the pane (operator was looking)
    //   mtime = pty was written to (claude wrote output, or input arrived)
    // Whichever is fresher beats "I haven't touched this pane".
    // Data attributes let the 1-second ticker advance the display between
    // 3-second server polls. "never" only when neither stat succeeded.
    let agoSpan = "";
    if (showAgo) {
      const ages = [p.tty_atime_age_s, p.tty_mtime_age_s]
        .filter(v => typeof v === "number" && v >= 0);
      if (ages.length) {
        const baseAge = Math.min(...ages);
        agoSpan = `<span class="ago-live" data-base-age="${baseAge}"
                          data-base-ts="${Date.now()}"
                          style="color:var(--txt-faint); font-size:10px; font-style:italic; margin-left:4px;">${escapeHtml(fmtAgo(baseAge))}</span>`;
      } else {
        agoSpan = `<span style="color:var(--txt-faint); font-size:10px; font-style:italic; margin-left:4px;">never</span>`;
      }
    }

    // Star/pin toggle. Filled ★ when this pane is in Active, hollow ☆ when not.
    // Clicking flips the state via togglePinned() (event delegated below).
    const pinned = !!pinnedPanes[p.pane_id];
    const starBtn = `<span class="pane-star" data-pane-id="${escapeHtml(p.pane_id)}"
            title="${pinned ? "unpin from Active" : "pin to Active"}"
            style="cursor:pointer; user-select:none; color:${pinned ? "var(--violet)" : "var(--txt-faint)"}; font-size:14px; width:14px; text-align:center;">${pinned ? "★" : "☆"}</span>`;

    // Delete button — kills the tmux pane via the server. Confirms before
    // sending. Red on panes with a live agent process so the operator
    // doesn't nuke a working chat by accident.
    const hasAgent = !!p.has_agent_process;
    const delBtn = `<span class="pane-delete" data-pane-id="${escapeHtml(p.pane_id)}"
            data-has-agent="${hasAgent ? "1" : "0"}"
            title="${hasAgent ? "kill pane (warning: agent process running)" : "kill pane"}"
            style="cursor:pointer; user-select:none; color:${hasAgent ? "var(--red)" : "var(--txt-faint)"}; font-size:13px; width:14px; text-align:center; margin-left:2px;">🗑</span>`;

    // Copy-resume button — works whether claude is live OR exited
    // (server caches the last-known session_id + cwd as agent_exited).
    // Disabled when the LIVE session is held by 2+ panes (claude
    // refuses a 3rd attach). Exited rows always stay copyable.
    let copyBtn = "";
    const sid = p.session_id || "";
    const sidInUseElsewhere = sid && sessionUseCount[sid] > 1;
    const hasResumable = (hasAgent || p.agent_exited) && p.agent_cwd && sid;
    if (hasResumable && !sidInUseElsewhere) {
      const tipTxt = p.agent_exited
        ? `copy resume command (session ${sid.slice(0,8)}… — claude exited, resume reopens it)`
        : `copy resume command (session ${sid.slice(0,8)}…)`;
      copyBtn = `<span class="pane-copy-resume"
              data-pane-id="${escapeHtml(p.pane_id)}"
              data-cwd="${escapeHtml(p.agent_cwd)}"
              data-sid="${escapeHtml(sid)}"
              title="${escapeHtml(tipTxt)}"
              style="cursor:pointer; user-select:none; color:var(--green); font-size:12px; width:14px; text-align:center; margin-left:2px;">📋</span>`;
    } else if (hasResumable && sidInUseElsewhere) {
      copyBtn = `<span title="session is open in ${sessionUseCount[sid]} panes — close all but one before resuming"
              style="cursor:not-allowed; user-select:none; color:var(--txt-faint); font-size:12px; width:14px; text-align:center; margin-left:2px;">📋</span>`;
    } else if (hasAgent && p.agent_cwd) {
      // Agent but no UUID — fallback to picker-style command.
      copyBtn = `<span class="pane-copy-resume"
              data-pane-id="${escapeHtml(p.pane_id)}"
              data-cwd="${escapeHtml(p.agent_cwd)}"
              data-sid=""
              title="copy resume command (UUID not derivable — opens picker)"
              style="cursor:pointer; user-select:none; color:var(--txt-dim2); font-size:12px; width:14px; text-align:center; margin-left:2px;">📋</span>`;
    } else {
      copyBtn = `<span style="width:14px; margin-left:2px;"></span>`;
    }

    return `
      <div class="agent-row" data-pane-id="${escapeHtml(p.pane_id)}">
        ${starBtn}
        ${dot}
        <span class="agent-name" style="min-width:120px;">${escapeHtml(display)}${agoSpan}</span>
        <span style="flex:1; text-align:right; display:flex; gap:4px; flex-wrap:wrap; justify-content:flex-end;">${browsersCell}</span>
        <span class="agent-relay-label ${browsersCount > 0 ? 'on' : ''}" style="width:auto; min-width:18px; text-align:right;">${browsersCount}</span>
        ${renameInput}
        ${copyBtn}
        ${delBtn}
      </div>`;
  };

  // Non-collapsable header (used for the Active section).
  const sectionHeader = (label, count) => `
    <div style="display:flex; align-items:center; justify-content:space-between;
                padding:4px 8px; background:var(--bg-hdr); border-bottom:1px solid var(--line);
                font-size:10px; text-transform:uppercase; letter-spacing:0.4px;
                color:var(--txt-dim); font-weight:600;">
      <span>${escapeHtml(label)}</span>
      <span style="font-family:monospace; color:var(--txt-dim2);">${count}</span>
    </div>`;
  const emptyRow = (msg) => `<div class="agent-row" style="color:var(--txt-faint); font-style:italic;">${escapeHtml(msg)}</div>`;

  const activeHtml = activeList.length
    ? activeList.map(p => renderRow(p)).join("")
    : emptyRow("no pinned agents — click ☆ on any row below to pin it here");
  const inactiveHtml = inactiveList.length
    ? inactiveList.map(p => renderRow(p, { showAgo: true })).join("")
    : emptyRow("none");

  // Inactive section: <details>/<summary> so the operator can collapse it.
  // Open/closed state is persisted in chrome.storage.local (inactiveSectionOpen).
  const openAttr = inactiveSectionOpen ? " open" : "";
  const summaryStyle =
    "display:flex; align-items:center; justify-content:space-between; " +
    "padding:4px 8px; background:var(--bg-hdr); border-bottom:1px solid var(--line); " +
    "font-size:10px; text-transform:uppercase; letter-spacing:0.4px; " +
    "color:var(--txt-dim); font-weight:600; cursor:pointer; list-style:none; user-select:none;";
  const inactiveBlock = `
    <details id="inactive-details"${openAttr} style="border-top:1px solid var(--line);">
      <summary style="${summaryStyle}">
        <span>▸ Inactive</span>
        <span style="font-family:monospace; color:var(--txt-dim2);">${inactiveList.length}</span>
      </summary>
      ${inactiveHtml}
    </details>`;

  root.innerHTML =
    sectionHeader("Active", activeList.length) + activeHtml +
    inactiveBlock;

  // Rotate the ▸ caret when open.
  const det = document.getElementById("inactive-details");
  if (det) {
    const sum = det.querySelector("summary > span:first-child");
    const setCaret = () => { if (sum) sum.textContent = (det.open ? "▾" : "▸") + " Inactive"; };
    setCaret();
    det.addEventListener("toggle", () => {
      setCaret();
      inactiveSectionOpen = det.open;
      chrome.storage.local.set({ inactiveSectionOpen: det.open });
    });
  }
}

// Persisted open/closed state for the Inactive sub-section. Loaded once at
// popup startup so the first render uses the user's last choice.
let inactiveSectionOpen = false;
async function loadInactiveOpenPref() {
  try {
    const { inactiveSectionOpen: v } = await chrome.storage.local.get("inactiveSectionOpen");
    if (typeof v === "boolean") inactiveSectionOpen = v;
  } catch (_) {}
}

const PANE_NAME_RE = /^[A-Za-z0-9_-]{0,32}$/;

async function renamePane(paneId, newName, rowEl, inputEl) {
  try {
    const r = await bridgeFetch("/tmux-pane/rename", {
      method: "POST",
      body: JSON.stringify({ pane_id: paneId, new_name: newName }),
    });
    if (r.ok) {
      if (rowEl) {
        rowEl.classList.add("flash-ok");
        setTimeout(() => rowEl.classList.remove("flash-ok"), 500);
      }
      if (inputEl) {
        inputEl.value = "";
        // Blur so the focus-guard in renderTmuxAgents doesn't skip the next
        // render — otherwise the new name never appears in the display column.
        inputEl.blur();
      }
      // Refresh so the new @name shows up in the display column + placeholder.
      refreshTmuxPanes().then(renderTmuxAgents);
    } else {
      const body = await r.text();
      alert(`rename failed (${r.status}): ${body}`);
    }
  } catch (e) {
    alert(`rename failed: ${e.message}`);
  }
}

async function refreshTargetState() {
  try {
    const r = await bridgeFetch("/relay-target");
    if (!r.ok) return;
    targetState = await r.json();
    // Refresh the default-target dropdown.
    const sel = document.getElementById("relay-default-target");
    if (sel) {
      const labels = availablePanes.map(p => (p.label && p.label !== "-") ? p.label : p.pane_id).filter(Boolean);
      // Preserve current selection unless it's gone.
      const cur = targetState.default || "orchestrator";
      const opts = Array.from(new Set([cur, ...labels])).filter(Boolean);
      sel.innerHTML = opts.map(l => `<option value="${escapeHtml(l)}"${l === cur ? " selected" : ""}>${escapeHtml(l)}</option>`).join("");
    }
  } catch (_) {}
}

function effectiveTarget(tabId) {
  const key = String(tabId);
  if (targetState.overrides && Object.prototype.hasOwnProperty.call(targetState.overrides, key)) {
    return targetState.overrides[key];
  }
  return targetState.default || "orchestrator";
}

function isTargetOverridden(tabId) {
  const key = String(tabId);
  return targetState.overrides && Object.prototype.hasOwnProperty.call(targetState.overrides, key);
}

async function setRelayTargetForTab(tabId, target) {
  try {
    const r = await bridgeFetch("/relay-target", {
      method: "POST",
      body: JSON.stringify({ tab_id: tabId, target, by: "popup" }),
    });
    if (!r.ok) return;
    await refreshTargetState();
    await refreshAgents();
  } catch (_) {}
}

async function clearRelayTargetOverride(tabId) {
  try {
    const r = await bridgeFetch("/relay-target", {
      method: "POST",
      body: JSON.stringify({ tab_id: tabId, target: null, by: "popup" }),
    });
    if (!r.ok) return;
    await refreshTargetState();
    await refreshAgents();
  } catch (_) {}
}

async function setRelayDefaultTarget(target) {
  try {
    const r = await bridgeFetch("/relay-target", {
      method: "POST",
      body: JSON.stringify({ target, by: "popup" }),
    });
    if (!r.ok) return;
    await refreshTargetState();
    await refreshAgents();
  } catch (_) {}
}

async function refreshRelayState() {
  try {
    const r = await bridgeFetch("/relay-enabled");
    if (!r.ok) return;
    relayState = await r.json();
    // Update the default toggle in the panel-default section.
    const cb = document.getElementById("relay-default-toggle");
    const lbl = document.getElementById("relay-default-label");
    if (cb && lbl) {
      cb.checked = !!relayState.default;
      lbl.textContent = relayState.default ? "ON" : "OFF";
      lbl.style.color = relayState.default ? "var(--green)" : "var(--red)";
    }
  } catch (_) { /* server offline; leave last known state */ }
}

// Returns the effective enabled state for one tab.
function effectiveEnabled(tabId) {
  const key = String(tabId);
  if (relayState.overrides && Object.prototype.hasOwnProperty.call(relayState.overrides, key)) {
    return !!relayState.overrides[key];
  }
  return !!relayState.default;
}

// Returns true if this tab has an explicit override (visual cue in the row).
function isOverridden(tabId) {
  const key = String(tabId);
  return relayState.overrides && Object.prototype.hasOwnProperty.call(relayState.overrides, key);
}

async function setRelayForTab(tabId, enabled) {
  try {
    const r = await bridgeFetch("/relay-enabled", {
      method: "POST",
      body: JSON.stringify({ tab_id: tabId, enabled, by: "popup" }),
    });
    if (!r.ok) return;
    await refreshRelayState();
    await refreshAgents();   // redraw rows so the new state is reflected
  } catch (_) {}
}

async function setRelayDefault(enabled) {
  try {
    const r = await bridgeFetch("/relay-enabled", {
      method: "POST",
      body: JSON.stringify({ enabled, by: "popup" }),
    });
    if (!r.ok) return;
    await refreshRelayState();
    await refreshAgents();
  } catch (_) {}
}

// Clear an override (revert this tab to follow the default).
async function clearRelayOverride(tabId) {
  try {
    const r = await bridgeFetch("/relay-enabled", {
      method: "POST",
      body: JSON.stringify({ tab_id: tabId, enabled: null, by: "popup" }),
    });
    if (!r.ok) return;
    await refreshRelayState();
    await refreshAgents();
  } catch (_) {}
}

// ── context fill ───────────────────────────────────────────────
// The API's own input_tokens for the chat's last turn, captured inside the
// Claude side panel by daemon/ctx_agent.js and served from /panels/ctx. It is
// a measurement, not an estimate: it counts the system prompt, tool results
// and page reads, none of which are visible from the DOM.
//
// A chat shows "—" until it has run one turn since the agent started.
const CTX_WINDOW_TOKENS = 200000;
let ctxByTab = {};         // tab_id -> percent | null
let ctxTokensByTab = {};   // tab_id -> input tokens | undefined

async function refreshContextUsage() {
  try {
    const r = await bridgeFetch("/panels/ctx");
    if (!r.ok) return;
    const { usage } = await r.json();
    for (const [tid, u] of Object.entries(usage || {})) {
      ctxByTab[tid] = (u && typeof u.pct === "number") ? u.pct : null;
      if (u && typeof u.input_tokens === "number") ctxTokensByTab[tid] = u.input_tokens;
    }
  } catch (_) {
    // Server down — keep the last known values rather than blanking the column.
  }
}

async function refreshAll() {
  await Promise.all([
    refreshHealth(),
    refreshRelayState(),
    refreshTmuxPanes(),
  ]);
  // refreshTargetState depends on availablePanes for the default-dropdown
  await refreshTargetState();
  await refreshAgents();
  // Context estimates need the tab list that refreshAgents just built. They
  // are measured after the first paint and land on the next tick, so rows
  // never block on scripting round-trips.
  refreshContextUsage();
  // renderTmuxAgents needs both availablePanes AND the panel snapshot
  // that refreshAgents just produced.
  renderTmuxAgents();
}

$("save").addEventListener("click", save);
$("check")?.addEventListener("click", refreshAll);
// Hard-reload the extension. Useful after editing popup.html or popup.js so
// the operator doesn't have to go to chrome://extensions/ and click the
// circular reload arrow. The popup closes immediately; reopen it to see the
// fresh code.
$("reload-ext")?.addEventListener("click", () => {
  chrome.runtime.reload();
});

window.addEventListener("unhandledrejection", (ev) => {
  console.error("[bridge] unhandled rejection during init:", ev.reason);
  const el = $("token-state");
  if (el && el.textContent === "stored: —") {
    el.textContent = `init failed: ${ev.reason && ev.reason.message ? ev.reason.message : ev.reason}`;
    el.className = "token-state bad";
  }
});

// `relay-default-toggle` and `relay-default-target` were removed from the
// popup — per-agent toggle + dropdown on each row are sufficient. Guard
// against missing elements in case the HTML drifts.
const _rdt = $("relay-default-toggle");
if (_rdt) _rdt.addEventListener("change", (ev) => setRelayDefault(ev.target.checked));
const _rdg = $("relay-default-target");
if (_rdg) _rdg.addEventListener("change", (ev) => setRelayDefaultTarget(ev.target.value));

// Per-row event delegation — the agent list is re-rendered each tick, so
// bind on the parent and let events bubble.
$("agents").addEventListener("change", (ev) => {
  const t = ev.target;
  if (!t || !t.classList) return;
  if (t.classList.contains("agent-relay-cb")) {
    const tabId = parseInt(t.dataset.tabId, 10);
    if (Number.isFinite(tabId)) setRelayForTab(tabId, t.checked);
    return;
  }
  if (t.classList.contains("agent-target-sel")) {
    const tabId = parseInt(t.dataset.tabId, 10);
    if (Number.isFinite(tabId)) setRelayTargetForTab(tabId, t.value);
    return;
  }
});

// Double-click handlers for clearing overrides (revert to default).
$("agents").addEventListener("dblclick", (ev) => {
  const lbl = ev.target.closest && ev.target.closest(".agent-relay-label");
  if (lbl) {
    const tabId = parseInt(lbl.dataset.tabId, 10);
    if (Number.isFinite(tabId)) clearRelayOverride(tabId);
    return;
  }
  const sel = ev.target.closest && ev.target.closest(".agent-target-sel");
  if (sel && sel.classList.contains("overridden")) {
    const tabId = parseInt(sel.dataset.tabId, 10);
    if (Number.isFinite(tabId)) clearRelayTargetOverride(tabId);
  }
});

// Star/pin toggle: click anywhere on the star span to flip pinned state.
// Delegated from the parent because rows re-render every tick.
$("tmux-agents")?.addEventListener("click", (ev) => {
  const t = ev.target;
  if (!t || !t.classList) return;
  if (t.classList.contains("pane-star")) {
    const paneId = t.dataset.paneId;
    if (paneId) togglePinned(paneId);
    return;
  }
  if (t.classList.contains("pane-delete")) {
    const paneId = t.dataset.paneId;
    if (!paneId) return;
    const hasAgent = t.dataset.hasAgent === "1";
    const warn = hasAgent
      ? `Kill pane ${paneId}?\n\nWARNING: this pane has a live agent process. Killing the pane will terminate the agent.`
      : `Kill pane ${paneId}?`;
    if (!confirm(warn)) return;
    deletePane(paneId);
    return;
  }
  if (t.classList.contains("pane-copy-resume")) {
    const cwd = t.dataset.cwd || "";
    const sid = t.dataset.sid || "";
    if (!cwd) return;
    // Shell-escape the cwd with single quotes; embedded single quotes (rare
    // in Windows-mapped paths but possible) are escaped via the standard
    // bash trick: '\'' breaks out and reopens the quoted string.
    const safeCwd = cwd.replace(/'/g, "'\\''");
    // `claude --resume <uuid>` resumes an existing session directly —
    // this is what claude itself suggests in its /exit message.
    // `--session-id <uuid>` is for STARTING a new session with a given
    // UUID and refuses with "already in use" when the UUID is taken
    // (which is exactly the resume case). For LIVE multi-pane sessions
    // --resume falls back to the picker; we already grey out 📋 in
    // that case via sessionUseCount.
    const resumeArg = sid ? `--resume ${sid}` : "--resume";
    const cmd = `cd '${safeCwd}' && claude ${resumeArg} --dangerously-skip-permissions`;
    const origColor = sid ? "var(--green)" : "var(--txt-dim2)";
    navigator.clipboard.writeText(cmd).then(() => {
      const orig = t.textContent;
      t.textContent = "✓";
      t.style.color = "var(--green)";
      setTimeout(() => { t.textContent = orig; t.style.color = origColor; }, 1000);
    }).catch((e) => alert(`copy failed: ${e.message}`));
    return;
  }
});

async function deletePane(paneId) {
  try {
    const r = await bridgeFetch("/tmux-pane/delete", {
      method: "POST",
      body: JSON.stringify({ pane_id: paneId }),
    });
    if (!r.ok) {
      const body = await r.text();
      alert(`delete failed (${r.status}): ${body}`);
      return;
    }
    // Drop our local star for the dead pane and refresh immediately.
    if (pinnedPanes[paneId]) {
      delete pinnedPanes[paneId];
      try { await chrome.storage.local.set({ pinnedPanes }); } catch (_) {}
    }
    await refreshTmuxPanes();
    renderTmuxAgents();
  } catch (e) {
    alert(`delete failed: ${e.message}`);
  }
}

// 1-second ticker that advances the "Xh ago" / "Xm ago" / "Xs ago" text on
// every Inactive row without re-rendering the whole list. The row span
// carries data-base-age (seconds, from last server poll) and data-base-ts
// (Date.now() at render time); we add elapsed wall time to base age and
// re-format.
setInterval(() => {
  const spans = document.querySelectorAll(".ago-live");
  if (!spans.length) return;
  const nowMs = Date.now();
  spans.forEach((el) => {
    const baseAge = parseInt(el.dataset.baseAge, 10);
    const baseTs = parseInt(el.dataset.baseTs, 10);
    if (!Number.isFinite(baseAge) || !Number.isFinite(baseTs)) return;
    const liveAge = baseAge + Math.floor((nowMs - baseTs) / 1000);
    el.textContent = fmtAgo(liveAge);
  });
}, 1000);

// Terminal-agent rename input: validate on each keystroke, submit on Enter.
// Re-render rebuilds the rows each tick, so we delegate from the parent.
$("tmux-agents")?.addEventListener("input", (ev) => {
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains("pane-rename")) return;
  // Allow empty during typing; otherwise validate.
  if (t.value === "" || PANE_NAME_RE.test(t.value)) {
    t.classList.remove("invalid");
  } else {
    t.classList.add("invalid");
  }
});

$("tmux-agents")?.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains("pane-rename")) return;
  ev.preventDefault();
  const value = t.value.trim();
  if (!PANE_NAME_RE.test(value)) {
    t.classList.add("invalid");
    return;
  }
  const paneId = t.dataset.paneId;
  const rowEl = t.closest(".agent-row");
  renamePane(paneId, value, rowEl, t);
});

// "📌" button: open the same UI as a pinned tab so it stays visible while you
// click around the browser. If a pinned dashboard tab already exists, focus it
// instead of opening a duplicate.
// 📊 Open dashboard: open the localhost dashboard in a new tab, passing
// the current bearer token via URL hash so the dashboard auto-saves it
// (no second copy-paste). Token is then stripped from the URL.
$("open-dashboard").addEventListener("click", async () => {
  const { token, panelTheme } = await chrome.storage.local.get(["token", "panelTheme"]);
  const base = "http://127.0.0.1:7777/dashboard";
  // Carry this panel's theme across so the dashboard opens in the same skin.
  const theme = panelTheme === "light" ? "bridge-light" : "bridge-dark";
  const frag = [
    token ? `token=${encodeURIComponent(token)}` : "",
    `theme=${theme}`,
  ].filter(Boolean).join("&");
  await chrome.tabs.create({ url: `${base}#${frag}`, active: true });
});

load();

// Click an agent's %N short id to copy its tab id to the clipboard. The tab
// id is what every CLI dispatch needs, and it is no longer printed in the
// row, so this is how you get at it.
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest && ev.target.closest(".agent-short");
  if (!el) return;
  const tabId = el.dataset.tabId;
  if (!tabId) return;
  try {
    await navigator.clipboard.writeText(String(tabId));
    // Same-width glyph swap (copy -> check) so nothing reflows.
    const prev = el.textContent;
    el.textContent = "\u2713";
    el.classList.add("copied");
    setTimeout(() => { el.textContent = prev; el.classList.remove("copied"); }, 900);
  } catch (_) { /* clipboard blocked — nothing useful to do */ }
});

// Belt and braces: commit the token on Enter and on blur, not only on the
// Save button. Closing the popup with an uncommitted field was the other
// way the token went missing.
$("token").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); save(); }
});
$("token").addEventListener("change", () => { save(); });

// If any other context (service worker, a second popup) changes the token,
// reflect it here rather than showing a stale field.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.token) return;
  const v = changes.token.newValue || "";
  const el = $("token");
  if (el && document.activeElement !== el) el.value = v;
  showTokenState();
});

// FADE button: turns media fade on or off. background.js reacts to the
// mediaFade storage key.
(async () => {
  const btn = document.getElementById("media-fade");
  if (!btn) return;
  const show = (on) => {
    btn.classList.toggle("on", on);
    btn.textContent = on ? "FADE" : "FADE OFF";
  };
  const { mediaFade = true } = await chrome.storage.local.get("mediaFade");
  show(mediaFade);
  btn.addEventListener("click", async () => {
    const { mediaFade: cur = true } = await chrome.storage.local.get("mediaFade");
    await chrome.storage.local.set({ mediaFade: !cur });
    show(!cur);
  });
})();
