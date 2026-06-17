#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from pypdf import PdfReader


OFF_TOPIC_TITLE_PATTERN = re.compile(
    r"\bemissions?\b|"
    r"\bemission factor\b|"
    r"\bemission peaking\b|"
    r"\bcarbon neutrality\b|"
    r"\breduction path implication\b|"
    r"\bleap model\b|"
    r"\bdrained peatlands\b|"
    r"\bfor reducing co2 emission\b",
    re.I,
)

SUPPLEMENTARY_FIRST_PAGE_PATTERN = re.compile(
    r"^\s*(supporting information|supporting information for|electronic supplementary information|"
    r"supplementary information for|s1\s+supporting information)",
    re.I,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit downloaded paper PDFs into accepted and rejected buckets.")
    parser.add_argument("--downloads-root", required=True, help="Root folder containing download_results.csv files.")
    parser.add_argument("--core-csv", required=True, help="Current clean core CSV used as source of truth for titles.")
    parser.add_argument("--out-dir", required=True, help="Output folder for cleaned manifests and copied PDFs.")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def first_page_text(pdf_path: Path) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        if not reader.pages:
            return ""
        return (reader.pages[0].extract_text() or "").strip()
    except Exception:
        return ""


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    downloads_root = Path(args.downloads_root)
    core_csv = Path(args.core_csv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    accepted_dir = out_dir / "accepted_pdfs"
    off_topic_dir = out_dir / "off_topic_pdfs"
    supplementary_dir = out_dir / "supplementary_pdfs"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    off_topic_dir.mkdir(parents=True, exist_ok=True)
    supplementary_dir.mkdir(parents=True, exist_ok=True)

    core_rows = {row["doi"]: row for row in read_csv(core_csv)}

    downloaded: dict[str, dict[str, str]] = {}
    for results_csv in downloads_root.glob("**/download_results.csv"):
        for row in read_csv(results_csv):
            if row.get("status") != "downloaded":
                continue
            doi = (row.get("doi") or "").strip()
            if not doi or doi in downloaded:
                continue
            row = dict(row)
            row["results_csv"] = str(results_csv)
            downloaded[doi] = row

    accepted_rows: list[dict[str, str]] = []
    off_topic_rows: list[dict[str, str]] = []
    supplementary_rows: list[dict[str, str]] = []
    bucket_counts: Counter[str] = Counter()

    fieldnames = [
        "doi",
        "title",
        "publisher",
        "bucket",
        "bucket_reason",
        "pdf_path",
        "copied_pdf_path",
        "results_csv",
        "first_page_preview",
    ]

    for doi, row in sorted(downloaded.items()):
        title = core_rows.get(doi, {}).get("title", row.get("title", ""))
        pdf_path = Path(row["pdf_path"])
        first_page = first_page_text(pdf_path)
        preview = re.sub(r"\s+", " ", first_page[:300]).strip()

        bucket = "accepted"
        reason = "main_article_likely"
        target_dir = accepted_dir

        if OFF_TOPIC_TITLE_PATTERN.search(title):
            bucket = "off_topic"
            reason = "off_topic_title_pattern"
            target_dir = off_topic_dir
        elif SUPPLEMENTARY_FIRST_PAGE_PATTERN.search(first_page):
            bucket = "supplementary_only"
            reason = "supplementary_first_page_pattern"
            target_dir = supplementary_dir

        copied_path = target_dir / pdf_path.name
        if pdf_path.exists():
            shutil.copy2(pdf_path, copied_path)

        out_row = {
            "doi": doi,
            "title": title,
            "publisher": row.get("publisher", ""),
            "bucket": bucket,
            "bucket_reason": reason,
            "pdf_path": str(pdf_path),
            "copied_pdf_path": str(copied_path),
            "results_csv": row.get("results_csv", ""),
            "first_page_preview": preview,
        }
        bucket_counts[bucket] += 1
        if bucket == "accepted":
            accepted_rows.append(out_row)
        elif bucket == "off_topic":
            off_topic_rows.append(out_row)
        else:
            supplementary_rows.append(out_row)

    write_csv(out_dir / "accepted_manifest.csv", accepted_rows, fieldnames)
    write_csv(out_dir / "off_topic_manifest.csv", off_topic_rows, fieldnames)
    write_csv(out_dir / "supplementary_manifest.csv", supplementary_rows, fieldnames)

    summary = {
        "downloads_root": str(downloads_root),
        "core_csv": str(core_csv),
        "out_dir": str(out_dir),
        "total_unique_downloaded": len(downloaded),
        "bucket_counts": dict(bucket_counts),
    }
    with (out_dir / "summary.json").open("w", encoding="utf-8-sig") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
