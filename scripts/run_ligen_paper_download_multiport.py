from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_USER_DATA_ROOT = Path.home() / "chrome-cdp-multiport"
DEFAULT_PORT_MAP = {
    "ACS": 9231,
    "Wiley": 9232,
    "Elsevier": 9233,
    "Nature": 9234,
    "Springer": 9235,
    "RSC": 9236,
    "MDPI": 9237,
    "Frontiers": 9238,
    "IOP": 9239,
    "ECS": 9240,
    "AIP": 9241,
    "Oxford": 9242,
    "PNAS": 9243,
    "OSTI": 9244,
    "UNKNOWN": 9249,
}
SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Warm up and download publisher PDFs on separate Chrome CDP ports.")
    parser.add_argument("--input", required=True, help="CSV or TXT file containing DOI/URL entries.")
    parser.add_argument("--mode", choices=("warmup", "download", "all"), default="all")
    parser.add_argument("--output-dir", default="", help="Optional output folder. Auto-created when omitted.")
    parser.add_argument("--sleep-seconds", type=float, default=1.5)
    parser.add_argument("--max-warmup-per-publisher", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=2, help="Per-publisher download workers inside one Chrome session.")
    parser.add_argument("--page-settle-seconds", type=float, default=6.0)
    parser.add_argument("--max-parallel-publishers", type=int, default=3, help="How many publisher jobs to run at once during download.")
    parser.add_argument("--keep-existing-tabs", action="store_true", help="Do not close warmup tabs before download.")
    parser.add_argument("--resume-existing", action="store_true", help="Reuse existing publisher results and skip rows already downloaded.")
    parser.add_argument("--per-doi-timeout-seconds", type=float, default=240.0, help="Hard timeout for one DOI fetch subprocess.")
    parser.add_argument("--per-publisher-timeout-seconds", type=float, default=0.0, help="Optional overall timeout for one publisher batch. 0 disables it.")
    parser.add_argument("--launch-chrome", action="store_true", help="Launch one Chrome debug session per missing port.")
    parser.add_argument("--chrome-path", default=str(DEFAULT_CHROME_PATH))
    parser.add_argument("--chrome-user-data-root", default=str(DEFAULT_USER_DATA_ROOT))
    parser.add_argument("--publisher-port", action="append", default=[], help="Override mapping like ACS=9231. Repeat as needed.")
    parser.add_argument("--publisher", action="append", default=[], help="Limit work to one or more publishers. Repeat as needed.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else _default_output_dir(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    batch_csv = input_path if input_path.suffix.lower() == ".csv" else _txt_to_csv(input_path, output_dir)
    rows = list(csv.DictReader(batch_csv.open("r", encoding="utf-8-sig", newline="")))
    if not rows:
        raise SystemExit(f"No rows found in {batch_csv}")

    selected_publishers = {value.strip() for value in args.publisher if value.strip()}
    port_map = _build_port_map(args.publisher_port)
    batches = _split_batches(rows, output_dir, port_map, selected_publishers)

    if not batches:
        raise SystemExit("No publisher batches were created.")

    manifest_path = output_dir / "multiport_manifest.json"
    _write_manifest(manifest_path, input_path, batch_csv, batches, port_map, args)
    print(f"Manifest: {manifest_path}")
    print(f"Publisher batches: {len(batches)}")
    for batch in batches:
        print(f"- {batch['publisher']}: {batch['count']} rows on port {batch['port']}")

    if args.mode in {"warmup", "all"}:
        _ensure_ports_ready(
            batches=batches,
            launch_chrome=args.launch_chrome,
            chrome_path=Path(args.chrome_path),
            user_data_root=Path(args.chrome_user_data_root),
        )
        for batch in batches:
            _run_publisher_warmup(batch, args)
        if args.mode == "all":
            print()
            print("Warm-up finished for all publishers. Complete login / verification in each Chrome window, then press Enter to continue.")
            try:
                input("> ")
            except EOFError:
                raise SystemExit("Interactive confirmation was not available after warm-up.")

    if args.mode in {"download", "all"}:
        _ensure_ports_ready(
            batches=batches,
            launch_chrome=False,
            chrome_path=Path(args.chrome_path),
            user_data_root=Path(args.chrome_user_data_root),
        )
        _run_multiport_downloads(batches, args, output_dir)
        _merge_results(output_dir, batches)
        print()
        print(f"Combined results CSV: {output_dir / 'combined_download_results.csv'}")
        print(f"Combined DOI map CSV: {output_dir / 'combined_downloaded_doi_filename_map.csv'}")


def _default_output_dir(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return input_path.parent / f"{input_path.stem}_multiport_run_{timestamp}"


def _python_exe() -> Path:
    return Path(sys.executable)


def _publisher_script(name: str) -> Path:
    return SCRIPT_DIR / name


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")


def _build_port_map(overrides: list[str]) -> dict[str, int]:
    port_map = dict(DEFAULT_PORT_MAP)
    for override in overrides:
        if "=" not in override:
            raise SystemExit(f"Invalid --publisher-port override: {override}. Expected PUBLISHER=PORT")
        publisher, port_value = override.split("=", 1)
        publisher = publisher.strip() or "UNKNOWN"
        try:
            port = int(port_value.strip())
        except ValueError as exc:
            raise SystemExit(f"Invalid port in override {override}: {exc}") from exc
        port_map[publisher] = port
    return port_map


def _txt_to_csv(input_path: Path, output_dir: Path) -> Path:
    out_csv = output_dir / f"{input_path.stem}.csv"
    rows: list[dict[str, str]] = []
    with input_path.open("r", encoding="utf-8-sig") as handle:
        for index, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            doi = _extract_doi_like(line)
            url = line if line.startswith(("http://", "https://")) else (f"https://doi.org/{doi}" if doi else "")
            rows.append(
                {
                    "idx": str(index),
                    "doi": doi,
                    "title": "",
                    "publisher": _infer_publisher(doi, url),
                    "url": url,
                }
            )
    with out_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["idx", "doi", "title", "publisher", "url"])
        writer.writeheader()
        writer.writerows(rows)
    return out_csv


def _extract_doi_like(value: str) -> str:
    text = value.strip()
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if "doi.org" in parsed.netloc.lower():
            return parsed.path.lstrip("/")
    if text.startswith("10."):
        return text
    return ""


def _infer_publisher(doi: str, url: str) -> str:
    normalized_doi = doi.lower()
    lower_url = url.lower()
    if normalized_doi.startswith("10.1021/") or "pubs.acs.org" in lower_url:
        return "ACS"
    if normalized_doi.startswith("10.1039/") or "pubs.rsc.org" in lower_url:
        return "RSC"
    if normalized_doi.startswith("10.1002/") or "wiley.com" in lower_url:
        return "Wiley"
    if normalized_doi.startswith("10.1038/") or "nature.com" in lower_url:
        return "Nature"
    if normalized_doi.startswith("10.1016/") or "sciencedirect.com" in lower_url:
        return "Elsevier"
    if normalized_doi.startswith("10.1007/") or "springer.com" in lower_url or "link.springer.com" in lower_url:
        return "Springer"
    if normalized_doi.startswith("10.3390/") or "mdpi.com" in lower_url:
        return "MDPI"
    if normalized_doi.startswith("10.3389/") or "frontiersin.org" in lower_url:
        return "Frontiers"
    if normalized_doi.startswith("10.1088/") or "iopscience.iop.org" in lower_url:
        return "IOP"
    if normalized_doi.startswith("10.1149/") or "electrochem.org" in lower_url:
        return "ECS"
    if normalized_doi.startswith("10.1063/") or "aip.scitation.org" in lower_url:
        return "AIP"
    if normalized_doi.startswith("10.1093/") or "academic.oup.com" in lower_url:
        return "Oxford"
    if "pnas.org" in lower_url:
        return "PNAS"
    if "osti.gov" in lower_url:
        return "OSTI"
    return "UNKNOWN"


def _split_batches(
    rows: list[dict[str, str]],
    output_dir: Path,
    port_map: dict[str, int],
    selected_publishers: set[str],
) -> list[dict[str, object]]:
    by_publisher: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        publisher = (row.get("publisher") or "").strip() or _infer_publisher(row.get("doi", ""), row.get("url", ""))
        if not publisher:
            publisher = "UNKNOWN"
        row["publisher"] = publisher
        if selected_publishers and publisher not in selected_publishers:
            continue
        by_publisher.setdefault(publisher, []).append(row)

    batches_dir = output_dir / "publisher_batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    batches: list[dict[str, object]] = []
    for publisher, pub_rows in sorted(by_publisher.items()):
        csv_path = batches_dir / f"{publisher}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["idx", "doi", "title", "publisher", "url"])
            writer.writeheader()
            writer.writerows(pub_rows)
        publisher_output_dir = output_dir / "publisher_runs" / publisher
        publisher_output_dir.mkdir(parents=True, exist_ok=True)
        batches.append(
            {
                "publisher": publisher,
                "port": port_map.get(publisher, port_map["UNKNOWN"]),
                "count": len(pub_rows),
                "batch_csv": csv_path,
                "output_dir": publisher_output_dir,
            }
        )
    return batches


def _write_manifest(
    path: Path,
    input_path: Path,
    batch_csv: Path,
    batches: list[dict[str, object]],
    port_map: dict[str, int],
    args: argparse.Namespace,
) -> None:
    payload = {
        "input": str(input_path),
        "batch_csv": str(batch_csv),
        "mode": args.mode,
        "publisher_port_map": port_map,
        "batches": [
            {
                "publisher": batch["publisher"],
                "port": batch["port"],
                "count": batch["count"],
                "batch_csv": str(batch["batch_csv"]),
                "output_dir": str(batch["output_dir"]),
            }
            for batch in batches
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _cdp_ready(port: int) -> bool:
    try:
        req = Request(f"http://127.0.0.1:{port}/json/version")
        with urlopen(req, timeout=3) as resp:
            return bool(resp.read())
    except Exception:
        return False


def _cdp_json(port: int, path: str) -> object:
    req = Request(f"http://127.0.0.1:{port}{path}")
    with urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _list_cdp_pages(port: int) -> list[dict[str, object]]:
    payload = _cdp_json(port, "/json")
    return payload if isinstance(payload, list) else []


def _close_cdp_page(port: int, page_id: str) -> None:
    try:
        _cdp_json(port, f"/json/close/{page_id}")
    except Exception:
        pass


def _close_existing_tabs(port: int) -> int:
    closed = 0
    for page in _list_cdp_pages(port):
        if not isinstance(page, dict):
            continue
        if page.get("type") != "page":
            continue
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            continue
        _close_cdp_page(port, page_id)
        closed += 1
    return closed


def _launch_chrome(chrome_path: Path, port: int, user_data_dir: Path) -> None:
    if not chrome_path.exists():
        raise SystemExit(f"Chrome not found: {chrome_path}")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(chrome_path),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure_ports_ready(
    batches: list[dict[str, object]],
    launch_chrome: bool,
    chrome_path: Path,
    user_data_root: Path,
) -> None:
    for batch in batches:
        port = int(batch["port"])
        publisher = str(batch["publisher"])
        if launch_chrome and not _cdp_ready(port):
            user_data_dir = user_data_root / f"{publisher}_{port}"
            _launch_chrome(chrome_path=chrome_path, port=port, user_data_dir=user_data_dir)
            for _ in range(20):
                if _cdp_ready(port):
                    break
                time.sleep(0.5)
        if not _cdp_ready(port):
            raise SystemExit(
                f"CDP port {port} for publisher {publisher} is not available. "
                "Launch Chrome with remote debugging or rerun with --launch-chrome."
            )


def _run_script(script_path: Path, script_args: list[str], log_path: Path) -> None:
    if getattr(sys, "frozen", False):
        cmd = [str(_python_exe()), "--internal-script", script_path.stem, *script_args]
    else:
        cmd = [str(_python_exe()), str(script_path), *script_args]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log_path.write_text(
        "\n".join(
            [
                f"COMMAND: {' '.join(cmd)}",
                "",
                "STDOUT:",
                result.stdout,
                "",
                "STDERR:",
                result.stderr,
            ]
        ),
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit(f"Command failed for {script_path.name}. See log: {log_path}")


def _run_publisher_warmup(batch: dict[str, object], args: argparse.Namespace) -> None:
    publisher = str(batch["publisher"])
    port = str(batch["port"])
    batch_csv = Path(batch["batch_csv"])
    log_path = Path(batch["output_dir"]) / "warmup.log"
    print(f"WARMUP {publisher} on port {port} ({batch['count']} rows)")
    _run_script(
        _publisher_script("open_publisher_warmup_tabs.py"),
        [
            "--input-csv",
            str(batch_csv),
            "--cdp-port",
            port,
            "--sleep-seconds",
            str(args.sleep_seconds),
            "--max-per-publisher",
            str(args.max_warmup_per_publisher),
        ],
        log_path=log_path,
    )


def _download_one_publisher(batch: dict[str, object], args: argparse.Namespace) -> tuple[str, str]:
    publisher = str(batch["publisher"])
    port = str(batch["port"])
    batch_csv = Path(batch["batch_csv"])
    output_dir = Path(batch["output_dir"])
    log_path = output_dir / "download.log"
    pdf_dir = output_dir / "pdfs"
    row_runs_dir = output_dir / "_row_runs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    row_runs_dir.mkdir(parents=True, exist_ok=True)
    results_csv = output_dir / "download_results.csv"
    map_csv = output_dir / "downloaded_doi_filename_map.csv"

    all_rows = list(csv.DictReader(batch_csv.open("r", encoding="utf-8-sig", newline="")))
    existing_results = _load_existing_results(results_csv)
    existing_results = _reindex_existing_results(existing_results, all_rows, publisher)
    existing_map = _load_existing_map(map_csv)

    downloaded_dois = {
        row.get("doi", "").strip()
        for row in existing_results.values()
        if (row.get("status") or "").strip() == "downloaded"
    }

    pending_rows: list[dict[str, str]] = []
    for row in all_rows:
        doi = (row.get("doi") or "").strip()
        if args.resume_existing and doi and doi in downloaded_dois:
            continue
        pending_rows.append(row)

    closed_tabs = 0 if args.keep_existing_tabs else _close_existing_tabs(int(port))
    log_lines = [
        f"PUBLISHER: {publisher}",
        f"PORT: {port}",
        f"CLOSED_EXISTING_TABS: {closed_tabs}",
        f"TOTAL_ROWS: {len(all_rows)}",
        f"EXISTING_RESULTS: {len(existing_results)}",
        f"PENDING_ROWS: {len(pending_rows)}",
        f"RESUME_EXISTING: {bool(args.resume_existing)}",
        f"KEEP_EXISTING_TABS: {bool(args.keep_existing_tabs)}",
        f"PER_DOI_TIMEOUT_SECONDS: {args.per_doi_timeout_seconds}",
        f"PER_PUBLISHER_TIMEOUT_SECONDS: {args.per_publisher_timeout_seconds}",
        "",
    ]

    deadline = None
    if args.per_publisher_timeout_seconds and args.per_publisher_timeout_seconds > 0:
        deadline = time.time() + float(args.per_publisher_timeout_seconds)

    completed_now = 0
    downloaded_now = 0
    failed_now = 0
    skipped_existing = len(all_rows) - len(pending_rows)

    for row in pending_rows:
        idx = str(row.get("idx") or "").strip() or str(completed_now + 1)
        doi = (row.get("doi") or "").strip()
        if deadline and time.time() >= deadline:
            result_row = _timeout_result_row(row, publisher, "publisher_timeout_before_start")
            existing_results[idx] = result_row
            failed_now += 1
            completed_now += 1
            _write_publisher_outputs(results_csv, map_csv, existing_results, existing_map)
            log_lines.append(f"{idx}\t{doi}\tfailed\tpublisher_timeout_before_start")
            continue

        result_row, map_row, detail = _run_single_row_download(
            row=row,
            publisher=publisher,
            port=port,
            output_dir=output_dir,
            row_runs_dir=row_runs_dir,
            pdf_dir=pdf_dir,
            page_settle_seconds=float(args.page_settle_seconds),
            per_doi_timeout_seconds=float(args.per_doi_timeout_seconds),
        )
        existing_results[idx] = result_row
        if map_row:
            existing_map[map_row["doi"]] = map_row
        if (result_row.get("status") or "").strip() == "downloaded":
            downloaded_now += 1
        else:
            failed_now += 1
        completed_now += 1
        _write_publisher_outputs(results_csv, map_csv, existing_results, existing_map)
        log_lines.append(f"{idx}\t{doi}\t{result_row.get('status','')}\t{detail}")

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    total_downloaded = sum(1 for row in existing_results.values() if (row.get("status") or "").strip() == "downloaded")
    total_failed = sum(1 for row in existing_results.values() if (row.get("status") or "").strip() != "downloaded")
    keep_note = "keeping warmup tabs" if args.keep_existing_tabs else f"closed {closed_tabs} tabs first"
    return (
        publisher,
        f"ok ({keep_note}; now {downloaded_now} downloaded, {failed_now} failed, {skipped_existing} reused; total {total_downloaded} downloaded / {total_failed} failed in {output_dir})",
    )


def _load_existing_results(results_csv: Path) -> dict[str, dict[str, str]]:
    if not results_csv.exists():
        return {}
    rows = list(csv.DictReader(results_csv.open("r", encoding="utf-8-sig", newline="")))
    return {str(row.get("idx") or "").strip(): row for row in rows if str(row.get("idx") or "").strip()}


def _load_existing_map(map_csv: Path) -> dict[str, dict[str, str]]:
    if not map_csv.exists():
        return {}
    rows = list(csv.DictReader(map_csv.open("r", encoding="utf-8-sig", newline="")))
    return {str(row.get("doi") or "").strip(): row for row in rows if str(row.get("doi") or "").strip()}


def _reindex_existing_results(
    existing_results: dict[str, dict[str, str]],
    all_rows: list[dict[str, str]],
    publisher: str,
) -> dict[str, dict[str, str]]:
    if not existing_results:
        return {}

    by_doi = {
        str(row.get("doi") or "").strip(): dict(row)
        for row in existing_results.values()
        if str(row.get("doi") or "").strip()
    }
    realigned: dict[str, dict[str, str]] = {}
    for row in all_rows:
        idx = str(row.get("idx") or "").strip()
        doi = str(row.get("doi") or "").strip()
        existing = existing_results.get(idx) or by_doi.get(doi)
        if not existing:
            continue
        aligned = dict(existing)
        aligned["idx"] = idx
        aligned["doi"] = doi
        aligned["title"] = str(row.get("title") or aligned.get("title") or "")
        aligned["publisher"] = publisher
        aligned["url"] = str(row.get("url") or aligned.get("url") or "")
        realigned[idx] = aligned
    return realigned


def _write_publisher_outputs(
    results_csv: Path,
    map_csv: Path,
    result_by_idx: dict[str, dict[str, str]],
    map_by_doi: dict[str, dict[str, str]],
) -> None:
    result_rows = sorted(result_by_idx.values(), key=lambda row: int(str(row.get("idx") or "0") or "0"))
    map_rows = sorted(map_by_doi.values(), key=lambda row: str(row.get("doi") or ""))

    with results_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "idx",
                "doi",
                "title",
                "publisher",
                "status",
                "pdf_filename",
                "pdf_path",
                "size_bytes",
                "detail",
                "url",
            ],
        )
        writer.writeheader()
        writer.writerows(result_rows)

    with map_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["doi", "title", "publisher", "pdf_filename", "pdf_path"],
        )
        writer.writeheader()
        writer.writerows(map_rows)


def _timeout_result_row(row: dict[str, str], publisher: str, reason: str) -> dict[str, str]:
    return {
        "idx": str(row.get("idx") or ""),
        "doi": str(row.get("doi") or ""),
        "title": str(row.get("title") or ""),
        "publisher": publisher,
        "status": "failed",
        "pdf_filename": "",
        "pdf_path": "",
        "size_bytes": "0",
        "detail": reason,
        "url": str(row.get("url") or ""),
    }


def _run_single_row_download(
    row: dict[str, str],
    publisher: str,
    port: str,
    output_dir: Path,
    row_runs_dir: Path,
    pdf_dir: Path,
    page_settle_seconds: float,
    per_doi_timeout_seconds: float,
) -> tuple[dict[str, str], dict[str, str] | None, str]:
    idx = str(row.get("idx") or "").strip() or "0"
    doi = (row.get("doi") or "").strip()
    run_dir = row_runs_dir / f"{idx}_{_safe_name(doi or 'no_doi')}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    single_csv = run_dir / "single.csv"
    with single_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["idx", "doi", "title", "publisher", "url"])
        writer.writeheader()
        writer.writerow(
            {
                "idx": idx,
                "doi": doi,
                "title": row.get("title", ""),
                "publisher": publisher,
                "url": row.get("url", ""),
            }
        )

    if getattr(sys, "frozen", False):
        cmd = [
            str(_python_exe()),
            "--internal-script",
            "fetch_publisher_pdfs",
        ]
    else:
        cmd = [
            str(_python_exe()),
            str(_publisher_script("fetch_publisher_pdfs.py")),
        ]
    cmd.extend(
        [
        "--input-csv",
        str(single_csv),
        "--output-dir",
        str(run_dir),
        "--cdp-port",
        str(port),
        "--max-workers",
        "1",
        "--page-settle-seconds",
        str(page_settle_seconds),
        ]
    )

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, per_doi_timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result_row(row, publisher, f"per_doi_timeout_{int(per_doi_timeout_seconds)}s"), None, f"timeout {exc.timeout}s"

    stdout_path = run_dir / "stdout_stderr.log"
    stdout_path.write_text(
        "\n".join(
            [
                f"COMMAND: {' '.join(cmd)}",
                "",
                "STDOUT:",
                completed.stdout,
                "",
                "STDERR:",
                completed.stderr,
            ]
        ),
        encoding="utf-8",
    )

    row_result_csv = run_dir / "download_results.csv"
    row_map_csv = run_dir / "downloaded_doi_filename_map.csv"
    if row_result_csv.exists():
        row_results = list(csv.DictReader(row_result_csv.open("r", encoding="utf-8-sig", newline="")))
    else:
        row_results = []
    if row_map_csv.exists():
        row_maps = list(csv.DictReader(row_map_csv.open("r", encoding="utf-8-sig", newline="")))
    else:
        row_maps = []

    if row_results:
        result_row = dict(row_results[0])
    else:
        detail = f"subprocess_exit_{completed.returncode}"
        if completed.stderr.strip():
            detail += f" | {completed.stderr.strip()[:500]}"
        result_row = _timeout_result_row(row, publisher, detail)

    result_row["idx"] = idx
    result_row["publisher"] = publisher
    result_row["doi"] = doi
    result_row["title"] = str(row.get("title") or result_row.get("title") or "")
    result_row["url"] = str(row.get("url") or result_row.get("url") or "")

    map_row = dict(row_maps[0]) if row_maps else None
    if (result_row.get("status") or "").strip() == "downloaded":
        source_pdf_path = Path(str(result_row.get("pdf_path") or ""))
        if source_pdf_path.exists():
            final_filename = f"{idx}_{_safe_name(doi)}.pdf"
            final_pdf_path = pdf_dir / final_filename
            shutil.copy2(source_pdf_path, final_pdf_path)
            result_row["pdf_filename"] = final_filename
            result_row["pdf_path"] = str(final_pdf_path)
            if map_row is None:
                map_row = {
                    "doi": doi,
                    "title": result_row["title"],
                    "publisher": publisher,
                    "pdf_filename": final_filename,
                    "pdf_path": str(final_pdf_path),
                }
            else:
                map_row["doi"] = doi
                map_row["title"] = result_row["title"]
                map_row["publisher"] = publisher
                map_row["pdf_filename"] = final_filename
                map_row["pdf_path"] = str(final_pdf_path)
            return result_row, map_row, f"downloaded via single-row subprocess exit {completed.returncode}"

    return result_row, map_row, f"failed via single-row subprocess exit {completed.returncode}"


def _run_multiport_downloads(batches: list[dict[str, object]], args: argparse.Namespace, output_dir: Path) -> None:
    summary_lines: list[str] = []
    print(f"Starting downloads across {len(batches)} publisher batches...")
    with ThreadPoolExecutor(max_workers=max(1, args.max_parallel_publishers)) as executor:
        future_map = {
            executor.submit(_download_one_publisher, batch, args): str(batch["publisher"])
            for batch in batches
        }
        for future in as_completed(future_map):
            publisher = future_map[future]
            try:
                pub_name, status = future.result()
                message = f"{pub_name}: {status}"
            except Exception as exc:
                message = f"{publisher}: exception {type(exc).__name__}: {exc}"
            print(message)
            summary_lines.append(message)
    (output_dir / "download_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def _merge_results(output_dir: Path, batches: list[dict[str, object]]) -> None:
    combined_results: list[dict[str, str]] = []
    combined_map: list[dict[str, str]] = []
    unified_pdf_dir = output_dir / "pdfs"
    unified_pdf_dir.mkdir(parents=True, exist_ok=True)
    for batch in batches:
        run_dir = Path(batch["output_dir"])
        results_csv = run_dir / "download_results.csv"
        map_csv = run_dir / "downloaded_doi_filename_map.csv"
        if results_csv.exists():
            combined_results.extend(csv.DictReader(results_csv.open("r", encoding="utf-8-sig", newline="")))
        if map_csv.exists():
            combined_map.extend(csv.DictReader(map_csv.open("r", encoding="utf-8-sig", newline="")))

    combined_results, combined_map = _materialize_unified_pdf_dir(
        combined_results=combined_results,
        combined_map=combined_map,
        unified_pdf_dir=unified_pdf_dir,
    )
    combined_results.sort(key=lambda row: (row.get("publisher", ""), row.get("idx", "")))
    combined_map.sort(key=lambda row: (row.get("publisher", ""), row.get("doi", "")))

    results_path = output_dir / "combined_download_results.csv"
    map_path = output_dir / "combined_downloaded_doi_filename_map.csv"

    if combined_results:
        with results_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(combined_results[0].keys()))
            writer.writeheader()
            writer.writerows(combined_results)
    if combined_map:
        with map_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(combined_map[0].keys()))
            writer.writeheader()
            writer.writerows(combined_map)


def _materialize_unified_pdf_dir(
    *,
    combined_results: list[dict[str, str]],
    combined_map: list[dict[str, str]],
    unified_pdf_dir: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    unified_pdf_dir.mkdir(parents=True, exist_ok=True)
    canonical_by_doi: dict[str, dict[str, str]] = {}
    canonical_by_source: dict[str, dict[str, str]] = {}

    def ensure_pdf(row: dict[str, str]) -> dict[str, str] | None:
        doi = str(row.get("doi") or "").strip()
        source_text = str(row.get("pdf_path") or "").strip()
        if not doi or not source_text:
            return None
        source_path = Path(source_text)
        if not source_path.exists():
            return None
        source_key = str(source_path.resolve())
        if doi in canonical_by_doi:
            canonical = canonical_by_doi[doi]
            canonical_by_source[source_key] = canonical
            return canonical
        if source_key in canonical_by_source:
            canonical = canonical_by_source[source_key]
            canonical_by_doi.setdefault(doi, canonical)
            return canonical

        target_name = str(row.get("pdf_filename") or "").strip()
        if not target_name:
            idx = str(row.get("idx") or "").strip() or "0"
            target_name = f"{idx}_{_safe_name(doi)}.pdf"
        target_path = unified_pdf_dir / target_name
        if not target_path.exists():
            shutil.copy2(source_path, target_path)
        canonical = {
            "pdf_filename": target_name,
            "pdf_path": str(target_path),
        }
        canonical_by_doi[doi] = canonical
        canonical_by_source[source_key] = canonical
        return canonical

    normalized_results: list[dict[str, str]] = []
    for row in combined_results:
        normalized = dict(row)
        canonical = ensure_pdf(normalized)
        if canonical:
            normalized["pdf_filename"] = canonical["pdf_filename"]
            normalized["pdf_path"] = canonical["pdf_path"]
        normalized_results.append(normalized)

    normalized_map: list[dict[str, str]] = []
    for row in combined_map:
        normalized = dict(row)
        canonical = ensure_pdf(normalized)
        if canonical:
            normalized["pdf_filename"] = canonical["pdf_filename"]
            normalized["pdf_path"] = canonical["pdf_path"]
        normalized_map.append(normalized)

    return normalized_results, normalized_map


if __name__ == "__main__":
    main()
