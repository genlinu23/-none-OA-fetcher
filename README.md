# None-OA Fetcher

Local literature harvesting workbench for DOI discovery, DOI queue management, and browser-assisted PDF downloading.

## What It Does

- Converts research topics into DOI search terms through a configurable LLM agent.
- Searches DOI candidates through sources such as Crossref and OpenAlex.
- Separates OA, non-OA, and unknown-access DOI lists.
- Downloads PDFs through local browser sessions and writes results to a unified `pdfs` folder.
- Runs as a local web app at `http://127.0.0.1:8765/`.

## Repository Layout

- `ligen_downloader/` - Python backend, local web server, storage, search providers, and React frontend source.
- `scripts/` - command-line launchers, build scripts, browser warmup, and PDF fetch scripts.
- `LIGEN_LOCAL_WEB_GUIDE.md` - local web usage notes.
- `LIGEN_DOWNLOADER_USER_GUIDE.md` - downloader usage notes.

## Local Development

```powershell
cd doi_harvest
python -m ligen_downloader.web_app
```

Frontend build:

```powershell
cd "ligen_downloader\web_frontend"
npm install
npm run build
```

Distributable build:

```powershell
cd doi_harvest
cmd /d /s /c "call scripts\build_ligen_web_exe.cmd"
```

## Runtime Data

Downloaded PDFs, logs, browser profiles, generated output folders, and packaged executables are intentionally excluded from Git. They stay local under runtime output folders such as:

```text
%APPDATA%\PKU Literature Workbench\outputs
```

## Agent Configuration

The LLM agent requires a user-provided API key. Do not commit API keys.

Configure it in the web UI or through environment variables:

```powershell
setx OPENAI_API_KEY "your_api_key"
setx OPENAI_BASE_URL "https://your-provider.example/v1"
```
