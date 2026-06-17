from __future__ import annotations

import json
import time
from collections import Counter
from logging import Logger

from .logging_utils import append_jsonl
from .models import DownloadResult
from .models import DownloadRow
from .models import RunConfig
from .models import RunSummary
from .providers import AcsBrowserProvider
from .providers import ElsevierGuiProvider
from .providers import WileyBrowserProvider
from .utils import read_download_rows
from .utils import write_results_csv


class UnsupportedPublisherError(RuntimeError):
    pass


def build_providers() -> list:
    return [
        AcsBrowserProvider(),
        ElsevierGuiProvider(),
        WileyBrowserProvider(),
    ]


def resolve_provider(row: DownloadRow, requested_provider: str):
    providers = build_providers()
    if requested_provider != "auto":
        for provider in providers:
            if provider.provider_name == requested_provider:
                return provider
        raise UnsupportedPublisherError(f"Requested provider not found: {requested_provider}")

    for provider in providers:
        if provider.can_handle(row):
            return provider
    raise UnsupportedPublisherError(f"No provider for DOI={row.doi} publisher={row.publisher}")


def run_batch(config: RunConfig, logger: Logger, run_jsonl_path) -> tuple[list[DownloadResult], RunSummary]:
    rows = read_download_rows(config.input_csv)
    results: list[DownloadResult] = []
    results_csv = config.output_dir / "download_results.csv"

    for i, row in enumerate(rows, start=1):
        logger.info(f"[{i}/{len(rows)}] start {row.doi} provider={config.provider}")
        provider = resolve_provider(row, config.provider)
        started = time.time()
        result = provider.download_one(row, config, logger)
        elapsed = round(time.time() - started, 2)
        results.append(result)
        write_results_csv(results_csv, results)
        append_jsonl(
            run_jsonl_path,
            {
                "idx": row.idx,
                "doi": row.doi,
                "provider": provider.provider_name,
                "status": result.status,
                "elapsed_seconds": elapsed,
                "pdf_path": result.pdf_path,
            },
        )
        logger.info(f"[{i}/{len(rows)}] done {row.doi} status={result.status} elapsed={elapsed}s")
        time.sleep(max(0.0, config.sleep_seconds))

    counts = dict(Counter(result.status for result in results))
    summary = RunSummary(
        input_csv=str(config.input_csv),
        output_dir=str(config.output_dir),
        provider=config.provider,
        total_rows=len(rows),
        status_counts=counts,
    )
    (config.output_dir / "summary.json").write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    return results, summary
