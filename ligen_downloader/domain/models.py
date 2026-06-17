from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@dataclass(frozen=True)
class KeywordSet:
    id: int | None
    name: str
    query_text: str
    include_terms: list[str]
    exclude_terms: list[str]
    filters: dict[str, Any]
    status: str
    version: int
    parent_id: int | None
    normalized_hash: str
    locked_at: str | None
    created_at: str
    updated_at: str
    notes: str = ""


@dataclass(frozen=True)
class SearchQuery:
    keyword_set_id: int | None
    query_text: str
    include_terms: list[str]
    exclude_terms: list[str]
    filters: dict[str, Any]


@dataclass(frozen=True)
class SearchRecord:
    provider_id: str
    provider_item_id: str
    title: str
    doi: str
    url: str
    authors: list[str]
    year: str
    venue: str
    abstract: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SearchPage:
    provider_id: str
    query_id: str
    records: list[SearchRecord]
    reported_total_count: int | None
    returned_count: int
    status: str
    error: str = ""
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class ProviderRunStats:
    provider_id: str
    display_name: str
    reported_total_count: int | None
    returned_count: int
    doi_count: int
    download_candidate_count: int
    error_count: int
    status: str
    elapsed_seconds: float


@dataclass(frozen=True)
class DedupedSearchResult:
    records: list[SearchRecord]
    duplicate_count: int
    overlap_count: int
    provider_stats: list[ProviderRunStats]

    @property
    def raw_total(self) -> int:
        return sum(stat.returned_count for stat in self.provider_stats)

    @property
    def unique_count(self) -> int:
        return len(self.records)

    @property
    def download_candidate_count(self) -> int:
        return sum(1 for record in self.records if record.doi or record.url)
