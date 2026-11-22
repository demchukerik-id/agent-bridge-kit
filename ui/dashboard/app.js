// tmux-bridge dashboard. Standalone web app served at /dashboard.
// Reuses the bridge HTTP API exactly like the extension popup, but with
// localStorage instead of chrome.storage and no chrome.tabs/tabGroups
// (browser agents come from the server's /panels endpoint).

const $ = (id) => document.getElementById(id);
const BRIDGE_URL = ""; // same origin

const LS = {
  get: (k) => { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set: (k, v) => localStorage.setItem(k, JSON.stringify(v)),
  del: (k) => localStorage.removeItem(k),
};

// ---- hash handoff from the extension popup ---------------------------
// The popup opens us with "#token=<tok>&theme=<name>". Parsed once, stashed
// in localStorage, then wiped from the URL so the token is not left sitting
// in the address bar.
const HASH_PARAMS = (() => {
  const raw = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const out = {};
  if (!raw) return out;
  for (const pair of raw.split("&")) {
    if (!pair) continue;
    const i = pair.indexOf("=");
    if (i < 0) continue;
    try { out[pair.slice(0, i)] = decodeURIComponent(pair.slice(i + 1)); } catch (_) {}
  }
  if (Object.keys(out).length) history.replaceState(null, "", location.pathname);
  return out;
})();

// ---- token bootstrap -------------------------------------------------
function getToken() {
  // 1. URL hash overrides (popup can pass via #token=...)
  if (HASH_PARAMS.token) {
    LS.set("token", HASH_PARAMS.token);
    return HASH_PARAMS.token;
  }
  return LS.get("token") || "";
}

async function bridgeFetch(path, init = {}) {
  const tok = getToken();
  const headers = new Headers(init.headers || {});
  if (tok) headers.set("Authorization", `Bearer ${tok}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(`${BRIDGE_URL}${path}`, { ...init, headers });
}

// ---- theme switcher --------------------------------------------------
function applyTheme(name) {
  document.documentElement.setAttribute("data-theme", name);
  LS.set("theme", name);
}
function initTheme() {
  // A theme handed over by the popup wins for this load, so opening the
  // dashboard from a dark panel lands you in the dark dashboard.
  const saved = HASH_PARAMS.theme || LS.get("theme") || "bridge-dark";
  applyTheme(saved);
  syncThemeButton(saved);
  const btn = $("theme-toggle");
  if (btn) btn.addEventListener("click", () => {
    const next = (LS.get("theme") === "bridge-light") ? "bridge-dark" : "bridge-light";
    applyTheme(next);
    syncThemeButton(next);
  });
}
// Same LGT/DRK affordance as the panel: the button names where you're going.
function syncThemeButton(theme) {
  const btn = $("theme-toggle");
  if (!btn) return;
  const isLight = theme === "bridge-light";
  btn.textContent = isLight ? "DRK" : "LGT";
  btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
}

// ---- pinned panes + collapse state (per-browser) ---------------------
let pinnedPanes = LS.get("pinnedPanes") || {};
function togglePinned(paneId) {
  if (pinnedPanes[paneId]) delete pinnedPanes[paneId];
  else pinnedPanes[paneId] = true;
  LS.set("pinnedPanes", pinnedPanes);
  renderAll();
}

// ---- data refresh ----------------------------------------------------
let panes = [];
let skills = [];
let panels = [];
// Same feed the extension panel reads, so dot states mean the same thing in
// both places instead of the dashboard's old busy/idle guess.
let browserActivity = { streaming_tab_ids: [], working_tab_ids: [], last_active_tab: {}, group_colors: {} };
// Same table popup.js uses. The dot takes its tab-group colour when the chat
// is in a Chrome group, so a row matches the tab strip in both surfaces;
// state (pulse) still comes from the activity feed.
// Live Chrome tab ids, as last pushed by the extension.
//   null      = the extension has not reported yet — we know nothing
//   Set()     = it reported, and every panel is closed — a real zero
//   Set(...)  = these tabs exist
// Treating an empty report as "unknown" is what put phantom rows on the
// dashboard after the last chat was closed, so the two states stay distinct.
let liveTabIds = null;
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
let healthOK = false;

async function refreshHealth() {
  try {
    const r = await fetch(`${BRIDGE_URL}/health`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const tok = getToken();
    if (tok) {
      const p = await fetch(`${BRIDGE_URL}/poll`, { headers: { Authorization: `Bearer ${tok}` } });
      healthOK = p.status !== 401;
    } else {
      healthOK = false;
    }
    setStatus(healthOK ? "ok" : "auth", `q:${j.queued} · pending:${j.pending_replies} · tabs:${j.tabs}`);
    setFooter(`q:${j.queued} · pending:${j.pending_replies} · tabs:${j.tabs}`);
  } catch (e) {
    healthOK = false;
    setStatus("offline", `server: ${e.message}`);
    setFooter(`server unreachable: ${e.message}`);
  }
}
// The dot and the meta readout were folded into the badge to match the
// panel, so every one of these is optional now.
function setFooter(text) { const el = $("dash-footer"); if (el) el.textContent = text; }
function setStatus(kind, meta) {
  const dot = $("status-dot"), text = $("status-text"), m = $("status-meta");
  if (dot) dot.className = "status-dot " + (kind === "ok" ? "ok" : "err");
  if (text) {
    text.textContent = kind === "ok" ? "OK" : (kind === "auth" ? "AUTH" : "OFFLINE");
    text.className = "status-text " + (kind === "ok" ? "ok" : "err");
    text.title = meta || "";
  }
  if (m) m.textContent = meta || "";
}

async function refreshPanes() {
  try {
    const r = await bridgeFetch("/tmux-panes");
    if (!r.ok) { panes = []; return; }
    const j = await r.json();
    panes = Array.isArray(j.panes) ? j.panes : [];
  } catch { panes = []; }
}
async function refreshSkills() {
  try {
    const r = await bridgeFetch("/skills");
    if (!r.ok) { skills = []; return; }
    const j = await r.json();
    skills = Array.isArray(j.skills) ? j.skills : [];
  } catch { skills = []; }
}
// Context-window usage per browser agent, served from /panels/ctx. Measured
// inside the Claude side panel by daemon/ctx_agent.js — the API's own
// input_tokens for the last turn, not a count of visible text.
let ctxUsage = {};
const CTX_WINDOW_TOKENS = 200000;

async function refreshPanels() {
  try {
    const r = await bridgeFetch("/panels/all");
    if (!r.ok) { panels = []; return; }
    const j = await r.json();
    panels = Array.isArray(j.panels) ? j.panels : [];
  } catch { panels = []; }
  try {
    const ra = await bridgeFetch("/panels/activity");
    if (ra.ok) browserActivity = await ra.json();
  } catch { /* keep the previous snapshot */ }
  try {
    const rc = await bridgeFetch("/panels/ctx");
    if (rc.ok) ctxUsage = (await rc.json()).usage || {};
  } catch { /* keep the previous readings */ }
  // Which tabs actually still exist. The extension pushes its live view of
  // Chrome tab groups here every poll, so a registered panel missing from
  // this set has had its tab closed — that is the panel's "stale" state,
  // which /panels/all alone cannot tell us.
  try {
    const rd = await bridgeFetch("/panels/discovered");
    if (rd.ok) {
      const jd = await rd.json();
      liveTabIds = new Set((jd.discovered || [])
        .map(d => d.tab_id).filter(v => v != null).map(Number));   // may be empty
    }
  } catch { /* keep the previous snapshot */ }
}

// ---- formatting ------------------------------------------------------
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
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

// ---- agent-row rendering ---------------------------------------------
const AUTO_ACTIVE_TTY_MAX_AGE_S = 2 * 60 * 60;

function bucketPanes() {
  const active = [], inactive = [];
  for (const p of panes) {
    const autoActive =
      !!p.has_agent_process &&
      typeof p.tty_atime_age_s === "number" &&
      p.tty_atime_age_s < AUTO_ACTIVE_TTY_MAX_AGE_S;
    const pinned = !!pinnedPanes[p.pane_id];
    if (autoActive || pinned) active.push(p);
    else inactive.push(p);
  }
  return { active, inactive };
}

function sessionUseCount() {
  const cnt = {};
  for (const p of panes) {
    if (p.session_id && !p.agent_exited) cnt[p.session_id] = (cnt[p.session_id] || 0) + 1;
  }
  return cnt;
}

function renderRow(p, opts = {}) {
  const { showAgo } = opts;
  const hasLabel = p.label && p.label !== "-";
  const display = hasLabel ? `${p.pane_id} ${p.label}` : p.pane_id;

  // dot
  let dotCls = "idle", dotTitle = "idle";
  if (p.agent_exited) { dotCls = "exited"; dotTitle = "agent exited — resume via 📋"; }
  else if (p.agent_status === "busy") { dotCls = "busy"; dotTitle = "busy"; }
  else if (p.agent_status) { dotCls = "idle"; dotTitle = p.agent_status; }
  else { dotCls = ""; dotTitle = ""; }

  // star
  const pinned = !!pinnedPanes[p.pane_id];
  const starHtml = `<span class="star ${pinned ? "on" : ""}" data-pane-id="${p.pane_id}" title="${pinned ? "unpin" : "pin"}">${pinned ? "★" : "☆"}</span>`;

  // ago
  let agoHtml = "";
  if (showAgo) {
    const ages = [p.tty_atime_age_s, p.tty_mtime_age_s].filter(v => typeof v === "number" && v >= 0);
    if (ages.length) {
      const baseAge = Math.min(...ages);
      agoHtml = `<span class="ago ago-live" data-base-age="${baseAge}" data-base-ts="${Date.now()}">${escapeHtml(fmtAgo(baseAge))}</span>`;
    } else {
      agoHtml = `<span class="ago">never</span>`;
    }
  }

  // copy-resume button
  const sid = p.session_id || "";
  const useCnt = (window._sessionUseCount || {})[sid] || 0;
  const sidInUseElsewhere = sid && useCnt > 1;
  const hasResumable = (p.has_agent_process || p.agent_exited) && p.agent_cwd && sid;
  let copyBtn;
  if (hasResumable && !sidInUseElsewhere) {
    copyBtn = `<span class="icon-btn pane-copy-resume" data-pane-id="${p.pane_id}" data-cwd="${escapeHtml(p.agent_cwd)}" data-sid="${escapeHtml(sid)}" title="copy resume command">📋</span>`;
  } else if (hasResumable) {
    copyBtn = `<span class="icon-btn disabled" title="session held by ${useCnt} panes">📋</span>`;
  } else if (p.has_agent_process && p.agent_cwd) {
    copyBtn = `<span class="icon-btn pane-copy-resume" data-pane-id="${p.pane_id}" data-cwd="${escapeHtml(p.agent_cwd)}" data-sid="" title="copy resume command (picker)">📋</span>`;
  } else {
    copyBtn = `<span class="icon-btn disabled">📋</span>`;
  }

  // browsers (placeholder)
  const browsersCount = Array.isArray(p.connected_browsers) ? p.connected_browsers.length : 0;
  const browsersHtml = browsersCount === 0
    ? `<span class="browsers">no browser agents</span>`
    : `<span class="browsers has-some">${browsersCount} routed</span>`;

  return `
    <div class="row" data-pane-id="${p.pane_id}">
      ${starHtml}
      <span class="dot ${dotCls}" title="${escapeHtml(dotTitle)}"></span>
      <span class="name-bracket"><span class="bk">[</span>${escapeHtml(display)}${agoHtml}<span class="bk">]</span></span>
      <span class="spacer"></span>
      ${browsersHtml}
      <input class="rename" data-pane-id="${p.pane_id}" placeholder="${escapeHtml(p.label || "rename…")}" maxlength="32" />
      <span class="actions">
        ${copyBtn}
        <span class="icon-btn pane-delete" data-pane-id="${p.pane_id}" data-has-agent="${p.has_agent_process ? "1" : "0"}" title="kill pane">🗑</span>
      </span>
    </div>`;
}

function renderAgents() {
  window._sessionUseCount = sessionUseCount();
  const { active, inactive } = bucketPanes();

  // The terminal-agent section is not part of the panel layout; bail out
  // cleanly when its containers are absent rather than throwing.
  if (!$("active-list")) return;
  const tc = $("terminal-count"); if (tc) tc.textContent = active.length + inactive.length;
  const ic = $("inactive-count"); if (ic) ic.textContent = inactive.length;

  const activeHtml = active.length
    ? active.map(p => renderRow(p)).join("")
    : `<div class="list-empty">no active agents</div>`;
  $("active-list").innerHTML = activeHtml;

  const inactiveHtml = inactive.length
    ? inactive.map(p => renderRow(p, { showAgo: true })).join("")
    : `<div class="list-empty">none</div>`;
  const il = $("inactive-list"); if (il) il.innerHTML = inactiveHtml;
}

// Which browser panels are real right now. Pure so tests can pin it down —
// see tests/browser-panel-visibility.test.js.
//   liveTabIds === null -> the extension has not reported; show everything
//                          rather than blanking the list.
//   otherwise           -> a panel is real only if its tab is still open,
//                          INCLUDING when the live set is empty.
function visibleBrowserPanels(panels, live) {
  if (!Array.isArray(panels)) return [];
  // Duck-typed rather than `instanceof Set` so the rule holds when the caller
  // is in another realm (the test harness loads this file in a vm context).
  if (!live || typeof live.has !== "function") return panels;
  return panels.filter(p => {
    if (p.kind === "stale") return false;
    const tid = Number(p.tab_id);
    return Number.isFinite(tid) && live.has(tid);
  });
}
if (typeof module === "object" && module.exports) module.exports = { visibleBrowserPanels };

function renderBrowsers() {
  const root = $("browser-list");
  // A registered panel whose Chrome tab is gone is not a chat you can talk to,
  // and showing it inflated BROWSER AGENTS. The entry stays in panels.json,
  // where `browser-bridge list` still surfaces it for cleanup.
  const panelsLive = visibleBrowserPanels(panels, liveTabIds);
  $("browser-count").textContent = panelsLive.length;
  if (!panelsLive.length) {
    root.innerHTML = `<div class="list-empty">no chats open</div>`;
    return;
  }
  // Chrome allows duplicate tab-group titles, so several rows can read
  // "Claude". Number the repeats, exactly as the extension panel does.
  const nameCounts = {};
  panelsLive.forEach(p => { const n = p.name || ""; nameCounts[n] = (nameCounts[n] || 0) + 1; });
  const nameSeen = {};
  panelsLive.forEach(p => {
    const n = p.name || "";
    if (nameCounts[n] > 1) {
      nameSeen[n] = (nameSeen[n] || 0) + 1;
      p.display_name = `${n} ${nameSeen[n]}`;
    } else {
      p.display_name = n;
    }
  });

  root.innerHTML = panelsLive.map(p => {
    const tid = p.tab_id;
    const clickable = Number.isFinite(tid);
    const short = p.short || ("/br" + tid);
    const name = p.name || "";
    // panel-name carried on row so double-click rename can resolve the
    // old name without re-fetching /panels/all. Registered panels can
    // be renamed; discovered ones can't (server rejects).
    const renameable = clickable && p.kind === "registered";

    // Identical decision tree to the extension panel's renderAgents(), so a
    // chat shows the same colour in both surfaces.
    const streaming = (browserActivity.streaming_tab_ids || []).includes(tid);
    const working   = (browserActivity.working_tab_ids   || []).includes(tid);
    const everReplied = (browserActivity.last_active_tab || {})[tid] !== undefined;
    let dotCls, dotTip;
    const tabGone = Number.isFinite(tid) && !!liveTabIds &&
                    typeof liveTabIds.has === "function" && !liveTabIds.has(tid);
    if (p.kind === "stale" || tabGone) { dotCls = "dead";       dotTip = "registered but tab closed"; }
    else if (streaming)                { dotCls = "responding"; dotTip = "responding (streaming reply)"; }
    else if (working)                  { dotCls = "active";     dotTip = "working (received task)"; }
    else if (p.kind === "discovered" && !everReplied)
                                       { dotCls = "discovered"; dotTip = "discovered (not registered yet)"; }
    else                               { dotCls = "idle";       dotTip = "idle"; }
    // Tab-group colour overrides the dot's fill, exactly as in the panel.
    // The pulse animation still comes from dotCls, so state is not lost.
    const tgCss = TG_COLOR_CSS[(browserActivity.group_colors || {})[String(tid)]];
    const collapsed = (browserActivity.collapsed_tab_ids || []).includes(tid);
    const openCls = collapsed ? " collapsed" : " expanded";

    // Context fill for this chat. "—" until the chat has run a turn.
    const cu = ctxUsage[String(tid)];
    const pct = cu && typeof cu.pct === "number" ? cu.pct : null;
    const ctxText = pct === null ? "&#8212;" : `${pct}%`;
    const ctxCls = pct === null ? "" : (pct >= 80 ? "ctx-hot" : pct >= 55 ? "ctx-warm" : "");
    const ctxTitle = pct === null
      ? "no turn measured yet in this chat"
      : `${(cu.input_tokens || 0).toLocaleString()} input tokens of ${CTX_WINDOW_TOKENS.toLocaleString()} on the last turn`;

    return `
    <div class="row ${renameable ? 'browser-row' : ''}"
         ${clickable ? `data-tab-id="${tid}"` : ''}
         ${renameable ? `data-panel-name="${escapeHtml(p.registered_name || name)}"` : ''}
         title="${renameable ? 'double-click → rename' : ''}">
      <span class="dot ${dotCls}${openCls}${clickable ? ' dot-focus' : ''}"
            ${clickable ? `data-focus-tab="${tid}"` : ''}
            ${tgCss ? `style="background:${tgCss}"` : ''}
            title="${dotTip}${collapsed ? ' · group collapsed' : ' · group open'}${clickable ? ' · click to focus this tab in Chrome' : ''}"></span>
      <span class="copy-id" data-copy-id="${tid || ""}" title="${short} — click to copy tab id ${tid || ""}">&#x29c9;</span>
      <span class="agent-name">${escapeHtml(p.display_name || name || short)}</span>
      <span class="spacer"></span>
      <span class="browsers ${ctxCls}" title="${ctxTitle}">${ctxText}</span>
    </div>`;
  }).join("");
}

async function renamePanelByOldName(oldName, newName) {
  try {
    const r = await bridgeFetch(`/panels/${encodeURIComponent(oldName)}/rename`, {
      method: "POST",
      body: JSON.stringify({ new_name: newName }),
    });
    if (!r.ok) {
      const body = await r.text();
      alert(`rename failed (${r.status}): ${body}`);
      return false;
    }
    return true;
  } catch (e) {
    alert(`rename failed: ${e.message}`);
    return false;
  }
}

// Swap a browser-row's name-bracket span for an <input> so the operator
// can edit. Enter saves; Escape cancels. On save, the next refreshAll
// re-renders the row with the new name.
function beginBrowserRowRename(row) {
  const oldName = row.dataset.panelName || "";
  const nameSpan = row.querySelector(".agent-name");
  if (!nameSpan) return;
  if (row.querySelector("input.panel-rename-input")) return;  // already editing
  const tabId = row.dataset.tabId;
  const short = "/br" + (panels.find(p => String(p.tab_id) === tabId)?.short?.replace(/^\/br/, "") ?? "?");
  const realShort = (panels.find(p => String(p.tab_id) === tabId)?.short) || "/br?";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "panel-rename-input";
  input.value = oldName;
  input.placeholder = "new name";
  input.maxLength = 40;
  // Preserve bracket framing visually: [/brN [input]]
  const prefix = document.createElement("span");
  prefix.className = "name-bracket";
  prefix.innerHTML = `<span class="bk">[</span>${escapeHtml(realShort)} `;
  const suffix = document.createElement("span");
  suffix.innerHTML = `<span class="bk">]</span>`;
  suffix.className = "name-bracket";
  nameSpan.replaceWith(prefix);
  prefix.appendChild(input);
  prefix.appendChild(suffix.firstChild);
  input.focus();
  input.select();
  let done = false;
  const cancel = () => { if (!done) { done = true; refreshAll(); } };
  input.addEventListener("keydown", async (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      const val = input.value.trim();
      if (!val || val === oldName) { cancel(); return; }
      if (!/^[A-Za-z0-9_-]{1,40}$/.test(val)) {
        input.classList.add("invalid"); return;
      }
      done = true;
      input.disabled = true;
      const ok = await renamePanelByOldName(oldName, val);
      if (ok) await refreshAll();
      else cancel();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", () => { setTimeout(cancel, 50); });  // 50ms so Enter handler can fire first
}

async function openNewClaudeTab() {
  try {
    const r = await bridgeFetch("/chrome/open-new-claude-tab", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (!r.ok) alert(`open tab failed: ${await r.text()}`);
  } catch (e) { alert(`open tab failed: ${e.message}`); }
}

async function focusChromeTab(tabId) {
  try {
    const r = await bridgeFetch("/chrome/focus-tab", {
      method: "POST",
      body: JSON.stringify({ tab_id: tabId }),
    });
    if (!r.ok) alert(`focus failed: ${await r.text()}`);
  } catch (e) { alert(`focus failed: ${e.message}`); }
}

function renderSkills() {
  $("skills-count").textContent = skills.length;
  const root = $("skills-strip");
  if (!skills.length) {
    root.innerHTML = `<span class="list-empty" style="border:none; padding:0;">no skills (drop .md files in ~/.claude/browser-skills/)</span>`;
    return;
  }
  root.innerHTML = skills.map(s => {
    const label = s.name || "(unnamed)";
    const desc = s.description || s.path || "";
    return `<span class="skill-chip" draggable="true"
                   data-skill-path="${escapeHtml(s.path)}"
                   data-skill-name="${escapeHtml(label)}"
                   title="${escapeHtml(desc)}">${escapeHtml(label)}</span>`;
  }).join("");
}

function renderAll() {
  renderBrowsers();
  renderAgents();
  renderSkills();
}

// ---- live ago ticker -------------------------------------------------
setInterval(() => {
  const spans = document.querySelectorAll(".ago-live");
  if (!spans.length) return;
  const nowMs = Date.now();
  spans.forEach((el) => {
    const baseAge = parseInt(el.dataset.baseAge, 10);
    const baseTs = parseInt(el.dataset.baseTs, 10);
    if (!Number.isFinite(baseAge) || !Number.isFinite(baseTs)) return;
    el.textContent = fmtAgo(baseAge + Math.floor((nowMs - baseTs) / 1000));
  });
}, 1000);

// ---- actions ---------------------------------------------------------
async function renamePane(paneId, newName) {
  try {
    const r = await bridgeFetch("/tmux-pane/rename", {
      method: "POST",
      body: JSON.stringify({ pane_id: paneId, new_name: newName }),
    });
    if (!r.ok) alert(`rename failed: ${await r.text()}`);
  } catch (e) { alert(`rename failed: ${e.message}`); }
}
async function deletePane(paneId) {
  try {
    const r = await bridgeFetch("/tmux-pane/delete", {
      method: "POST",
      body: JSON.stringify({ pane_id: paneId }),
    });
    if (!r.ok) { alert(`delete failed: ${await r.text()}`); return; }
    if (pinnedPanes[paneId]) { delete pinnedPanes[paneId]; LS.set("pinnedPanes", pinnedPanes); }
    await refreshPanes(); renderAgents();
  } catch (e) { alert(`delete failed: ${e.message}`); }
}
async function dispatchSkill(paneId, skillPath, rowEl) {
  try {
    const r = await bridgeFetch("/skills/dispatch", {
      method: "POST",
      body: JSON.stringify({ pane_id: paneId, skill_path: skillPath }),
    });
    if (!r.ok) { alert(`dispatch failed: ${await r.text()}`); return; }
    if (rowEl) {
      rowEl.classList.add("flash-ok");
      setTimeout(() => rowEl.classList.remove("flash-ok"), 600);
    }
  } catch (e) { alert(`dispatch failed: ${e.message}`); }
}

// ---- event delegation ------------------------------------------------
document.addEventListener("click", (ev) => {
  const t = ev.target;
  if (!t || !t.classList) return;
  // + NEW button — open a fresh Claude tab via the extension
  if (t.id === "new-claude-tab") {
    openNewClaudeTab();
    return;
  }
  // Browser-agent row clicked — focus that tab in Chrome AFTER a short
  // delay so a double-click can intercept and become a rename instead.
  // The flash class gives instant visual feedback so the operator knows
  // their click registered even before Chrome's window comes forward.
  // Focus-in-Chrome is bound to the status dot alone. It used to fire from
  // anywhere on the row, which made the copy glyph and the name ambiguous
  // to click.
  const focusDot = t.closest && t.closest(".dot-focus");
  if (focusDot && focusDot.dataset.focusTab) {
    const row = focusDot.closest(".row");
    if (row && row.querySelector("input.panel-rename-input")) return;
    focusChromeTab(parseInt(focusDot.dataset.focusTab, 10));
    return;
  }
  if (t.classList.contains("star")) {
    togglePinned(t.dataset.paneId);
    return;
  }
  if (t.classList.contains("pane-delete")) {
    const paneId = t.dataset.paneId;
    const hasAgent = t.dataset.hasAgent === "1";
    const msg = hasAgent
      ? `Kill pane ${paneId}?\n\nWARNING: this pane has a live agent process. Killing terminates it.`
      : `Kill pane ${paneId}?`;
    if (confirm(msg)) deletePane(paneId);
    return;
  }
  if (t.classList.contains("pane-copy-resume")) {
    const cwd = t.dataset.cwd, sid = t.dataset.sid;
    if (!cwd) return;
    const safeCwd = cwd.replace(/'/g, "'\\''");
    const resumeArg = sid ? `--resume ${sid}` : "--resume";
    const cmd = `cd '${safeCwd}' && claude ${resumeArg} --dangerously-skip-permissions`;
    navigator.clipboard.writeText(cmd).then(() => {
      const orig = t.textContent;
      t.textContent = "✓";
      setTimeout(() => { t.textContent = orig; }, 800);
    }).catch((e) => alert(`copy failed: ${e.message}`));
    return;
  }
});

const PANE_NAME_RE = /^[A-Za-z0-9_-]{0,32}$/;
document.addEventListener("input", (ev) => {
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains("rename")) return;
  if (t.value === "" || PANE_NAME_RE.test(t.value)) t.classList.remove("invalid");
  else t.classList.add("invalid");
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains("rename")) return;
  ev.preventDefault();
  const value = t.value.trim();
  if (!PANE_NAME_RE.test(value) || !value) { t.classList.add("invalid"); return; }
  renamePane(t.dataset.paneId, value).then(() => { t.value = ""; });
});

// drag-and-drop dispatch
// Double-click a browser-row -> inline rename. Cancels the pending
// single-click focus timer so a rename doesn't ALSO focus the tab.
document.addEventListener("dblclick", (ev) => {
  const row = ev.target.closest && ev.target.closest(".browser-row");
  if (!row || !row.dataset.panelName) return;
  ev.preventDefault();
  if (row._clickTimer) { clearTimeout(row._clickTimer); row._clickTimer = null; }
  // Clear text selection that the browser auto-selects on dblclick
  if (window.getSelection) window.getSelection().removeAllRanges();
  beginBrowserRowRename(row);
});

document.addEventListener("dragstart", (ev) => {
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains("skill-chip")) return;
  ev.dataTransfer.setData("text/x-skill-path", t.dataset.skillPath || "");
  ev.dataTransfer.effectAllowed = "copy";
});
document.addEventListener("dragover", (ev) => {
  const row = ev.target.closest && ev.target.closest(".row");
  if (!row || !row.dataset.paneId) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "copy";
  row.classList.add("drop-target");
});
document.addEventListener("dragleave", (ev) => {
  const row = ev.target.closest && ev.target.closest(".row");
  if (row) row.classList.remove("drop-target");
});
document.addEventListener("drop", (ev) => {
  const row = ev.target.closest && ev.target.closest(".row");
  if (!row || !row.dataset.paneId) return;
  ev.preventDefault();
  row.classList.remove("drop-target");
  const skillPath = ev.dataTransfer.getData("text/x-skill-path");
  if (!skillPath) return;
  dispatchSkill(row.dataset.paneId, skillPath, row);
});

// inactive section caret toggle (persisted)
$("inactive-details")?.addEventListener("toggle", (ev) => {
  LS.set("inactiveOpen", ev.target.open);
  const caret = ev.target.querySelector("summary .caret");
  if (caret) caret.textContent = ev.target.open ? "▾" : "▸";
});
$("skills-details").addEventListener("toggle", (ev) => {
  LS.set("skillsOpen", ev.target.open);
  const caret = ev.target.querySelector("summary .caret");
  if (caret) caret.textContent = ev.target.open ? "▾" : "▸";
});

// settings token save
$("settings-save").addEventListener("click", () => {
  const t = $("settings-token").value.trim();
  if (t) LS.set("token", t);
  refreshAll();
});
$("settings-reset")?.addEventListener("click", () => {
  LS.del("token");
  showTokenSetup();
});

// token bootstrap save
$("token-save").addEventListener("click", () => {
  const t = $("token-input").value.trim();
  if (!t) return;
  LS.set("token", t);
  showMain();
  refreshAll();
});

// ---- bootstrap -------------------------------------------------------
function showTokenSetup() {
  $("token-setup").hidden = false;
  $("dash-main").hidden = true;
}
function showMain() {
  $("token-setup").hidden = true;
  $("dash-main").hidden = false;
  $("settings-token").value = getToken();
  // Restore collapse prefs
  const inactiveOpen = LS.get("inactiveOpen");
  const inact = $("inactive-details");
  if (inact && typeof inactiveOpen === "boolean") inact.open = inactiveOpen;
  const skillsOpen = LS.get("skillsOpen");
  if (typeof skillsOpen === "boolean") $("skills-details").open = skillsOpen;
}

async function refreshAll() {
  await Promise.all([refreshHealth(), refreshPanes(), refreshSkills(), refreshPanels()]);
  renderAll();
}

function boot() {
  initTheme();
  const tok = getToken();
  if (!tok) { showTokenSetup(); return; }
  showMain();
  refreshAll();
  setInterval(refreshAll, 3000);

  // Chrome throttles setInterval in a BACKGROUND tab to roughly once a
  // minute. The dashboard normally sits in a background tab while you work
  // elsewhere, so a panel opened in the meantime could take up to a minute
  // to appear — it looked like the panel was missing when it was only late.
  // Refresh the moment the tab becomes visible or regains focus.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshAll();
  });
  window.addEventListener("focus", () => refreshAll());
}
boot();

// Click the ⧉ glyph on a browser row to copy that chat's tab id, matching
// the extension panel. Same-width glyph swap so the row never reflows.
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest && ev.target.closest(".copy-id");
  if (!el) return;
  ev.stopPropagation();                       // don't also focus the tab
  const id = el.dataset.copyId;
  if (!id) return;
  try {
    await navigator.clipboard.writeText(String(id));
    const prev = el.textContent;
    el.textContent = "\u2713";
    el.classList.add("copied");
    setTimeout(() => { el.textContent = prev; el.classList.remove("copied"); }, 900);
  } catch (_) {}
});
