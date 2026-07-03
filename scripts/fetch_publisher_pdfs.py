import argparse
import base64
import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

import websocket


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")


def choose_open_url(doi: str, source_url: str) -> str:
    url = (source_url or "").strip()
    trusted_hosts = [
        "pmc.ncbi.nlm.nih.gov",
        "pubs.rsc.org",
        "pubs.acs.org",
        "nature.com",
        "mdpi.com",
        "onlinelibrary.wiley.com",
        "sciencedirect.com",
        "academic.oup.com",
        "osti.gov",
    ]
    if any(host in url for host in trusted_hosts):
        return url
    return f"https://doi.org/{doi}"


def is_elsevier_row(doi: str, source_url: str) -> bool:
    lower_url = (source_url or "").lower()
    return doi.lower().startswith("10.1016/") or "sciencedirect.com" in lower_url or "linkinghub.elsevier.com" in lower_url


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


def summarize_robot_captcha(body: str) -> str:
    text = re.sub(r"\s+", " ", body or "").strip()
    parts = []
    for label in ["Reference number", "IP Address", "Timestamp"]:
        match = re.search(rf"{re.escape(label)}:\s*([^|]+?)(?=\s+(?:Reference number|IP Address|User Agent|Timestamp):|$)", text, flags=re.I)
        if match:
            parts.append(f"{label}={match.group(1).strip()[:120]}")
    return "; ".join(parts) or text[:300]


def extract_elsevier_pii(value: str) -> str:
    text = value or ""
    patterns = [
        r"/(?:science/(?:article|chapter/bookseries)/(?:abs/)?pii|retrieve/pii)/([^/?#]+)",
        r"[?&]pii=([^&#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return ""


def matches_elsevier_pii(url: str, target_pii: str) -> bool:
    if not target_pii:
        return True
    return target_pii.lower() in (url or "").lower()


def is_elsevier_pdf_candidate_url(href: str, text: str, target_pii: str = "") -> bool:
    href_lower = (href or "").lower()
    text_lower = (text or "").lower()
    if "sciencedirect.com" not in href_lower and "sciencedirectassets.com" not in href_lower:
        return False
    if target_pii and not matches_elsevier_pii(href, target_pii):
        return False
    return (
        "view pdf" in text_lower
        or "download pdf" in text_lower
        or "main.pdf" in href_lower
        or "/pdfft?" in href_lower
        or "/pdf?" in href_lower
        or href_lower.endswith("/pdf")
    )


def open_cdp_page(port: int, url: str) -> dict:
    req = Request(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=%')}", method="PUT")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_cdp_pages(port: int) -> list[dict]:
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, list) else []


def close_cdp_page(port: int, page_id: str) -> None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/close/{page_id}", timeout=10):
            pass
    except Exception:
        pass


def eval_js(ws_url: str, expression: str, msg_id: int = 1):
    ws = websocket.create_connection(ws_url, timeout=90, suppress_origin=True)
    try:
        ws.send(
            json.dumps(
                {
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                        "userGesture": True,
                    },
                }
            )
        )
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                return msg.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()


def fetch_in_page(ws_url: str, url: str, msg_id: int = 2) -> dict:
    js = """
    async (u) => {
      try {
        const r = await fetch(u, {credentials:'include'});
        const b = await r.arrayBuffer();
        const bytes = new Uint8Array(b);
        const isPdf = bytes.length >= 5 &&
          bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 &&
          bytes[3] === 0x46 && bytes[4] === 0x2d;
        let textSnippet = '';
        let b64 = '';
        if (isPdf) {
          const chunk = 0x8000;
          let s = '';
          for (let i = 0; i < bytes.length; i += chunk) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
          }
          b64 = btoa(s);
        } else {
          try {
            textSnippet = new TextDecoder('utf-8', {fatal:false}).decode(bytes.slice(0, 4000));
          } catch (e) {
            textSnippet = '';
          }
        }
        return JSON.stringify({
          ok: true,
          status: r.status,
          ct: r.headers.get('content-type') || '',
          finalUrl: r.url,
          isPdf,
          textSnippet: textSnippet.slice(0, 20000),
          b64,
        });
      } catch (e) {
        return JSON.stringify({ok: false, error: String(e)});
      }
    }
    """
    raw = eval_js(ws_url, f"({js})({json.dumps(url)})", msg_id)
    return json.loads(raw or "{}")


def wait_for_page_context(ws_url: str, timeout_seconds: float, msg_id: int = 50) -> dict:
    deadline = time.time() + max(1.0, timeout_seconds)
    last = {}
    while time.time() < deadline:
        try:
            raw = eval_js(
                ws_url,
                "JSON.stringify({href:location.href,ready:document.readyState,title:document.title})",
                msg_id=msg_id,
            )
            info = json.loads(raw or "{}")
            last = info
            href = (info.get("href") or "").strip()
            ready = (info.get("ready") or "").strip().lower()
            if href and href not in {"about:blank", "chrome-error://chromewebdata/"} and ready in {"interactive", "complete"}:
                return info
        except Exception:
            pass
        time.sleep(0.5)
    return last


def wait_for_elsevier_ready(ws_url: str, timeout_seconds: float, msg_id: int = 150) -> dict:
    deadline = time.time() + max(3.0, timeout_seconds)
    last = {}
    while time.time() < deadline:
        try:
            raw = eval_js(
                ws_url,
                """JSON.stringify({
                    href: location.href,
                    ready: document.readyState,
                    title: document.title,
                    hasViewPdf: Array.from(document.querySelectorAll('a')).some(a => {
                      const text = (a.innerText || a.textContent || '').trim().toLowerCase();
                      const href = a.href || '';
                      return (text.includes('view pdf') || href.includes('main.pdf') || href.includes('/pdfft?md5=')) && href.includes('sciencedirect.com');
                    }),
                    bodyText: (document.body ? (document.body.innerText || '') : '').slice(0, 6000),
                    hasFullTextAccess: /full text access/i.test(document.body ? (document.body.innerText || '') : ''),
                    hasViewFullText: Array.from(document.querySelectorAll('a')).some(a => {
                      const text = (a.innerText || a.textContent || '').trim().toLowerCase();
                      return text.includes('view full text') && !!a.href;
                    })
                })""",
                msg_id=msg_id,
            )
            info = json.loads(raw or "{}")
            last = info
            href = (info.get("href") or "").strip()
            ready = (info.get("ready") or "").strip().lower()
            if href and ready == "complete" and is_robot_captcha_text(str(info.get("bodyText") or "")):
                return info
            if href and ready == "complete" and is_elsevier_no_subscription_text(str(info.get("bodyText") or "")):
                return info
            if href and ready == "complete" and (info.get("hasViewPdf") or info.get("hasFullTextAccess")):
                return info
            if href and ready == "complete" and info.get("hasViewFullText"):
                return info
        except Exception:
            pass
        time.sleep(0.5)
    return last


def discover_elsevier_pdf_candidates(
    cdp_port: int,
    ws_url: str,
    timeout_seconds: float,
    msg_id: int = 250,
    target_pii: str = "",
) -> list[str]:
    before_pages = {
        str(page.get("id") or ""): str(page.get("url") or "")
        for page in list_cdp_pages(cdp_port)
        if isinstance(page, dict) and page.get("type") == "page"
    }
    try:
        eval_js(
            ws_url,
            """(() => {
                const el = Array.from(document.querySelectorAll('a,button')).find(node => {
                  const text = ((node.innerText || node.textContent || '').replace(/\\s+/g, ' ')).trim().toLowerCase();
                  const href = node.href || '';
                  return text.includes('view pdf') || text.includes('download pdf') || href.includes('/pdfft?') || href.includes('/pdf?');
                });
                if (el) {
                  const href = el.href || '';
                  el.scrollIntoView({block:'center', inline:'center'});
                  el.click();
                  el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, cancelable:true, view:window}));
                  el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                  el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                  el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
                  if (href) setTimeout(() => { try { window.open(href, '_blank'); } catch (e) {} }, 250);
                  return JSON.stringify({clicked:true, tag:el.tagName, href});
                }
                return JSON.stringify({clicked:false});
            })()""",
            msg_id=msg_id,
        )
    except Exception:
        return []

    discovered: list[str] = []
    deadline = time.time() + max(5.0, timeout_seconds)
    while time.time() < deadline:
        try:
            current_href = str(eval_js(ws_url, "location.href", msg_id=msg_id + 1) or "").strip()
            if current_href.startswith("https://pdf.sciencedirectassets.com/") and matches_elsevier_pii(current_href, target_pii):
                discovered.append(current_href)
                break
            if (
                "sciencedirect.com/science/article/pii/" in current_href
                and ("/pdfft?" in current_href or "/pdf?" in current_href)
                and matches_elsevier_pii(current_href, target_pii)
            ):
                discovered.append(current_href)
        except Exception:
            pass

        try:
            for page in list_cdp_pages(cdp_port):
                if not isinstance(page, dict) or page.get("type") != "page":
                    continue
                page_id = str(page.get("id") or "")
                page_url = str(page.get("url") or "").strip()
                if not page_url:
                    continue
                if page_url.startswith("https://pdf.sciencedirectassets.com/") and matches_elsevier_pii(page_url, target_pii):
                    discovered.append(page_url)
                if "sciencedirect.com/science/article/pii/" in page_url and (
                    "/pdfft?" in page_url
                    or "/pdf?" in page_url
                    or page_url.endswith("/pdf")
                ) and matches_elsevier_pii(page_url, target_pii):
                    discovered.append(page_url)
                if page_id not in before_pages or before_pages.get(page_id) != page_url:
                    continue
        except Exception:
            pass
        if discovered:
            return list(dict.fromkeys(discovered))
        time.sleep(0.5)
    return list(dict.fromkeys(discovered))


def fetch_json(url: str, params: dict[str, str] | None = None, timeout: int = 20) -> dict:
    if params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def cookies_for_domain(ws_url: str, domain_part: str, msg_id: int = 20) -> str:
    ws = websocket.create_connection(ws_url, timeout=90, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": msg_id, "method": "Network.getAllCookies", "params": {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                cookies = msg.get("result", {}).get("cookies", [])
                return "; ".join(
                    f"{cookie['name']}={cookie['value']}"
                    for cookie in cookies
                    if domain_part in cookie.get("domain", "")
                )
    finally:
        ws.close()


def open_access_candidates(doi: str) -> list[str]:
    doi = (doi or "").strip()
    if not doi:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        candidate = str(value or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    try:
        openalex = fetch_json(f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}")
    except Exception:
        openalex = {}
    if openalex:
        open_access = openalex.get("open_access")
        if isinstance(open_access, dict):
            add(open_access.get("oa_url", ""))
        for location_key in ("best_oa_location", "primary_location"):
            location = openalex.get(location_key)
            if not isinstance(location, dict):
                continue
            if location_key == "primary_location" and not location.get("is_oa"):
                continue
            add(location.get("pdf_url", ""))
            add(location.get("landing_page_url", ""))
        locations = openalex.get("locations")
        if isinstance(locations, list):
            for location in locations:
                if not isinstance(location, dict) or not location.get("is_oa"):
                    continue
                add(location.get("pdf_url", ""))
                add(location.get("landing_page_url", ""))

    email = os.getenv("UNPAYWALL_EMAIL", "").strip()
    if email:
        try:
            unpaywall = fetch_json(
                f"https://api.unpaywall.org/v2/{quote(doi, safe='')}",
                params={"email": email},
            )
        except Exception:
            unpaywall = {}
        if unpaywall:
            best_location = unpaywall.get("best_oa_location")
            if isinstance(best_location, dict):
                add(best_location.get("url_for_pdf", ""))
                add(best_location.get("url", ""))
            oa_locations = unpaywall.get("oa_locations")
            if isinstance(oa_locations, list):
                for location in oa_locations:
                    if not isinstance(location, dict):
                        continue
                    add(location.get("url_for_pdf", ""))
                    add(location.get("url", ""))

    return candidates


def request_binary(url: str, referer: str = "", cookie: str = b"") -> tuple[bytes, str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148 Safari/537.36",
        "Accept": "application/pdf,text/html,*/*",
        "Referer": referer or url,
    }
    if cookie:
        headers["Cookie"] = cookie
    req = Request(url, headers=headers)
    with urlopen(req, timeout=120) as resp:
        return resp.read(), resp.headers.get("content-type", ""), resp.geturl()


def domain_cookie_fragment(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    for prefix in ("www.", "api.", "link."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def write_outputs(results_csv: Path, mapping_csv: Path, result_rows: list[dict], map_rows: list[dict]) -> None:
    with results_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
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

    with mapping_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["doi", "title", "publisher", "pdf_filename", "pdf_path"],
        )
        writer.writeheader()
        writer.writerows(map_rows)


def build_rsc_candidates(url: str) -> list[str]:
    match = re.search(r"pubs\.rsc\.org/([^/]+)/content/articlelanding/(\d{4})/([^/]+)/([a-z0-9]+)", url, re.I)
    if not match:
        return []
    locale, year, journal, code = match.groups()
    return [
        f"https://pubs.rsc.org/{locale}/content/articlepdf/{year}/{journal}/{code}",
        f"https://pubs.rsc.org/en/content/articlepdf/{year}/{journal}/{code}",
        f"https://pubs.rsc.org/{locale}/content/articlepdf/{year}/{journal}/{code}?page=search",
        f"https://pubs.rsc.org/en/content/articlepdf/{year}/{journal}/{code}?page=search",
    ]


def build_oup_candidates(url: str, doi: str) -> list[str]:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if "academic.oup.com" not in host or "/article/" not in path:
        return []

    doi_tail = doi.split("/", 1)[1] if "/" in doi else doi
    doi_tail = doi_tail.strip()
    if not doi_tail:
        return []

    article_pdf_path = path.replace("/article/", "/article-pdf/", 1).rstrip("/")
    return [
        f"https://{host}{article_pdf_path}/{doi_tail}.pdf",
        f"https://{host}{article_pdf_path}/{doi_tail}.pdf?download=true",
    ]


def href_matches_doi(href: str, doi: str) -> bool:
    href_lower = (href or "").lower()
    doi_lower = (doi or "").lower()
    encoded = quote(doi_lower, safe="").lower()
    encoded_slash = doi_lower.replace("/", "%2f")
    return doi_lower in href_lower or encoded in href_lower or encoded_slash in href_lower


def is_wiley_host(value: str) -> bool:
    host = (urlparse(value or "").netloc or "").lower()
    return host.endswith("onlinelibrary.wiley.com")


def is_wiley_non_article_pdf(href: str) -> bool:
    href_lower = (href or "").lower()
    blocked = [
        "/action/downloadsupplement",
        "/doi/suppl/",
        "suppl_file",
        "suppmat",
        "pb-assets",
        "wechat",
    ]
    return any(marker in href_lower for marker in blocked)


def build_wiley_candidates(landing_url: str, source_url: str, links: list[dict], doi: str) -> list[str]:
    if not doi.startswith("10.1002/"):
        return []

    hosts: list[str] = []
    for value in [landing_url, source_url]:
        parsed = urlparse(value or "")
        host = (parsed.netloc or "").lower()
        if host.endswith("onlinelibrary.wiley.com") and host not in hosts:
            hosts.append(host)
    if "onlinelibrary.wiley.com" not in hosts:
        hosts.append("onlinelibrary.wiley.com")

    candidates: list[str] = []
    for host in hosts:
        # Wiley's ePDF and /doi/pdf routes are often HTML readers; pdfdirect is
        # the canonical main-article PDF when institutional access is active.
        candidates.extend(
            [
                f"https://{host}/doi/pdfdirect/{doi}",
                f"https://{host}/doi/pdf/{doi}",
                f"https://{host}/doi/epdf/{doi}",
            ]
        )

    exact_links: list[str] = []
    for link in links:
        href = (link.get("href") or "").replace("&amp;", "&")
        if not href.startswith("http") or not is_wiley_host(href):
            continue
        if is_wiley_non_article_pdf(href) or not href_matches_doi(href, doi):
            continue
        if "/doi/pdfdirect/" in href.lower():
            exact_links.insert(0, href)
        elif "/doi/pdf/" in href.lower() or "/doi/epdf/" in href.lower():
            exact_links.append(href)
    return list(dict.fromkeys(candidates + exact_links))


def classify_candidates(
    landing_url: str,
    source_url: str,
    links: list[dict],
    doi: str,
    meta_pdf_urls: list[str] | None = None,
) -> list[str]:
    candidates: list[str] = []
    if doi.startswith("10.1002/"):
        candidates.extend(build_wiley_candidates(landing_url, source_url, links, doi))
    if is_elsevier_row(doi, source_url) or is_elsevier_row(doi, landing_url):
        pii = extract_elsevier_pii(landing_url) or extract_elsevier_pii(source_url)
        for link in links:
            href = (link.get("href") or "").replace("&amp;", "&")
            text = (link.get("text") or "").strip().lower()
            if not href.startswith("http"):
                continue
            if is_elsevier_pdf_candidate_url(href, text, pii):
                candidates.append(href)
    for url in meta_pdf_urls or []:
        if url:
            candidates.append(url)
    candidates.extend(open_access_candidates(doi))
    if "pubs.rsc.org" in landing_url or "pubs.rsc.org" in source_url:
        candidates.extend(build_rsc_candidates(landing_url))
        candidates.extend(build_rsc_candidates(source_url))
    if "academic.oup.com" in landing_url or "academic.oup.com" in source_url:
        candidates.extend(build_oup_candidates(landing_url, doi))
        candidates.extend(build_oup_candidates(source_url, doi))
    if "nature.com" in landing_url:
        match = re.search(r"/articles/([^/?#]+)", landing_url)
        if match:
            candidates.append(f"https://www.nature.com/articles/{match.group(1)}.pdf")
    if (
        "sciencedirect.com" in landing_url
        or "sciencedirect.com" in source_url
        or "linkinghub.elsevier.com" in landing_url
        or "linkinghub.elsevier.com" in source_url
    ):
        pii_source = landing_url if ("sciencedirect.com" in landing_url or "linkinghub.elsevier.com" in landing_url) else source_url
        pii = extract_elsevier_pii(pii_source)
        if pii:
            candidates.extend(
                [
                    f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true",
                    f"https://www.sciencedirect.com/science/article/pii/{pii}/pdf?isDTMRedir=true&download=true",
                    f"https://www.sciencedirect.com/science/article/pii/{pii}/pdf",
                ]
            )
    if doi.startswith("10.1021/"):
        candidates.extend(
            [
                f"https://pubs.acs.org/doi/pdf/{doi}?ref=article_openPDF",
                f"https://pubs.acs.org/doi/pdf/{doi}",
            ]
        )
    if "pmc.ncbi.nlm.nih.gov" in landing_url or "pmc.ncbi.nlm.nih.gov" in source_url:
        for link in links:
            href = (link.get("href") or "").replace("&amp;", "&")
            if "/pdf/" in href.lower():
                candidates.append(href)
    for link in links:
        href = (link.get("href") or "").replace("&amp;", "&")
        text = (link.get("text") or "").lower()
        if not href.startswith("http"):
            continue
        if doi.startswith("10.1002/") and (
            is_wiley_non_article_pdf(href)
            or (is_wiley_host(href) and not href_matches_doi(href, doi))
        ):
            continue
        if (
            ".pdf" in href.lower()
            or "/pdf" in href.lower()
            or "/epdf" in href.lower()
            or "articlepdf" in href.lower()
            or "open pdf" in text
            or text == "pdf"
        ):
            if is_elsevier_row(doi, source_url) or is_elsevier_row(doi, landing_url):
                pii = extract_elsevier_pii(landing_url) or extract_elsevier_pii(source_url)
                if ("sciencedirect.com" in href.lower() or "sciencedirectassets.com" in href.lower()) and not is_elsevier_pdf_candidate_url(href, text, pii):
                    continue
            candidates.append(href)
    if doi.startswith("10.3390/"):
        base_mdpi = landing_url if "mdpi.com" in landing_url else source_url
        if "mdpi.com" in base_mdpi:
            candidates.extend(
                [
                    f"{base_mdpi.rstrip('/')}/pdf",
                    f"{base_mdpi.rstrip('/')}/pdf?download=1",
                ]
            )
        tail = doi.split("/", 1)[1]
        journal_match = re.match(r"([a-z]+)(\d+)$", tail)
        if journal_match:
            journal_code = journal_match.group(1)
            digits = journal_match.group(2)
            article_number = int(digits[-4:])
            volume = digits[:-4] or "0"
            stem = f"{journal_code}-{int(volume):02d}-{article_number:05d}"
            candidates.extend(
                [
                    f"https://mdpi-res.com/d_attachment/{journal_code}/{stem}/article_deploy/{stem}.pdf",
                    f"https://mdpi-res.com/d_attachment/{journal_code}/{stem}/article_deploy/{stem}-v2.pdf",
                ]
            )
    return list(dict.fromkeys(candidates))


def process_row(
    i: int,
    row: dict[str, str],
    cdp_port: int,
    pdf_dir: Path,
    page_settle_seconds: float,
) -> tuple[dict, dict | None]:
    doi = (row.get("doi") or "").strip()
    title = (row.get("title") or "").strip()
    publisher = (row.get("publisher") or "").strip()
    url = (row.get("url") or f"https://doi.org/{doi}").strip()
    page = None
    try:
        open_url = choose_open_url(doi, url)
        page = open_cdp_page(cdp_port, open_url)
        ws_url = page["webSocketDebuggerUrl"]
        time.sleep(max(0.0, min(1.5, page_settle_seconds)))
        wait_for_page_context(ws_url, page_settle_seconds, msg_id=50 + i)
        elsevier_row = is_elsevier_row(doi, url) or is_elsevier_row(doi, open_url)
        if elsevier_row:
            wait_for_elsevier_ready(ws_url, max(page_settle_seconds, 20.0), msg_id=70 + i)
            redirected_href = eval_js(ws_url, "location.href", msg_id=80 + i) or ""
            if "/abs/pii/" in str(redirected_href) and "sciencedirect.com" in str(redirected_href).lower():
                view_full_text = eval_js(
                    ws_url,
                    """(() => {
                        const a = Array.from(document.querySelectorAll('a')).find(x => {
                          const text = (x.innerText || x.textContent || '').trim().toLowerCase();
                          return text.includes('view full text') && !!x.href;
                        });
                        return a ? a.href : '';
                    })()""",
                    msg_id=90 + i,
                ) or ""
                if str(view_full_text).startswith("http"):
                    eval_js(ws_url, f"location.href = {json.dumps(str(view_full_text))}", msg_id=95 + i)
                    wait_for_elsevier_ready(ws_url, max(page_settle_seconds, 20.0), msg_id=96 + i)
        info = json.loads(
            eval_js(
                ws_url,
                "JSON.stringify({href:location.href,title:document.title,bodyText:(document.body&&document.body.innerText||'').slice(0,6000),links:Array.from(document.querySelectorAll('a')).map(a=>({text:((a.innerText||a.textContent||'').replace(/\\s+/g,' ')).trim(),href:a.href})).slice(0,400),metaPdfUrls:Array.from(document.querySelectorAll('meta[name=\"citation_pdf_url\"],meta[property=\"citation_pdf_url\"],link[rel=\"alternate\"][type=\"application/pdf\"]')).map(n=>n.content||n.href||'').filter(Boolean)})",
                msg_id=100 + i,
            )
            or "{}"
        )
        landing_url = info.get("href", "")
        if is_robot_captcha_text(str(info.get("bodyText") or "")):
            return (
                {
                    "idx": i,
                    "doi": doi,
                    "title": title,
                    "publisher": publisher or ("Elsevier" if elsevier_row else ""),
                    "status": "failed",
                    "pdf_filename": "",
                    "pdf_path": "",
                    "size_bytes": 0,
                    "detail": f"publisher_robot_captcha; landing={landing_url}; {summarize_robot_captcha(str(info.get('bodyText') or ''))}",
                    "url": url,
                },
                None,
            )
        if elsevier_row and is_elsevier_no_subscription_text(str(info.get("bodyText") or "")):
            return (
                {
                    "idx": i,
                    "doi": doi,
                    "title": title,
                    "publisher": publisher or "Elsevier",
                    "status": "failed",
                    "pdf_filename": "",
                    "pdf_path": "",
                    "size_bytes": 0,
                    "detail": f"elsevier_no_subscription; landing={landing_url}; title={info.get('title', '')}",
                    "url": url,
                },
                None,
            )
        links = info.get("links", [])
        meta_pdf_urls = info.get("metaPdfUrls", [])
        dynamic_candidates: list[str] = []
        if elsevier_row:
            target_pii = extract_elsevier_pii(str(landing_url)) or extract_elsevier_pii(url) or extract_elsevier_pii(open_url)
            dynamic_candidates = discover_elsevier_pdf_candidates(
                cdp_port=cdp_port,
                ws_url=ws_url,
                timeout_seconds=max(page_settle_seconds, 12.0),
                msg_id=1200 + i,
                target_pii=target_pii,
            )
        candidates = list(
            dict.fromkeys(
                dynamic_candidates
                + classify_candidates(landing_url, url, links, doi, meta_pdf_urls=meta_pdf_urls)
            )
        )

        details = []
        for candidate in candidates:
            try:
                raw = b""
                ct = ""
                final = ""
                if candidate.startswith("http"):
                    res = fetch_in_page(ws_url, candidate, msg_id=500 + i)
                    if res.get("isPdf") and res.get("b64"):
                        raw = base64.b64decode(res["b64"])
                        ct = res.get("ct", "")
                        final = res.get("finalUrl", candidate)
                    else:
                        if elsevier_row:
                            snippet = str(res.get("textSnippet") or "")
                            if is_robot_captcha_text(snippet):
                                return (
                                    {
                                        "idx": i,
                                        "doi": doi,
                                        "title": title,
                                        "publisher": publisher or "Elsevier",
                                        "status": "failed",
                                        "pdf_filename": "",
                                        "pdf_path": "",
                                        "size_bytes": 0,
                                        "detail": f"publisher_robot_captcha; landing={res.get('finalUrl', candidate)}; {summarize_robot_captcha(snippet)}",
                                        "url": url,
                                    },
                                    None,
                                )
                            if is_elsevier_no_subscription_text(snippet):
                                return (
                                    {
                                        "idx": i,
                                        "doi": doi,
                                        "title": title,
                                        "publisher": publisher or "Elsevier",
                                        "status": "failed",
                                        "pdf_filename": "",
                                        "pdf_path": "",
                                        "size_bytes": 0,
                                        "detail": f"elsevier_no_subscription; landing={res.get('finalUrl', candidate)}",
                                        "url": url,
                                    },
                                    None,
                                )
                        details.append(json.dumps(res, ensure_ascii=False)[:300])
                        if doi.startswith("10.1021/"):
                            cookie = cookies_for_domain(ws_url, "acs.org", msg_id=300 + i)
                            raw, ct, final = request_binary(candidate, referer=landing_url, cookie=cookie)
                        elif doi.startswith("10.1002/"):
                            cookie = cookies_for_domain(ws_url, "wiley.com", msg_id=400 + i)
                            raw, ct, final = request_binary(
                                candidate,
                                referer=f"https://onlinelibrary.wiley.com/doi/{doi}",
                                cookie=cookie,
                            )
                        elif "sciencedirect.com" in candidate or "sciencedirectassets.com" in candidate:
                            cookie = cookies_for_domain(ws_url, "sciencedirect.com", msg_id=470 + i)
                            raw, ct, final = request_binary(candidate, referer=landing_url, cookie=cookie)
                        elif doi.startswith("10.3390/"):
                            cookie = cookies_for_domain(ws_url, "mdpi.com", msg_id=450 + i)
                            raw, ct, final = request_binary(candidate, referer=landing_url, cookie=cookie)
                        else:
                            cookie = cookies_for_domain(ws_url, domain_cookie_fragment(candidate), msg_id=600 + i)
                            raw, ct, final = request_binary(candidate, referer=landing_url or url, cookie=cookie)
                else:
                    details.append(f"unsupported candidate {candidate}")
                    continue

                if raw[:5] == b"%PDF-":
                    filename = f"{i}_{safe_name(doi)}.pdf"
                    pdf_path = pdf_dir / filename
                    pdf_path.write_bytes(raw)
                    return (
                        {
                            "idx": i,
                            "doi": doi,
                            "title": title,
                            "publisher": publisher,
                            "status": "downloaded",
                            "pdf_filename": filename,
                            "pdf_path": str(pdf_path),
                            "size_bytes": len(raw),
                            "detail": f"{ct}; final={final}",
                            "url": url,
                        },
                        {
                            "doi": doi,
                            "title": title,
                            "publisher": publisher,
                            "pdf_filename": filename,
                            "pdf_path": str(pdf_path),
                        },
                    )
                details.append(f"nonpdf {candidate}")
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                details.append(f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                details.append(f"{type(exc).__name__}: {exc}")

        if not candidates:
            details.append(f"no candidates found; landing={landing_url}")
        return (
            {
                "idx": i,
                "doi": doi,
                "title": title,
                "publisher": publisher,
                "status": "failed",
                "pdf_filename": "",
                "pdf_path": "",
                "size_bytes": 0,
                "detail": " | ".join(details)[:1000],
                "url": url,
            },
            None,
        )
    except Exception as exc:
        return (
            {
                "idx": i,
                "doi": doi,
                "title": title,
                "publisher": publisher,
                "status": "failed",
                "pdf_filename": "",
                "pdf_path": "",
                "size_bytes": 0,
                "detail": f"row_exception {type(exc).__name__}: {exc}"[:1000],
                "url": url,
            },
            None,
        )
    finally:
        if page:
            close_cdp_page(cdp_port, page["id"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cdp-port", type=int, default=9231)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--page-settle-seconds", type=float, default=6.0)
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    output_dir = Path(args.output_dir)
    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    results_csv = output_dir / "download_results.csv"
    mapping_csv = output_dir / "downloaded_doi_filename_map.csv"

    rows = list(csv.DictReader(input_csv.open("r", encoding="utf-8-sig", newline="")))
    result_by_idx: dict[int, dict] = {}
    map_by_doi: dict[str, dict] = {}
    total = len(rows)

    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        future_map = {
            executor.submit(process_row, i, row, args.cdp_port, pdf_dir, args.page_settle_seconds): i
            for i, row in enumerate(rows, start=1)
        }
        completed = 0
        for future in as_completed(future_map):
            completed += 1
            result_row, map_row = future.result()
            result_by_idx[int(result_row["idx"])] = result_row
            if map_row:
                map_by_doi[map_row["doi"]] = map_row
            print(
                f"[{completed}/{total}] {result_row['status']} {result_row['doi']}",
                flush=True,
            )
            write_outputs(
                results_csv,
                mapping_csv,
                [result_by_idx[idx] for idx in sorted(result_by_idx)],
                [map_by_doi[doi] for doi in sorted(map_by_doi)],
            )


if __name__ == "__main__":
    main()
