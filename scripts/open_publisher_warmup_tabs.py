import argparse
import csv
import json
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


def open_cdp_page(port: int, url: str) -> dict:
    req = Request(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=%')}", method="PUT")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_cdp_pages(port: int) -> list[dict]:
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, list) else []


def reusable_pages(port: int) -> list[dict]:
    try:
        pages = list_cdp_pages(port)
    except Exception:
        return []
    reusable: list[dict] = []
    for page in pages:
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        url = str(page.get("url") or "").strip()
        if not url or url.startswith(("chrome://", "devtools://")):
            continue
        reusable.append(page)
    return reusable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--cdp-port", type=int, default=9231)
    parser.add_argument("--sleep-seconds", type=float, default=1.5)
    parser.add_argument(
        "--max-per-publisher",
        type=int,
        default=0,
        help="Open at most this many warmup tabs per publisher. 0 means no limit.",
    )
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    rows = list(csv.DictReader(input_csv.open("r", encoding="utf-8-sig", newline="")))
    opened = []
    opened_per_publisher: dict[str, int] = {}
    reused_per_publisher: dict[str, int] = {}
    failures = []

    for row in rows:
        url = (row.get("url") or "").strip()
        doi = (row.get("doi") or "").strip()
        title = (row.get("title") or "").strip()
        publisher = (row.get("publisher") or "").strip() or "UNKNOWN"
        if publisher not in reused_per_publisher and args.max_per_publisher > 0:
            existing_pages = reusable_pages(args.cdp_port)
            reused_per_publisher[publisher] = min(len(existing_pages), args.max_per_publisher)
            opened_per_publisher[publisher] = reused_per_publisher[publisher]
            for page in existing_pages[: args.max_per_publisher]:
                print(f"REUSED {publisher} {page.get('url', '')}", flush=True)
        if args.max_per_publisher > 0 and opened_per_publisher.get(publisher, 0) >= args.max_per_publisher:
            continue
        if not url and doi:
            url = f"https://doi.org/{doi}"
        if not url:
            continue
        try:
            page = open_cdp_page(args.cdp_port, url)
            opened.append(
                {
                    "doi": doi,
                    "title": title,
                    "url": url,
                    "publisher": publisher,
                    "page_id": page.get("id", ""),
                }
            )
            opened_per_publisher[publisher] = opened_per_publisher.get(publisher, 0) + 1
            print(f"OPENED {publisher} {doi} {url}", flush=True)
            time.sleep(max(0.0, args.sleep_seconds))
        except Exception as exc:
            failures.append(
                {
                    "doi": doi,
                    "publisher": publisher,
                    "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"FAILED {publisher} {doi} {url} :: {type(exc).__name__}: {exc}", flush=True)
            continue

    print()
    print(f"Opened {len(opened)} tabs.")
    if reused_per_publisher:
        print(f"Reused {sum(reused_per_publisher.values())} existing tabs.")
    if failures:
        print(f"Failed to open {len(failures)} tabs.")
        for item in failures[:20]:
            print(f"FAIL {item['publisher']} {item['doi']} {item['error']}")
    print("Complete login / verification / institutional access in that Chrome window, then run fetch_publisher_pdfs.py.")


if __name__ == "__main__":
    main()
