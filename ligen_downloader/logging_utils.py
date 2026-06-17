from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path


def build_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"ligen_downloader.{log_path.stem}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(stream_handler)
    return logger


def append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

