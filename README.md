# None-OA Fetcher

A local literature workbench for DOI discovery, OA / non-OA DOI list generation, and browser-assisted PDF downloading.

The app runs on your own computer. It opens a local web page at `http://127.0.0.1:8765/` and stores runtime data locally.

## For Users

Download the Windows executable from the GitHub Release page, then double-click:

```text
PKU-Literature-Workbench.exe
```

The executable starts the local backend and opens the web UI automatically. A `.lnk` shortcut is not required for distribution; shortcuts are only local convenience files.

First-run notes:

- The Research Agent needs your own model API settings. Enter them in the app settings, or set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `LIGEN_AGENT_MODEL` before launch.
- PDF downloading still depends on your local browser access, publisher login state, and institutional permissions.
- Outputs are written under `%APPDATA%\PKU Literature Workbench\outputs` by default.
- If `LIGEN_APP_HOME` is set, the app uses that folder instead. This is useful for advanced users who want a custom portable data directory.

## What It Does

- Turns a loose research topic into confirmed search keywords through a configurable LLM agent.
- Searches DOI candidates through sources such as Crossref and OpenAlex.
- Generates separate DOI text lists for OA, non-OA, and unknown-access records.
- Deduplicates DOI candidates before download.
- Downloads PDFs through local browser sessions into a unified `pdfs` folder.

## Repository Layout

- `ligen_downloader/` - Python backend, local web server, storage, search providers, and React frontend source.
- `ligen_downloader/web_frontend/` - React / Vite / Tailwind frontend.
- `scripts/` - launchers, build scripts, browser warmup, and PDF fetch scripts.
- `LIGEN_LOCAL_WEB_GUIDE.md` - local web usage notes.
- `LIGEN_DOWNLOADER_USER_GUIDE.md` - downloader usage notes.

## Local Development

Run the backend directly:

```powershell
cd "C:\path\to\doi_harvest"
python -m ligen_downloader.web_app
```

Build the frontend:

```powershell
cd "C:\path\to\doi_harvest\ligen_downloader\web_frontend"
npm install
npm run build
```

Build the Windows executable:

```powershell
cd "C:\path\to\doi_harvest"
python -m pip install pyinstaller
cmd /d /s /c "call scripts\build_ligen_web_exe.cmd"
```

The build output is:

```text
dist\PKU-Literature-Workbench.exe
```

## Release Packaging

This repository intentionally does not commit generated executables, PDFs, browser profiles, logs, or output folders.

Recommended release flow:

1. Build `dist\PKU-Literature-Workbench.exe`.
2. Create a GitHub Release.
3. Upload the exe as a Release asset.
4. Ask users to download and run the exe directly.

## Agent Configuration

Do not commit API keys.

Environment-variable setup:

```powershell
setx OPENAI_API_KEY "your_api_key"
setx OPENAI_BASE_URL "https://your-provider.example/v1"
setx LIGEN_AGENT_MODEL "your_fast_model"
```

The app is designed to fail visibly when the model API is not configured. It should not silently fall back to fake local answers.

## Runtime Data

Runtime files stay local and are ignored by Git:

```text
%APPDATA%\PKU Literature Workbench\outputs
downloads\
outputs\
logs\
dist\
build\
```

Set a custom app data directory only when needed:

```powershell
setx LIGEN_APP_HOME "D:\LiteratureWorkbench"
```
