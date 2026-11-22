# Setup

Steps to get the bridge running from scratch on Windows + WSL2 Ubuntu.
Do tab mode first (it's enough for most uses); add side-panel mode only
if you need to drive the Claude in Chrome (Beta) extension.

## Prerequisites

- Windows 11 (we tested on build 26200) with WSL2 Ubuntu.
- Chrome installed at `C:\Program Files\Google\Chrome\Application\chrome.exe`
  (adjust paths if elsewhere).
- Python 3 in WSL (`python3` — we used 3.14).
- Python on Windows (we used 3.12) — only needed for the side-panel relay.
- `tmux` and `tmux-bridge` already configured in WSL (this project sits on
  top of the existing smux skill).

## Tab mode (~5 minutes)

### 1. Start the bridge server in WSL

```bash
python3 <repo>/server/server.py
```

It generates a bearer token at `~/.config/claude-web-bridge/token` on first
run. Keep this terminal open (or run under `nohup` / a systemd unit later).

### 2. Load the Chrome extension

1. Open `chrome://extensions`.
2. Toggle **Developer mode** on.
3. Click **Load unpacked**, select
   `\\wsl.localhost\Ubuntu\home\you\browser-claude-bridge\extension`.
4. Click the *browser-claude-bridge* icon. Paste the bearer token. Save.
5. Status should show `server: ok` and `auth: ok`.

### 3. Use it

```bash
# from any tmux pane
browser-bridge list                                   # list panels with their /brN aliases
browser-bridge name /br1 my-panel                    # give a panel a stable name
browser-bridge message my-panel "hello claude"       # send and get the reply in your pane
```

Replies arrive in your tmux pane via `tmux-bridge message`. See
[`usage.md`](usage.md) for the full CLI surface.

## Side-panel mode (~30 minutes — one-time)

This drives Anthropic's Claude in Chrome (Beta) extension. It cannot be
done from inside Chrome; an external CDP daemon is required. The Chrome
restrictions add several gotchas — do these in order.

### 4. Launch Chrome with debug flag in a separate profile

Chrome 136+ silently disables `--remote-debugging-port` on the default
user profile. Use a separate `--user-data-dir`. Run on Desktop:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\Desktop\launch-chrome-debug.ps1"
```

Script content (already saved to your Desktop):

```powershell
$chrome  = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$dataDir = Join-Path $env:LOCALAPPDATA "chrome-debug"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
& $chrome `
    "--user-data-dir=$dataDir" `
    "--remote-debugging-port=9222" `
    "--remote-debugging-address=0.0.0.0" `
    "--no-first-run" `
    "--no-default-browser-check"
```

### 5. Set up the new profile (one-time)

Inside the new Chrome window:
- Install Anthropic's "Claude in Chrome (Beta)" from the Chrome Web Store.
  Sign in to Claude.
- Load *browser-claude-bridge* from `\\wsl.localhost\...\extension` and paste
  the bearer token (just like step 2).
- Click the orange Claude icon in the toolbar to open the side panel — it
  must be open for the daemon to find a CDP target.

### 6. Start the WSL → Windows TCP relay

Chrome silently ignores `--remote-debugging-address=0.0.0.0` and only binds
127.0.0.1. WSL2 cannot reach Windows 127.0.0.1 directly. The relay binds
the WSL-facing virtual NIC and forwards to Chrome's loopback:

```powershell
# Run from your interactive PowerShell (NOT through anything that detaches)
python "$HOME\Desktop\relay-9222.py"
```

Listens on `172.22.112.1:9222` (WSL gateway) and forwards to `127.0.0.1:9222`.

### 7. Allow inbound TCP/9222 on the Hyper-V firewall (one-time)

Win11's WSL networking has its own Hyper-V firewall, separate from standard
Windows Firewall. Default action is *Block*. Add an allow rule:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\Desktop\allow-wsl-9222.ps1"
```

Accept the UAC prompt. The rule is permanent.

### 8. Remove the auto-block of python.exe (one-time)

When Python first tried to listen, Windows likely created
`Block Inbound python.exe` rules in standard Firewall. Remove them:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\Desktop\fix-fw-python.ps1"
```

Accept UAC. This deletes the auto-block rules and adds an explicit
*Allow Inbound TCP/9222*.

### 9. Verify Chrome is reachable from WSL

```bash
python3 <repo>/daemon/cdp_daemon.py targets
```

You should see a `★`-marked line for the Claude side panel. If you see
*connection timed out* or *connection refused*, walk back through 6–8.

### 10. Run the side-panel daemon

```bash
CDP_HOST=172.22.112.1 python3 <repo>/daemon/cdp_daemon.py run
```

Leave it running. From a tmux pane:

```bash
browser-bridge message /br1 "Switch to Gmail and summarize the latest email"
```

Approve any actions Claude requests in the side panel. The reply lands back
in your pane when Claude finishes (text stable for 3s).

## After-reboot routine

```powershell
# Windows: launch debug Chrome, open side panel, start relay
launch-chrome-debug.ps1
python relay-9222.py
```

```bash
# WSL
python3 <repo>/server/server.py &
CDP_HOST=172.22.112.1 python3 <repo>/daemon/cdp_daemon.py run &
```

A future iteration could systemd-ify the WSL processes and use Task
Scheduler for the Windows ones.
