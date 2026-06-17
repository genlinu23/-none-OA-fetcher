# Ligen Literature Downloader MVP

## Conclusion
The downloader is now moving from scattered scripts toward a software-style core.

This pass now includes a real desktop operator client on top of the download engine.
It is not a packaged commercial installer yet, but it is no longer CLI-only:
- stable batch entrypoint
- structured run logs
- provider abstraction
- machine-readable run summaries
- a productionized Elsevier handler that has already been verified on real downloads
- a desktop GUI for DOI-list intake, session warmup, download control, and results review

## What Exists Now

### Core package
- Package root: `C:\Users\logan\doi_harvest\ligen_downloader`
- Main CLI: `python C:\Users\logan\doi_harvest\scripts\download_corpus_mvp.py ...`
- Input formats:
  - CSV with `idx,doi,title,publisher,url`
  - plain-text DOI / URL list, one entry per line
- Desktop GUI launcher:
  - `python C:\Users\logan\doi_harvest\scripts\launch_ligen_gui.py`
- Shared modules:
  - `models.py`
  - `utils.py`
  - `logging_utils.py`
  - `runner.py`
  - `gui_app.py`

### First provider
- `providers\elsevier_gui.py`
- Uses:
  - authenticated Chrome CDP session
  - Chrome PDF viewer click path
  - Windows Win32 Save-As confirmation
  - structured `DownloadResult`

### Second provider
- `providers\wiley_browser.py`
- Uses:
  - authenticated Chrome CDP session
  - Wiley main-article PDF link probing
  - browser-response capture for entitled main PDFs
  - explicit failure when the current browser session is not entitled

### Third provider
- `providers\acs_browser.py`
- Uses:
  - authenticated Chrome CDP session
  - ACS main-article PDF route `https://pubs.acs.org/doi/pdf/{doi}`
  - validation that captured bytes start with `%PDF-`
  - explicit failure when the current session only exposes paywall HTML or supplementary material

### Output artifacts
Each run now has a software-like output contract:
- `download_results.csv`
- `summary.json`
- `logs\run_*.log`
- `logs\run_*.jsonl`
- `pdfs\*.pdf`

## Why This Matters
This is the layer that lets the project stop being a pile of ad hoc scripts.

It gives:
- reproducible batch runs
- traceable success/failure logs
- provider-specific download logic behind a stable interface
- a path to add other publishers without rewriting the runner

## What Is Still Missing For A Commercial Product

### Product surface
- packaged desktop installer
- auto-update and versioned desktop release flow
- account / session manager
- job queue dashboard
- retry controls
- publisher health status panel

Current reality:
- There is now a working desktop GUI client.
- The GUI is Windows-first and runs through Python, not through a packaged installer yet.
- Plain-text DOI-list input is supported so users do not need to build CSVs by hand.

### Reliability features
- crash recovery
- resumable jobs
- duplicate detection against prior download folders
- download fingerprinting
- automatic stale-dialog cleanup

### Security / operations
- secret storage
- publisher credential isolation
- audit log retention policy
- packaged installer
- auto-update mechanism

### Commercial readiness
- licensing
- telemetry policy
- user-visible error messages
- legal review
- terms / usage guardrails

## Recommended Next Product Steps

### P0
- Add Wiley / ACS / Springer handlers behind the same provider interface
- Add resume / retry support to the runner
- Add duplicate DOI skip logic

Status update:
- Elsevier: verified working
- ACS: verified working on at least one real main-PDF article
- Wiley: integrated with honest not-entitled behavior, but not yet verified as successful in the current browser session

### P1
- Add a simple local GUI on top of the package:
  - input CSV picker
  - provider selector
  - live progress log
  - result table

### P2
- Convert to packaged desktop software:
  - PySide6 or Tauri front-end
  - one-click launcher
  - settings page for ports, profiles, download dirs, API keys

## Current Invocation Example

```powershell
python C:\Users\logan\doi_harvest\scripts\download_corpus_mvp.py `
  --input-csv C:\Users\logan\doi_harvest\outputs\elsevier_web_fallback_11.csv `
  --output-dir C:\Users\logan\doi_harvest\outputs\elsevier_web_fallback_11_gui_win32_20260527 `
  --provider elsevier_gui `
  --cdp-port 9233
```

## Product Positioning
Short version:

This is now a download engine MVP, not yet a finished software product.

That is the right order.
If the engine is not stable, a GUI only hides the instability.
