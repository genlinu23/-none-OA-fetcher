from __future__ import annotations

from collections.abc import Callable
from collections import defaultdict
from typing import Any
import re

from ..domain import DedupedSearchResult
from ..domain import KeywordSet
from ..domain import ProviderRunStats
from ..domain import SearchPage
from ..domain import SearchQuery
from ..domain import SearchRecord
from ..search_providers import CrossrefProvider
from ..search_providers import LocalManualProvider
from ..search_providers import OpenAlexProvider
from ..search_providers.base import SearchProvider
from ..storage import SQLiteStore


class SearchWorkflow:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.providers: dict[str, SearchProvider] = {
            "crossref": CrossrefProvider(),
            "openalex": OpenAlexProvider(),
            "local_manual": LocalManualProvider(),
        }

    def run(
        self,
        keyword_set: KeywordSet,
        *,
        provider_ids: list[str],
        limit_per_provider: int = 20,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[int, DedupedSearchResult]:
        if keyword_set.status != "locked":
            raise ValueError("KeywordSet must be locked before search.")
        provider_query_text = _build_provider_query_text(keyword_set)
        query = SearchQuery(
            keyword_set_id=keyword_set.id,
            query_text=provider_query_text,
            include_terms=keyword_set.include_terms,
            exclude_terms=keyword_set.exclude_terms,
            filters=keyword_set.filters,
        )
        pages: list[SearchPage] = []
        for index, provider_id in enumerate(provider_ids, start=1):
            provider = self.providers[provider_id]
            if progress_callback:
                progress_callback({
                    "event": "provider_start",
                    "provider_id": provider.provider_id,
                    "display_name": provider.display_name,
                    "provider_index": index,
                    "provider_total": len(provider_ids),
                    "fetched": 0,
                    "cap": limit_per_provider,
                })
            pages.append(
                provider.search(
                    query,
                    limit=limit_per_provider,
                    progress_callback=progress_callback,
                )
            )
            if progress_callback:
                progress_callback({
                    "event": "provider_done",
                    "provider_id": provider.provider_id,
                    "display_name": provider.display_name,
                    "provider_index": index,
                    "provider_total": len(provider_ids),
                    "fetched": pages[-1].returned_count,
                    "cap": limit_per_provider,
                })
        result = _dedupe_pages(pages, self.providers)
        run_id = self.store.save_search_run(
            keyword_set=keyword_set,
            provider_stats=result.provider_stats,
            records=result.records,
            duplicate_count=result.duplicate_count,
            overlap_count=result.overlap_count,
        )
        return run_id, result


def _dedupe_pages(pages: list[SearchPage], providers: dict[str, SearchProvider]) -> DedupedSearchResult:
    seen: dict[str, SearchRecord] = {}
    key_providers: dict[str, set[str]] = defaultdict(set)
    duplicate_count = 0
    provider_stats: list[ProviderRunStats] = []

    for page in pages:
        quality_records = [record for record in page.records if not _is_obvious_non_article_record(record)]
        doi_count = sum(1 for record in quality_records if record.doi)
        provider_stats.append(
            ProviderRunStats(
                provider_id=page.provider_id,
                display_name=providers[page.provider_id].display_name,
                reported_total_count=page.reported_total_count,
                returned_count=len(quality_records),
                doi_count=doi_count,
                download_candidate_count=sum(1 for record in quality_records if record.doi or record.url),
                error_count=1 if page.status == "error" else 0,
                status=page.status if not page.error else f"{page.status}: {page.error[:120]}",
                elapsed_seconds=page.elapsed_seconds,
            )
        )
        for record in quality_records:
            key = _dedupe_key(record)
            key_providers[key].add(record.provider_id)
            if key in seen:
                duplicate_count += 1
                continue
            seen[key] = record

    overlap_count = sum(1 for providers_for_key in key_providers.values() if len(providers_for_key) > 1)
    return DedupedSearchResult(
        records=list(seen.values()),
        duplicate_count=duplicate_count,
        overlap_count=overlap_count,
        provider_stats=provider_stats,
    )


def _dedupe_key(record: SearchRecord) -> str:
    doi = (record.doi or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = " ".join((record.title or "").lower().split())
    if title:
        return f"title:{title}:{record.year}"
    return f"{record.provider_id}:{record.provider_item_id}"


def _is_obvious_non_article_record(record: SearchRecord) -> bool:
    title = " ".join((record.title or "").lower().split())
    if not title:
        return True
    blocked_exact = {
        "copyright",
        "index",
        "front matter",
        "back matter",
        "table of contents",
        "contents",
    }
    return title in blocked_exact


def _build_provider_query_text(keyword_set: KeywordSet) -> str:
    terms = [_normalize_provider_term(term) for term in keyword_set.include_terms]
    terms = [term for term in terms if term and not _looks_garbled(term)]
    text = " ".join(terms) or keyword_set.query_text
    if _looks_garbled(text):
        text = ""
    lower = text.lower()
    if "transformer" in lower and not re.search(r"computer vision|machine learning|deep learning|artificial intelligence|natural language processing|neural network", lower):
        text = f"{text} computer vision machine learning artificial intelligence"
    return " ".join(text.split())


def _normalize_provider_term(term: str) -> str:
    value = " ".join(str(term or "").split())
    mapping = {
        "人工智能": "artificial intelligence",
        "机器学习": "machine learning",
        "深度学习": "deep learning",
        "计算机视觉": "computer vision",
        "边缘计算": "edge computing",
        "自然语言处理": "natural language processing",
        "潜伏固化": "latent curing",
        "防水": "waterproof",
        "涂层": "coating",
        "聚氨酯": "polyurethane",
    }
    return mapping.get(value, value)


def _looks_garbled(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.count("?") >= max(3, len(text) // 3)
