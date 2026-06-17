from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.request import Request
from urllib.request import urlopen


DEFAULT_CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_PYTHON = Path(r"C:\Users\logan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
DEFAULT_WARMUP_SCRIPT = Path(r"C:\Users\logan\.codex\skills\publisher-pdf-fetch\scripts\open_publisher_warmup_tabs.py")
DEFAULT_FETCH_SCRIPT = Path(r"C:\Users\logan\.codex\skills\publisher-pdf-fetch\scripts\fetch_publisher_pdfs.py")
DEFAULT_BY_PUBLISHER_DIR = Path(r"C:\Users\logan\Desktop\PKU group\doi_harvest\download_inputs\by_publisher_v2")
DEFAULT_INPUT_ROOT = Path(r"C:\Users\logan\Desktop\PKU group\doi_harvest\download_inputs\shards")
DEFAULT_DOWNLOAD_ROOT = Path(r"C:\Users\logan\Desktop\PKU group\doi_harvest\downloads")
DEFAULT_LOG_ROOT = Path(r"C:\Users\logan\Desktop\PKU group\doi_harvest\logs")


DEFAULT_SHARDS: list[dict[str, Any]] = [
    {
        "port": 9231,
        "name": "wiley_acs_nature_frontiers",
        "publishers": ["Wiley", "ACS", "Nature", "Frontiers"],
        "workers": 3,
    },
    {
        "port": 9232,
        "name": "elsevier_rsc_oxford_aip",
        "publishers": ["Elsevier", "RSC", "Oxford", "AIP"],
        "workers": 2,
    },
    {
        "port": 9233,
        "name": "springer_mdpi_iop_ecs_unknown",
        "publishers": ["Springer", "MDPI", "IOP", "ECS", "UNKNOWN"],
        "workers": 2,
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch multi-port publisher-sharded PDF downloads.")
    parser.add_argument("--run-name", required=True, help="Run folder name under download_inputs/shards, downloads, and logs.")
    parser.add_argument("--by-publisher-dir", default=str(DEFAULT_BY_PUBLISHER_DIR))
    parser.add_argument("--chrome-path", default=str(DEFAULT_CHROME_PATH))
    parser.add_argument("--python-exe", default=str(DEFAULT_PYTHON))
    parser.add_argument("--warmup-script", default=str(DEFAULT_WARMUP_SCRIPT))
    parser.add_argument("--fetch-script", default=str(DEFAULT_FETCH_SCRIPT))
    parser.add_argument("--input-root", default=str(DEFAULT_INPUT_ROOT))
    parser.add_argument("--download-root", default=str(DEFAULT_DOWNLOAD_ROOT))
    parser.add_argument("--log-root", default=str(DEFAULT_LOG_ROOT))
    parser.add_argument("--warmup-sleep-seconds", type=float, default=1.5)
    parser.add_argument("--warmup-wait-seconds", type=float, default=12.0)
    parser.add_argument("--launch-stagger-seconds", type=float, default=4.0)
    parser.add_argument("--max-warmup-per-publisher", type=int, default=1)
    parser.add_argument("--page-settle-seconds", type=float, default=8.0)
    parser.add_argument(
        "--max-rows-per-shard",
        type=int,
        default=0,
        help="For validation. 0 means full shard input.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare shard CSVs and manifest, but do not launch Chrome or downloads.",
    )
    args = parser.parse_args()

    by_publisher_dir = Path(args.by_publisher_dir).expanduser().resolve()
    python_exe = Path(args.python_exe).expanduser().resolve()
    chrome_path = Path(args.chrome_path).expanduser().resolve()
    warmup_script = Path(args.warmup_script).expanduser().resolve()
    fetch_script = Path(args.fetch_script).expanduser().resolve()
    input_root = Path(args.input_root).expanduser().resolve() / args.run_name
    download_root = Path(args.download_root).expanduser().resolve() / args.run_name
    log_root = Path(args.log_root).expanduser().resolve() / args.run_name

    input_root.mkdir(parents=True, exist_ok=True)
    download_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)

    shard_runs: list[dict[str, Any]] = []
    for shard in DEFAULT_SHARDS:
        shard_runs.append(
            prepare_shard(
                shard=shard,
                by_publisher_dir=by_publisher_dir,
                input_root=input_root,
                max_rows_per_shard=args.max_rows_per_shard,
            )
        )

    manifest = {
        "run_name": args.run_name,
        "max_rows_per_shard": args.max_rows_per_shard,
        "page_settle_seconds": args.page_settle_seconds,
        "warmup_wait_seconds": args.warmup_wait_seconds,
        "launch_stagger_seconds": args.launch_stagger_seconds,
        "shards": shard_runs,
    }
    manifest_path = input_root / "launch_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if args.prepare_only:
        print(f"Prepared only. Manifest: {manifest_path}")
        return

    ensure_exists(chrome_path, "Chrome")
    ensure_exists(python_exe, "Python")
    ensure_exists(warmup_script, "Warmup script")
    ensure_exists(fetch_script, "Fetch script")

    for shard_run in shard_runs:
        port = int(shard_run["port"])
        profile_dir = Path(shard_run["profile_dir"])
        if not cdp_ready(port):
            launch_chrome(chrome_path, port, profile_dir)
        wait_for_cdp(port)

    for shard_run in shard_runs:
        run_warmup(
            python_exe=python_exe,
            warmup_script=warmup_script,
            warmup_csv=Path(shard_run["warmup_csv"]),
            port=int(shard_run["port"]),
            sleep_seconds=args.warmup_sleep_seconds,
            max_per_publisher=args.max_warmup_per_publisher,
        )

    time.sleep(max(0.0, args.warmup_wait_seconds))

    launched: list[dict[str, Any]] = []
    for index, shard_run in enumerate(shard_runs, start=1):
        proc_info = start_download(
            python_exe=python_exe,
            fetch_script=fetch_script,
            input_csv=Path(shard_run["input_csv"]),
            output_dir=download_root / shard_run["name"],
            log_root=log_root,
            port=int(shard_run["port"]),
            workers=int(shard_run["workers"]),
            page_settle_seconds=args.page_settle_seconds,
            shard_name=str(shard_run["name"]),
        )
        shard_run.update(proc_info)
        launched.append(
            {
                "index": index,
                "name": shard_run["name"],
                "port": shard_run["port"],
                "pid": proc_info["pid"],
                "input_csv": shard_run["input_csv"],
                "output_dir": proc_info["output_dir"],
                "stdout_log": proc_info["stdout_log"],
                "stderr_log": proc_info["stderr_log"],
            }
        )
        time.sleep(max(0.0, args.launch_stagger_seconds))

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    for row in launched:
        print(
            f"[{row['index']}/{len(launched)}] "
            f"{row['name']} port={row['port']} pid={row['pid']} output={row['output_dir']}"
        )
        print(f"  input={row['input_csv']}")
        print(f"  stdout={row['stdout_log']}")
        print(f"  stderr={row['stderr_log']}")
    print(f"Manifest: {manifest_path}")


def prepare_shard(
    shard: dict[str, Any],
    by_publisher_dir: Path,
    input_root: Path,
    max_rows_per_shard: int,
) -> dict[str, Any]:
    publishers = list(shard["publishers"])
    all_rows: list[dict[str, str]] = []
    warmup_rows: list[dict[str, str]] = []

    for publisher in publishers:
        csv_path = by_publisher_dir / f"{publisher}.csv"
        ensure_exists(csv_path, f"Publisher CSV for {publisher}")
        rows = read_csv_rows(csv_path)
        if not rows:
            continue
        warmup_rows.append(normalize_row(rows[0], len(warmup_rows) + 1))
        all_rows.extend(rows)

    if max_rows_per_shard > 0:
        all_rows = all_rows[:max_rows_per_shard]

    normalized_rows = [normalize_row(row, index + 1) for index, row in enumerate(all_rows)]
    input_csv = input_root / f"{shard['port']}_{shard['name']}.csv"
    warmup_csv = input_root / f"{shard['port']}_{shard['name']}_warmup.csv"
    write_csv(input_csv, normalized_rows)
    write_csv(warmup_csv, warmup_rows)

    return {
        "name": shard["name"],
        "port": shard["port"],
        "publishers": publishers,
        "workers": shard["workers"],
        "row_count": len(normalized_rows),
        "warmup_count": len(warmup_rows),
        "input_csv": str(input_csv),
        "warmup_csv": str(warmup_csv),
        "profile_dir": str(Path(rf"C:\Users\logan\chrome-cdp-{shard['port']}")),
    }


def normalize_row(row: dict[str, str], index: int) -> dict[str, str]:
    return {
        "idx": str(index),
        "doi": (row.get("doi") or "").strip(),
        "title": (row.get("title") or "").strip(),
        "publisher": (row.get("publisher") or "").strip(),
        "url": (row.get("url") or "").strip(),
    }


def read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["idx", "doi", "title", "publisher", "url"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise SystemExit(f"{label} not found: {path}")


def cdp_ready(port: int) -> bool:
    try:
        req = Request(f"http://127.0.0.1:{port}/json/version")
        with urlopen(req, timeout=3) as resp:
            return bool(resp.read())
    except Exception:
        return False


def wait_for_cdp(port: int, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if cdp_ready(port):
            return
        time.sleep(0.5)
    raise SystemExit(f"CDP port {port} did not become ready in time.")


def launch_chrome(chrome_path: Path, port: int, profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(chrome_path),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_warmup(
    python_exe: Path,
    warmup_script: Path,
    warmup_csv: Path,
    port: int,
    sleep_seconds: float,
    max_per_publisher: int,
) -> None:
    cmd = [
        str(python_exe),
        str(warmup_script),
        "--input-csv",
        str(warmup_csv),
        "--cdp-port",
        str(port),
        "--sleep-seconds",
        str(sleep_seconds),
        "--max-per-publisher",
        str(max_per_publisher),
    ]
    subprocess.run(cmd, check=True)


def start_download(
    python_exe: Path,
    fetch_script: Path,
    input_csv: Path,
    output_dir: Path,
    log_root: Path,
    port: int,
    workers: int,
    page_settle_seconds: float,
    shard_name: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = log_root / f"{shard_name}_stdout.log"
    stderr_log = log_root / f"{shard_name}_stderr.log"
    stdout_handle = stdout_log.open("w", encoding="utf-8")
    stderr_handle = stderr_log.open("w", encoding="utf-8")
    cmd = [
        str(python_exe),
        str(fetch_script),
        "--input-csv",
        str(input_csv),
        "--output-dir",
        str(output_dir),
        "--cdp-port",
        str(port),
        "--max-workers",
        str(workers),
        "--page-settle-seconds",
        str(page_settle_seconds),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )
    return {
        "pid": proc.pid,
        "output_dir": str(output_dir),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }


if __name__ == "__main__":
    main()
