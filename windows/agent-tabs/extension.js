// Agent Tabs: turns request files written by agent-spawn, agent-resume and agent-rename
// into VS Code terminal tabs attached to tmux sessions in WSL.
//
// Queue: <home>/.claude/agent-tabs/*.json (Windows home; WSL sees it under /mnt/c/Users/<you>).
//   {"session":"claude-pane-6","distro":"Ubuntu","from":"<caller tab>","below":"<tab>"}  open a tab
//   {"action":"rename","from":"<tab>","to":"<new name>"}
//   {"action":"focus","name":"<tab>"}
//   {"action":"move","name":"<tab>","to":"left|right|above|below"}
//   {"action":"below","name":"<tab>","parent":"<tab>"}
//   {"action":"list"}
// State: state/tabs.json and state/tabs-<pid>.json (tab labels), state/agent-tabs.log.
const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const POLL_MS = 1000;
const CLAIM_GRACE_MS = 3000;
let queue = Promise.resolve();

function queueDir() {
  const configured = vscode.workspace.getConfiguration('agentTabs').get('queueDir');
  return configured && configured.trim() ? configured.trim() : path.join(os.homedir(), '.claude', 'agent-tabs');
}

// agent-spawn reads this log to confirm the tab opened, and in which window (the "groups" part).
function log(msg) {
  try {
    const dir = path.join(queueDir(), 'state');
    fs.mkdirSync(dir, { recursive: true });
    const groups = vscode.window.tabGroups.all.map((g) => `${g.viewColumn}:[${g.tabs.map((t) => t.label).join(',')}]`).join(' ');
    fs.appendFileSync(path.join(dir, 'agent-tabs.log'), `${new Date().toISOString()} ${msg} | groups ${groups}\n`);
  } catch {}
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const findTerminal = (name) => vscode.window.terminals.filter((t) => t.name === name).pop();
function groupOf(label) {
  return vscode.window.tabGroups.all.find((g) => g.tabs.some((t) => t.input instanceof vscode.TabInputTerminal && t.label === label));
}

// A tab attached to a tmux session. When the attach fails, the shell stays open and shows why,
// instead of the tab closing at once with no message.
function agentTerminal(session, distro) {
  const args = [];
  if (distro) args.push('-d', String(distro));
  const cmd =
    `tmux attach -t '${session}' || { rc=$?; echo; ` +
    `echo "agent-tabs: could not attach to tmux session '${session}' (exit $rc)"; ` +
    `echo "sessions on this server:"; tmux list-sessions 2>&1 | sed "s/^/  /"; ` +
    `echo; echo "this shell stays open so you can read the error"; exec bash -i; }`;
  args.push('--', 'bash', '-lc', cmd);
  return vscode.window.createTerminal({
    name: session,
    shellPath: 'wsl.exe',
    shellArgs: args,
    isTransient: true,
    location: { viewColumn: vscode.ViewColumn.Active },
  });
}

async function openTab(req) {
  const session = String(req.session || '').trim();
  if (!/^[A-Za-z0-9._][A-Za-z0-9._ -]*$/.test(session)) {
    vscode.window.showErrorMessage(`Agent Tabs: bad session name ${JSON.stringify(req.session)}`);
    return;
  }
  const terminal = agentTerminal(session, req.distro);
  log(`open ${session} below=${req.below || ''}`);
  terminal.show(false);
  if (req.below) {
    await wait(800);
    await moveBelow({ name: session, parent: req.below });
  }
}

// Walk the tab into the parent's group, then split it down from there. Both groups are re-read
// every step: a group that empties closes and every column after it shifts by one.
async function moveBelow(req) {
  const name = String(req.name || '').trim();
  const parent = String(req.parent || '').trim();
  const terminal = findTerminal(name);
  if (!terminal || !groupOf(parent)) {
    vscode.window.showErrorMessage(`Agent Tabs: cannot move ${JSON.stringify(name)} below ${JSON.stringify(parent)}`);
    return;
  }
  terminal.show(false);
  await wait(300);
  for (let i = 0; i < 12 && groupOf(name) && groupOf(parent) && groupOf(name).viewColumn !== groupOf(parent).viewColumn; i++) {
    await vscode.commands.executeCommand(groupOf(name).viewColumn > groupOf(parent).viewColumn
      ? 'workbench.action.moveEditorToPreviousGroup'
      : 'workbench.action.moveEditorToNextGroup');
    await wait(300);
  }
  if (!groupOf(name) || !groupOf(parent) || groupOf(name).viewColumn !== groupOf(parent).viewColumn) {
    log(`below abort ${name}: not in ${parent}'s group`);
    return;
  }
  await vscode.commands.executeCommand('workbench.action.moveEditorToBelowGroup');
  log(`below done ${name}`);
}

// The tab was created with a fixed name, so tmux's title never reaches it. renameWithArg acts on
// the active terminal, so focus the tab first and let the focus land.
async function renameTab(req) {
  const from = String(req.from || '').trim();
  const to = String(req.to || '').trim();
  const terminal = findTerminal(from);
  if (!terminal || !to) {
    log(`rename FAIL no tab ${from} -> ${to}`);
    return;
  }
  terminal.show(false);
  await wait(300);
  await vscode.commands.executeCommand('workbench.action.terminal.renameWithArg', { name: to });
  log(`rename ${terminal.name === to ? 'ok' : 'FAIL still ' + terminal.name} ${from} -> ${to}`);
}

function focusTab(req) {
  const terminal = findTerminal(String(req.name || '').trim());
  if (terminal) terminal.show(false);
}

async function moveTab(req) {
  const cmds = {
    left: 'workbench.action.moveEditorToLeftGroup',
    right: 'workbench.action.moveEditorToRightGroup',
    above: 'workbench.action.moveEditorToAboveGroup',
    below: 'workbench.action.moveEditorToBelowGroup',
  };
  const terminal = findTerminal(String(req.name || '').trim());
  const cmd = cmds[String(req.to || '').trim()];
  if (!terminal || !cmd) return;
  terminal.show(false);
  await wait(300);
  // The move command acts on the active editor: never move a different tab.
  if (vscode.window.activeTerminal !== terminal) return;
  await vscode.commands.executeCommand(cmd);
}

// tabs-<pid>.json per window, so agent-rename sees the tabs of every window.
function writeTabs(dir) {
  const tabs = [];
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      if (tab.input instanceof vscode.TabInputTerminal) tabs.push({ label: tab.label, active: tab.isActive && group.isActive });
    }
  }
  try {
    const state = path.join(dir, 'state');
    fs.mkdirSync(state, { recursive: true });
    const body = JSON.stringify({ ts: Date.now(), tabs });
    fs.writeFileSync(path.join(state, 'tabs.json'), body);
    fs.writeFileSync(path.join(state, `tabs-${process.pid}.json`), body);
  } catch {}
}

// Several windows poll the same queue. A request that names a tab goes to the window that has
// that tab; a new tab goes to the window holding the caller's tab, else the focused window.
// After CLAIM_GRACE_MS any window takes it, so nothing stays in the queue.
function shouldClaim(file) {
  let req, age;
  try {
    req = JSON.parse(fs.readFileSync(file, 'utf8'));
    age = Date.now() - fs.statSync(file).mtimeMs;
  } catch {
    return false;
  }
  if (age > CLAIM_GRACE_MS) return true;
  const has = (n) => n && vscode.window.terminals.some((t) => t.name === n);
  if (req.action && req.action !== 'list' && (req.from || req.name)) return has(req.from || req.name);
  if (!req.action && (req.below || req.from)) return has(req.below) || has(req.from);
  return vscode.window.state.focused;
}

function drain(dir) {
  let names;
  try { names = fs.readdirSync(dir); } catch { return; }
  for (const name of names) {
    if (!name.endsWith('.json')) continue;
    const file = path.join(dir, name);
    if (!shouldClaim(file)) continue;
    let req;
    try {
      req = JSON.parse(fs.readFileSync(file, 'utf8'));
      fs.unlinkSync(file);
    } catch {
      continue;
    }
    // One request at a time: each is a chain of focus and move commands on the active editor.
    queue = queue.then(() => {
      if (req.action === 'rename') return renameTab(req);
      if (req.action === 'focus') return focusTab(req);
      if (req.action === 'below') return moveBelow(req);
      if (req.action === 'move') return moveTab(req);
      if (req.action === 'list') return writeTabs(dir);
      return openTab(req);
    }).catch((err) => vscode.window.showErrorMessage(`Agent Tabs: ${name} failed: ${err.message}`));
  }
}

function activate(context) {
  const dir = queueDir();
  try { fs.mkdirSync(dir, { recursive: true }); } catch {}
  // Requests queued before this window started are stale: a reload must not reopen old tabs.
  try {
    for (const name of fs.readdirSync(dir)) if (name.endsWith('.json')) fs.unlinkSync(path.join(dir, name));
  } catch {}
  log('activate');
  const timer = setInterval(() => drain(dir), POLL_MS);
  let watcher = null, soon = null;
  try {
    watcher = fs.watch(dir, (_, name) => {
      if (!name || !String(name).endsWith('.json')) return;
      clearTimeout(soon);
      soon = setTimeout(() => drain(dir), 50);
    });
    watcher.on('error', () => {});
  } catch {}
  writeTabs(dir);
  context.subscriptions.push(
    vscode.window.tabGroups.onDidChangeTabs(() => writeTabs(dir)),
    vscode.window.tabGroups.onDidChangeTabGroups(() => writeTabs(dir)),
    vscode.commands.registerCommand('agentTabs.processQueue', () => drain(dir)),
    { dispose: () => { clearInterval(timer); clearTimeout(soon); watcher && watcher.close(); } },
  );
}

function deactivate() {
  try { fs.unlinkSync(path.join(queueDir(), 'state', `tabs-${process.pid}.json`)); } catch {}
}

module.exports = { activate, deactivate };
