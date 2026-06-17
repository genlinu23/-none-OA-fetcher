#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove already-successful DOIs from a source DOI list using a Sci-Hub record file.")
    parser.add_argument("--record", required=True, help="Sci-Hub record txt path")
    parser.add_argument("--source", required=True, help="Source DOI txt path")
    parser.add_argument("--out", required=True, help="Output remaining DOI txt path")
    parser.add_argument("--report", required=True, help="Summary report path")
    return parser.parse_args()


def load_success(record_path: Path) -> set[str]:
    success: set[str] = set()
    lines = record_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[1:]:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        doi = parts[0].strip().lower()
        fname = parts[1].strip()
        if doi and fname not in {"", "-"}:
            success.add(doi)
    return success


def main() -> int:
    args = parse_args()
    record_path = Path(args.record)
    source_path = Path(args.source)
    out_path = Path(args.out)
    report_path = Path(args.report)

    success = load_success(record_path)
    source_dois = [line.strip() for line in source_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    remaining = [doi for doi in source_dois if doi.lower() not in success]
    removed = [doi for doi in source_dois if doi.lower() in success]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")

    report = [
        f"source_total={len(source_dois)}",
        f"success_removed={len(removed)}",
        f"remaining={len(remaining)}",
        "sample_removed=",
        *removed[:20],
        "sample_remaining=",
        *remaining[:20],
    ]
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
