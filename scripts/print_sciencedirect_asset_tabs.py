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
    parser = argparse.ArgumentParser(description="Export open ScienceDirect PDF viewer tabs via Chrome printToPDF.")
    parser.add_argument("--cdp-port", type=int, default=9233)
    parser.add_argument("--missing-csv", required=True, help="CSV with doi and expected_pii columns.")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = list(csv.DictReader(Path(args.missing_csv).open("r", encoding="utf-8-sig", newline="")))
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = list_pages(args.cdp_port)

    out_rows = []
    for row in rows:
        doi = normalize_doi(row.get("doi") or "")
        pii = (row.get("expected_pii") or "").strip()
        if not doi or not pii:
            continue
        page = find_asset_page(pages, pii)
        if page is None:
            out_rows.append(status_row(doi, pii, "", "missing_asset_tab", "", 0))
            continue
        try:
            data = print_to_pdf(str(page.get("webSocketDebuggerUrl") or ""))
            raw = base64.b64decode(data)
            pdf_path = out_dir / f"{safe_name(doi)}.printed_from_viewer.pdf"
            pdf_path.write_bytes(raw)
            status = "printed_pdf" if raw.startswith(b"%PDF-") else "bad_pdf_header"
            out_rows.append(status_row(doi, pii, str(page.get("url") or ""), status, str(pdf_path), len(raw)))
        except Exception as exc:
            out_rows.append(status_row(doi, pii, str(page.get("url") or ""), f"print_failed:{type(exc).__name__}", str(exc), 0))

    out_csv = out_dir / "printed_sciencedirect_asset_tabs.csv"
    write_csv(out_csv, out_rows)
    summary = {
        "output_dir": str(out_dir),
        "csv": str(out_csv),
        "rows": len(out_rows),
        "printed_pdf": sum(1 for row in out_rows if row["status"] == "printed_pdf"),
        "status_counts": count_by(out_rows, "status"),
    }
    (out_dir / "printed_sciencedirect_asset_tabs_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def list_pages(port: int) -> list[dict]:
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, list) else []


def find_asset_page(pages: list[dict], pii: str) -> dict | None:
    target = pii.lower().rstrip(";,.")
    for page in pages:
        url = str(page.get("url") or "").lower()
        if url.startswith("https://pdf.sciencedirectassets.com/") and target in url:
            return page
    return None


def print_to_pdf(ws_url: str) -> str:
    if not ws_url:
        raise RuntimeError("missing websocket URL")
    ws = websocket.create_connection(ws_url, timeout=120, suppress_origin=True)
    try:
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Page.printToPDF",
                    "params": {"printBackground": True, "preferCSSPageSize": True},
                }
            )
        )
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return str(msg.get("result", {}).get("data") or "")
    finally:
        ws.close()


def status_row(doi: str, pii: str, asset_url: str, status: str, path_or_detail: str, size: int) -> dict[str, str]:
    return {
        "doi": doi,
        "expected_pii": pii,
        "asset_url": asset_url,
        "status": status,
        "pdf_path": path_or_detail if status in {"printed_pdf", "bad_pdf_header"} else "",
        "detail": "" if status in {"printed_pdf", "bad_pdf_header"} else path_or_detail,
        "size_bytes": str(size),
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["doi", "expected_pii", "asset_url", "status", "pdf_path", "detail", "size_bytes"]
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


def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.lower()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")


if __name__ == "__main__":
    raise SystemExit(main())
