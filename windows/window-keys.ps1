# window-keys.ps1 — bring one named Chrome window to the front and press keys in it.
#
# Called by scripts/bridge-open-panel, which prepends $WindowName and $Keys
# and runs this through powershell.exe -EncodedCommand (no file on the
# Windows side needed).
#
#   $WindowName  exact title of the bridge window, its --window-name
#                ("you@ - bridge pfl")
#   $Keys        array of chords, pressed in order: "ctrl+t", "alt+w".
#                Empty: only bring the window to the front.
#   $Restore     $true: after the keys, give focus back to the window that had
#                it before, so Chrome is in front only for the presses.
#
# Keys are pressed ONLY after GetForegroundWindow() returns that exact window.
# On 2026-09-26 an unguarded SendKeys("%w") landed in the VS Code window that
# hosted the calling agent and killed its session. Exit 1 = nothing pressed.

Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class BW {
 public delegate bool EnumProc(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
 [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
 [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
 [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@

$target = [IntPtr]::Zero
[BW]::EnumWindows({ param($h, $l)
  if ([BW]::IsWindowVisible($h)) {
    $sb = New-Object Text.StringBuilder 512
    [BW]::GetWindowText($h, $sb, 512) | Out-Null
    if ($sb.ToString() -eq $WindowName) { $script:target = $h }
  }
  $true }, [IntPtr]::Zero) | Out-Null
if ($target -eq [IntPtr]::Zero) { Write-Output "ABORT: no window titled '$WindowName'"; exit 1 }
$prev = [BW]::GetForegroundWindow()
$t0 = Get-Date

function Focus-Window($w) {
  if ([BW]::GetForegroundWindow() -eq $w) { return $true }
  if ([BW]::IsIconic($w)) { [BW]::ShowWindow($w, 9) | Out-Null }
  # Windows only lets the process that sent the last input change focus. A
  # Shift tap makes that this process and does nothing in the window that has
  # focus. (Alt would open the menu of whatever app the user is using.)
  [BW]::keybd_event(0x10, 0, 0, [UIntPtr]::Zero); [BW]::keybd_event(0x10, 0, 2, [UIntPtr]::Zero)
  $fg = [BW]::GetForegroundWindow(); $p = 0
  $fgThread = [BW]::GetWindowThreadProcessId($fg, [ref]$p)
  $me = [BW]::GetCurrentThreadId()
  # Joining the foreground thread's input queue lifts Windows' focus-steal lock.
  [BW]::AttachThreadInput($me, $fgThread, $true) | Out-Null
  [BW]::BringWindowToTop($w) | Out-Null
  [BW]::SetForegroundWindow($w) | Out-Null
  [BW]::AttachThreadInput($me, $fgThread, $false) | Out-Null
  for ($i = 0; $i -lt 10 -and [BW]::GetForegroundWindow() -ne $w; $i++) { Start-Sleep -Milliseconds 25 }
  # Foreground is reported before Chrome reads keys: a Ctrl+T pressed at once
  # opened no tab (2026-10-04). Let a freshly activated window settle.
  Start-Sleep -Milliseconds 100
  return ([BW]::GetForegroundWindow() -eq $w)
}
function Focus-Target { Focus-Window $target }

function Restore-Prev {
  # Only after the last key is released, and never to the bridge window itself.
  if (-not $Restore -or $prev -eq [IntPtr]::Zero -or $prev -eq $target) { return }
  $ok = Focus-Window $prev
  $ms = [int]((Get-Date) - $t0).TotalMilliseconds
  Write-Output ("restored focus=" + $ok + " chrome_front_ms=" + $ms)
}

$vk = @{ ctrl = 0x11; alt = 0x12; shift = 0x10 }
function Press($chord) {
  $parts = $chord.ToLower().Split('+')
  $mods = $parts[0..($parts.Length - 2)] | ForEach-Object { [byte]$vk[$_] }
  $key = [byte][char]$parts[-1].ToUpper()
  foreach ($m in $mods) { [BW]::keybd_event($m, 0, 0, [UIntPtr]::Zero) }
  [BW]::keybd_event($key, 0, 0, [UIntPtr]::Zero)
  [BW]::keybd_event($key, 0, 2, [UIntPtr]::Zero)
  [array]::Reverse($mods)
  foreach ($m in $mods) { [BW]::keybd_event($m, 0, 2, [UIntPtr]::Zero) }
}

function Focus-Retry {
  # Windows sometimes refuses one focus change while the user is typing
  # elsewhere (seen 2026-09-26). Three tries, then give up without pressing.
  for ($i = 0; $i -lt 3; $i++) { if (Focus-Target) { return $true }; Start-Sleep -Milliseconds 200 }
  return $false
}

if ($Keys.Count -eq 0) {
  if (-not (Focus-Retry)) { Write-Output "ABORT: '$WindowName' is not in the foreground"; exit 1 }
  Write-Output "focused"; exit 0
}

$n = 0
foreach ($chord in $Keys) {
  # Re-check before every chord: focus can move between two presses.
  if (-not (Focus-Retry)) { Write-Output "ABORT: '$WindowName' is not in the foreground, '$chord' not pressed"; Restore-Prev; exit 1 }
  Press $chord
  Write-Output "pressed $chord"
  $n++
  # Chrome reads the keys from its queue in order, so the gap only has to let
  # one chord land before the next. None after the last one.
  if ($n -lt $Keys.Count) { Start-Sleep -Milliseconds 150 }
}
# Chrome acts on an extension shortcut (Alt+W) only while its window is
# active; handing focus back 100 ms after the last key dropped it in about
# one run in four (2026-10-04). 600 ms held in every test run.
Start-Sleep -Milliseconds 600
Restore-Prev
exit 0
