#!/usr/bin/env python3
"""
Split a harvested CO2RR DOI list into:
  - primary_clean
  - background_reviews
  - manual_review
  - excluded_noise

This cleaner is intentionally conservative. It removes obvious non-corpus
noise and separates review-like material, while sending ambiguous records
to manual review instead of hard deletion.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Tuple


EXCLUDE_PATTERNS = [
    (re.compile(r"\bcorrection\b", re.I), "correction"),
    (re.compile(r"\bcorrigendum\b", re.I), "corrigendum"),
    (re.compile(r"\berratum\b", re.I), "erratum"),
    (re.compile(r"\baddendum\b", re.I), "addendum"),
    (re.compile(r"\bretraction\b", re.I), "retraction"),
    (re.compile(r"\bwithdrawn\b", re.I), "withdrawn"),
    (re.compile(r"\bexpression of concern\b", re.I), "expression_of_concern"),
    (re.compile(r"\bberichtigung\b", re.I), "berichtigung"),
    (re.compile(r'^review for\s+"', re.I), "review_comment"),
    (re.compile(r"\bemission reduction\b", re.I), "emission_reduction"),
    (re.compile(r"\bemissions reduction\b", re.I), "emissions_reduction"),
    (re.compile(r"\bcarbon dioxide emissions?\b|\bco2 emissions?\b", re.I), "co2_emissions"),
    (re.compile(r"\bemission of carbon dioxide\b|\banthropogenic emission\b|\bcarbon emissions?\b", re.I), "carbon_emissions"),
    (re.compile(r"\btransport\b", re.I), "transport_policy"),
    (re.compile(r"\bcivil aviation\b", re.I), "aviation_policy"),
    (re.compile(r"\bcoal-fired\b", re.I), "energy_policy"),
    (re.compile(r"\bpower enterprise\b", re.I), "energy_policy"),
    (re.compile(r"\broad transport\b", re.I), "transport_policy"),
    (re.compile(r"\bengine\b", re.I), "engine_efficiency"),
    (re.compile(r"\bbuildings?\b", re.I), "building_emissions"),
    (re.compile(r"\bhouses?\b", re.I), "house_emissions"),
    (re.compile(r"\bmangrove\b", re.I), "environmental_emissions"),
    (re.compile(r"\bpolicy\b", re.I), "policy"),
    (re.compile(r"\bwelfare\b", re.I), "policy"),
    (re.compile(r"\bnet[- ]zero\b", re.I), "net_zero_policy"),
    (re.compile(r"\bsupercritical co2\b", re.I), "supercritical_co2"),
    (re.compile(r"\btechnical and industrial applications of co2\b", re.I), "industrial_applications"),
    (re.compile(r"\bsequestration\b", re.I), "sequestration"),
    (re.compile(r"\bmineralization\b", re.I), "mineralization"),
    (re.compile(r"\bcarbon capture\b", re.I), "carbon_capture"),
    (re.compile(r"\bcapture and utilization\b", re.I), "ccu_broad"),
    (re.compile(r"\blaser\b", re.I), "carbon_dioxide_laser"),
    (re.compile(r"\bend-tidal carbon dioxide\b", re.I), "medical_end_tidal_co2"),
    (re.compile(r"\bcarboxytherapy\b", re.I), "medical_carboxytherapy"),
    (re.compile(r"\bmobile phase\b", re.I), "chromatography"),
    (re.compile(r"\bradiopharmaceuticals\b", re.I), "radiochemistry"),
    (re.compile(r"\bstandards\b", re.I), "standards"),
    (re.compile(r"\bin general\b", re.I), "reference_chapter"),
    (re.compile(r"\bintroductory chapter\b", re.I), "reference_chapter"),
    (re.compile(r"\bsubject index\b", re.I), "subject_index"),
    (re.compile(r"\brelated titles\b", re.I), "related_titles"),
    (re.compile(r"\bcover feature\b", re.I), "cover_feature"),
    (re.compile(r"\binside cover\b", re.I), "cover_feature"),
    (re.compile(r"\bfrontispiece\b", re.I), "frontispiece"),
    (re.compile(r"\btitelbild\b|\binnenr[üu]cktitelbild\b|\binnentitelbild\b", re.I), "cover_feature_non_english"),
    (re.compile(r"\b(issue information|table of contents)\b", re.I), "issue_metadata"),
    (re.compile(r"^editorial\b", re.I), "editorial"),
    (re.compile(r"\bdirect capture\b|\bfrom air\b", re.I), "direct_capture"),
    (re.compile(r"\bcapture liquid\b", re.I), "capture_liquid"),
]

STRICT_CATALYSIS_EXCLUDE_PATTERNS = [
    (re.compile(r"\boxygen reduction\b", re.I), "oxygen_reduction"),
    (re.compile(r"\boxygen reduction reaction\b", re.I), "oxygen_reduction_reaction"),
    (re.compile(r"\bhydrogen evolution\b", re.I), "hydrogen_evolution"),
    (re.compile(r"\bh2 evolution\b", re.I), "h2_evolution"),
    (re.compile(r"\bhydrogen oxidation\b", re.I), "hydrogen_oxidation"),
    (re.compile(r"\bher\b", re.I), "hydrogen_evolution_abbrev"),
    (re.compile(r"\boer\b", re.I), "oxygen_evolution_abbrev"),
    (re.compile(r"\boxygen evolution\b", re.I), "oxygen_evolution"),
    (re.compile(r"\bsulfur dioxide\b", re.I), "sulfur_dioxide"),
    (re.compile(r"\bnitrous oxide\b", re.I), "nitrous_oxide"),
    (re.compile(r"\bnitrogen oxides?\b|\bnox\b|\bnh3-scr\b|\bselective catalytic reduction\b", re.I), "nox_scr"),
    (re.compile(r"\bli-co2 batteries?\b|\bzn-co2 batteries?\b|\bbatter(?:y|ies)\b|\bdischarge reaction\b", re.I), "battery_system"),
    (re.compile(r"\bco2 capture\b|\bcarbon dioxide capture\b|\bccs\b|\bccus\b", re.I), "carbon_capture_broad"),
    (re.compile(r"\bflooding\b|\bcore flooding\b|\bpermeability reduction\b", re.I), "flooding"),
    (re.compile(r"\btransportation sector\b|\btransport sector\b|\baviation\b|\bpower plants?\b", re.I), "sector_policy"),
    (re.compile(r"\brenewable energy for co2 reduction\b|\bbiomass recycling\b", re.I), "energy_system"),
    (re.compile(r"\bco2 pollution reduction\b", re.I), "co2_pollution_reduction"),
    (re.compile(r"\bccu\b", re.I), "ccu_broad"),
    (re.compile(r"\bdecarbonization\b|\blife cycle assessment\b|\blca\b|\bcarbon footprint\b", re.I), "sustainability_meta"),
    (re.compile(r"\bmethanation\b|\bco2 methanation\b", re.I), "methanation"),
    (re.compile(r"\breforming\b|\bdry reforming\b|\bsteam reforming\b|\bphotoreforming\b|\bbireforming\b", re.I), "reforming"),
    (re.compile(r"\bco2 fixation\b|\bcarbon dioxide fixation\b|\bcycloaddition\b|\bepoxide\b|\bcyclic carbonates?\b", re.I), "fixation"),
    (re.compile(r"\bhydrogenation of co2\b|\bco2 hydrogenation\b", re.I), "hydrogenation"),
    (re.compile(r"\bnitrogen fixation\b", re.I), "nitrogen_fixation"),
    (re.compile(r"\bmicrobial\b|\bbiohybrid\b|\bshewanella\b|\bmethanosarcina\b|\bwhole-cell\b|\bextracellular electron uptake\b|\bcarbonic anhydrase\b|\benzyme\b", re.I), "bioelectrochemical_scope"),
    (re.compile(r"\band nitrate\b|\band nitrite\b|\bnitrate reduction\b|\bnitrite ions\b|\bnitrogenous pollutants\b|\burea synthesis\b|\bto urea\b|\burea electrosynthesis\b|\bethylamine\b", re.I), "nitrate_urea_coreduction"),
    (re.compile(r"\bcarbon dioxide\b.*\bemissions?\b|\bemissions?\b.*\bcarbon dioxide\b", re.I), "co2_emissions_broad"),
]

BACKGROUND_PATTERNS = [
    (re.compile(r"\breview\b", re.I), "review"),
    (re.compile(r"\breviews\b", re.I), "reviews"),
    (re.compile(r"\bperspective\b", re.I), "perspective"),
    (re.compile(r"\bperspectives\b", re.I), "perspectives"),
    (re.compile(r"\baccount\b", re.I), "account"),
    (re.compile(r"\bminireview\b", re.I), "minireview"),
    (re.compile(r"\boverview\b", re.I), "overview"),
    (re.compile(r"\broadmap\b", re.I), "roadmap"),
    (re.compile(r"\brecent advances\b", re.I), "recent_advances"),
    (re.compile(r"\brecent progress(?:es)?\b", re.I), "recent_progress"),
    (re.compile(r"\brecent research progress\b", re.I), "recent_research_progress"),
    (re.compile(r"\bresearch progress\b", re.I), "research_progress"),
    (re.compile(r"\bcurrent progress\b", re.I), "current_progress"),
    (re.compile(r"\bprogress in\b", re.I), "progress"),
    (re.compile(r"\bprogress toward\b", re.I), "progress_toward"),
    (re.compile(r"\bprogress and challenges\b", re.I), "progress_challenges"),
    (re.compile(r"^strateg(?:y|ies) for\b", re.I), "strategies"),
    (re.compile(r"^state of the art\b", re.I), "state_of_the_art"),
    (re.compile(r"what.?s next\??$", re.I), "whats_next"),
]

MANUAL_PATTERNS = [
    (re.compile(r"\bcarbon monoxide reduction\b", re.I), "carbon_monoxide_reduction"),
    (re.compile(r"\bco reduction reaction\b", re.I), "co_reduction_reaction"),
    (re.compile(r"\bco reduction\b", re.I), "co_reduction"),
    (re.compile(r"\bcarbon monoxide dehydrogenase\b", re.I), "codh_related"),
    (re.compile(r"\bcarbon dioxide\b", re.I), "carbon_dioxide_only"),
]

GENERIC_MANUAL_TITLES = {
    "carbon dioxide",
    "oxidation and reduction",
}

GARBAGE_TITLES = {
    "index",
    "front matter",
    "copyright",
}

PRIMARY_PATTERNS = [
    re.compile(r"\bco2 reduction\b", re.I),
    re.compile(r"\bco2rr\b", re.I),
    re.compile(r"\bco2 reduction reaction\b", re.I),
    re.compile(r"\bcarbon dioxide reduction\b", re.I),
    re.compile(r"\breduction of carbon dioxide\b", re.I),
    re.compile(r"\bco2 electroreduction\b", re.I),
    re.compile(r"\bcarbon dioxide electroreduction\b", re.I),
    re.compile(r"\belectroreduction of co2\b", re.I),
    re.compile(r"\belectroreduction of carbon dioxide\b", re.I),
    re.compile(r"\bphotocatalytic reduction of carbon dioxide\b", re.I),
    re.compile(r"\bphotocatalytic reduction carbon dioxide\b", re.I),
    re.compile(r"\belectrochemical reduction of carbon dioxide\b", re.I),
    re.compile(r"\belectrochemical co2 reduction\b", re.I),
    re.compile(r"\belectrochemical reduction of aqueous carbon dioxide\b", re.I),
    re.compile(r"\belectrocatalytic reduction of carbon dioxide\b", re.I),
    re.compile(r"\belectrocatalytic co2 reduction\b", re.I),
    re.compile(r"\bphotoelectrochemical reduction of carbon dioxide\b", re.I),
    re.compile(r"\bphoto electrocatalytic reduction of carbon dioxide\b", re.I),
    re.compile(r"\breduction of .*carbon dioxide\b", re.I),
    re.compile(r"\bcarbon dioxide .* reduction\b", re.I),
]


@dataclass
class Row:
    doi: str
    title: str
    year: str
    work_type: str
    source: str
    matched_query: str
    work_id: str
    landing_page: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean harvested CO2RR DOI lists.")
    parser.add_argument("--input-csv", required=True, help="Input CSV from harvest_dois.py")
    parser.add_argument("--out-prefix", required=True, help="Output prefix without suffix")
    parser.add_argument(
        "--strict-catalysis",
        action="store_true",
        help="Use a higher-precision catalysis mode that pushes ambiguous titles to manual review.",
    )
    return parser.parse_args()


def read_rows(path: Path) -> List[Row]:
    rows: List[Row] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            rows.append(
                Row(
                    doi=(raw.get("doi") or "").strip(),
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


def normalize_title(title: str) -> str:
    text = unescape(title or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("鈥", "-")
    text = text.casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\bco\s+2\b", "co2", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_preprint(row: Row) -> bool:
    doi = row.doi.casefold()
    page = row.landing_page.casefold()
    return (
        doi.startswith("10.26434/chemrxiv")
        or "chemrxiv" in page
        or "arxiv" in page
        or "researchsquare" in page
        or doi.startswith("10.21203/")
        or doi.startswith("10.2139/ssrn.")
        or "ssrn.com" in page
    )


def looks_like_book_chapter_doi(doi: str) -> bool:
    lowered = doi.casefold()
    return "/978" in lowered and ".ch" in lowered


def looks_like_meeting_abstract_doi(doi: str) -> bool:
    lowered = doi.casefold()
    return "mtgabs" in lowered


def normalized_work_types(row: Row) -> List[str]:
    value = row.work_type.casefold().replace("_", "-")
    return [part.strip() for part in value.split("+") if part.strip()]


def classify_row(row: Row, strict_catalysis: bool = False) -> Tuple[str, str]:
    normalized = normalize_title(row.title)
    if not normalized:
        return "excluded_noise", "empty_title"

    work_types = normalized_work_types(row)
    excluded_work_types = {
        "book",
        "book-chapter",
        "book-part",
        "book-section",
        "book-set",
        "preprint",
        "paratext",
        "monograph",
        "proceedings",
        "proceedings-article",
        "report",
        "dataset",
        "reference-entry",
        "posted-content",
        "dissertation",
        "thesis",
        "peer-review",
    }
    background_work_types = {
        "review",
        "editorial",
    }

    for work_type in work_types:
        if work_type in excluded_work_types:
            return "excluded_noise", f"work_type_{work_type}"
        if work_type in background_work_types:
            return "background_reviews", f"work_type_{work_type}"

    if looks_like_book_chapter_doi(row.doi):
        return "excluded_noise", "doi_book_chapter_pattern"

    if looks_like_meeting_abstract_doi(row.doi):
        return "excluded_noise", "doi_meeting_abstract_pattern"

    if is_preprint(row):
        return "excluded_noise", "preprint"

    if normalized in GARBAGE_TITLES:
        return "excluded_noise", normalized.replace(" ", "_")

    for pattern, reason in EXCLUDE_PATTERNS:
        if pattern.search(normalized):
            return "excluded_noise", reason

    if strict_catalysis:
        for pattern, reason in STRICT_CATALYSIS_EXCLUDE_PATTERNS:
            if pattern.search(normalized):
                return "excluded_noise", reason

    if normalized in GENERIC_MANUAL_TITLES:
        return "manual_review", normalized.replace(" ", "_")

    for pattern, reason in BACKGROUND_PATTERNS:
        if pattern.search(normalized):
            return "background_reviews", reason

    for pattern in PRIMARY_PATTERNS:
        if pattern.search(normalized):
            return "primary_clean", "reaction_scope_article"

    for pattern, reason in MANUAL_PATTERNS:
        if pattern.search(normalized):
            return "manual_review", reason

    if len(normalized.split()) <= 3:
        return "manual_review", "very_short_title"

    if strict_catalysis:
        return "manual_review", "ambiguous_nonprimary_title"

    return "primary_clean", "reaction_scope_article"


def dedupe_score(bucket: str, row: Row) -> Tuple[int, int, int, int, str]:
    bucket_priority = {
        "primary_clean": 0,
        "manual_review": 1,
        "background_reviews": 2,
        "excluded_noise": 3,
    }[bucket]
    preprint_penalty = 1 if is_preprint(row) else 0
    year_value = 0
    try:
        year_value = -int(row.year)
    except ValueError:
        year_value = 0
    source_bonus = 0 if "+" in row.source else 1
    return (bucket_priority, preprint_penalty, source_bonus, year_value, row.doi)


def choose_best(rows: Iterable[Tuple[Row, str, str]]) -> Tuple[Row, str, str]:
    return min(rows, key=lambda item: dedupe_score(item[1], item[0]))


def write_csv(path: Path, items: List[Dict[str, str]]) -> None:
    fieldnames = [
        "doi",
        "title",
        "year",
        "work_type",
        "source",
        "matched_query",
        "work_id",
        "landing_page",
        "bucket",
        "bucket_reason",
        "normalized_title",
        "is_preprint",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)


def write_txt(path: Path, items: List[Dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(item["doi"] + "\n")


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_csv)
    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    rows = read_rows(input_path)

    grouped: Dict[str, List[Tuple[Row, str, str]]] = {}
    for row in rows:
        bucket, reason = classify_row(row, strict_catalysis=args.strict_catalysis)
        normalized = normalize_title(row.title)
        grouped.setdefault(normalized, []).append((row, bucket, reason))

    winners: List[Dict[str, str]] = []
    duplicate_titles = 0
    for normalized_title, variants in grouped.items():
        if len(variants) > 1:
            duplicate_titles += 1
        row, bucket, reason = choose_best(variants)
        winners.append(
            {
                "doi": row.doi,
                "title": row.title,
                "year": row.year,
                "work_type": row.work_type,
                "source": row.source,
                "matched_query": row.matched_query,
                "work_id": row.work_id,
                "landing_page": row.landing_page,
                "bucket": bucket,
                "bucket_reason": reason,
                "normalized_title": normalized_title,
                "is_preprint": "yes" if is_preprint(row) else "no",
            }
        )

    winners.sort(key=lambda item: (item["bucket"], item["year"] or "9999", item["doi"]))

    buckets = {
        "primary_clean": [],
        "background_reviews": [],
        "manual_review": [],
        "excluded_noise": [],
    }
    reason_counts: Counter[str] = Counter()

    for item in winners:
        buckets[item["bucket"]].append(item)
        reason_counts[f'{item["bucket"]}:{item["bucket_reason"]}'] += 1

    for bucket_name, items in buckets.items():
        csv_path = out_prefix.parent / f"{out_prefix.name}_{bucket_name}.csv"
        txt_path = out_prefix.parent / f"{out_prefix.name}_{bucket_name}.txt"
        write_csv(csv_path, items)
        write_txt(txt_path, items)

    summary = {
        "input_csv": str(input_path),
        "strict_catalysis": args.strict_catalysis,
        "raw_rows": len(rows),
        "unique_titles_after_dedupe": len(winners),
        "duplicate_title_groups": duplicate_titles,
        "bucket_counts": {bucket: len(items) for bucket, items in buckets.items()},
        "top_reasons": reason_counts.most_common(20),
        "outputs": {
            bucket: {
                "csv": str(out_prefix.parent / f"{out_prefix.name}_{bucket}.csv"),
                "txt": str(out_prefix.parent / f"{out_prefix.name}_{bucket}.txt"),
            }
            for bucket in buckets
        },
    }
    summary_path = out_prefix.parent / f"{out_prefix.name}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
