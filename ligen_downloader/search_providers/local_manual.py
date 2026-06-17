from __future__ import annotations

import time

from ..domain import SearchPage
from ..domain import SearchQuery
from ..domain import SearchRecord
from .base import ProgressCallback
from ..utils import extract_doi_like
from ..utils import infer_publisher


class LocalManualProvider:
    provider_id = "local_manual"
    display_name = "Manual DOI/List"

    def search(self, query: SearchQuery, *, limit: int, progress_callback: ProgressCallback | None = None) -> SearchPage:
        started = time.time()
        records: list[SearchRecord] = []
        for line in query.query_text.splitlines():
            cleaned = line.strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            doi = extract_doi_like(cleaned)
            url = cleaned if cleaned.startswith(("http://", "https://")) else (f"https://doi.org/{doi}" if doi else "")
            title = cleaned if not doi else doi
            records.append(
                SearchRecord(
                    provider_id=self.provider_id,
                    provider_item_id=doi or cleaned[:120],
                    title=title,
                    doi=doi,
                    url=url,
                    authors=[],
                    year="",
                    venue=infer_publisher(doi, url),
                    abstract="",
                    raw={"line": cleaned},
                )
            )
            if limit > 0 and len(records) >= limit:
                break
        if progress_callback:
            progress_callback({
                "provider_id": self.provider_id,
                "display_name": self.display_name,
                "fetched": len(records),
                "cap": limit,
                "reported_total_count": len(records),
                "page": 1,
            })
        return SearchPage(
            provider_id=self.provider_id,
            query_id="manual",
            records=records,
            reported_total_count=len(records),
            returned_count=len(records),
            status="ok",
            elapsed_seconds=round(time.time() - started, 3),
        )
