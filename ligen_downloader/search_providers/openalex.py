from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from ..domain import SearchPage
from ..domain import SearchQuery
from ..domain import SearchRecord
from .base import ProgressCallback


class OpenAlexProvider:
    provider_id = "openalex"
    display_name = "OpenAlex"

    def search(self, query: SearchQuery, *, limit: int, progress_callback: ProgressCallback | None = None) -> SearchPage:
        started = time.time()
        cap = _resolve_cap(limit, env_name="LIGEN_OPENALEX_FULL_HARVEST_CAP", default=1000)
        page_size = max(1, min(200, _resolve_cap(0, env_name="LIGEN_OPENALEX_PAGE_SIZE", default=200)))
        cursor = "*"
        page = 1
        records: list[SearchRecord] = []
        reported_total_count: int | None = None
        try:
            while cap <= 0 or len(records) < cap:
                per_page = page_size if cap <= 0 else min(page_size, cap - len(records))
                encoded = urllib.parse.urlencode({
                    "search": query.query_text,
                    "per-page": str(per_page),
                    "cursor": cursor,
                })
                url = f"https://api.openalex.org/works?{encoded}"
                with urllib.request.urlopen(url, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                meta = payload.get("meta", {})
                if reported_total_count is None:
                    reported_total_count = _safe_int(meta.get("count"))
                items = payload.get("results") or []
                if not items:
                    break
                records.extend(self._record_from_item(item) for item in items)
                if progress_callback:
                    progress_callback({
                        "provider_id": self.provider_id,
                        "display_name": self.display_name,
                        "fetched": len(records),
                        "cap": cap,
                        "reported_total_count": reported_total_count,
                        "page": page,
                    })
                next_cursor = str(meta.get("next_cursor") or "")
                if not next_cursor or next_cursor == cursor or len(items) < per_page:
                    break
                cursor = next_cursor
                page += 1
                time.sleep(0.15)
            return SearchPage(
                provider_id=self.provider_id,
                query_id=query.query_text,
                records=records,
                reported_total_count=reported_total_count,
                returned_count=len(records),
                status="ok",
                elapsed_seconds=round(time.time() - started, 3),
            )
        except Exception as exc:
            return SearchPage(
                provider_id=self.provider_id,
                query_id=query.query_text,
                records=[],
                reported_total_count=None,
                returned_count=0,
                status="error",
                error=str(exc),
                elapsed_seconds=round(time.time() - started, 3),
            )

    def _record_from_item(self, item: dict) -> SearchRecord:
        doi = str(item.get("doi") or "").replace("https://doi.org/", "").strip().lower()
        title = str(item.get("title") or item.get("display_name") or "")
        authors = []
        for author_entry in item.get("authorships") or []:
            author = author_entry.get("author") or {}
            name = str(author.get("display_name") or "")
            if name:
                authors.append(name)
        venue = str(((item.get("primary_location") or {}).get("source") or {}).get("display_name") or "")
        return SearchRecord(
            provider_id=self.provider_id,
            provider_item_id=str(item.get("id") or doi or title),
            title=title,
            doi=doi,
            url=str(item.get("doi") or item.get("id") or ""),
            authors=authors,
            year=str(item.get("publication_year") or ""),
            venue=venue,
            abstract="",
            raw=item,
        )


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _resolve_cap(limit: int, *, env_name: str, default: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    try:
        if raw:
            value = int(raw)
        elif "FULL_HARVEST_CAP" in env_name and int(limit) == 0:
            value = 0
        else:
            value = int(limit or default)
    except Exception:
        value = default
    return max(0, value)
