@echo off
REM bridge-profiles.cmd - open the bridge Chrome profile group.
REM Both profiles live in one isolated user-data-dir, so they share one CDP
REM endpoint and the browser bridge sees every tab from both.
REM Usage: bridge-profiles.cmd [first-profile-dir] [second-profile-dir]
REM   default: Default only; pass a second directory to open it too
REM   (directory names differ per PC; check chrome://version -> Profile Path)
REM --window-name makes the title static: Chrome's normal title follows the
REM active tab, a named window shows only the name in the taskbar. Set
REM BRIDGE_WIN1 / BRIDGE_WIN2 to the titles accounts.json expects
REM (default "<email name>@ - bridge pfl").
REM A URL is required: without one, a second invocation hands its args to the
REM already-running Chrome and exits without opening a window.
REM The occlusion flags keep a window that sits behind others drawing.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set UDD=%LOCALAPPDATA%\chrome-debug
set P1=%~1
set P2=%~2
if "%P1%"=="" set P1=Default
if "%BRIDGE_WIN1%"=="" set BRIDGE_WIN1=%P1% - bridge pfl
if "%BRIDGE_WIN2%"=="" set BRIDGE_WIN2=%P2% - bridge pfl
set PORT=9333

start "" /B %CHROME% --user-data-dir="%UDD%" --profile-directory="%P1%" ^
  --remote-debugging-port=%PORT% --disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows --window-name="%BRIDGE_WIN1%" ^
  --no-first-run --no-default-browser-check "chrome://newtab/"

if "%P2%"=="" goto :eof
timeout /t 3 /nobreak >nul

start "" /B %CHROME% --user-data-dir="%UDD%" --profile-directory="%P2%" ^
  --remote-debugging-port=%PORT% --disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows --window-name="%BRIDGE_WIN2%" ^
  --no-first-run --no-default-browser-check "chrome://newtab/"
