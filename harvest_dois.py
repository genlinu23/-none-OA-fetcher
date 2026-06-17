#!/usr/bin/env python3
"""
Harvest DOI records from Crossref and OpenAlex for one or more search terms.

Examples:
  python harvest_dois.py --query "CO2 reduction" --query "carbon dioxide reduction" --max-per-query 500
  python harvest_dois.py --query-file queries.txt --from-year 2018 --to-year 2025 --out-prefix co2rr
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Optional
from urllib.parse import quote

import requests


USER_AGENT = "codex-doi-harvest/1.0 (mailto:none@example.com)"
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_URL = "https://api.openalex.org/works"


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
    raw_json: str


def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def emit_log(message: str, log_path: Optional[Path]) -> None:
    line = f"[{timestamp()}] {message}"
    print(line, file=sys.stderr, flush=True)
    if log_path is not None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def normalize_doi(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    doi = value.strip()
    if not doi:
        return None
    lowered = doi.lower()
    prefixes = (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix):
            doi = doi[len(prefix) :]
            lowered = doi.lower()
            break
    return doi.strip().lower() or None


def first_nonempty(values: Iterable[Optional[str]]) -> str:
    for value in values:
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return ""


def year_in_range(year: str, from_year: Optional[int], to_year: Optional[int]) -> bool:
    if not year:
        return True
    try:
        numeric = int(year)
    except ValueError:
        return True
    if from_year is not None and numeric < from_year:
        return False
    if to_year is not None and numeric > to_year:
        return False
    return True


def request_json(
    session: requests.Session,
    url: str,
    params: Dict[str, object],
    timeout: int = 120,
    retries: int = 8,
    backoff_sec: float = 3.0,
    log_path: Optional[Path] = None,
    label: str = "",
) -> Dict[str, object]:
    last_error: Optional[Exception] = None

    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = backoff_sec * (2 ** attempt)
                else:
                    delay = backoff_sec * (2 ** attempt)
                emit_log(
                    f"retryable status={response.status_code} delay={round(delay,1)}s attempt={attempt + 1}/{retries} label={label}",
                    log_path,
                )
                time.sleep(delay + random.uniform(0, 1))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == retries - 1:
                break
            delay = backoff_sec * (2 ** attempt)
            emit_log(
                f"request exception={type(exc).__name__} delay={round(delay,1)}s attempt={attempt + 1}/{retries} label={label} detail={exc}",
                log_path,
            )
            time.sleep(delay + random.uniform(0, 1))

    if last_error is not None:
        raise last_error
    raise RuntimeError("request_json failed without an exception")


def crossref_query(
    query: str,
    session: requests.Session,
    from_year: Optional[int],
    to_year: Optional[int],
    max_per_query: Optional[int],
    rows: int = 250,
    pause_sec: float = 0.2,
    log_path: Optional[Path] = None,
) -> Iterator[Record]:
    cursor = "*"
    yielded = 0
    page = 0

    filters: List[str] = []
    if from_year is not None:
        filters.append(f"from-pub-date:{from_year}-01-01")
    if to_year is not None:
        filters.append(f"until-pub-date:{to_year}-12-31")

    while True:
        page += 1
        params = {
            "query.bibliographic": query,
            "rows": rows,
            "cursor": cursor,
            "select": "DOI,title,published-print,published-online,issued,URL,type",
        }
        if filters:
            params["filter"] = ",".join(filters)

        payload = request_json(
            session,
            CROSSREF_URL,
            params=params,
            log_path=log_path,
            label=f"crossref query={query} page={page}",
        )["message"]
        items = payload.get("items", [])
        emit_log(f"crossref page={page} query={query!r} items={len(items)} yielded={yielded}", log_path)
        if not items:
            break

        for item in items:
            doi = normalize_doi(item.get("DOI"))
            if not doi:
                continue

            title = ""
            if isinstance(item.get("title"), list) and item["title"]:
                title = first_nonempty(item["title"])

            year = ""
            for field in ("published-print", "published-online", "issued"):
                parts = item.get(field, {}).get("date-parts", [])
                if parts and parts[0]:
                    year = str(parts[0][0])
                    break

            if not year_in_range(year, from_year, to_year):
                continue

            raw_json = json.dumps(item, ensure_ascii=False)
            yield Record(
                doi=doi,
                title=title,
                year=year,
                work_type=item.get("type", "") or "",
                source="crossref",
                matched_query=query,
                work_id=item.get("URL", ""),
                landing_page=f"https://doi.org/{quote(doi, safe='/')}",
                raw_json=raw_json,
            )
            yielded += 1
            if max_per_query is not None and yielded >= max_per_query:
                return

        next_cursor = payload.get("next-cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(pause_sec)


def openalex_query(
    query: str,
    session: requests.Session,
    from_year: Optional[int],
    to_year: Optional[int],
    max_per_query: Optional[int],
    per_page: int = 100,
    pause_sec: float = 0.5,
    log_path: Optional[Path] = None,
) -> Iterator[Record]:
    cursor = "*"
    yielded = 0
    page = 0

    filters: List[str] = []
    if from_year is not None:
        filters.append(f"from_publication_date:{from_year}-01-01")
    if to_year is not None:
        filters.append(f"to_publication_date:{to_year}-12-31")

    while True:
        page += 1
        params = {
            "search": query,
            "cursor": cursor,
            "per-page": per_page,
            "mailto": "none@example.com",
        }
        if filters:
            params["filter"] = ",".join(filters)

        payload = request_json(
            session,
            OPENALEX_URL,
            params=params,
            log_path=log_path,
            label=f"openalex query={query} page={page}",
        )
        results = payload.get("results", [])
        remaining = payload.get("meta", {}).get("count")
        emit_log(
            f"openalex page={page} query={query!r} results={len(results)} yielded={yielded} total_count={remaining}",
            log_path,
        )
        if not results:
            break

        for item in results:
            doi = normalize_doi(item.get("doi"))
            if not doi:
                continue

            year = str(item.get("publication_year") or "")
            if not year_in_range(year, from_year, to_year):
                continue

            title = item.get("display_name", "") or ""
            raw_json = json.dumps(item, ensure_ascii=False)
            yield Record(
                doi=doi,
                title=title,
                year=year,
                work_type=item.get("type", "") or "",
                source="openalex",
                matched_query=query,
                work_id=item.get("id", ""),
                landing_page=item.get("primary_location", {}).get("landing_page_url", "") or f"https://doi.org/{quote(doi, safe='/')}",
                raw_json=raw_json,
            )
            yielded += 1
            if max_per_query is not None and yielded >= max_per_query:
                return

        next_cursor = payload.get("meta", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(pause_sec)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest DOI metadata from Crossref and OpenAlex.")
    parser.add_argument("--query", action="append", default=[], help="Query term. Repeat for multiple terms.")
    parser.add_argument("--query-file", help="Text file with one query per line.")
    parser.add_argument("--from-year", type=int, help="Earliest publication year to keep.")
    parser.add_argument("--to-year", type=int, help="Latest publication year to keep.")
    parser.add_argument(
        "--source",
        action="append",
        choices=("crossref", "openalex"),
        help="Data source(s). Defaults to both.",
    )
    parser.add_argument(
        "--max-per-query",
        type=int,
        default=500,
        help="Maximum records to pull per query per source. Use 0 for no cap. Default: 500.",
    )
    parser.add_argument(
        "--crossref-rows",
        type=int,
        default=250,
        help="Crossref page size. Default: 250.",
    )
    parser.add_argument(
        "--openalex-per-page",
        type=int,
        default=100,
        help="OpenAlex page size. Default: 100.",
    )
    parser.add_argument(
        "--out-prefix",
        default="doi_harvest",
        help="Output prefix for .csv, .txt, and .jsonl files. Default: doi_harvest",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress while harvesting.",
    )
    parser.add_argument(
        "--log-file",
        help="Optional log file for progress and retry diagnostics.",
    )
    parser.add_argument(
        "--max-dois-per-txt",
        type=int,
        default=1000,
        help="Maximum DOI lines per DOI-only TXT file. Outputs are split when this limit is exceeded.",
    )
    return parser.parse_args()


def load_queries(args: argparse.Namespace) -> List[str]:
    queries = [q.strip() for q in args.query if q and q.strip()]
    if args.query_file:
        file_queries = [
            line.strip()
            for line in Path(args.query_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        queries.extend(file_queries)
    seen = set()
    unique: List[str] = []
    for query in queries:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


def write_doi_txt_parts(prefix: Path, records: List[Record], max_dois_per_txt: int) -> tuple[List[Path], Path]:
    if max_dois_per_txt <= 0:
        raise ValueError("--max-dois-per-txt must be positive")

    full_txt_path = prefix.with_suffix(".txt")
    if full_txt_path.exists() and len(records) > max_dois_per_txt:
        full_txt_path.unlink()

    old_parts = sorted(prefix.parent.glob(f"{prefix.name}_part*.txt"))
    for old_part in old_parts:
        old_part.unlink()

    if len(records) <= max_dois_per_txt:
        part_paths = [full_txt_path]
    else:
        part_paths = [
            prefix.parent / f"{prefix.name}_part{index + 1:03d}.txt"
            for index in range((len(records) + max_dois_per_txt - 1) // max_dois_per_txt)
        ]

    for path_index, path in enumerate(part_paths):
        start = path_index * max_dois_per_txt
        end = min(start + max_dois_per_txt, len(records))
        with path.open("w", encoding="utf-8") as handle:
            for record in records[start:end]:
                handle.write(record.doi + "\n")

    manifest_path = prefix.parent / f"{prefix.name}_doi_parts_manifest.json"
    manifest = {
        "total_dois": len(records),
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


def write_outputs(prefix: Path, records: List[Record], max_dois_per_txt: int) -> tuple[List[Path], Path]:
    csv_path = prefix.with_suffix(".csv")
    jsonl_path = prefix.with_suffix(".jsonl")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["doi", "title", "year", "work_type", "source", "matched_query", "work_id", "landing_page"])
        for record in records:
            writer.writerow(
                [
                    record.doi,
                    record.title,
                    record.year,
                    record.work_type,
                    record.source,
                    record.matched_query,
                    record.work_id,
                    record.landing_page,
                ]
            )

    txt_paths, parts_manifest_path = write_doi_txt_parts(prefix, records, max_dois_per_txt)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.raw_json + "\n")
    return txt_paths, parts_manifest_path


def main() -> int:
    args = parse_args()
    queries = load_queries(args)
    if not queries:
        print("No queries provided. Use --query or --query-file.", file=sys.stderr)
        return 2

    sources = args.source or ["crossref", "openalex"]
    max_per_query = None if args.max_per_query == 0 else args.max_per_query

    prefix = Path(args.out_prefix)
    if prefix.parent and str(prefix.parent) != ".":
        prefix.parent.mkdir(parents=True, exist_ok=True)

    log_path = Path(args.log_file) if args.log_file else None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    unique_records: Dict[str, Record] = {}
    source_counts = {"crossref": 0, "openalex": 0}

    try:
        for source in sources:
            for query in queries:
                if args.verbose:
                    emit_log(f"{source} start query={query!r}", log_path)

                if source == "crossref":
                    iterator = crossref_query(
                        query=query,
                        session=session,
                        from_year=args.from_year,
                        to_year=args.to_year,
                        max_per_query=max_per_query,
                        rows=args.crossref_rows,
                        log_path=log_path,
                    )
                else:
                    iterator = openalex_query(
                        query=query,
                        session=session,
                        from_year=args.from_year,
                        to_year=args.to_year,
                        max_per_query=max_per_query,
                        per_page=args.openalex_per_page,
                        log_path=log_path,
                    )

                for record in iterator:
                    source_counts[source] += 1
                    if record.doi not in unique_records:
                        unique_records[record.doi] = record
                    else:
                        existing = unique_records[record.doi]
                        if record.source not in existing.source.split("+"):
                            merged_source = "+".join(sorted(set(existing.source.split("+") + [record.source])))
                            existing.source = merged_source
                        if record.matched_query not in existing.matched_query.split(" | "):
                            existing.matched_query = existing.matched_query + " | " + record.matched_query
                        if not existing.title and record.title:
                            existing.title = record.title
                        if not existing.year and record.year:
                            existing.year = record.year
                        if not existing.work_type and record.work_type:
                            existing.work_type = record.work_type
                        if not existing.work_id and record.work_id:
                            existing.work_id = record.work_id
                        if not existing.landing_page and record.landing_page:
                            existing.landing_page = record.landing_page
    finally:
        session.close()

    records = sorted(unique_records.values(), key=lambda rec: (rec.year or "9999", rec.doi))
    txt_paths, parts_manifest_path = write_outputs(prefix, records, args.max_dois_per_txt)

    summary = {
        "queries": queries,
        "sources": sources,
        "from_year": args.from_year,
        "to_year": args.to_year,
        "raw_counts": source_counts,
        "unique_dois": len(records),
        "csv": str(prefix.with_suffix(".csv")),
        "txt": str(txt_paths[0]) if len(txt_paths) == 1 else "",
        "txt_parts": [str(path) for path in txt_paths],
        "doi_parts_manifest": str(parts_manifest_path),
        "jsonl": str(prefix.with_suffix(".jsonl")),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
