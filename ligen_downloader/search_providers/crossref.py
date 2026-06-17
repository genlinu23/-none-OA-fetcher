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


class CrossrefProvider:
    provider_id = "crossref"
    display_name = "Crossref"

    def search(self, query: SearchQuery, *, limit: int, progress_callback: ProgressCallback | None = None) -> SearchPage:
        started = time.time()
        cap = _resolve_cap(limit, env_name="LIGEN_CROSSREF_FULL_HARVEST_CAP", default=1000)
        page_size = max(1, min(100, _resolve_cap(0, env_name="LIGEN_CROSSREF_PAGE_SIZE", default=100)))
        cursor = "*"
        records: list[SearchRecord] = []
        reported_total_count: int | None = None
        try:
            while cap <= 0 or len(records) < cap:
                rows = page_size if cap <= 0 else min(page_size, cap - len(records))
                encoded = urllib.parse.urlencode({
                    "query.bibliographic": query.query_text,
                    "rows": str(rows),
                    "cursor": cursor,
                })
                url = f"https://api.crossref.org/works?{encoded}"
                with urllib.request.urlopen(url, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                message = payload.get("message", {})
                if reported_total_count is None:
                    reported_total_count = _safe_int(message.get("total-results"))
                items = message.get("items") or []
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
                        "page": len(records) // page_size + (1 if len(records) % page_size else 0),
                    })
                next_cursor = str(message.get("next-cursor") or "")
                if not next_cursor or next_cursor == cursor or len(items) < rows:
                    break
                cursor = next_cursor
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
        title = _first(item.get("title"))
        doi = str(item.get("DOI") or "").strip().lower()
        year = ""
        date_parts = ((item.get("published-print") or item.get("published-online") or {}).get("date-parts") or [])
        if date_parts and date_parts[0]:
            year = str(date_parts[0][0])
        authors = []
        for author in item.get("author") or []:
            name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
            if name:
                authors.append(name)
        url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
        venue = _first(item.get("container-title"))
        return SearchRecord(
            provider_id=self.provider_id,
            provider_item_id=doi or url or title,
            title=title,
            doi=doi,
            url=url,
            authors=authors,
            year=year,
            venue=venue,
            abstract=str(item.get("abstract") or ""),
            raw=item,
        )


def _first(value) -> str:
    if isinstance(value, list) and value:
        return str(value[0] or "")
    return str(value or "")


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
