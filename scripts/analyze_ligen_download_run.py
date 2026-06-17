from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


STATUS_FIELDS = [
    "idx",
    "doi",
    "publisher",
    "status",
    "category",
    "action",
    "pdf_path",
    "size_bytes",
    "detail",
    "url",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a Ligen multiport download run and generate retry/action lists."
    )
    parser.add_argument("--run-dir", required=True, help="Ligen run folder containing publisher_runs/.")
    parser.add_argument("--input", default="", help="Optional original DOI TXT/CSV for missing-row accounting.")
    parser.add_argument("--library-dir", default="", help="Optional target PDF library folder for current PDF counts.")
    parser.add_argument("--out-dir", default="", help="Output folder. Defaults to <run-dir>/analysis.")
    parser.add_argument("--include-running-processes", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.exists():
        raise SystemExit(f"Run folder not found: {run_dir}")

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = collect_latest_rows(run_dir)
    expected_dois = read_expected_dois(Path(args.input).expanduser().resolve()) if args.input else []
    rows = add_missing_expected_rows(rows, expected_dois)
    for row in rows:
        category, action = classify_row(row)
        row["category"] = category
        row["action"] = action

    write_csv(out_dir / "download_run_status_rows.csv", rows, STATUS_FIELDS)
    write_retry_lists(out_dir, rows)

    summary = build_summary(
        run_dir=run_dir,
        rows=rows,
        expected_dois=expected_dois,
        library_dir=Path(args.library_dir).expanduser().resolve() if args.library_dir else None,
        include_running=args.include_running_processes,
    )
    (out_dir / "download_run_status_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "download_run_status_report.md").write_text(
        render_report(summary, rows),
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report={out_dir / 'download_run_status_report.md'}")
    print(f"rows={out_dir / 'download_run_status_rows.csv'}")
    return 0


def collect_latest_rows(run_dir: Path) -> list[dict[str, str]]:
    publisher_runs = run_dir / "publisher_runs"
    if not publisher_runs.exists():
        rows = read_download_results(run_dir / "combined_download_results.csv")
        return rows or read_download_results(run_dir / "download_results.csv")

    rows: list[dict[str, str]] = []
    for result_path in sorted(publisher_runs.glob("*/download_results.csv")):
        rows.extend(read_download_results(result_path))

    if rows:
        return dedupe_rows(rows)
    rows = read_download_results(run_dir / "combined_download_results.csv")
    return rows or read_download_results(run_dir / "download_results.csv")


def read_download_results(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(
                {
                    "idx": str(row.get("idx") or ""),
                    "doi": normalize_doi(row.get("doi") or ""),
                    "publisher": str(row.get("publisher") or ""),
                    "status": str(row.get("status") or ""),
                    "pdf_path": str(row.get("pdf_path") or ""),
                    "size_bytes": str(row.get("size_bytes") or ""),
                    "detail": str(row.get("detail") or ""),
                    "url": str(row.get("url") or ""),
                }
            )
        return rows


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    # Prefer downloaded rows, then rows with more detail, while keeping one row per DOI.
    best: dict[str, dict[str, str]] = {}
    for row in rows:
        doi = row.get("doi", "")
        if not doi:
            continue
        old = best.get(doi)
        if old is None or row_rank(row) > row_rank(old):
            best[doi] = row
    return sorted(best.values(), key=lambda item: (item.get("publisher", ""), item.get("idx", ""), item.get("doi", "")))


def row_rank(row: dict[str, str]) -> tuple[int, int, int]:
    status_score = 2 if row.get("status") == "downloaded" else 1 if row.get("status") else 0
    path_score = 1 if row.get("pdf_path") else 0
    detail_score = len(row.get("detail") or "")
    return (status_score, path_score, detail_score)


def read_expected_dois(path: Path) -> list[str]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [normalize_doi(row.get("doi") or "") for row in csv.DictReader(handle) if row.get("doi")]
    return [normalize_doi(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def add_missing_expected_rows(rows: list[dict[str, str]], expected_dois: list[str]) -> list[dict[str, str]]:
    if not expected_dois:
        return rows
    existing = {row.get("doi") for row in rows}
    output = list(rows)
    for doi in expected_dois:
        if doi and doi not in existing:
            output.append(
                {
                    "idx": "",
                    "doi": doi,
                    "publisher": "",
                    "status": "not_attempted_or_no_result",
                    "pdf_path": "",
                    "size_bytes": "",
                    "detail": "",
                    "url": f"https://doi.org/{doi}",
                }
            )
    return output


def classify_row(row: dict[str, str]) -> tuple[str, str]:
    status = (row.get("status") or "").lower()
    publisher = (row.get("publisher") or "").upper()
    detail = (row.get("detail") or "").lower()
    url = (row.get("url") or "").lower()
    doi = (row.get("doi") or "").lower()
    if not publisher and (doi.startswith("10.1016/") or "sciencedirect.com" in url or "sciencedirect.com" in detail):
        publisher = "ELSEVIER"

    if status == "downloaded":
        return "downloaded", "No action needed."
    if "elsevier_no_subscription" in detail or (
        publisher == "ELSEVIER"
        and (
            "does not subscribe to this content" in detail
            or "your institution does not subscribe" in detail
            or "not entitled to access this content" in detail
            or "not have access to this content" in detail
        )
    ):
        return "elsevier_no_subscription", "Institution verification is active, but ScienceDirect reports no subscription for this item. Do not retry automatically; mark for manual alternative access."
    if "modulenotfounderror" in detail:
        return "missing_dependency", "Install the missing Python package, then rerun download with --resume-existing."
    if "connection refused" in detail or "cdp" in detail and "refused" in detail:
        return "cdp_not_ready", "Launch Chrome CDP warmup for this publisher, then rerun download."
    if "publisher_robot_captcha" in detail or "captcha challenge" in detail or "are you a robot" in detail or "are you a human" in detail:
        return "publisher_robot_captcha", "Open the publisher warmup tab and complete the robot/captcha challenge, then rerun failed rows."
    if "captcha" in detail or "robot" in detail or "cloudflare" in detail:
        return "manual_verification", "Open the warmup tab and complete captcha/robot verification, then rerun."
    if "login" in detail or "sign in" in detail or "institution" in detail or "shibboleth" in detail:
        return "manual_login_or_institution", "Open the publisher tab, complete institution/login verification, then rerun."
    if (
        "err_connection_timed_out" in detail
        or "operation has timed out" in detail
        or "timed out" in detail
        or "tcp connect" in detail
        or "getaddrinfo failed" in detail
        or "temporary failure in name resolution" in detail
        or "name or service not known" in detail
    ):
        return "site_unreachable_or_network_timeout", "The site did not respond from this network. Check VPN/proxy/DNS or retry later; this is not a permissions conclusion."
    if "ssrn.com" in detail or "ssrn.com" in url or "10.2139/ssrn" in doi:
        if "failed to fetch" in detail or not detail:
            return "site_unreachable_or_network_timeout", "SSRN often times out on this network. Check proxy/VPN or open the SSRN abstract manually."
    if publisher == "ACS" and (
        "http error 403" in detail
        or '"status": 403' in detail
        or "forbidden" in detail
        or "pubs.acs.org/doi/pdf" in detail and "text/html" in detail
    ):
        return "acs_needs_verification", "Open the ACS warmup tab, complete institution/campus verification, then rerun ACS. Verified ACS returns application/pdf with a %PDF header."
    if publisher == "ELSEVIER" and (
        "access through" in detail
        or "peking" in detail
        or "shibboleth" in detail
        or "sciencedirect.com/science/article/abs/" in detail
    ):
        return "elsevier_needs_institution_verification", "Open ScienceDirect, click Access through institution/Peking U, complete verification, then rerun Elsevier."
    if publisher == "ELSEVIER" and (
        "http error 403" in detail
        or '"status": 403' in detail
        or "forbidden" in detail
        or ("text/html" in detail and ("pdfft" in detail or "/pdf" in detail))
    ):
        return "elsevier_auth_or_antibot", "ScienceDirect returned HTML/403 instead of a PDF. Complete visible robot/institution verification in the Elsevier browser session, then rerun only these rows."
    if publisher == "ELSEVIER" and (
        "sciencedirect.com/science/article/pii/" in detail
        and ("pdfft" in detail or "/pdf" in detail)
        and ("text/html" in detail or '"ispdf": false' in detail)
    ):
        return "elsevier_view_pdf_not_materialized", "ScienceDirect has an article/PDF route but returned HTML. Click View PDF once in Chrome or rerun after institution access is active."
    if "http error 403" in detail or '"status": 403' in detail or "forbidden" in detail or "access denied" in detail:
        return "auth_or_entitlement", "Verify campus/VPN entitlement in the publisher tab, then rerun this publisher."
    if "bad gateway" in detail or "http error 502" in detail or "http error 503" in detail or "http error 504" in detail:
        return "publisher_site_error", "Publisher returned a server error. Retry later; keep the DOI in the retry list."
    if "urlerror" in detail or "failed to fetch" in detail or "chrome-error://chromewebdata" in detail:
        return "browser_fetch_or_cors", "Chrome/CDP could not fetch the PDF from the page context. Retry after network/VPN check; if repeated, open DOI manually."
    if "nonpdf" in detail or '"ispdf": false' in detail or "text/html" in detail:
        return "non_pdf_or_landing_page", "Open DOI manually; the script reached HTML instead of a PDF."
    if publisher == "UNKNOWN":
        return "unknown_publisher", "Resolve DOI manually or provide publisher URL, then retry."
    if status == "not_attempted_or_no_result":
        return "not_attempted_or_no_result", "Rerun download; no result row was found for this DOI."
    return "other_failed", "Inspect detail; rerun after warmup if the cause is unclear."


def write_retry_lists(out_dir: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        doi = row.get("doi") or ""
        if not doi or row.get("category") == "downloaded":
            continue
        groups["all_failed"].append(doi)
        groups[row.get("category") or "other_failed"].append(doi)
        publisher = safe_name(row.get("publisher") or "UNKNOWN")
        groups[f"publisher_{publisher}"].append(doi)

    for name, dois in sorted(groups.items()):
        unique = list(dict.fromkeys(dois))
        (out_dir / f"retry_{name}_doi_only.txt").write_text(
            "\n".join(unique) + ("\n" if unique else ""),
            encoding="utf-8",
        )


def build_summary(
    *,
    run_dir: Path,
    rows: list[dict[str, str]],
    expected_dois: list[str],
    library_dir: Path | None,
    include_running: bool,
) -> dict[str, object]:
    status_counts = Counter(row.get("status") or "" for row in rows)
    category_counts = Counter(row.get("category") or "" for row in rows)
    publisher_counts: dict[str, dict[str, int]] = {}
    for publisher, pub_rows in group_by(rows, "publisher").items():
        publisher_counts[publisher or "UNKNOWN"] = dict(Counter(row.get("category") or "" for row in pub_rows))

    downloaded_dois = [row.get("doi") for row in rows if row.get("category") == "downloaded"]
    failed_dois = [row.get("doi") for row in rows if row.get("category") != "downloaded"]
    summary: dict[str, object] = {
        "run_dir": str(run_dir),
        "expected_doi_count": len(expected_dois) if expected_dois else len(rows),
        "result_row_count": len(rows),
        "downloaded_unique_doi_count": len(set(downloaded_dois)),
        "failed_or_pending_unique_doi_count": len(set(failed_dois)),
        "status_counts": dict(status_counts),
        "category_counts": dict(category_counts),
        "publisher_category_counts": publisher_counts,
        "next_actions": next_actions(category_counts),
        "outputs": {
            "status_rows_csv": str((run_dir / "analysis" / "download_run_status_rows.csv")),
            "report_md": str((run_dir / "analysis" / "download_run_status_report.md")),
        },
    }
    if library_dir:
        summary["library_pdf_counts"] = pdf_counts(library_dir)
    if include_running:
        summary["running_ligen_processes"] = running_ligen_processes()
    return summary


def next_actions(category_counts: Counter[str]) -> list[str]:
    actions = []
    if category_counts.get("missing_dependency"):
        actions.append("Install missing Python dependencies, then rerun with --resume-existing.")
    if category_counts.get("acs_needs_verification"):
        actions.append("ACS rows need institution verification: open ACS warmup tabs until Open PDF returns application/pdf, then rerun ACS.")
    if category_counts.get("elsevier_needs_institution_verification"):
        actions.append("Elsevier rows need institution verification: click Access through institution/Peking U until the URL changes from /article/abs/pii/ to /article/pii/ and View PDF appears.")
    if category_counts.get("elsevier_view_pdf_not_materialized"):
        actions.append("Elsevier View PDF rows should be opened once in Chrome so ScienceDirect generates a temporary pdf.sciencedirectassets.com main.pdf URL, then rerun Elsevier.")
    if category_counts.get("elsevier_no_subscription"):
        actions.append("Elsevier no-subscription rows are entitlement gaps after verification; skip automatic retry and use manual alternative access if the paper is essential.")
    if category_counts.get("auth_or_entitlement"):
        actions.append("Complete publisher login/VPN entitlement checks for 403/Forbidden rows.")
    if category_counts.get("manual_verification"):
        actions.append("Complete captcha/robot verification in warmup tabs.")
    if category_counts.get("publisher_robot_captcha"):
        actions.append("Publisher robot/captcha rows need manual browser verification first; after the challenge passes, rerun only those failed rows.")
    if category_counts.get("manual_login_or_institution"):
        actions.append("Complete institution login in the relevant publisher tabs, then rerun.")
    if category_counts.get("site_unreachable_or_network_timeout"):
        actions.append("Site/network timeout rows are not permissions conclusions; check VPN/proxy/DNS or retry later.")
    if category_counts.get("publisher_site_error"):
        actions.append("Publisher server-error rows should be retried later or opened manually.")
    if category_counts.get("unknown_publisher"):
        actions.append("Resolve UNKNOWN publisher DOI pages manually or convert them to direct publisher URLs.")
    if category_counts.get("browser_fetch_or_cors"):
        actions.append("Browser-fetch rows reached a page but not a PDF; retry after warmup or open DOI manually.")
    if not actions:
        actions.append("No special remediation detected; rerun failed rows after checking warmup tabs.")
    return actions


def pdf_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"exists": 0}
    total = valid = zero = bad = 0
    for pdf in path.glob("*.pdf"):
        total += 1
        try:
            size = pdf.stat().st_size
            if size == 0:
                zero += 1
                continue
            with pdf.open("rb") as handle:
                if handle.read(4) == b"%PDF":
                    valid += 1
                else:
                    bad += 1
        except OSError:
            bad += 1
    return {"exists": 1, "total_pdf": total, "valid_pdf_header": valid, "zero_byte": zero, "bad_header": bad}


def running_ligen_processes() -> list[dict[str, str]]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python.exe' -and $_.CommandLine -match 'ligen|fetch_publisher|run_ligen' } | "
        "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Depth 3"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except Exception:
        return []
    stdout = completed.stdout or ""
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    return [
        {
            "process_id": str(item.get("ProcessId") or ""),
            "creation_date": str(item.get("CreationDate") or ""),
            "command_line": str(item.get("CommandLine") or ""),
        }
        for item in payload
        if isinstance(item, dict)
    ]


def render_report(summary: dict[str, object], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Ligen Download Run Status",
        "",
        f"- Run folder: `{summary['run_dir']}`",
        f"- Expected DOI count: {summary['expected_doi_count']}",
        f"- Downloaded unique DOI count: {summary['downloaded_unique_doi_count']}",
        f"- Failed or pending unique DOI count: {summary['failed_or_pending_unique_doi_count']}",
        "",
        "## Failure Categories",
        "",
    ]
    for category, count in sorted(dict(summary["category_counts"]).items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{category}`: {count}")
    lines.extend(["", "## Next Actions", ""])
    for action in summary["next_actions"]:
        lines.append(f"- {action}")
    lines.extend(["", "## Publisher Breakdown", ""])
    for publisher, counts in sorted(dict(summary["publisher_category_counts"]).items()):
        formatted = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        lines.append(f"- `{publisher}`: {formatted}")
    lines.extend(["", "## Example Failed Rows", ""])
    for row in [item for item in rows if item.get("category") != "downloaded"][:25]:
        detail = (row.get("detail") or "").replace("\n", " ")
        if len(detail) > 180:
            detail = detail[:177] + "..."
        lines.append(f"- `{row.get('doi')}` `{row.get('publisher')}` `{row.get('category')}`: {detail}")
    lines.append("")
    return "\n".join(lines)


def write_csv(path: Path, rows: Iterable[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def group_by(rows: Iterable[dict[str, str]], key: str) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row.get(key, "")].append(row)
    return groups


def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.lower()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "UNKNOWN").strip("_") or "UNKNOWN"


if __name__ == "__main__":
    raise SystemExit(main())
