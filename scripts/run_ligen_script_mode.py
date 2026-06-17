from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent


def _script_command(script_name: str, script_args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable)), "--internal-script", Path(script_name).stem, *script_args]
    return [str(Path(sys.executable)), str(SCRIPT_DIR / script_name), *script_args]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Script-friendly entrypoint for the Ligen literature downloader."
    )
    parser.add_argument("--config", default="", help="Optional JSON config file.")
    parser.add_argument("--input", default="", help="CSV/TXT DOI or URL list.")
    parser.add_argument("--phase", choices=("warmup", "download", "all"), default="")
    parser.add_argument("--engine", choices=("multiport", "single"), default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument(
        "--assume-ready",
        action="store_true",
        default=None,
        help="For --phase all, continue to download immediately after warmup.",
    )

    parser.add_argument("--launch-chrome", action="store_true", default=None)
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--chrome-user-data-root", default="")
    parser.add_argument("--chrome-user-data-dir", default="")
    parser.add_argument("--cdp-port", type=int, default=None)

    parser.add_argument("--publisher", action="append", default=[])
    parser.add_argument("--publisher-port", action="append", default=[])
    parser.add_argument("--sleep-seconds", type=float, default=None)
    parser.add_argument("--max-warmup-per-publisher", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--page-settle-seconds", type=float, default=None)
    parser.add_argument("--max-parallel-publishers", type=int, default=None)
    parser.add_argument("--keep-existing-tabs", action="store_true", default=None)
    parser.add_argument("--resume-existing", action="store_true", default=None)
    parser.add_argument("--per-doi-timeout-seconds", type=float, default=None)
    parser.add_argument("--per-publisher-timeout-seconds", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    options = _merge_config(args)

    input_path = Path(_required(options, "input")).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    phase = str(options.get("phase") or "warmup")
    engine = str(options.get("engine") or "multiport")
    output_dir = _resolve_output_dir(options, input_path, phase)

    if phase == "all":
        _run_or_print(_build_command(options, engine, input_path, output_dir, "warmup"), options)
        if not bool(options.get("assume_ready")):
            print()
            print(f"Warmup finished. Complete browser login or captcha checks, then run:")
            print(
                _format_command(
                    _build_command(options, engine, input_path, output_dir, "download")
                )
            )
            return
        _run_or_print(_build_command(options, engine, input_path, output_dir, "download"), options)
        return

    _run_or_print(_build_command(options, engine, input_path, output_dir, phase), options)


def _load_json_config(path: str) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"Config must be a JSON object: {config_path}")
    return payload


def _merge_config(args: argparse.Namespace) -> dict[str, Any]:
    options = _load_json_config(args.config)
    for key, value in vars(args).items():
        if key == "config":
            continue
        if value is None or value == "" or value == []:
            continue
        options[key] = value
    return options


def _required(options: dict[str, Any], key: str) -> str:
    value = str(options.get(key) or "").strip()
    if not value:
        raise SystemExit(f"--{key.replace('_', '-')} is required.")
    return value


def _resolve_output_dir(options: dict[str, Any], input_path: Path, phase: str) -> Path | None:
    raw_output_dir = str(options.get("output_dir") or "").strip()
    if raw_output_dir:
        return Path(raw_output_dir).expanduser().resolve()
    if phase == "all":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return input_path.parent / f"{input_path.stem}_script_run_{timestamp}"
    return None


def _build_command(
    options: dict[str, Any],
    engine: str,
    input_path: Path,
    output_dir: Path | None,
    mode: str,
) -> list[str]:
    script_name = (
        "run_ligen_paper_download_multiport.py"
        if engine == "multiport"
        else "run_ligen_paper_download.py"
    )
    cmd = _script_command(
        script_name,
        [
        "--input",
        str(input_path),
        "--mode",
        mode,
        ],
    )
    if output_dir is not None:
        cmd.extend(["--output-dir", str(output_dir)])

    _append_value(cmd, options, "sleep_seconds")
    _append_value(cmd, options, "max_warmup_per_publisher")
    _append_value(cmd, options, "max_workers")
    _append_value(cmd, options, "page_settle_seconds")
    _append_value(cmd, options, "chrome_path")
    _append_flag(cmd, options, "launch_chrome")

    if engine == "multiport":
        _append_value(cmd, options, "chrome_user_data_root")
        _append_value(cmd, options, "max_parallel_publishers")
        _append_value(cmd, options, "per_doi_timeout_seconds")
        _append_value(cmd, options, "per_publisher_timeout_seconds")
        _append_repeated(cmd, options, "publisher")
        _append_repeated(cmd, options, "publisher_port")
        _append_flag(cmd, options, "keep_existing_tabs")
        _append_flag(cmd, options, "resume_existing")
    else:
        _append_value(cmd, options, "chrome_user_data_dir")
        _append_value(cmd, options, "cdp_port")

    return cmd


def _append_value(cmd: list[str], options: dict[str, Any], key: str) -> None:
    value = options.get(key)
    if value is None or value == "":
        return
    cmd.extend([f"--{key.replace('_', '-')}", str(value)])


def _append_repeated(cmd: list[str], options: dict[str, Any], key: str) -> None:
    values = options.get(key) or []
    if isinstance(values, str):
        values = [values]
    for value in values:
        if str(value).strip():
            cmd.extend([f"--{key.replace('_', '-')}", str(value)])


def _append_flag(cmd: list[str], options: dict[str, Any], key: str) -> None:
    if bool(options.get(key)):
        cmd.append(f"--{key.replace('_', '-')}")


def _run_or_print(cmd: list[str], options: dict[str, Any]) -> None:
    print(_format_command(cmd))
    if bool(options.get("dry_run")):
        return
    subprocess.run(cmd, check=True)


def _format_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd)


if __name__ == "__main__":
    main()
