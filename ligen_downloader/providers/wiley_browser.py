from __future__ import annotations

import asyncio
import json
from logging import Logger

from playwright.async_api import async_playwright

from ..models import DownloadResult
from ..models import DownloadRow
from ..models import RunConfig
from ..utils import safe_name
from .base import DownloadProvider


async def close_page_quietly(page) -> None:
    if not page:
        return
    try:
        await page.close()
    except Exception:
        pass


async def fetch_wiley_main_pdf(row: DownloadRow, config: RunConfig) -> dict:
    article_page = None
    pdf_page = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{config.cdp_port}")
            if not browser.contexts:
                raise RuntimeError("No browser contexts found in connected Chrome session")
            ctx = browser.contexts[0]

            article_page = await ctx.new_page()
            await article_page.goto(row.url, wait_until="domcontentloaded", timeout=120000)
            await article_page.wait_for_timeout(int(config.page_wait_seconds * 1000))

            article_url = article_page.url or ""
            article_title = await article_page.title()
            body_text = await article_page.evaluate("() => document.body ? document.body.innerText.slice(0, 6000) : ''")

            pdf_links = await article_page.eval_on_selector_all(
                "a[href]",
                """els => els
                    .map(e => ({text:(e.innerText||'').trim(), href:e.href}))
                    .filter(x => /\\/doi\\/(pdf|epdf)\\//i.test(x.href))
                """,
            )
            if not pdf_links:
                raise RuntimeError("No Wiley main-PDF link found on article page")

            pdf_candidates = []
            for item in pdf_links:
                href = str(item.get("href") or "").strip()
                if href and href not in pdf_candidates:
                    pdf_candidates.append(href)

            pdf_page = await ctx.new_page()
            for candidate in pdf_candidates:
                pdf_response_holder = {"response": None}

                async def maybe_capture(resp):
                    ctype = (resp.headers.get("content-type") or "").lower()
                    url = resp.url or ""
                    if resp.status == 200 and "application/pdf" in ctype and "/doi/pdf/" in url.lower():
                        pdf_response_holder["response"] = resp

                pdf_page.on("response", lambda resp: asyncio.create_task(maybe_capture(resp)))
                await pdf_page.goto(candidate, wait_until="domcontentloaded", timeout=120000)
                await pdf_page.wait_for_timeout(8000)

                resp = pdf_response_holder["response"]
                if resp is not None:
                    raw = await resp.body()
                    if raw.startswith(b"%PDF-"):
                        result = {
                            "article_title": article_title,
                            "article_url": article_url,
                            "pdf_url": resp.url,
                            "pdf_bytes": raw,
                            "strategy": "browser_response_capture",
                            "candidate_url": candidate,
                        }
                        await close_page_quietly(pdf_page)
                        pdf_page = None
                        await close_page_quietly(article_page)
                        article_page = None
                        return result

                final_url = pdf_page.url or ""
                if "/doi/abs/" in final_url.lower():
                    continue

            result = {
                "article_title": article_title,
                "article_url": article_url,
                "pdf_url": "",
                "pdf_bytes": b"",
                "strategy": "no_entitled_main_pdf",
                "candidate_urls": pdf_candidates,
                "article_body_excerpt": body_text[:1200],
            }
            await close_page_quietly(pdf_page)
            pdf_page = None
            await close_page_quietly(article_page)
            article_page = None
            return result
    finally:
        await close_page_quietly(pdf_page)
        await close_page_quietly(article_page)


class WileyBrowserProvider(DownloadProvider):
    provider_name = "wiley_browser"

    def can_handle(self, row: DownloadRow) -> bool:
        publisher = row.publisher.lower()
        return "wiley" in publisher or "onlinelibrary.wiley.com" in row.url.lower() or row.doi.lower().startswith("10.1002/")

    def download_one(self, row: DownloadRow, config: RunConfig, logger: Logger) -> DownloadResult:
        pdf_dir = config.output_dir / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)

        result = DownloadResult(
            idx=row.idx,
            doi=row.doi,
            title=row.title,
            publisher=row.publisher or "Wiley",
            status="started",
            source_url=row.url,
        )

        try:
            info = asyncio.run(fetch_wiley_main_pdf(row, config))
            pdf_bytes = info.pop("pdf_bytes", b"")
            pdf_url = str(info.get("pdf_url") or "")
            result.final_pdf_url = pdf_url
            if pdf_bytes:
                pdf_filename = f"{row.idx}_{safe_name(row.doi)}.pdf"
                final_path = pdf_dir / pdf_filename
                final_path.write_bytes(pdf_bytes)
                result.status = "downloaded_main_pdf_via_browser"
                result.pdf_filename = pdf_filename
                result.pdf_path = str(final_path)
                result.size_bytes = final_path.stat().st_size
                result.detail = json.dumps(info, ensure_ascii=False)
                logger.info(f"[OK] {row.doi} -> {final_path}")
                return result

            result.status = "main_pdf_not_available_in_current_session"
            result.detail = json.dumps(info, ensure_ascii=False)
            logger.info(f"[FAIL] {row.doi} -> {result.status}")
            return result
        except Exception as exc:
            result.status = "wiley_provider_exception"
            result.detail = f"{type(exc).__name__}: {exc}"
            logger.info(f"[FAIL] {row.doi} -> {result.detail}")
            return result
