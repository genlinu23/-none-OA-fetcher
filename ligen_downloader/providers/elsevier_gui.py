from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from logging import Logger
from pathlib import Path

import win32con
import win32gui
import win32api
from playwright.async_api import async_playwright

from ..models import DownloadResult
from ..models import DownloadRow
from ..models import RunConfig
from ..utils import safe_name
from .base import DownloadProvider


VIEWER_DOWNLOAD_CLICK_JS = r"""
() => {
  const viewer = document.querySelector('pdf-viewer');
  if (!viewer || !viewer.shadowRoot) return {ok:false, reason:'no_viewer'};
  const toolbar = viewer.shadowRoot.querySelector('viewer-toolbar#toolbar');
  if (!toolbar || !toolbar.shadowRoot) return {ok:false, reason:'no_toolbar'};
  const downloads = toolbar.shadowRoot.querySelector('viewer-download-controls#downloads');
  if (!downloads || !downloads.shadowRoot) return {ok:false, reason:'no_download_controls'};
  const save = downloads.shadowRoot.querySelector('#save');
  if (!save) return {ok:false, reason:'no_save_button'};
  save.click();
  return {ok:true, clicked:'save'};
}
"""


async def close_page_quietly(page) -> None:
    if not page:
        return
    try:
        await page.close()
    except Exception:
        pass


async def safe_page_title(page) -> str:
    if not page:
        return ""
    try:
        return await page.title()
    except Exception:
        return ""


async def safe_page_text_excerpt(page, limit: int = 2000) -> str:
    if not page:
        return ""
    try:
        return await page.evaluate(f"() => document.body ? document.body.innerText.slice(0, {int(limit)}) : ''")
    except Exception:
        return ""


def classify_popup_state(popup_url: str, popup_title: str, popup_text: str, has_extension_frame: bool) -> dict:
    text_lower = (popup_text or "").lower()
    title_lower = (popup_title or "").lower()
    url_lower = (popup_url or "").lower()

    state = {
        "popup_opened": bool(popup_url),
        "popup_url_kind": "unknown",
        "signed_pdf_url_detected": False,
        "captcha_blocked": False,
        "loading_stuck": False,
        "viewer_ready": bool(has_extension_frame),
        "recommended_action": "",
    }

    if "sciencedirect.com/science/article/pii/" in url_lower and "crasolve=1" in url_lower:
        state["popup_url_kind"] = "sciencedirect_crasolve_pdf"
    elif "pdf.sciencedirectassets.com/" in url_lower:
        state["popup_url_kind"] = "signed_pdf_asset"
    elif "sciencedirect.com/science/article/pii/" in url_lower and "/pdf" in url_lower:
        state["popup_url_kind"] = "sciencedirect_pdf_route"

    if "pdf.sciencedirectassets.com/" in title_lower or "pdf.sciencedirectassets.com/" in text_lower:
        state["signed_pdf_url_detected"] = True

    if "are you a robot" in text_lower or "captcha challenge" in text_lower:
        state["captcha_blocked"] = True
        state["recommended_action"] = "complete_captcha_then_reopen_pdf"
    elif title_lower.startswith("loading https://pdf.sciencedirectassets.com/"):
        state["loading_stuck"] = True
        state["recommended_action"] = "wait_or_refresh_until_pdf_viewer_materializes"
    elif has_extension_frame:
        state["recommended_action"] = "trigger_viewer_save"
    else:
        state["recommended_action"] = "try_browser_ctrl_s"

    return state


class SaveAsAutoConfirmer:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status = "not_started"
        self.detail = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        deadline = time.time() + max(1.0, self.timeout_seconds)
        self.status = "watching"
        while not self._stop.is_set() and time.time() < deadline:
            try:
                dialog_hwnd = self._find_save_as_dialog()
                if dialog_hwnd:
                    try:
                        win32gui.ShowWindow(dialog_hwnd, win32con.SW_RESTORE)
                    except Exception:
                        pass
                    try:
                        win32gui.SetActiveWindow(dialog_hwnd)
                    except Exception:
                        pass
                    try:
                        win32gui.SetForegroundWindow(dialog_hwnd)
                    except Exception:
                        pass
                    time.sleep(0.3)
                    save_button = self._find_button(dialog_hwnd, ["保存(&S)", "保存", "Save"])
                    if save_button:
                        try:
                            win32gui.SendMessage(save_button, win32con.BM_CLICK, 0, 0)
                            self.status = "clicked_save"
                            self.detail = f"dialog={dialog_hwnd}; button={save_button}; method=BM_CLICK"
                            return
                        except Exception:
                            pass
                        try:
                            left, top, right, bottom = win32gui.GetWindowRect(save_button)
                            x = (left + right) // 2
                            y = (top + bottom) // 2
                            original = win32api.GetCursorPos()
                            win32api.SetCursorPos((x, y))
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                            win32api.SetCursorPos(original)
                            self.status = "clicked_save"
                            self.detail = f"dialog={dialog_hwnd}; button={save_button}; method=mouse_event"
                            return
                        except Exception as exc:
                            self.detail = f"button_click_error: {type(exc).__name__}: {exc}"
                    self.status = "dialog_found_but_no_button"
                    self.detail = f"dialog={dialog_hwnd}"
            except Exception as exc:
                self.detail = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
        if self.status == "watching":
            self.status = "timeout"

    def _find_save_as_dialog(self) -> int | None:
        found: list[int] = []

        def cb(hwnd: int, extra: object) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            cls = win32gui.GetClassName(hwnd) or ""
            if cls == "#32770" and ("另存为" in title or "Save As" in title):
                found.append(hwnd)
                return False
            return True

        win32gui.EnumWindows(cb, None)
        return found[0] if found else None

    def _find_button(self, dialog_hwnd: int, labels: list[str]) -> int | None:
        children: list[tuple[int, str, str]] = []

        def cb(hwnd: int, extra: object) -> bool:
            children.append((hwnd, win32gui.GetClassName(hwnd) or "", win32gui.GetWindowText(hwnd) or ""))
            return True

        win32gui.EnumChildWindows(dialog_hwnd, cb, None)
        for label in labels:
            for hwnd, cls, title in children:
                if cls == "Button" and title == label:
                    return hwnd
        return None

    def wait(self) -> None:
        if self._thread:
            self._thread.join(timeout=self.timeout_seconds + 2)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def find_new_pdf(downloads_dir: Path, started_at: float, timeout_seconds: float) -> Path:
    deadline = time.time() + max(1.0, timeout_seconds)
    while time.time() < deadline:
        candidates = []
        for path in downloads_dir.glob("*.pdf"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= started_at and stat.st_size > 0:
                candidates.append((stat.st_mtime, path))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        time.sleep(0.5)
    raise TimeoutError(f"No new PDF appeared in {downloads_dir} within {timeout_seconds} seconds")


async def browser_download_one(row: DownloadRow, config: RunConfig) -> dict:
    article_page = None
    pdf_popup = None
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
            article_title = await safe_page_title(article_page)
            article_text = await safe_page_text_excerpt(article_page, limit=1200)
            pii_match = re.search(r"/pii/([^/?]+)", article_url)
            article_pii = pii_match.group(1) if pii_match else ""
            selector = f'a[href*="{article_pii}/pdfft"], a[href*="{article_pii}/pdf?"]'
            link = article_page.locator(selector).first
            await link.wait_for(timeout=60000)
            link_href = await link.get_attribute("href")

            async with article_page.expect_popup(timeout=60000) as popup_info:
                await link.click()
            pdf_popup = await popup_info.value
            await pdf_popup.wait_for_load_state("domcontentloaded", timeout=120000)
            await pdf_popup.wait_for_timeout(5000)

            ext_frame = None
            deadline = time.time() + max(10.0, config.page_wait_seconds)
            while time.time() < deadline:
                for frame in pdf_popup.frames:
                    if "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html" in (frame.url or ""):
                        ext_frame = frame
                        break
                if ext_frame:
                    break
                await pdf_popup.wait_for_timeout(500)

            popup_url = pdf_popup.url or ""
            popup_title = await safe_page_title(pdf_popup)
            popup_text = await safe_page_text_excerpt(pdf_popup, limit=2000)
            step_states = {
                "article_opened": True,
                "article_url": article_url,
                "article_has_pii": bool(article_pii),
                "pdf_link_found": bool(link_href),
                "popup_opened": bool(popup_url),
                "popup_state": classify_popup_state(
                    popup_url=popup_url,
                    popup_title=popup_title,
                    popup_text=popup_text,
                    has_extension_frame=bool(ext_frame),
                ),
            }

            viewer_strategy = "chrome_pdf_viewer_save_button"
            if ext_frame:
                click_result = await ext_frame.evaluate(VIEWER_DOWNLOAD_CLICK_JS)
            else:
                popup_state = step_states["popup_state"]
                if popup_state.get("captcha_blocked"):
                    return {
                        "article_title": article_title,
                        "article_url": article_url,
                        "article_excerpt": article_text,
                        "article_pii": article_pii,
                        "pdf_link_href": link_href or "",
                        "pdf_popup_url": popup_url,
                        "pdf_popup_title": popup_title,
                        "pdf_popup_excerpt": popup_text[:1000],
                        "click_result": {
                            "ok": False,
                            "reason": "captcha_blocked",
                            "popup_title": popup_title,
                        },
                        "viewer_strategy": "captcha_detection",
                        "step_states": step_states,
                    }
                if popup_state.get("loading_stuck"):
                    return {
                        "article_title": article_title,
                        "article_url": article_url,
                        "article_excerpt": article_text,
                        "article_pii": article_pii,
                        "pdf_link_href": link_href or "",
                        "pdf_popup_url": popup_url,
                        "pdf_popup_title": popup_title,
                        "pdf_popup_excerpt": popup_text[:1000],
                        "click_result": {
                            "ok": False,
                            "reason": "pdf_loading_stuck_before_viewer",
                            "popup_title": popup_title,
                        },
                        "viewer_strategy": "loading_stuck_detection",
                        "step_states": step_states,
                    }
                # Some Elsevier PDF URLs render as a top-level loading page rather than the
                # usual extension frame. In that case, the most reliable GUI fallback is the
                # browser's native Save shortcut.
                await pdf_popup.bring_to_front()
                await pdf_popup.keyboard.press("Control+S")
                click_result = {"ok": True, "clicked": "ctrl+s", "reason": "no_extension_frame"}
                viewer_strategy = "browser_ctrl_s_fallback"
            result = {
                "article_title": article_title,
                "article_url": article_url,
                "article_excerpt": article_text,
                "article_pii": article_pii,
                "pdf_link_href": link_href or "",
                "pdf_popup_url": pdf_popup.url,
                "pdf_popup_title": popup_title,
                "click_result": click_result,
                "viewer_strategy": viewer_strategy,
                "step_states": step_states,
            }
            await close_page_quietly(pdf_popup)
            pdf_popup = None
            await close_page_quietly(article_page)
            article_page = None
            return result
    finally:
        await close_page_quietly(pdf_popup)
        await close_page_quietly(article_page)


class ElsevierGuiProvider(DownloadProvider):
    provider_name = "elsevier_gui"

    def can_handle(self, row: DownloadRow) -> bool:
        publisher = row.publisher.lower()
        return "elsevier" in publisher or "sciencedirect" in row.url.lower() or "10.1016/" in row.doi.lower()

    def download_one(self, row: DownloadRow, config: RunConfig, logger: Logger) -> DownloadResult:
        pdf_dir = config.output_dir / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir = Path.home() / "Downloads"

        started_at = time.time()
        confirmer = SaveAsAutoConfirmer(timeout_seconds=config.download_timeout_seconds)
        result = DownloadResult(
            idx=row.idx,
            doi=row.doi,
            title=row.title,
            publisher=row.publisher or "Elsevier",
            status="started",
            source_url=row.url,
        )

        try:
            confirmer.start()
            step_info = asyncio.run(browser_download_one(row, config))
            confirmer.wait()
            click_result = step_info.get("click_result") or {}
            result.final_pdf_url = str(step_info.get("pdf_popup_url") or "")
            if not click_result.get("ok"):
                reason = str(click_result.get("reason") or "")
                if reason == "captcha_blocked":
                    result.status = "captcha_blocked"
                elif reason == "pdf_loading_stuck_before_viewer":
                    result.status = "pdf_loading_stuck"
                else:
                    result.status = "viewer_download_click_failed"
                result.detail = json.dumps(step_info, ensure_ascii=False)
                return result

            downloaded = find_new_pdf(
                downloads_dir,
                started_at=started_at,
                timeout_seconds=config.download_timeout_seconds,
            )
            pdf_filename = f"{row.idx}_{safe_name(row.doi)}.pdf"
            final_path = pdf_dir / pdf_filename
            final_path.write_bytes(downloaded.read_bytes())

            result.status = "downloaded_via_save_as_gui"
            result.pdf_filename = pdf_filename
            result.pdf_path = str(final_path)
            result.size_bytes = final_path.stat().st_size
            result.detail = json.dumps(
                {
                    **step_info,
                    "save_as_status": confirmer.status,
                    "save_as_detail": confirmer.detail,
                    "downloads_source_path": str(downloaded),
                },
                ensure_ascii=False,
            )
            logger.info(f"[OK] {row.doi} -> {final_path}")
            return result
        except Exception as exc:
            result.status = "web_fallback_exception"
            result.detail = (
                f"{type(exc).__name__}: {exc}; "
                f"save_as_status={confirmer.status}; save_as_detail={confirmer.detail}"
            )
            logger.info(f"[FAIL] {row.doi} -> {result.detail}")
            return result
        finally:
            confirmer.stop()
