#!/usr/bin/env python3
"""
Audit a cleaned CO2RR corpus for record authenticity and record-type purity.

This script sits after thematic cleaning. It separates the current DOI list into:
  - core_journal_article
  - residual_review_like
  - journal_commentary_editorial
  - cover_teaser
  - peer_review_material
  - chemrxiv_preprint
  - ssrn_record
  - posted_content_other
  - book_or_chapter
  - proceedings
  - report
  - dataset
  - needs_manual_verification

It also adds a display-clean title and writes a full audited CSV plus per-bucket exports.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Tuple

from openpyxl import Workbook


COVER_PREFIX = re.compile(
    r"^(cover feature|front cover|back cover|inside cover|innentitelbild|innenr\S*cktitelbild)\s*:\s*",
    re.I,
)
COVER_SUFFIX = re.compile(
    r"\((adv\.|adv\. energy mater\.|adv\. mater\.|adv\. sci\.|chemsuschem|angew\.|"
    r"angew\. chem|angew\. chem\. int\. ed\.|chemcatchem|chem\. eur\. j\.|"
    r"chemphotochem|chemistryselect|small|chemelectrochem|chem\. methods)[^)]+\)$",
    re.I,
)
CHAPTER_DOI_PATTERN = re.compile(r"/(?:978|bk-)|\.ch\d+(?:$|[./])", re.I)
CHEMRXIV_PATTERN = re.compile(r"^10\.26434/chemrxiv\.", re.I)
SSRN_PATTERN = re.compile(r"^10\.2139/ssrn\.", re.I)
OTHER_POSTED_PATTERN = re.compile(
    r"^(10\.21203/rs\.3\.rs-|10\.22541/au\.|10\.31219/osf\.io/|10\.1149/osf\.io/|"
    r"10\.1021/scimeetings\.|10\.26226/morressier\.|10\.46855/energy-proceedings-)",
    re.I,
)
PEER_REVIEW_TITLE_PATTERN = re.compile(r"\bdecision letter\b|\bauthor response\b", re.I)
COMMENTARY_PATTERN = re.compile(r"^(comment on|reply to|editorial\b|commentary\b)\s*:?", re.I)
REVIEW_LIKE_PATTERN = re.compile(
    r"^(advances? in|recent advances? in|recent progress in|progress in|"
    r"recent progress on|recent progress toward|recent progress towards|"
    r"progress on|progress toward|progress towards|recent advances on|"
    r"advances toward|advances towards|recent progresses? on|recent progresses? in|"
    r"recent progress(?:es)? of|recent advances? of|progress of|"
    r"state of the art|state-of-the-art|"
    r"a review of|review of)\b",
    re.I,
)
MEETING_ABSTRACT_DOI_PATTERN = re.compile(r"^10\.1149/ma\d{4}-", re.I)
MEETING_ABSTRACT_TITLE_PATTERN = re.compile(r"\bmtgabs\b|\(keynote\)|\binvited\)", re.I)
THEMATIC_NON_RR_PATTERN = re.compile(
    r"\bdeveloping countries\b|"
    r"\bglobal cement industry\b|"
    r"\bcommercial services trade\b|"
    r"\beconomic growth\b|"
    r"\bretrofitting regional heating boilers\b|"
    r"\bfootprint reduction\b|"
    r"\bstainless steel\b|"
    r"\bunited states-mexico-canada agreement\b|"
    r"\bwastewater remediation\b|"
    r"\bbio-fa[çc]ades\b|"
    r"\bin plants\b|"
    r"\bbioplastic in malaysia\b|"
    r"\batmospheric carbon dioxide reduction and conversion\b|"
    r"\bcarbon dioxide reduction potential\b|"
    r"\blithium energy storage\b|"
    r"\benergy storage and co2 reduction\b|"
    r"\bpyrolysis/gasification\b|"
    r"\bcarbon dioxide atmosphere\b|"
    r"\bcarbothermal reduction\b",
    re.I,
)
DUAL_OXYGEN_CO2RR_PATTERN = re.compile(
    r"\boxygen and carbon dioxide reduction\b|"
    r"\borr\b.*\bco2rr\b|"
    r"\bco2rr\b.*\borr\b",
    re.I,
)


BUCKET_EXPORTS = {
    "core_journal_article": "core_journal_articles",
    "residual_review_like": "residual_review_like",
    "journal_commentary_editorial": "journal_commentary_editorial",
    "cover_teaser": "cover_teasers",
    "peer_review_material": "peer_review_materials",
    "chemrxiv_preprint": "chemrxiv_preprints",
    "ssrn_record": "ssrn_records",
    "posted_content_other": "posted_content_other",
    "book_or_chapter": "book_or_chapters",
    "proceedings": "proceedings",
    "report": "reports",
    "dataset": "datasets",
    "meeting_abstract": "meeting_abstracts",
    "thematic_non_rr": "thematic_non_rr",
    "dual_oxygen_co2rr": "dual_oxygen_co2rr",
    "needs_manual_verification": "needs_manual_verification",
}


@dataclass
class SourceRow:
    doi: str
    title: str
    year: str
    source: str
    matched_query: str
    work_id: str
    landing_page: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit CO2RR corpus record authenticity.")
    parser.add_argument("--input-csv", required=True, help="Thematically cleaned input CSV.")
    parser.add_argument("--raw-csv", required=True, help="Raw Crossref harvest CSV with work_type.")
    parser.add_argument("--out-prefix", required=True, help="Output prefix without suffix.")
    return parser.parse_args()


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def display_title(title: str) -> str:
    text = unescape(title or "")
    had_markup = "<" in text and ">" in text
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u2005", " ")
    text = text.replace("\xa0", " ")
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(
        {
            ord("\u2010"): "-",
            ord("\u2011"): "-",
            ord("\u2012"): "-",
            ord("\u2013"): "-",
            ord("\u2014"): "-",
            ord("\u2212"): "-",
        }
    )
    text = re.sub(r"\s+", " ", text).strip()
    if had_markup:
        text = re.sub(r"\s+([,.;:)])", r"\1", text)
    return text


def normalize_lookup_title(title: str) -> str:
    text = display_title(title).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\bco\s+2\b", "co2", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def had_markup(title: str) -> bool:
    text = title or ""
    return "<" in text and ">" in text


def classify_record(doi: str, source_work_type: str, cleaned_title: str) -> Tuple[str, str]:
    work_type = (source_work_type or "").strip()

    if work_type == "peer-review" or PEER_REVIEW_TITLE_PATTERN.search(cleaned_title):
        return "peer_review_material", "crossref_peer_review_or_decision_response"

    if COMMENTARY_PATTERN.search(cleaned_title):
        return "journal_commentary_editorial", "commentary_editorial_title"

    if MEETING_ABSTRACT_DOI_PATTERN.search(doi) or MEETING_ABSTRACT_TITLE_PATTERN.search(cleaned_title):
        return "meeting_abstract", "meeting_abstract_doi_or_title_pattern"

    if THEMATIC_NON_RR_PATTERN.search(cleaned_title):
        return "thematic_non_rr", "non_co2rr_thematic_title_pattern"

    if DUAL_OXYGEN_CO2RR_PATTERN.search(cleaned_title):
        return "dual_oxygen_co2rr", "dual_oxygen_and_co2rr_title_pattern"

    if COVER_PREFIX.search(cleaned_title) or COVER_SUFFIX.search(cleaned_title):
        return "cover_teaser", "cover_feature_or_issue_highlight"

    if work_type in {"book-chapter", "book", "edited-book"} or CHAPTER_DOI_PATTERN.search(doi):
        return "book_or_chapter", "book_or_chapter_record"

    if work_type == "dataset":
        return "dataset", "crossref_dataset"

    if work_type == "report":
        return "report", "crossref_report"

    if work_type == "proceedings-article":
        return "proceedings", "crossref_proceedings_article"

    if CHEMRXIV_PATTERN.search(doi):
        return "chemrxiv_preprint", "chemrxiv_doi_prefix"

    if SSRN_PATTERN.search(doi):
        return "ssrn_record", "ssrn_doi_prefix"

    if work_type == "posted-content" or OTHER_POSTED_PATTERN.search(doi):
        return "posted_content_other", "posted_content_or_repository_prefix"

    if work_type == "journal-article" and REVIEW_LIKE_PATTERN.search(cleaned_title):
        return "residual_review_like", "review_like_title_pattern"

    if work_type == "journal-article":
        return "core_journal_article", "journal_article_not_flagged"

    return "needs_manual_verification", f"unhandled_work_type:{work_type or 'missing'}"


def strip_cover_title(cleaned_title: str) -> str:
    stripped = COVER_PREFIX.sub("", cleaned_title)
    stripped = COVER_SUFFIX.sub("", stripped).strip()
    parts = stripped.split(": ", 1)
    if len(parts) == 2 and len(parts[0]) <= 45:
        return parts[1].strip()
    return stripped


def write_csv(path: Path, fieldnames: Iterable[str], rows: List[Dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def write_txt(path: Path, rows: List[Dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig") as handle:
        for row in rows:
            doi = row.get("doi", "")
            title = row.get("display_title", "") or row.get("title", "")
            bucket = row.get("auth_bucket", "")
            reason = row.get("auth_reason", "")
            handle.write(f"{doi}\t{title}\t{bucket}\t{reason}\n")


def build_clean_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    clean_rows: List[Dict[str, str]] = []
    for row in rows:
        clean_rows.append(
            {
                "doi": row.get("doi", ""),
                "title": row.get("display_title", "") or row.get("title", ""),
                "year": row.get("year", ""),
                "matched_query": row.get("matched_query", ""),
                "landing_page": row.get("landing_page", ""),
            }
        )
    return clean_rows


def write_xlsx(path: Path, fieldnames: Iterable[str], rows: List[Dict[str, str]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "doi_corpus"
    headers = list(fieldnames)
    worksheet.append(headers)
    for row in rows:
        worksheet.append([row.get(header, "") for header in headers])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_cells in worksheet.columns:
        length = max(len(str(cell.value or "")) for cell in column_cells)
        worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 10), 80)
    workbook.save(path)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_csv)
    raw_path = Path(args.raw_csv)
    out_prefix = Path(args.out_prefix)

    source_rows = read_csv(input_path)
    raw_rows = read_csv(raw_path)
    raw_by_doi = {(row.get("doi") or "").strip().lower(): row for row in raw_rows}

    audited_rows: List[Dict[str, str]] = []
    bucket_counts: Counter[str] = Counter()
    raw_type_counts: Counter[str] = Counter()

    for row in source_rows:
        doi = (row.get("doi") or "").strip().lower()
        raw = raw_by_doi.get(doi, {})
        source_work_type = (raw.get("work_type") or "").strip()
        cleaned = display_title(row.get("title") or "")
        bucket, reason = classify_record(doi, source_work_type, cleaned)

        audited = dict(row)
        audited["display_title"] = cleaned
        audited["source_work_type"] = source_work_type
        audited["title_had_markup"] = "yes" if had_markup(row.get("title") or "") else "no"
        audited["auth_bucket"] = bucket
        audited["auth_reason"] = reason
        audited["possible_duplicate_core_doi"] = ""
        audited["possible_duplicate_core_title"] = ""
        audited_rows.append(audited)
        bucket_counts[bucket] += 1
        raw_type_counts[source_work_type or "missing"] += 1

    core_title_index: Dict[str, Dict[str, str]] = {}
    for row in audited_rows:
        if row["auth_bucket"] != "core_journal_article":
            continue
        key = normalize_lookup_title(row["display_title"])
        if key and key not in core_title_index:
            core_title_index[key] = row

    for row in audited_rows:
        if row["auth_bucket"] != "cover_teaser":
            continue
        stripped = strip_cover_title(row["display_title"])
        lookup = normalize_lookup_title(stripped)
        match = core_title_index.get(lookup)
        if match:
            row["possible_duplicate_core_doi"] = match.get("doi", "")
            row["possible_duplicate_core_title"] = match.get("display_title", "")

    fieldnames = list(audited_rows[0].keys()) if audited_rows else []
    write_csv(out_prefix.with_name(out_prefix.name + "_audited.csv"), fieldnames, audited_rows)

    by_bucket: Dict[str, List[Dict[str, str]]] = {bucket: [] for bucket in BUCKET_EXPORTS}
    for row in audited_rows:
        by_bucket.setdefault(row["auth_bucket"], []).append(row)

    for bucket, suffix in BUCKET_EXPORTS.items():
        rows = by_bucket.get(bucket, [])
        if not rows:
            continue
        write_csv(out_prefix.with_name(out_prefix.name + f"_{suffix}.csv"), fieldnames, rows)
        write_txt(out_prefix.with_name(out_prefix.name + f"_{suffix}.txt"), rows)

    core_clean_rows = build_clean_rows(by_bucket.get("core_journal_article", []))
    core_clean_fields = ["doi", "title", "year", "matched_query", "landing_page"]
    write_csv(
        out_prefix.with_name(out_prefix.name + "_core_journal_articles_clean.csv"),
        core_clean_fields,
        core_clean_rows,
    )
    write_txt(
        out_prefix.with_name(out_prefix.name + "_core_journal_articles_clean.txt"),
        by_bucket.get("core_journal_article", []),
    )
    write_xlsx(
        out_prefix.with_name(out_prefix.name + "_core_journal_articles_clean.xlsx"),
        core_clean_fields,
        core_clean_rows,
    )

    cover_duplicate_count = sum(
        1 for row in by_bucket.get("cover_teaser", []) if row.get("possible_duplicate_core_doi")
    )
    summary = {
        "input_csv": str(input_path),
        "raw_csv": str(raw_path),
        "audited_csv": str(out_prefix.with_name(out_prefix.name + "_audited.csv")),
        "total_rows": len(audited_rows),
        "bucket_counts": dict(bucket_counts),
        "source_work_type_counts": dict(raw_type_counts),
        "title_markup_rows": sum(1 for row in audited_rows if row["title_had_markup"] == "yes"),
        "cover_teaser_duplicate_matches": cover_duplicate_count,
    }
    with out_prefix.with_name(out_prefix.name + "_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
