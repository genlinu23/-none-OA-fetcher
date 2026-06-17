#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


SI_PATTERNS = [
    re.compile(r"\.s\d+$", re.I),
    re.compile(r"/s\d+$", re.I),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove SI/supplementary-like DOIs from a plain DOI list.")
    parser.add_argument("--input", required=True, help="Input DOI txt file")
    parser.add_argument("--clean-out", required=True, help="Output DOI txt without SI-like entries")
    parser.add_argument("--si-out", required=True, help="Output DOI txt containing SI-like entries")
    parser.add_argument("--report", required=True, help="Summary report txt")
    return parser.parse_args()


def is_si_like(doi: str) -> bool:
    value = doi.strip()
    if not value:
        return False
    return any(pattern.search(value) for pattern in SI_PATTERNS)


def main() -> int:
    args = parse_args()
    in_path = Path(args.input)
    clean_out = Path(args.clean_out)
    si_out = Path(args.si_out)
    report_path = Path(args.report)

    dois = [line.strip() for line in in_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    clean = [doi for doi in dois if not is_si_like(doi)]
    si = [doi for doi in dois if is_si_like(doi)]

    clean_out.write_text("\n".join(clean) + ("\n" if clean else ""), encoding="utf-8")
    si_out.write_text("\n".join(si) + ("\n" if si else ""), encoding="utf-8")

    report = [
        f"input_total={len(dois)}",
        f"clean_total={len(clean)}",
        f"si_removed={len(si)}",
        "sample_si_removed=",
        *si[:40],
        "sample_clean=",
        *clean[:40],
    ]
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
