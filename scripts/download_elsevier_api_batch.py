from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

from pypdf import PdfReader


PDF_ACCEPT = "application/pdf"
JSON_ACCEPT = "application/json"
API_BASE = "https://api.elsevier.com/content/article/doi"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Elsevier PDFs via Elsevier API key.")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--api-key", default="", help="Elsevier API key. Prefer env var ELS_API_KEY when omitted.")
    parser.add_argument("--max-rows", type=int, default=0, help="Validation cap. 0 means all rows.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Delay between requests.")
    return parser.parse_args()


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (value or "")).strip("_")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def download_pdf(doi: str, api_key: str) -> tuple[int, bytes, dict[str, str], str]:
    url = f"{API_BASE}/{quote(doi, safe='')}"
    req = Request(
        url,
        headers={
            "X-ELS-APIKey": api_key,
            "Accept": PDF_ACCEPT,
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urlopen(req, timeout=120) as resp:
        headers = {k: v for k, v in resp.headers.items()}
        return resp.status, resp.read(), headers, resp.geturl()


def fetch_json_error(doi: str, api_key: str) -> tuple[int, str]:
    url = f"{API_BASE}/{quote(doi, safe='')}?view=META"
    req = Request(
        url,
        headers={
            "X-ELS-APIKey": api_key,
            "Accept": JSON_ACCEPT,
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def status_from_headers(http_status: int, headers: dict[str, str], raw: bytes) -> tuple[str, str]:
    x_els_status = headers.get("X-ELS-Status", "")
    content_type = headers.get("Content-Type", "")
    lowered = x_els_status.lower()
    if http_status == 200 and raw[:5] == b"%PDF-":
        if "limited to first page" in lowered or "not entitled" in lowered:
            return "preview_only_not_entitled", x_els_status
        return "downloaded_full_pdf", x_els_status
    if http_status == 200:
        return "api_non_pdf", f"{x_els_status}; content_type={content_type}"
    return "api_http_error", x_els_status


def inspect_pdf(pdf_path: Path) -> dict[str, str]:
    result = {
        "pdf_pages": "",
        "page1_text_len": "",
        "has_intro": "",
        "has_conclusions": "",
        "has_references": "",
        "quality_status": "",
        "quality_reason": "",
    }
    try:
        reader = PdfReader(str(pdf_path))
        pages = len(reader.pages)
        page1_text = reader.pages[0].extract_text() or ""
        has_intro = "introduction" in page1_text.lower()
        has_conclusions = "conclusions" in page1_text.lower() or "conclusion" in page1_text.lower()
        has_references = "references" in page1_text.lower()
        result["pdf_pages"] = str(pages)
        result["page1_text_len"] = str(len(page1_text))
        result["has_intro"] = str(has_intro)
        result["has_conclusions"] = str(has_conclusions)
        result["has_references"] = str(has_references)

        if pages >= 2:
            result["quality_status"] = "likely_full_pdf"
            result["quality_reason"] = f"pages={pages}"
        elif pages == 1 and (has_references or has_conclusions):
            result["quality_status"] = "possible_full_single_page_pdf"
            result["quality_reason"] = "single page but includes late-section markers"
        elif pages == 1 and has_intro and not has_references and not has_conclusions:
            result["quality_status"] = "likely_preview_pdf"
            result["quality_reason"] = "single page with early-section content only"
        elif pages == 1:
            result["quality_status"] = "single_page_pdf_unknown"
            result["quality_reason"] = "single page without clear late-section markers"
        else:
            result["quality_status"] = "pdf_parse_unknown"
            result["quality_reason"] = "unexpected page count"
    except Exception as exc:
        result["quality_status"] = "pdf_parse_error"
        result["quality_reason"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> None:
    args = parse_args()
    api_key = (args.api_key or "").strip()
    if not api_key:
        import os

        api_key = os.getenv("ELS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Missing Elsevier API key. Pass --api-key or set ELS_API_KEY.")

    input_csv = Path(args.input_csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    pdf_dir = output_dir / "pdfs"
    preview_dir = output_dir / "preview_pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    rows = read_rows(input_csv)
    if args.max_rows > 0:
        rows = rows[: args.max_rows]

    results_csv = output_dir / "download_results.csv"
    fieldnames = [
        "idx",
        "doi",
        "title",
        "publisher",
        "status",
        "pdf_filename",
        "pdf_path",
        "size_bytes",
        "detail",
        "content_type",
        "x_els_status",
        "final_url",
        "pdf_pages",
        "page1_text_len",
        "has_intro",
        "has_conclusions",
        "has_references",
        "quality_status",
        "quality_reason",
        "url",
    ]

    results: list[dict[str, str]] = []
    for done, row in enumerate(rows, start=1):
        doi = (row.get("doi") or "").strip()
        title = (row.get("title") or "").strip()
        publisher = (row.get("publisher") or "").strip()
        source_url = (row.get("url") or f"https://doi.org/{doi}").strip()
        result = {
            "idx": str((row.get("idx") or "").strip() or done),
            "doi": doi,
            "title": title,
            "publisher": publisher,
            "status": "",
            "pdf_filename": "",
            "pdf_path": "",
            "size_bytes": "0",
            "detail": "",
            "content_type": "",
            "x_els_status": "",
            "final_url": "",
            "pdf_pages": "",
            "page1_text_len": "",
            "has_intro": "",
            "has_conclusions": "",
            "has_references": "",
            "quality_status": "",
            "quality_reason": "",
            "url": source_url,
        }
        try:
            http_status, raw, headers, final_url = download_pdf(doi, api_key)
            x_els_status = headers.get("X-ELS-Status", "")
            content_type = headers.get("Content-Type", "")
            status, detail = status_from_headers(http_status, headers, raw)
            result["status"] = status
            result["detail"] = detail
            result["content_type"] = content_type
            result["x_els_status"] = x_els_status
            result["final_url"] = final_url
            if raw[:5] == b"%PDF-":
                filename = f"{done}_{safe_name(doi)}.pdf"
                target_dir = pdf_dir if status == "downloaded_full_pdf" else preview_dir
                pdf_path = target_dir / filename
                pdf_path.write_bytes(raw)
                result["pdf_filename"] = filename
                result["pdf_path"] = str(pdf_path)
                result["size_bytes"] = str(len(raw))
                pdf_diag = inspect_pdf(pdf_path)
                result.update(pdf_diag)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            result["status"] = "api_http_error"
            result["detail"] = body[:1000]
            result["content_type"] = exc.headers.get("Content-Type", "")
            result["x_els_status"] = exc.headers.get("X-ELS-Status", "")
        except Exception as exc:
            meta_status, meta_body = fetch_json_error(doi, api_key)
            result["status"] = "api_exception"
            result["detail"] = f"{type(exc).__name__}: {exc} | meta_status={meta_status} | {meta_body[:700]}"
        results.append(result)
        print(f"[{done}/{len(rows)}] {result['status']} {doi}", flush=True)
        write_csv(results_csv, results, fieldnames)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    summary = {
        "input_csv": str(input_csv),
        "output_dir": str(output_dir),
        "total_rows": len(rows),
        "counts": {},
        "quality_counts": {},
    }
    counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        quality = row.get("quality_status", "").strip()
        if quality:
            quality_counts[quality] = quality_counts.get(quality, 0) + 1
    summary["counts"] = counts
    summary["quality_counts"] = quality_counts
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
