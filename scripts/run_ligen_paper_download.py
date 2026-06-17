from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_USER_DATA_DIR = Path.home() / "chrome-cdp-9231"
DEFAULT_CDP_PORT = 9231
SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Warm up publishers and batch-download paper PDFs.")
    parser.add_argument("--input", required=True, help="CSV or TXT file containing DOI/URL entries.")
    parser.add_argument("--mode", choices=("warmup", "download", "all"), default="all")
    parser.add_argument("--output-dir", default="", help="Optional output folder. Auto-created when omitted.")
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT)
    parser.add_argument("--sleep-seconds", type=float, default=1.5)
    parser.add_argument(
        "--max-warmup-per-publisher",
        type=int,
        default=0,
        help="Open at most this many representative warmup tabs per publisher. 0 means no limit.",
    )
    parser.add_argument("--max-workers", type=int, default=3, help="Bounded parallel download workers inside one Chrome session.")
    parser.add_argument("--page-settle-seconds", type=float, default=6.0, help="Wait time after opening a page before scraping links.")
    parser.add_argument("--launch-chrome", action="store_true", help="Launch Chrome debug session if the CDP port is closed.")
    parser.add_argument("--chrome-path", default=str(DEFAULT_CHROME_PATH))
    parser.add_argument("--chrome-user-data-dir", default=str(DEFAULT_USER_DATA_DIR))
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else _default_output_dir(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    batch_csv = input_path if input_path.suffix.lower() == ".csv" else _txt_to_csv(input_path, output_dir)

    if args.launch_chrome and not _cdp_ready(args.cdp_port):
        _launch_chrome(
            chrome_path=Path(args.chrome_path),
            port=args.cdp_port,
            user_data_dir=Path(args.chrome_user_data_dir),
        )
        for _ in range(20):
            if _cdp_ready(args.cdp_port):
                break
            time.sleep(0.5)

    if not _cdp_ready(args.cdp_port):
        raise SystemExit(
            f"CDP port {args.cdp_port} is not available. "
            "Launch Chrome with remote debugging first or rerun with --launch-chrome."
        )

    if args.mode in {"warmup", "all"}:
        _run_script(
            _publisher_script("open_publisher_warmup_tabs.py"),
            [
                "--input-csv",
                str(batch_csv),
                "--cdp-port",
                str(args.cdp_port),
                "--sleep-seconds",
                str(args.sleep_seconds),
                "--max-per-publisher",
                str(args.max_warmup_per_publisher),
            ],
        )
        if args.mode == "all":
            print()
            print("Warm-up tabs opened. Complete login / verification in Chrome, then press Enter to continue.")
            try:
                input("> ")
            except EOFError:
                raise SystemExit("Interactive confirmation was not available after warm-up.")

    if args.mode in {"download", "all"}:
        _run_script(
            _publisher_script("fetch_publisher_pdfs.py"),
            [
                "--input-csv",
                str(batch_csv),
                "--output-dir",
                str(output_dir),
                "--cdp-port",
                str(args.cdp_port),
                "--max-workers",
                str(args.max_workers),
                "--page-settle-seconds",
                str(args.page_settle_seconds),
            ],
        )
        print()
        print(f"Download complete. Output folder: {output_dir}")
        print(f"PDF folder: {output_dir / 'pdfs'}")
        print(f"Results CSV: {output_dir / 'download_results.csv'}")
        print(f"DOI map CSV: {output_dir / 'downloaded_doi_filename_map.csv'}")


def _default_output_dir(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = input_path.stem
    return input_path.parent / f"{stem}_run_{timestamp}"


def _publisher_script(name: str) -> Path:
    return SCRIPT_DIR / name


def _python_exe() -> Path:
    return Path(sys.executable)


def _run_script(script_path: Path, script_args: list[str]) -> None:
    if getattr(sys, "frozen", False):
        cmd = [str(_python_exe()), "--internal-script", script_path.stem, *script_args]
    else:
        cmd = [str(_python_exe()), str(script_path), *script_args]
    subprocess.run(cmd, check=True)


def _cdp_ready(port: int) -> bool:
    try:
        req = Request(f"http://127.0.0.1:{port}/json/version")
        with urlopen(req, timeout=3) as resp:
            return bool(resp.read())
    except Exception:
        return False


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
    return ""


if __name__ == "__main__":
    main()
