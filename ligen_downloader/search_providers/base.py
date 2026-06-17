from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import Protocol

from ..domain import SearchPage
from ..domain import SearchQuery


ProgressCallback = Callable[[dict[str, Any]], None]


class SearchProvider(Protocol):
    provider_id: str
    display_name: str

    def search(self, query: SearchQuery, *, limit: int, progress_callback: ProgressCallback | None = None) -> SearchPage:
        ...
