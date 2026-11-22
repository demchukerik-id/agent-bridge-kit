# Troubleshooting

Every issue we hit during the build, with the fix that worked.

## Tab mode

### Popup says `server unreachable`

The bridge server isn't running, or WSL2 localhost forwarding is off.
Start the server in WSL:

```bash
python3 <repo>/server/server.py
```

Test from PowerShell on Windows:

```powershell
Invoke-WebRequest http://localhost:7777/health
```

Should return `{"ok":true,...}`.

### Popup says `auth: WRONG TOKEN`

Re-paste the token from `~/.config/claude-web-bridge/token`. The server
only regenerates a token if the file is missing.

### Badge stuck at `OFF`

Service worker can't reach the bridge. Click the extension's *Service worker*
link at `chrome://extensions` and check the console. Common: token not saved
yet (use the popup), or WSL2 localhost forwarding is off (rebooted with a
network fault — try `wsl --shutdown` and relaunch).

### Prompt types but never submits

claude.ai changed the send-button selector. Open devtools on the claude.ai
tab, inspect the send button, update `SELECTORS.send` in
`ui/extension/content.js`, reload the extension.

### Reply never comes back

The assistant-turn selector is stale, or the streaming-done detection isn't
firing. From devtools console on the claude.ai tab:

```js
__claudeBridge.SELECTORS
```

Cross-reference with the actual DOM and patch.

### `[bridge error] cannot inject into tab N`

The pinned URL is on a Chrome internal page (`chrome://`,
`chrome-extension://`, `file://`, etc.). The popup refuses these now, but
older pins from before that check may persist. `browser-bridge forget <panel>`
and re-pin a regular https:// page.

## Side-panel mode

### `cannot reach Chrome on any of [...]:9222`

Chrome isn't running with the debug flag, or our networking pieces aren't
in place. Check, in order:

1. **Chrome process exists with the flag**:
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
       Where-Object { $_.CommandLine -match 'remote-debugging-port' } |
       Measure-Object | Select-Object -ExpandProperty Count
   ```
   If 0, run `launch-chrome-debug.ps1`.

2. **Listener on 9222**:
   ```powershell
   Get-NetTCPConnection -LocalPort 9222
   ```
   Should show `127.0.0.1:9222 Listen` (Chrome) and `172.22.112.1:9222 Listen`
   (relay). If only Chrome, start `relay-9222.py`.

3. **WSL can connect**:
   ```bash
   curl -sS -m 5 http://172.22.112.1:9222/json/version
   ```
   If timeout: Hyper-V firewall is blocking. Run `allow-wsl-9222.ps1`.
   If "connection refused": relay isn't running.

### Chrome ignores `--remote-debugging-port`

Symptoms: Chrome processes exist with the flag in their command line, but
no listener on 9222.

Cause: Chrome 136+ silently disables the flag on the default user profile
as a security measure (anti-token-theft).

Fix: launch with `--user-data-dir=$env:LOCALAPPDATA\chrome-debug` (separate
profile). The `launch-chrome-debug.ps1` script does this.

If even that fails: ensure no other Chrome processes are using the same
data dir (`Stop-Process -Name chrome -Force` before launching).

### Daemon picks up item but reply never arrives

In order of likelihood:

1. **Selectors stale.** Run `cdp_daemon.py discover` while the side panel
   is open and the conversation is fresh. Compare to selectors in
   `cdp_daemon.py`. The most likely-to-rot ones are
   `[data-test-id="message-input"]`, `button[aria-label="Send message"]`,
   `.claude-response`.

2. **Daemon is in a 600s wait** because Claude is slow or stuck. Check
   daemon log for the `polling: len=N, stable_for=M.Ks` lines emitted every
   5 seconds.

3. **Side panel was closed.** Reopen it (orange Claude icon) and retry. The
   daemon errors out cleanly when it can't find the target.

4. **Conversation got into a stuck state** (we hit this when sending many
   prompts rapidly). Click "Clear chat" in the side panel and retry. There's
   also a programmatic `clear_chat.py` helper in `/tmp` from earlier
   diagnostics if you want to script it.

5. **Multi-debugger interference.** Don't run `inspect-stream.py` or other
   CDP scripts while the daemon is mid-dispatch — multiple WebSocket
   connections to the same target can interact badly.

### Prompt arrives in side panel with a stray character at the start (e.g. "gtest...")

You touched the keyboard during `Input.insertText`. The CDP types into the
focused element exactly like a real keyboard. Stay off the keyboard during
dispatch.

### `[bridge error] CDP send failed: CDP Runtime.evaluate timed out`

The 650s safety timeout fired. Either Claude is taking longer than 10
minutes (rare; bump `TIMEOUT_S` in `cdp_daemon.py`), or the daemon's CDP
client lost its grip on the WebSocket. Restart the daemon.

## Networking gotchas (WSL ↔ Windows)

### Windows-side server unreachable from WSL

WSL2's `127.0.0.1` is WSL itself, not Windows. To reach Windows from WSL:

- The Windows host is at the WSL gateway IP (try `ip route | grep default`
  in WSL).
- Or use mirrored networking mode (.wslconfig) — clean but requires WSL
  restart.
- Or run a relay on Windows binding the WSL-facing virtual NIC's IP.

### WSL-side server unreachable from Windows

This direction *does* work automatically in WSL2 — Chrome on Windows can hit
`http://localhost:7777` and reach a server in WSL bound to 127.0.0.1.

### Hyper-V firewall blocks new ports by default

`Get-NetFirewallHyperVVMSetting` shows `DefaultInboundAction = Block` for
the WSL VM creator. Add an allow rule per port:

```powershell
# Requires admin
New-NetFirewallHyperVRule -Name "WSL-9222" `
    -VMCreatorId "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" `
    -Direction Inbound -Protocol TCP -LocalPorts 9222 -Action Allow
```

### Standard Windows Firewall auto-blocks python.exe

Windows sometimes creates `Block Inbound python.exe` rules silently when a
UAC prompt is dismissed. Symptoms: relay listens fine but no inbound
connections succeed even with Hyper-V allow rule. Remove the block rules
(see `fix-fw-python.ps1`).

## Shell quoting issues

The Windows-side Bash tool we used during build interpolates variables
inside single quotes despite POSIX semantics. Symptom: `"Bearer $TOKEN"`
arrives at the inner shell as `"Bearer "`.

Workaround in this project: scripts in `bin/` and `daemon/` are file-based
and don't suffer this. For one-off testing, use base64-encoded scripts or
write to a file first.

## When all else fails

1. Stop everything (`pkill -f cdp_daemon.py`, kill the bridge server, close
   Chrome).
2. Restart in the order from `setup.md` step 4 onward.
3. If still broken, run `cdp_daemon.py targets` and `cdp_daemon.py discover`
   to verify CDP plumbing, then send one simple prompt with everything else
   quiet (no extra CDP scripts running).
