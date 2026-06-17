from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path


@dataclass(slots=True)
class DownloadRow:
    idx: str
    doi: str
    title: str
    publisher: str
    url: str

    @classmethod
    def from_csv_row(cls, row: dict[str, str], fallback_idx: int) -> "DownloadRow":
        doi = (row.get("doi") or "").strip()
        return cls(
            idx=str((row.get("idx") or "").strip() or fallback_idx),
            doi=doi,
            title=(row.get("title") or "").strip(),
            publisher=(row.get("publisher") or "").strip(),
            url=(row.get("url") or f"https://doi.org/{doi}").strip(),
        )


@dataclass(slots=True)
class DownloadResult:
    idx: str
    doi: str
    title: str
    publisher: str
    status: str
    pdf_filename: str = ""
    pdf_path: str = ""
    size_bytes: int = 0
    detail: str = ""
    final_pdf_url: str = ""
    source_url: str = ""

    def to_csv_row(self) -> dict[str, str]:
        return {
            "idx": self.idx,
            "doi": self.doi,
            "title": self.title,
            "publisher": self.publisher,
            "status": self.status,
            "pdf_filename": self.pdf_filename,
            "pdf_path": self.pdf_path,
            "size_bytes": str(self.size_bytes),
            "detail": self.detail,
            "final_pdf_url": self.final_pdf_url,
            "url": self.source_url,
        }


@dataclass(slots=True)
class RunConfig:
    input_csv: Path
    output_dir: Path
    logs_dir: Path
    cdp_port: int = 9233
    page_wait_seconds: float = 20.0
    download_timeout_seconds: float = 90.0
    sleep_seconds: float = 0.5
    provider: str = "auto"


@dataclass(slots=True)
class RunSummary:
    input_csv: str
    output_dir: str
    provider: str
    total_rows: int
    status_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

