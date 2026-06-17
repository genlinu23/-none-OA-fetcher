from __future__ import annotations

import argparse
import base64
import csv
import json
import re
from pathlib import Path
from urllib.request import urlopen

import websocket


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest already-open ScienceDirect PDF asset tabs and map them back to DOI rows by PII.")
    parser.add_argument("--cdp-port", type=int, default=9233)
    parser.add_argument("--results-csv", default="", help="Downloader results CSV containing doi plus a PII-bearing detail/url column.")
    parser.add_argument("--expected-csv", default="", help="CSV with doi and expected_pii columns; useful for closing a small Elsevier retry set.")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.results_csv and not args.expected_csv:
        raise SystemExit("Provide either --results-csv or --expected-csv.")
    results_csv = Path(args.results_csv).expanduser().resolve() if args.results_csv else None
    expected_csv = Path(args.expected_csv).expanduser().resolve() if args.expected_csv else None
    output_dir = Path(args.output_dir).expanduser().resolve()
    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    rows = read_rows(expected_csv or results_csv)
    expected = []
    for row in rows:
        doi = normalize_doi(row.get("doi") or "")
        pii = (row.get("expected_pii") or "").strip() or extract_pii(" ".join([row.get("detail") or "", row.get("url") or ""]))
        if doi and pii:
            expected.append((doi, pii, row))

    pages = list_pages(args.cdp_port)
    asset_by_pii: dict[str, dict] = {}
    for page in pages:
        url = str(page.get("url") or "")
        if not url.startswith("https://pdf.sciencedirectassets.com/"):
            continue
        pii = extract_pii(url)
        if pii:
            asset_by_pii[pii.lower()] = page

    out_rows = []
    for doi, pii, row in expected:
        asset_page = asset_by_pii.get(pii.lower())
        if not asset_page:
            state, detail = classify_open_sciencedirect_state(args.cdp_port, pii)
            out_rows.append(status_row(row, pii, "", state, 0, detail or "No matching pdf.sciencedirectassets.com tab is open."))
            continue
        asset = str(asset_page.get("url") or "")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("_")
        pdf_path = pdf_dir / f"{safe}.pdf"
        try:
            raw = b""
            ws_url = str(asset_page.get("webSocketDebuggerUrl") or "")
            if ws_url:
                fetched = fetch_in_page(ws_url, asset, msg_id=700)
                if fetched.get("isPdf") and fetched.get("b64"):
                    raw = base64.b64decode(str(fetched["b64"]))
                else:
                    detail = json.dumps(fetched, ensure_ascii=False)[:500]
                    out_rows.append(status_row(row, pii, asset, classify_fetch_failure(fetched), int(fetched.get("size") or 0), detail))
                    continue
            else:
                with urlopen(asset, timeout=60) as response:
                    raw = response.read()
            if not raw.startswith(b"%PDF-"):
                out_rows.append(status_row(row, pii, asset, "non_pdf_asset", len(raw), "Asset URL did not return a PDF header."))
                continue
            pdf_path.write_bytes(raw)
            out_rows.append(status_row(row, pii, asset, "downloaded", len(raw), str(pdf_path)))
        except Exception as exc:
            out_rows.append(status_row(row, pii, asset, "download_failed", 0, f"{type(exc).__name__}: {exc}"))

    out_csv = output_dir / "harvested_sciencedirect_asset_tabs.csv"
    write_csv(out_csv, out_rows)
    summary = {
        "results_csv": str(results_csv) if results_csv else "",
        "expected_csv": str(expected_csv) if expected_csv else "",
        "output_dir": str(output_dir),
        "expected_pii_count": len(expected),
        "open_asset_tab_count": len(asset_by_pii),
        "downloaded_count": sum(1 for row in out_rows if row["status"] == "downloaded"),
        "status_counts": count_by(out_rows, "status"),
        "csv": str(out_csv),
        "pdf_dir": str(pdf_dir),
    }
    summary_path = output_dir / "harvested_sciencedirect_asset_tabs_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def list_pages(port: int) -> list[dict]:
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, list) else []


def eval_js(ws_url: str, expression: str, msg_id: int) -> str:
    ws = websocket.create_connection(ws_url, timeout=120, suppress_origin=True)
    try:
        ws.send(
            json.dumps(
                {
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                        "userGesture": True,
                    },
                }
            )
        )
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                return str(msg.get("result", {}).get("result", {}).get("value") or "")
    finally:
        ws.close()


def fetch_in_page(ws_url: str, url: str, msg_id: int) -> dict:
    js = """
    async (u) => {
      try {
        const r = await fetch(u, {credentials:'include'});
        const b = await r.arrayBuffer();
        const bytes = new Uint8Array(b);
        const isPdf = bytes.length >= 5 &&
          bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 &&
          bytes[3] === 0x46 && bytes[4] === 0x2d;
        let b64 = '';
        if (isPdf) {
          const chunk = 0x8000;
          let s = '';
          for (let i = 0; i < bytes.length; i += chunk) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
          }
          b64 = btoa(s);
        }
        return JSON.stringify({
          ok: true,
          status: r.status,
          ct: r.headers.get('content-type') || '',
          finalUrl: r.url,
          size: b.byteLength,
          isPdf,
          b64,
        });
      } catch (e) {
        return JSON.stringify({ok: false, error: String(e)});
      }
    }
    """
    raw = eval_js(ws_url, f"({js})({json.dumps(url)})", msg_id=msg_id)
    return json.loads(raw or "{}")


def classify_fetch_failure(fetched: dict) -> str:
    status = int(fetched.get("status") or 0)
    ct = str(fetched.get("ct") or "").lower()
    if status in {401, 403}:
        return "asset_auth_or_entitlement_failed"
    if status in {404, 410}:
        return "asset_expired_or_not_found"
    if "text/html" in ct:
        return "asset_returned_html"
    if not fetched.get("ok"):
        return "browser_fetch_failed"
    return "non_pdf_browser_fetch"


def classify_open_sciencedirect_state(port: int, pii: str) -> tuple[str, str]:
    target = pii.lower().rstrip(";,.")
    for page in list_pages(port):
        url = str(page.get("url") or "")
        if target not in url.lower() or "sciencedirect.com" not in url.lower():
            continue
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            continue
        try:
            raw = eval_js(
                ws_url,
                "JSON.stringify({href:location.href,title:document.title,bodyText:(document.body&&document.body.innerText||'').slice(0,8000)})",
                msg_id=900,
            )
            info = json.loads(raw or "{}")
        except Exception as exc:
            return "open_article_state_unreadable", f"Matching ScienceDirect tab exists but state read failed: {type(exc).__name__}: {exc}"
        body = re.sub(r"\s+", " ", str(info.get("bodyText") or "")).lower()
        href = str(info.get("href") or url)
        if is_robot_captcha_text(body):
            return "publisher_robot_captcha", f"ScienceDirect captcha/robot page for PII {pii}; complete challenge and rerun."
        if is_elsevier_no_subscription_text(body):
            return "elsevier_no_subscription", f"ScienceDirect says institution does not subscribe; href={href}"
        if "/science/article/abs/pii/" in href.lower() or "access through" in body:
            return "elsevier_needs_institution_verification", f"Article is on abstract/access page; complete institution verification; href={href}"
        if "/science/article/pii/" in href.lower():
            return "elsevier_view_pdf_not_materialized", f"Full article tab exists but no matching PDF asset tab; click View PDF or rerun materialization; href={href}"
        return "missing_asset_tab", f"Matching non-asset ScienceDirect tab exists; href={href}"
    return "missing_asset_tab", "No matching ScienceDirect asset/article tab is open."


def is_elsevier_no_subscription_text(body: str) -> bool:
    text = re.sub(r"\s+", " ", body or "").lower()
    markers = [
        "does not subscribe to this content on sciencedirect",
        "does not subscribe to this content",
        "not subscribe to this content",
        "your institution does not subscribe",
        "not entitled to access this content",
        "not have access to this content",
    ]
    return any(marker in text for marker in markers)


def is_robot_captcha_text(body: str) -> bool:
    text = re.sub(r"\s+", " ", body or "").lower()
    markers = [
        "are you a robot",
        "confirm you are a human",
        "captcha challenge",
        "complete the captcha",
        "reference number:",
        "user agent:",
        "ip address:",
    ]
    return any(marker in text for marker in markers) and ("captcha" in text or "robot" in text or "human" in text)


def extract_pii(text: str) -> str:
    patterns = [
        r"[?&]pii=([^&#\s]+)",
        r"/(?:article|chapter/bookseries)/(?:abs/)?pii/([^/?#\s]+)",
        r"/1-s2\.0-([^/?#\s]+)/main\.pdf",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            return match.group(1)
    return ""


def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.lower()


def status_row(row: dict[str, str], pii: str, asset_url: str, status: str, size: int, detail: str) -> dict[str, str]:
    return {
        "doi": normalize_doi(row.get("doi") or ""),
        "publisher": row.get("publisher") or "Elsevier",
        "expected_pii": pii,
        "asset_url": asset_url,
        "status": status,
        "size_bytes": str(size),
        "detail": detail,
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["doi", "publisher", "expected_pii", "asset_url", "status", "size_bytes", "detail"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def count_by(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get(field) or ""
        counts[key] = counts.get(key, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
