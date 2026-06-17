from __future__ import annotations

import hashlib
from datetime import datetime

from ..domain import KeywordSet
from ..domain.models import utc_now_iso
from ..storage import SQLiteStore


class KeywordService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create_draft(self, query_text: str, *, name: str = "", include_terms: list[str] | None = None) -> KeywordSet:
        clean_query = query_text.strip()
        terms = include_terms or _extract_terms(clean_query)
        now = utc_now_iso()
        keyword_set = KeywordSet(
            id=None,
            name=name or f"KeywordSet {datetime.now().strftime('%Y%m%d %H%M%S')}",
            query_text=clean_query,
            include_terms=terms,
            exclude_terms=[],
            filters={},
            normalized_hash=_hash_keyword_payload(clean_query, terms, [], {}),
            status="draft",
            version=1,
            parent_id=None,
            locked_at=None,
            created_at=now,
            updated_at=now,
            notes="",
        )
        return self.store.save_keyword_set(keyword_set)

    def lock(self, keyword_set_id: int) -> KeywordSet:
        return self.store.lock_keyword_set(keyword_set_id)


def _extract_terms(query_text: str) -> list[str]:
    candidates = []
    for raw in query_text.replace("(", " ").replace(")", " ").replace('"', " ").split():
        term = raw.strip(" ,;")
        if len(term) >= 3 and term.upper() not in {"AND", "OR", "NOT"}:
            candidates.append(term)
    deduped: list[str] = []
    for term in candidates:
        if term.lower() not in {item.lower() for item in deduped}:
            deduped.append(term)
    return deduped[:20]


def _hash_keyword_payload(query_text: str, include_terms: list[str], exclude_terms: list[str], filters: dict) -> str:
    payload = "|".join(
        [
            " ".join(query_text.lower().split()),
            ",".join(term.lower() for term in include_terms),
            ",".join(term.lower() for term in exclude_terms),
            str(sorted(filters.items())),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
