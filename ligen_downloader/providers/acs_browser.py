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


async def fetch_acs_main_pdf(row: DownloadRow, config: RunConfig) -> dict:
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
            article_body = await article_page.evaluate("() => document.body ? document.body.innerText.slice(0, 6000) : ''")

            pdf_url = f"https://pubs.acs.org/doi/pdf/{row.doi}"
            pdf_page = await ctx.new_page()
            pdf_hits: list[dict] = []
            pdf_future: asyncio.Future[tuple[bytes, str]] = asyncio.get_running_loop().create_future()
            captured_pdf: dict[str, object] = {"bytes": b"", "url": ""}

            async def maybe_capture(resp):
                ctype = (resp.headers.get("content-type") or "").lower()
                if resp.status != 200 or "application/pdf" not in ctype:
                    return
                raw = await resp.body()
                hit = {
                    "url": resp.url,
                    "content_type": ctype,
                    "size": len(raw),
                    "head": raw[:8].decode("latin1", errors="ignore"),
                }
                pdf_hits.append(hit)
                if raw.startswith(b"%PDF-"):
                    pdf_hits.append(
                        {
                            "validated_main_pdf": True,
                            "validated_url": resp.url,
                            "validated_size": len(raw),
                        }
                    )
                    captured_pdf["bytes"] = raw
                    captured_pdf["url"] = resp.url
                    if not pdf_future.done():
                        pdf_future.set_result((raw, resp.url))

            pdf_page.on("response", lambda resp: asyncio.create_task(maybe_capture(resp)))

            await pdf_page.goto(pdf_url, wait_until="domcontentloaded", timeout=120000)
            try:
                raw, final_pdf_url = await asyncio.wait_for(
                    pdf_future,
                    timeout=max(5.0, config.page_wait_seconds + 5.0),
                )
                result = {
                    "article_title": article_title,
                    "article_url": article_url,
                    "requested_pdf_url": pdf_url,
                    "pdf_url": final_pdf_url,
                    "pdf_bytes": raw,
                    "strategy": "browser_pdf_capture",
                    "article_body_excerpt": article_body[:1500],
                    "pdf_hits": pdf_hits,
                }
                await close_page_quietly(pdf_page)
                pdf_page = None
                await close_page_quietly(article_page)
                article_page = None
                return result
                
            except asyncio.TimeoutError:
                await pdf_page.wait_for_timeout(int(config.page_wait_seconds * 1000))

            raw = captured_pdf.get("bytes", b"")
            final_pdf_url = str(captured_pdf.get("url") or "")
            if isinstance(raw, (bytes, bytearray)) and raw.startswith(b"%PDF-"):
                result = {
                    "article_title": article_title,
                    "article_url": article_url,
                    "requested_pdf_url": pdf_url,
                    "pdf_url": final_pdf_url,
                    "pdf_bytes": bytes(raw),
                    "strategy": "browser_pdf_capture_post_timeout_recovery",
                    "article_body_excerpt": article_body[:1500],
                    "pdf_hits": pdf_hits,
                }
                await close_page_quietly(pdf_page)
                pdf_page = None
                await close_page_quietly(article_page)
                article_page = None
                return result

            pdf_body = await pdf_page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''")
            result = {
                "article_title": article_title,
                "article_url": article_url,
                "requested_pdf_url": pdf_url,
                "pdf_url": "",
                "pdf_bytes": b"",
                "strategy": "no_entitled_main_pdf",
                "article_body_excerpt": article_body[:1500],
                "pdf_page_url": pdf_page.url or "",
                "pdf_page_excerpt": pdf_body[:1500],
                "pdf_hits": pdf_hits,
            }
            await close_page_quietly(pdf_page)
            pdf_page = None
            await close_page_quietly(article_page)
            article_page = None
            return result
    finally:
        await close_page_quietly(pdf_page)
        await close_page_quietly(article_page)


class AcsBrowserProvider(DownloadProvider):
    provider_name = "acs_browser"

    def can_handle(self, row: DownloadRow) -> bool:
        publisher = row.publisher.lower()
        doi = row.doi.lower()
        url = row.url.lower()
        return "acs" in publisher or "pubs.acs.org" in url or doi.startswith("10.1021/")

    def download_one(self, row: DownloadRow, config: RunConfig, logger: Logger) -> DownloadResult:
        pdf_dir = config.output_dir / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)

        result = DownloadResult(
            idx=row.idx,
            doi=row.doi,
            title=row.title,
            publisher=row.publisher or "ACS",
            status="started",
            source_url=row.url,
        )

        try:
            info = asyncio.run(fetch_acs_main_pdf(row, config))
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
            result.status = "acs_provider_exception"
            result.detail = f"{type(exc).__name__}: {exc}"
            logger.info(f"[FAIL] {row.doi} -> {result.detail}")
            return result
