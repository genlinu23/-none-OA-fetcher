from __future__ import annotations

import argparse
from pathlib import Path

from .logging_utils import build_run_id
from .logging_utils import setup_logger
from .models import RunConfig
from .runner import run_batch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ligen literature downloader product MVP")
    parser.add_argument("--input", default="", help="Input file. Supports CSV or plain-text DOI/URL list.")
    parser.add_argument("--input-csv", default="", help="Backward-compatible alias for --input.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--logs-dir", default="")
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--cdp-port", type=int, default=9233)
    parser.add_argument("--page-wait-seconds", type=float, default=20.0)
    parser.add_argument("--download-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_value = args.input or args.input_csv
    if not input_value:
        raise SystemExit("One of --input or --input-csv is required.")
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = Path(args.logs_dir).expanduser().resolve() if args.logs_dir else (output_dir / "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = build_run_id()
    logger = setup_logger(logs_dir / f"run_{run_id}.log")
    run_jsonl = logs_dir / f"run_{run_id}.jsonl"

    config = RunConfig(
        input_csv=Path(input_value).expanduser().resolve(),
        output_dir=output_dir,
        logs_dir=logs_dir,
        provider=args.provider,
        cdp_port=args.cdp_port,
        page_wait_seconds=args.page_wait_seconds,
        download_timeout_seconds=args.download_timeout_seconds,
        sleep_seconds=args.sleep_seconds,
    )
    _, summary = run_batch(config, logger, run_jsonl)
    logger.info(f"summary={summary.status_counts}")


if __name__ == "__main__":
    main()
