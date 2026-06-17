#!/usr/bin/env python3
"""
Merge DOI harvest CSV outputs from multiple sources into one deduplicated CSV/TXT.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from typing import List


@dataclass
class Record:
    doi: str
    title: str
    year: str
    work_type: str
    source: str
    matched_query: str
    work_id: str
    landing_page: str


FIELDNAMES = [
    "doi",
    "title",
    "year",
    "work_type",
    "source",
    "matched_query",
    "work_id",
    "landing_page",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge harvested DOI CSV files.")
    parser.add_argument("--input", action="append", required=True, help="Input CSV. Repeat for multiple files.")
    parser.add_argument("--out-prefix", required=True, help="Output prefix without suffix.")
    parser.add_argument(
        "--max-dois-per-txt",
        type=int,
        default=1000,
        help="Maximum DOI lines per DOI-only TXT file. Outputs are split when this limit is exceeded.",
    )
    return parser.parse_args()


def read_csv(path: Path) -> List[Record]:
    rows: List[Record] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            doi = (raw.get("doi") or "").strip().lower()
            if not doi:
                continue
            rows.append(
                Record(
                    doi=doi,
                    title=(raw.get("title") or "").strip(),
                    year=(raw.get("year") or "").strip(),
                    work_type=(raw.get("work_type") or "").strip(),
                    source=(raw.get("source") or "").strip(),
                    matched_query=(raw.get("matched_query") or "").strip(),
                    work_id=(raw.get("work_id") or "").strip(),
                    landing_page=(raw.get("landing_page") or "").strip(),
                )
            )
    return rows


def merge_records(inputs: List[Path]) -> Dict[str, Record]:
    merged: Dict[str, Record] = {}
    for path in inputs:
        for row in read_csv(path):
            existing = merged.get(row.doi)
            if existing is None:
                merged[row.doi] = row
                continue

            source_parts = sorted(set(part for part in (existing.source.split("+") + row.source.split("+")) if part))
            existing.source = "+".join(source_parts)

            query_parts = []
            for value in [existing.matched_query, row.matched_query]:
                for part in value.split(" | "):
                    part = part.strip()
                    if part and part not in query_parts:
                        query_parts.append(part)
            existing.matched_query = " | ".join(query_parts)

            if not existing.title and row.title:
                existing.title = row.title
            elif row.title and len(row.title) > len(existing.title):
                existing.title = row.title

            if not existing.year and row.year:
                existing.year = row.year
            elif existing.year and row.year:
                try:
                    existing.year = str(min(int(existing.year), int(row.year)))
                except ValueError:
                    pass

            if not existing.work_type and row.work_type:
                existing.work_type = row.work_type
            elif existing.work_type and row.work_type and existing.work_type != row.work_type:
                type_parts = sorted(set(part for part in (existing.work_type.split("+") + row.work_type.split("+")) if part))
                existing.work_type = "+".join(type_parts)

            if not existing.work_id and row.work_id:
                existing.work_id = row.work_id
            if not existing.landing_page and row.landing_page:
                existing.landing_page = row.landing_page

    return merged


def write_doi_txt_parts(prefix: Path, rows: List[Record], max_dois_per_txt: int) -> tuple[List[Path], Path]:
    if max_dois_per_txt <= 0:
        raise ValueError("--max-dois-per-txt must be positive")

    full_txt_path = prefix.with_suffix(".txt")
    if full_txt_path.exists() and len(rows) > max_dois_per_txt:
        full_txt_path.unlink()

    old_parts = sorted(prefix.parent.glob(f"{prefix.name}_part*.txt"))
    for old_part in old_parts:
        old_part.unlink()

    part_paths: List[Path] = []
    if len(rows) <= max_dois_per_txt:
        part_paths = [full_txt_path]
    else:
        for start in range(0, len(rows), max_dois_per_txt):
            part_index = len(part_paths) + 1
            part_paths.append(prefix.parent / f"{prefix.name}_part{part_index:03d}.txt")

    for path_index, path in enumerate(part_paths):
        start = path_index * max_dois_per_txt
        end = min(start + max_dois_per_txt, len(rows))
        with path.open("w", encoding="utf-8") as handle:
            for row in rows[start:end]:
                handle.write(row.doi + "\n")

    manifest_path = prefix.parent / f"{prefix.name}_doi_parts_manifest.json"
    manifest = {
        "total_dois": len(rows),
        "max_dois_per_file": max_dois_per_txt,
        "files": [
            {
                "path": str(path),
                "doi_count": sum(1 for line in path.open("r", encoding="utf-8") if line.strip()),
            }
            for path in part_paths
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return part_paths, manifest_path


def write_outputs(prefix: Path, rows: List[Record], input_paths: List[Path], max_dois_per_txt: int) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = prefix.with_suffix(".csv")
    summary_path = prefix.parent / f"{prefix.name}_summary.json"

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: getattr(row, field) for field in FIELDNAMES})

    txt_paths, parts_manifest_path = write_doi_txt_parts(prefix, rows, max_dois_per_txt)

    summary = {
        "inputs": [str(path) for path in input_paths],
        "unique_dois": len(rows),
        "source_counts": {
            "crossref_only": sum(1 for row in rows if row.source == "crossref"),
            "openalex_only": sum(1 for row in rows if row.source == "openalex"),
            "both": sum(1 for row in rows if row.source == "crossref+openalex"),
        },
        "outputs": {
            "csv": str(csv_path),
            "txt": str(txt_paths[0]) if len(txt_paths) == 1 else "",
            "txt_parts": [str(path) for path in txt_paths],
            "doi_parts_manifest": str(parts_manifest_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    args = parse_args()
    input_paths = [Path(value) for value in args.input]
    merged = merge_records(input_paths)
    rows = sorted(merged.values(), key=lambda row: (row.year or "9999", row.doi))
    write_outputs(Path(args.out_prefix), rows, input_paths, args.max_dois_per_txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
