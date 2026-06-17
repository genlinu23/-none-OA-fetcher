from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from logging import Logger

from ..models import DownloadResult
from ..models import DownloadRow
from ..models import RunConfig


class DownloadProvider(ABC):
    provider_name = "base"

    @abstractmethod
    def can_handle(self, row: DownloadRow) -> bool:
        raise NotImplementedError

    @abstractmethod
    def download_one(self, row: DownloadRow, config: RunConfig, logger: Logger) -> DownloadResult:
        raise NotImplementedError
