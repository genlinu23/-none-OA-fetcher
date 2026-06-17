from __future__ import annotations

import csv
import re
from pathlib import Path

from .models import DownloadResult
from .models import DownloadRow


RESULT_FIELDNAMES = [
    "idx",
    "doi",
    "title",
    "publisher",
    "status",
    "pdf_filename",
    "pdf_path",
    "size_bytes",
    "detail",
    "final_pdf_url",
    "url",
]


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (value or "")).strip("_")


def read_download_rows(path: Path) -> list[DownloadRow]:
    if path.suffix.lower() != ".csv":
        return read_text_download_rows(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.DictReader(handle))
    return [DownloadRow.from_csv_row(row, fallback_idx=i) for i, row in enumerate(raw_rows, start=1)]


def read_text_download_rows(path: Path) -> list[DownloadRow]:
    rows: list[DownloadRow] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for i, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            doi = extract_doi_like(line)
            url = line if line.startswith(("http://", "https://")) else (f"https://doi.org/{doi}" if doi else "")
            rows.append(
                DownloadRow(
                    idx=str(len(rows) + 1),
                    doi=doi,
                    title="",
                    publisher=infer_publisher(doi, url),
                    url=url,
                )
            )
    return rows


def extract_doi_like(value: str) -> str:
    text = (value or "").strip()
    if text.startswith(("http://", "https://")):
        match = re.search(r"doi\.org/(10\.\S+)$", text, re.I)
        if match:
            return match.group(1).strip()
        return ""
    if text.startswith("10."):
        return text
    return ""


def infer_publisher(doi: str, url: str) -> str:
    normalized_doi = (doi or "").lower()
    lower_url = (url or "").lower()
    if normalized_doi.startswith("10.1021/") or "pubs.acs.org" in lower_url:
        return "ACS"
    if normalized_doi.startswith("10.1039/") or "pubs.rsc.org" in lower_url:
        return "RSC"
    if normalized_doi.startswith("10.1002/") or "wiley.com" in lower_url:
        return "Wiley"
    if normalized_doi.startswith("10.1038/") or "nature.com" in lower_url:
        return "Nature"
    if normalized_doi.startswith("10.1016/") or "sciencedirect.com" in lower_url or "elsevier.com" in lower_url:
        return "Elsevier"
    if normalized_doi.startswith("10.1007/") or "springer.com" in lower_url or "link.springer.com" in lower_url:
        return "Springer"
    if normalized_doi.startswith("10.1246/") or "academic.oup.com" in lower_url or "oup.com" in lower_url:
        return "Oxford"
    if normalized_doi.startswith("10.3390/") or "mdpi.com" in lower_url:
        return "MDPI"
    return "UNKNOWN"


def write_results_csv(path: Path, results: list[DownloadResult]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(result.to_csv_row() for result in results)
