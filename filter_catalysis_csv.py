#!/usr/bin/env python3
"""
Filter DOI harvest CSVs toward catalytic CO2 reduction literature.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


INCLUDE_KEYWORDS = [
    "catal",
    "electrocatal",
    "photocatal",
    "electrochemical",
    "photoelectrochemical",
    "faradaic",
    "co2rr",
    "carbon dioxide reduction",
    "co2 reduction",
]

EXCLUDE_KEYWORDS = [
    "carbon capture",
    "co2 capture",
    "capture and storage",
    "ccs",
    "decarbonization",
    "decarbonisation",
    "emission",
    "greenhouse",
    "carbon footprint",
    "carbon tax",
    "policy",
    "life cycle",
    "lca",
    "sequestration",
    "adsorption",
    "absorption",
    "direct air capture",
    "dac",
]

EXCLUDE_TITLE_PREFIXES = [
    "corrigendum",
    "erratum",
    "correction:",
    "publisher correction",
    "author correction",
    "retraction",
    "withdrawn",
    "editorial",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter a DOI CSV toward catalytic CO2 reduction papers.")
    parser.add_argument("--input", required=True, help="Input CSV from harvest_dois.py")
    parser.add_argument("--output", required=True, help="Filtered output CSV")
    parser.add_argument("--doi-output", help="Optional TXT file with DOI list only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    doi_output_path = Path(args.doi_output) if args.doi_output else None

    rows = list(csv.DictReader(input_path.open(encoding="utf-8")))
    kept = []

    for row in rows:
        title = (row.get("title") or "").strip()
        lowered = title.casefold()
        if not title:
            continue
        if any(lowered.startswith(prefix) for prefix in EXCLUDE_TITLE_PREFIXES):
            continue
        if any(keyword in lowered for keyword in EXCLUDE_KEYWORDS):
            continue
        if not any(keyword in lowered for keyword in INCLUDE_KEYWORDS):
            continue
        kept.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(kept)

    if doi_output_path is not None:
        doi_output_path.parent.mkdir(parents=True, exist_ok=True)
        with doi_output_path.open("w", encoding="utf-8") as handle:
            for row in kept:
                doi = (row.get("doi") or "").strip()
                if doi:
                    handle.write(doi + "\n")

    print(f"input_rows={len(rows)} kept_rows={len(kept)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
