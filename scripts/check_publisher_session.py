from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

import websocket


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether the current Chrome CDP publisher session has PDF access.")
    parser.add_argument("--doi", required=True)
    parser.add_argument("--publisher", default="", help="Publisher name. Currently has ACS-specific checks.")
    parser.add_argument("--cdp-port", type=int, default=9231)
    parser.add_argument("--article-url", default="", help="Optional current publisher article URL, useful for ScienceDirect PII pages.")
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--out-json", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    doi = normalize_doi(args.doi)
    publisher = (args.publisher or infer_publisher(doi)).upper()
    if publisher == "ACS":
        result = check_acs(doi=doi, port=args.cdp_port, open_if_missing=args.open_if_missing)
    elif publisher in {"ELSEVIER", "SCIENCEDIRECT"}:
        result = check_elsevier(
            doi=doi,
            port=args.cdp_port,
            article_url=args.article_url.strip(),
            open_if_missing=args.open_if_missing,
        )
    else:
        result = {
            "doi": doi,
            "publisher": publisher,
            "cdp_port": args.cdp_port,
            "state": "unsupported_publisher_check",
            "action": "No publisher-specific session check is implemented yet.",
        }

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def check_acs(*, doi: str, port: int, open_if_missing: bool) -> dict[str, object]:
    article_url = f"https://pubs.acs.org/doi/{doi}"
    pdf_url = f"https://pubs.acs.org/doi/pdf/{doi}?ref=article_openPDF"
    page = find_page(port, "pubs.acs.org", doi)
    if page is None and open_if_missing:
        page = open_cdp_page(port, article_url)
    if page is None:
        return {
            "doi": doi,
            "publisher": "ACS",
            "cdp_port": port,
            "state": "no_acs_tab",
            "article_url": article_url,
            "pdf_url": pdf_url,
            "action": "Open ACS warmup for this DOI, complete institution verification, then rerun this check.",
        }

    ws_url = str(page.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        return {
            "doi": doi,
            "publisher": "ACS",
            "cdp_port": port,
            "state": "cdp_page_missing_websocket",
            "article_url": article_url,
            "pdf_url": pdf_url,
            "action": "Refresh the ACS Chrome debugging session.",
        }

    page_info = eval_json(
        ws_url,
        """
        JSON.stringify({
          href: location.href,
          title: document.title,
          ready: document.readyState,
          bodyText: (document.body && document.body.innerText || '').slice(0, 8000),
          hasOpenPdf: Array.from(document.querySelectorAll('a,button')).some((node) => {
            const text = ((node.innerText || node.textContent || '').replace(/\\s+/g, ' ')).trim().toLowerCase();
            const href = node.href || '';
            return text.includes('open pdf') || href.includes('/doi/pdf/');
          })
        })
        """,
        msg_id=1,
    )
    pdf_probe = probe_pdf(ws_url, pdf_url)
    state, action = classify_acs_state(page_info, pdf_probe)
    return {
        "doi": doi,
        "publisher": "ACS",
        "cdp_port": port,
        "state": state,
        "action": action,
        "article_url": article_url,
        "pdf_url": pdf_url,
        "page": {
            "href": page_info.get("href", ""),
            "title": page_info.get("title", ""),
            "ready": page_info.get("ready", ""),
            "has_open_pdf_link": bool(page_info.get("hasOpenPdf")),
            "has_access_provided_text": "access provided" in str(page_info.get("bodyText", "")).lower(),
            "has_log_in_text": "log in" in str(page_info.get("bodyText", "")).lower(),
            "has_institution_text": "institution" in str(page_info.get("bodyText", "")).lower(),
        },
        "pdf_probe": pdf_probe,
    }


def classify_acs_state(page_info: dict, pdf_probe: dict) -> tuple[str, str]:
    if pdf_probe.get("is_pdf") and str(pdf_probe.get("head", "")).startswith("%PDF"):
        return "acs_verified_pdf_access", "ACS verification is active. Rerun ACS downloads."
    status = int(pdf_probe.get("status") or 0)
    content_type = str(pdf_probe.get("content_type") or "").lower()
    body_text = str(page_info.get("bodyText") or "").lower()
    if status == 403 or "forbidden" in str(pdf_probe.get("error", "")).lower():
        return "acs_needs_verification", "Open the ACS tab and complete institution/campus verification. PDF fetch is still forbidden."
    if "application/pdf" not in content_type and status in {200, 401, 403}:
        return "acs_needs_verification", "ACS returned HTML/login/landing content instead of PDF. Verify institution access, then rerun."
    if "access provided" in body_text and page_info.get("hasOpenPdf"):
        return "acs_probably_verified_but_pdf_probe_failed", "ACS page shows access, but PDF probe failed. Click Open PDF once in Chrome, then rerun."
    if "log in" in body_text or "institution" in body_text:
        return "acs_needs_verification", "ACS page still shows login/institution prompts. Complete verification, then rerun."
    return "acs_unknown_session_state", "Could not determine ACS access state. Open PDF manually in the ACS tab and rerun."


def check_elsevier(*, doi: str, port: int, article_url: str, open_if_missing: bool) -> dict[str, object]:
    target_pii = extract_pii(article_url)
    asset_url = find_elsevier_pdf_asset_url(port, target_pii)
    page = find_elsevier_page(port, article_url)
    if page is None and article_url and open_if_missing:
        page = open_cdp_page(port, article_url)
    if page is None:
        return {
            "doi": doi,
            "publisher": "Elsevier",
            "cdp_port": port,
            "state": "no_elsevier_tab",
            "article_url": article_url,
            "action": "Open Elsevier/ScienceDirect warmup, complete institution verification, then rerun this check.",
        }
    ws_url = str(page.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        return {
            "doi": doi,
            "publisher": "Elsevier",
            "cdp_port": port,
            "state": "cdp_page_missing_websocket",
            "article_url": article_url,
            "action": "Refresh the ScienceDirect Chrome debugging session.",
        }
    if article_url:
        ensure_page_navigated(ws_url, article_url)
    page_info = eval_json(
        ws_url,
        """
        JSON.stringify({
          href: location.href,
          title: document.title,
          ready: document.readyState,
          bodyText: (document.body && document.body.innerText || '').slice(0, 12000),
          links: Array.from(document.querySelectorAll('a,button')).map((node) => ({
            text: ((node.innerText || node.textContent || '').replace(/\\s+/g, ' ')).trim(),
            href: node.href || '',
            aria: node.getAttribute('aria-label') || ''
          })).filter((x) => /view pdf|download pdf|access through|institution|main\\.pdf|pdfft|pdf/i.test(x.text + ' ' + x.href + ' ' + x.aria)).slice(0, 80)
        })
        """,
        msg_id=10,
    )
    pdf_url = asset_url or discover_elsevier_pdf_url(ws_url, page_info, target_pii=target_pii)
    pdf_probe = probe_pdf(ws_url, pdf_url) if pdf_url else {}
    state, action = classify_elsevier_state(page_info, pdf_probe, pdf_url, has_asset_tab=bool(asset_url))
    body = str(page_info.get("bodyText") or "")
    return {
        "doi": doi,
        "publisher": "Elsevier",
        "cdp_port": port,
        "state": state,
        "action": action,
        "article_url": article_url or page_info.get("href", ""),
        "pdf_url": pdf_url,
        "page": {
            "href": page_info.get("href", ""),
            "title": page_info.get("title", ""),
            "ready": page_info.get("ready", ""),
            "has_view_pdf": contains_link_text(page_info, "view pdf"),
            "has_access_through": "access through" in body.lower(),
            "is_abs_page": "/science/article/abs/pii/" in str(page_info.get("href", "")).lower(),
            "is_full_article_page": "/science/article/pii/" in str(page_info.get("href", "")).lower(),
            "is_pdf_asset_page": str(page_info.get("href", "")).startswith("https://pdf.sciencedirectassets.com/"),
            "has_matching_pdf_asset_tab": bool(asset_url),
        },
        "pdf_probe": pdf_probe,
    }


def discover_elsevier_pdf_url(ws_url: str, page_info: dict, target_pii: str = "") -> str:
    href = str(page_info.get("href") or "")
    if href.startswith("https://pdf.sciencedirectassets.com/") and matches_elsevier_pii(href, target_pii):
        return href
    for link in page_info.get("links") or []:
        url = str(link.get("href") or "")
        text = str(link.get("text") or "").lower()
        if url.startswith("https://pdf.sciencedirectassets.com/") and matches_elsevier_pii(url, target_pii):
            return url
        if "sciencedirect.com/science/article/pii/" in url and ("/pdfft" in url or "/pdf" in url) and matches_elsevier_pii(url, target_pii):
            return url
        if "view pdf" in text and url and matches_elsevier_pii(url, target_pii):
            return url

    # ScienceDirect often materializes a temporary pdf.sciencedirectassets.com URL
    # only after clicking the View PDF control.
    clicked = eval_json(
        ws_url,
        """
        (() => {
          const node = Array.from(document.querySelectorAll('a,button')).find((el) => {
            const text = ((el.innerText || el.textContent || '').replace(/\\s+/g, ' ')).trim().toLowerCase();
            return text.includes('view pdf') || text.includes('download pdf');
          });
          if (!node) return JSON.stringify({clicked:false});
          node.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
          return JSON.stringify({clicked:true, href: node.href || ''});
        })()
        """,
        msg_id=11,
    )
    candidate = str(clicked.get("href") or "")
    if candidate:
        return candidate
    return ""


def classify_elsevier_state(page_info: dict, pdf_probe: dict, pdf_url: str, *, has_asset_tab: bool = False) -> tuple[str, str]:
    href = str(page_info.get("href") or "").lower()
    body = str(page_info.get("bodyText") or "").lower()
    if is_robot_captcha_text(body):
        return "publisher_robot_captcha", "ScienceDirect is showing an anti-bot captcha. Complete the browser challenge manually, then rerun downloads."
    if is_elsevier_no_subscription_text(body):
        return "elsevier_no_subscription", "Peking University access is verified, but ScienceDirect says the institution does not subscribe to this content. Mark as entitlement gap and skip automatic retry."
    if pdf_probe.get("is_pdf") and str(pdf_probe.get("head", "")).startswith("%PDF"):
        return "elsevier_verified_pdf_access", "Elsevier verification is active. Rerun Elsevier downloads."
    if has_asset_tab or str(pdf_url).startswith("https://pdf.sciencedirectassets.com/"):
        return "elsevier_verified_pdf_asset_open", "ScienceDirect generated a temporary pdf.sciencedirectassets.com main.pdf tab. Save/download it now or rerun Elsevier while the token is fresh."
    if href.startswith("https://pdf.sciencedirectassets.com/"):
        return "elsevier_pdf_asset_open_but_probe_failed", "A temporary ScienceDirect PDF asset page is open, but probing failed. Save manually or refresh the PDF page."
    if "/science/article/abs/pii/" in href or "access through" in body:
        return "elsevier_needs_institution_verification", "Click Access through institution/Peking U and complete verification until the URL changes to /science/article/pii/ and View PDF appears."
    if contains_link_text(page_info, "view pdf") and not pdf_url:
        return "elsevier_view_pdf_needs_click", "ScienceDirect full article is available. Click View PDF once so a temporary pdf.sciencedirectassets.com URL is generated."
    if pdf_url and not pdf_probe.get("is_pdf"):
        return "elsevier_view_pdf_not_materialized", "ScienceDirect produced a PDF route but returned non-PDF content. Click View PDF in Chrome, then rerun."
    if "cloudflare" in body or "bad gateway" in body:
        return "elsevier_site_error", "ScienceDirect returned a site or Cloudflare error. Retry later."
    return "elsevier_unknown_session_state", "Could not determine Elsevier access state. Open View PDF manually and rerun this check."


def is_elsevier_no_subscription_text(body: str) -> bool:
    text = re.sub(r"\s+", " ", body or "").lower()
    markers = [
        "does not subscribe to this content on sciencedirect",
        "does not subscribe to this content",
        "not subscribe to this content",
        "your institution does not subscribe",
        "not entitled to access this content",
        "not have access to this content",
    ]
    return any(marker in text for marker in markers)


def is_robot_captcha_text(body: str) -> bool:
    text = re.sub(r"\s+", " ", body or "").lower()
    markers = [
        "are you a robot",
        "confirm you are a human",
        "captcha challenge",
        "complete the captcha",
        "reference number:",
        "user agent:",
        "ip address:",
    ]
    return any(marker in text for marker in markers) and ("captcha" in text or "robot" in text or "human" in text)


def contains_link_text(page_info: dict, needle: str) -> bool:
    target = needle.lower()
    for link in page_info.get("links") or []:
        if target in str(link.get("text") or "").lower():
            return True
    return False


def find_elsevier_page(port: int, article_url: str) -> dict | None:
    pages = list_pages(port)
    target_pii = extract_pii(article_url)
    if target_pii:
        for page in pages:
            if not isinstance(page, dict) or page.get("type") != "page":
                continue
            url = str(page.get("url") or "")
            if "sciencedirect.com" in url and target_pii.lower() in url.lower():
                return page
    for page in pages:
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        url = str(page.get("url") or "")
        if target_pii and not matches_elsevier_pii(url, target_pii):
            continue
        if "sciencedirect.com" in url or url.startswith("https://pdf.sciencedirectassets.com/"):
            return page
    return None


def find_elsevier_pdf_asset_url(port: int, target_pii: str) -> str:
    if not target_pii:
        return ""
    for page in list_pages(port):
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        url = str(page.get("url") or "")
        if url.startswith("https://pdf.sciencedirectassets.com/") and target_pii.lower() in url.lower():
            return url
    return ""


def extract_pii(value: str) -> str:
    text = value or ""
    for pattern in [r"/pii/([^/?#]+)", r"[?&]pii=([^&#]+)"]:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return ""


def matches_elsevier_pii(url: str, target_pii: str) -> bool:
    if not target_pii:
        return True
    return target_pii.lower() in (url or "").lower()


def probe_pdf(ws_url: str, pdf_url: str) -> dict[str, object]:
    js = """
    async (u) => {
      try {
        const r = await fetch(u, {credentials:'include'});
        const b = await r.arrayBuffer();
        const bytes = new Uint8Array(b.slice(0, 8));
        const head = Array.from(bytes).map(x => String.fromCharCode(x)).join('');
        return JSON.stringify({
          ok: true,
          status: r.status,
          content_type: r.headers.get('content-type') || '',
          final_url: r.url,
          size: b.byteLength,
          head,
          is_pdf: head.startsWith('%PDF')
        });
      } catch (e) {
        return JSON.stringify({ok: false, error: String(e)});
      }
    }
    """
    return eval_json(ws_url, f"({js})({json.dumps(pdf_url)})", msg_id=2)


def ensure_page_navigated(ws_url: str, target_url: str) -> None:
    try:
        current = eval_json(
            ws_url,
            "JSON.stringify({href: location.href, ready: document.readyState})",
            msg_id=90,
        )
        href = str(current.get("href") or "")
        if not href or href == "about:blank" or href.startswith("chrome-error://"):
            eval_json(
                ws_url,
                f"(() => {{ location.href = {json.dumps(target_url)}; return JSON.stringify({{navigated:true}}); }})()",
                msg_id=91,
            )
    except Exception:
        return

    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            current = eval_json(
                ws_url,
                "JSON.stringify({href: location.href, ready: document.readyState})",
                msg_id=92,
            )
            href = str(current.get("href") or "")
            ready = str(current.get("ready") or "").lower()
            if href and href != "about:blank" and ready in {"interactive", "complete"}:
                return
        except Exception:
            pass
        time.sleep(0.5)


def eval_json(ws_url: str, expression: str, msg_id: int) -> dict:
    ws = websocket.create_connection(ws_url, timeout=90, suppress_origin=True)
    try:
        ws.send(
            json.dumps(
                {
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
                }
            )
        )
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                raw = msg.get("result", {}).get("result", {}).get("value", "{}")
                return json.loads(raw or "{}")
    finally:
        ws.close()


def list_pages(port: int) -> list[dict]:
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, list) else []


def find_page(port: int, host: str, doi: str) -> dict | None:
    for page in list_pages(port):
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        url = str(page.get("url") or "")
        if host in url and doi in url:
            return page
    return None


def open_cdp_page(port: int, url: str) -> dict:
    req = Request(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=%')}", method="PUT")
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.lower()


def infer_publisher(doi: str) -> str:
    if doi.lower().startswith("10.1021/"):
        return "ACS"
    if doi.lower().startswith("10.1016/"):
        return "Elsevier"
    return "UNKNOWN"


if __name__ == "__main__":
    raise SystemExit(main())
