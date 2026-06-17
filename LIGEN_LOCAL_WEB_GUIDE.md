# Ligen Local Web Guide

## What It Is

This is a local-only browser UI for the DOI literature downloader in:

`C:\Users\logan\doi_harvest`

It wraps the existing download scripts. It does not replace the underlying publisher logic.

## Start It

Double-click:

`C:\Users\logan\Desktop\Ligen Local Web.vbs`

What it does now:

- checks whether `http://127.0.0.1:8765/` is already alive
- if not, starts the local server in the background
- waits for the site to respond
- prefers opening the site as a dedicated Chrome/Edge app window
- falls back to the default browser if no app-capable browser is found

This is the recommended "app-like" launcher for local use.

Fallback launcher:

`C:\Users\logan\Desktop\Ligen Local Web.bat`

Or run from scripts:

`C:\Users\logan\doi_harvest\scripts\launch_ligen_web.cmd`

These launchers now route through `wscript` so they do not depend on fragile `cmd / start` quoting behavior.

PowerShell:

```powershell
& "C:\Users\logan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  "C:\Users\logan\doi_harvest\scripts\launch_ligen_web.py" `
  --open-browser
```

If you prefer the default Python on `PATH`:

```powershell
python "C:\Users\logan\doi_harvest\scripts\launch_ligen_web.py" --open-browser
```

Default local address:

`http://127.0.0.1:8765/`

## Recommended Flow

1. Paste DOI or DOI URL lines.
2. Click `分析输入`.
3. Check publisher detection and port status.
4. Click `启动 Warmup`.
5. Finish login, VPN/campus auth, and captcha in the opened Chrome windows.
6. Return to the page and click `启动 Download`.
7. Watch live logs and the result summary.

## Current Scope

- Local web page only, bound to `127.0.0.1`
- DOI input parsing
- Publisher inference preview
- Warmup / download launch
- Live log polling
- Result CSV summary
- Open output directory / result CSV

## Not Included Yet

- Full `Research Search` studio tab
- Installer packaging
- Remote multi-user deployment
- Native `.exe` shell packaging
