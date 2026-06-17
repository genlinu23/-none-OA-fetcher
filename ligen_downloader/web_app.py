from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import difflib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .app import KeywordService
from .app import SearchWorkflow
from .storage import SQLiteStore
from .utils import extract_doi_like
from .utils import infer_publisher


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
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
DEFAULT_AGENT_MODEL = "gpt-5.4-mini"
DEFAULT_AGENT_BASE_URL = "https://api.vectorengine.cn/v1"
DEFAULT_AGENT_RETRY_ATTEMPTS = 2
DEFAULT_AGENT_RETRY_BASE_DELAY_SECONDS = 0.4
DEFAULT_AGENT_REQUEST_TIMEOUT_SECONDS = 18
DEFAULT_RESEARCH_HARVEST_CAP = 0
RESEARCH_STRATEGY_LIMITS = {
    "quality": 0,
    "recall": 0,
}


def _resolve_skill_root() -> Path:
    env_root = os.environ.get("LIGEN_SKILL_ROOT", "").strip()
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser())
    executable = Path(sys.executable).resolve()
    candidates.extend(
        [
            Path(__file__).resolve().parents[1],
            executable.parent,
            executable.parent.parent,
            Path.cwd(),
        ]
    )
    for candidate in candidates:
        if (candidate / "scripts" / "run_ligen_script_mode.py").exists():
            return candidate.resolve()
    return Path(__file__).resolve().parents[1]


def _resolve_python_exe() -> str:
    env_python = os.environ.get("LIGEN_PYTHON_EXE", "").strip()
    if env_python:
        return env_python
    if not getattr(sys, "frozen", False):
        return sys.executable
    for candidate in ("python", "py"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    local_python = Path.home() / "AppData" / "Local" / "Python" / "pythoncore-3.14-64" / "python.exe"
    if local_python.exists():
        return str(local_python)
    return "python"


def _default_app_home() -> Path:
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "PKU Literature Workbench"
    return SKILL_ROOT


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[1]


def _resolve_frontend_dist_dir() -> Path:
    candidates = [
        _resource_root() / "ligen_downloader" / "web_frontend" / "dist",
        Path(__file__).resolve().parent / "web_frontend" / "dist",
    ]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return candidates[-1]


def _internal_script_command(script_name: str, args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable)), "--internal-script", script_name, *args]
    return [str(PYTHON_EXE), str(SKILL_ROOT / "scripts" / f"{script_name}.py"), *args]


SKILL_ROOT = _resolve_skill_root()
APP_HOME = Path(os.environ.get("LIGEN_APP_HOME", str(_default_app_home()))).expanduser()
OUTPUTS_DIR = APP_HOME / "outputs"
STATE_PATH = OUTPUTS_DIR / "web_client_state.json"
AGENT_CONFIG_PATH = APP_HOME / "agent_config.json"
SCRIPT_MODE = SKILL_ROOT / "scripts" / "run_ligen_script_mode.py"
PYTHON_EXE = _resolve_python_exe()


WEB_FRONTEND_DIST_DIR = _resolve_frontend_dist_dir()



def _default_output_dir() -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(OUTPUTS_DIR / f"session_archive_{timestamp}")


def _default_task_name() -> str:
    return f"Task {datetime.now().strftime('%Y-%m-%d %H:%M')}"


def _default_state() -> dict[str, Any]:
    return {
        "input_text": "",
        "task_name": _default_task_name(),
        "output_dir": _default_output_dir(),
        "output_dir_auto": True,
        "page_settle_seconds": "6",
        "sleep_seconds": "1.5",
        "per_doi_timeout_seconds": "240",
        "per_publisher_timeout_seconds": "3600",
        "max_parallel_publishers": "3",
        "max_warmup_per_publisher": "1",
        "launch_chrome": True,
        "keep_existing_tabs": True,
        "resume_existing": True,
        "research_query_text": "",
        "research_search_strategy": "quality",
        "research_limit_per_provider": str(RESEARCH_STRATEGY_LIMITS["quality"]),
        "research_provider_crossref": True,
        "research_provider_openalex": True,
        "research_provider_local_manual": False,
        "research_confirmed_terms_text": "",
        "research_confirmed_query_text": "",
        "research_keywords_confirmed": False,
        "research_title_review_status_text": "",
        "research_title_review_summary_text": "",
        "research_title_review_items_json": "[]",
        "research_progress_json": "{}",
        "research_doi_file_all": "",
        "research_doi_file_oa": "",
        "research_doi_file_non_oa": "",
        "research_doi_file_unknown": "",
        "research_doi_file_csv": "",
        "port_map": {name: str(port) for name, port in DEFAULT_PORT_MAP.items()},
        "last_run_dir": "",
    }


class ProcessMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.logs: list[str] = []
        self.max_logs = 500
        self.current_command = ""
        self.current_run_dir = ""
        self.current_mode = ""
        self.last_exit_code: int | None = None

    def running(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self, command: list[str], workdir: Path, run_dir: Path, mode: str) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("A run is already active.")
            self.process = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.logs = []
            self.current_command = subprocess.list2cmdline(command)
            self.current_run_dir = str(run_dir)
            self.current_mode = mode
            self.last_exit_code = None

            assert self.process.stdout is not None
            assert self.process.stderr is not None
            threading.Thread(
                target=self._pump_stream,
                args=("stdout", self.process.stdout),
                daemon=True,
            ).start()
            threading.Thread(
                target=self._pump_stream,
                args=("stderr", self.process.stderr),
                daemon=True,
            ).start()
            threading.Thread(target=self._watch_exit, daemon=True).start()

        self._append_log("stdout", f"$ {self.current_command}")

    def stop(self) -> None:
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                return
            self.process.terminate()
        self._append_log("stdout", "Stop requested.")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "running": running,
                "status_text": (
                    f"Running {self.current_mode}" if running else (
                        f"Finished ({self.last_exit_code})"
                        if self.last_exit_code is not None
                        else "准备就绪"
                    )
                ),
                "current_command": self.current_command,
                "current_run_dir": self.current_run_dir,
                "current_mode": self.current_mode,
                "last_exit_code": self.last_exit_code,
                "logs": list(self.logs),
            }

    def _pump_stream(self, name: str, handle) -> None:
        try:
            for line in handle:
                self._append_log(name, line.rstrip("\n"))
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def _watch_exit(self) -> None:
        process = None
        with self._lock:
            process = self.process
        if process is None:
            return
        return_code = process.wait()
        with self._lock:
            self.last_exit_code = return_code
            self.process = None
        self._append_log("event", f"PROCESS_EXIT::{return_code}")

    def _append_log(self, name: str, line: str) -> None:
        prefix = {
            "stdout": "[log]",
            "stderr": "[err]",
            "event": "[evt]",
        }.get(name, "[log]")
        with self._lock:
            self.logs.append(f"{prefix} {line}")
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs :]


class LigenWebController:
    def __init__(self) -> None:
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        self._state_lock = threading.Lock()
        self.state = self._load_state()
        self.monitor = ProcessMonitor()
        self.app_store = SQLiteStore(OUTPUTS_DIR / "app_state.db")
        self.keyword_service = KeywordService(self.app_store)
        self.search_workflow = SearchWorkflow(self.app_store)

    def render_html(self) -> str:
        react_index = WEB_FRONTEND_DIST_DIR / "index.html"
        if react_index.exists():
            return react_index.read_text(encoding="utf-8")
        return """<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>React frontend is not built</title></head>
  <body style="font-family: sans-serif; padding: 32px; line-height: 1.6">
    <h1>React 前端尚未构建</h1>
    <p>当前后端已禁止回退到旧版静态页。请在 <code>ligen_downloader/web_frontend</code> 运行 <code>npm install</code> 和 <code>npm run build</code>。</p>
  </body>
</html>"""

    def get_state_payload(self) -> dict[str, Any]:
        state = self._copy_state()
        if bool(state.get("output_dir_auto")):
            state["output_dir"] = _build_task_output_dir(state)
        preview_rows = _parse_input_text(state.get("input_text", ""))
        publisher_counts = Counter(row["publisher"] for row in preview_rows)
        run_state = self.monitor.snapshot()
        current_run_dir = run_state.get("current_run_dir") or state.get("last_run_dir") or ""
        results = _summarize_results(Path(current_run_dir)) if current_run_dir else _empty_results()
        progress = _build_progress_snapshot(
            preview_rows=preview_rows,
            run_state=run_state,
            results=results,
        )
        payload = {
            **state,
            "preview_rows": preview_rows,
            "publisher_counts": dict(publisher_counts),
            "publisher_summary": ", ".join(
                f"{publisher} {count}" for publisher, count in sorted(publisher_counts.items())
            ) or "等待 DOI 队列",
            "port_statuses": _port_status_rows(state.get("port_map") or {}),
            "run": run_state,
            "current_run_dir": current_run_dir,
            "results": results,
            "progress": progress,
            "research": _research_state_payload(state),
        }
        return payload

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._merge_state(payload)
        self._refresh_output_dir_if_needed()
        return self.get_state_payload()

    def handle_research_agent_turn(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message") or payload.get("user_message") or "").strip()
        if not message:
            raise ValueError("请先输入一句话。")
        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        if not history and isinstance(payload.get("conversation"), list):
            history = payload.get("conversation") or []
        brief = str(payload.get("research_query_text") or "").strip()
        agent_payload = _build_research_agent_reply(
            message=message,
            history=history,
            current_brief=brief,
        )
        if bool(agent_payload.get("include_in_brief")):
            updated_brief = _merge_research_brief(
                brief,
                str(agent_payload.get("normalized_requirement") or ""),
                replace=bool(agent_payload.get("replace_brief")),
            )
            self._merge_state({
                "research_query_text": updated_brief,
                "research_confirmed_terms_text": "",
                "research_confirmed_query_text": "",
                "research_keywords_confirmed": False,
                "research_last_keyword_set_id": "",
                "research_last_run_id": "",
                "research_status_text": "研究条件已更新，等待生成关键词方案。",
                "research_summary_text": "当前对话改变了研究简报；旧关键词和旧 DOI 候选已失效。",
                "research_title_review_status_text": "",
                "research_title_review_summary_text": "",
                "research_title_review_items_json": "[]",
                "research_progress_json": "{}",
                "research_doi_file_all": "",
                "research_doi_file_oa": "",
                "research_doi_file_non_oa": "",
                "research_doi_file_unknown": "",
                "research_doi_file_csv": "",
            })
            self._refresh_output_dir_if_needed()
        return {
            "agent": agent_payload,
            "llm_available": bool(_agent_llm_config()),
            "state": self.get_state_payload(),
        }

    def create_research_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._merge_state(payload)
        state = self._copy_state()
        query_text = _resolve_research_query_text(state)
        if not query_text:
            raise ValueError("请先输入研究主题或检索需求。")
        terms = _keyword_terms_from_brief(query_text)
        if not terms:
            self._merge_state({
                "research_confirmed_terms_text": "",
                "research_confirmed_query_text": "",
                "research_keywords_confirmed": False,
                "research_last_keyword_set_id": "",
                "research_last_run_id": "",
                "research_status_text": "缺少研究主题",
                "research_summary_text": "当前只识别到期刊/来源限制。请补充材料、方法、反应体系、应用场景或具体研究问题。",
                "research_title_review_status_text": "",
                "research_title_review_summary_text": "",
                "research_title_review_items_json": "[]",
                "research_progress_json": "{}",
                "research_doi_file_all": "",
                "research_doi_file_oa": "",
                "research_doi_file_non_oa": "",
                "research_doi_file_unknown": "",
                "research_doi_file_csv": "",
            })
            raise ValueError("当前只识别到期刊/来源限制，还缺研究主题。请补充你要找的具体方向，例如材料、方法、反应体系或应用场景。")
        keyword_set = self.keyword_service.create_draft(
            query_text,
            include_terms=terms,
        )
        research_patch = {
            "research_last_keyword_set_id": str(keyword_set.id or ""),
            "research_status_text": f"关键词草案 #{keyword_set.id} 已生成，共 {len(keyword_set.include_terms)} 个词。",
            "research_summary_text": "请检查并编辑关键词。只有确认后，系统才会检索 DOI 候选。",
            "research_confirmed_terms_text": "\n".join(keyword_set.include_terms),
            "research_confirmed_query_text": "",
            "research_keywords_confirmed": False,
            "research_title_review_status_text": "",
            "research_title_review_summary_text": "",
            "research_title_review_items_json": "[]",
            "research_last_run_id": "",
            "research_doi_file_all": "",
            "research_doi_file_oa": "",
            "research_doi_file_non_oa": "",
            "research_doi_file_unknown": "",
            "research_doi_file_csv": "",
        }
        self._merge_state(research_patch)
        self._refresh_output_dir_if_needed()
        return self.get_state_payload()

    def confirm_research_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._merge_state(payload)
        state = self._copy_state()
        query_text = _resolve_research_query_text(state)
        terms = [term for term in _parse_terms_text(str(state.get("research_confirmed_terms_text") or "")) if not _is_filter_only_term(term)]
        if not query_text:
            raise ValueError("请先输入研究主题或检索需求。")
        if not terms:
            inferred_terms = _keyword_terms_from_brief(query_text)
            if not inferred_terms:
                raise ValueError("当前只识别到期刊/来源限制，还缺研究主题。请补充你要找的具体方向，例如材料、方法、反应体系或应用场景。")
            terms = inferred_terms
        keyword_set = self.keyword_service.create_draft(
            query_text,
            name=f"Confirmed {datetime.now().strftime('%Y%m%d %H%M%S')}",
            include_terms=terms,
        )
        locked = self.keyword_service.lock(keyword_set.id or 0)
        research_patch = {
            "research_last_keyword_set_id": str(locked.id or ""),
            "research_confirmed_query_text": query_text,
            "research_confirmed_terms_text": "\n".join(terms),
            "research_keywords_confirmed": True,
            "research_status_text": f"关键词集 #{locked.id} 已确认。",
            "research_summary_text": "关键词已锁定。现在可以开始 DOI 候选检索。",
            "research_title_review_status_text": "",
            "research_title_review_summary_text": "",
            "research_title_review_items_json": "[]",
            "research_last_run_id": "",
            "research_doi_file_all": "",
            "research_doi_file_oa": "",
            "research_doi_file_non_oa": "",
            "research_doi_file_unknown": "",
            "research_doi_file_csv": "",
        }
        self._merge_state(research_patch)
        self._refresh_output_dir_if_needed()
        return self.get_state_payload()

    def confirm_and_run_research_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.confirm_research_terms(payload)
        return self.run_research_search({})

    def run_research_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._merge_state(payload)
        self._ensure_scholarly_research_providers()
        state = self._copy_state()
        if not bool(state.get("research_keywords_confirmed")):
            raise ValueError("请先确认关键词，再运行 DOI 检索。")
        provider_ids = _selected_research_provider_ids(state)
        if not provider_ids:
            raise ValueError("请至少选择一个文献来源。")
        keyword_set_id = _safe_int(str(state.get("research_last_keyword_set_id") or "0"), 0)
        if keyword_set_id <= 0:
            raise ValueError("没有找到已确认的关键词集。")
        locked = self.app_store.get_keyword_set(keyword_set_id)
        if locked.status != "locked":
            raise ValueError("当前关键词集还没有锁定，请重新确认关键词。")
        strategy = _normalize_research_search_strategy(state.get("research_search_strategy"))
        limit = RESEARCH_STRATEGY_LIMITS[strategy]
        self._merge_state({
            "research_search_strategy": strategy,
            "research_limit_per_provider": str(limit),
        })
        self._start_research_progress(provider_ids=provider_ids, limit=limit)
        run_id, result = self.search_workflow.run(
            locked,
            provider_ids=provider_ids,
            limit_per_provider=limit,
            progress_callback=lambda event: self._update_research_progress(event),
        )
        research_patch = {
            "research_last_keyword_set_id": str(locked.id or ""),
            "research_last_run_id": str(run_id),
            "research_search_strategy": strategy,
            "research_limit_per_provider": str(limit),
            "research_status_text": f"检索记录 #{run_id} 已保存。",
            "research_summary_text": (
                f"原始记录 {result.raw_total} | 去重后 {result.unique_count} | "
                f"重复 {result.duplicate_count} | 来源重叠 {result.overlap_count} | "
                f"可下载候选 {result.download_candidate_count}"
            ),
            "research_progress_json": json.dumps({
                "running": False,
                "status": "complete",
                "label": f"检索完成：{result.download_candidate_count} 条候选",
                "provider": "Review Agent",
                "percent": 100,
                "fetched": result.raw_total,
                "cap": result.raw_total if limit <= 0 else limit * max(1, len(provider_ids)),
            }, ensure_ascii=False),
        }
        self._merge_state(research_patch)
        snapshot = _load_research_run_snapshot(run_id)
        export = _write_research_doi_exports(self._copy_state(), run_id, snapshot.get("records") or [])
        oa_summary = snapshot.get("oa_summary") or {}
        self._merge_state({
            "research_status_text": f"DOI 清单已生成：检索记录 #{run_id}。",
            "research_summary_text": (
                f"原始记录 {result.raw_total} | 去重后 {result.unique_count} | "
                f"重复 {result.duplicate_count} | 来源重叠 {result.overlap_count} | "
                f"OA {oa_summary.get('oa_count', 0)} | 非 OA {oa_summary.get('non_oa_count', 0)} | "
                f"未知 {oa_summary.get('unknown_oa_count', 0)}"
            ),
            "research_doi_file_all": export.get("all", ""),
            "research_doi_file_oa": export.get("oa", ""),
            "research_doi_file_non_oa": export.get("non_oa", ""),
            "research_doi_file_unknown": export.get("unknown", ""),
            "research_doi_file_csv": export.get("csv", ""),
        })
        return self.get_state_payload()

    def review_research_titles(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._merge_state(payload)
        state = self._copy_state()
        research = _research_state_payload(state)
        records = research.get("records") or []
        if not records:
            raise ValueError("Run provider search before starting title review.")

        cleanup = _review_agent_cleanup_records(records)
        review_items = [_review_record_title(record) for record in cleanup["records"][:60]]
        counts = Counter(item["status"] for item in review_items)
        review_patch = {
            "research_title_review_status_text": "Review Agent 已完成清洗与题名抽检。",
            "research_title_review_summary_text": (
                f"清洗前 {cleanup['input_count']} | 清洗后 {cleanup['output_count']} | "
                f"去重 {cleanup['duplicate_count']} | 去噪 {cleanup['noise_count']} | "
                f"题名抽检 {len(review_items)}：完全一致 {counts.get('exact_match', 0)}，"
                f"高度相似 {counts.get('likely_match', 0)}，不一致 {counts.get('mismatch', 0)}"
            ),
            "research_title_review_items_json": json.dumps(review_items, ensure_ascii=False),
        }
        self._merge_state(review_patch)
        return self.get_state_payload()

    def use_research_results_as_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._copy_state()
        research = _research_state_payload(state)
        records = research.get("records") or []
        if not records:
            raise ValueError("还没有可导入的 DOI 候选记录。")
        cleaned = _review_agent_cleanup_records(records)
        lines: list[str] = []
        for record in cleaned["records"]:
            doi = str(record.get("doi") or "").strip()
            url = str(record.get("url") or "").strip()
            if doi:
                lines.append(doi)
            elif url:
                lines.append(url)
        if not lines:
            raise ValueError("当前检索结果里还没有 DOI 或 URL 候选。")
        self._merge_state({"input_text": "\n".join(lines)})
        self._refresh_output_dir_if_needed()
        return self.get_state_payload()

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "").strip()
        if mode not in {"warmup", "download"}:
            raise ValueError("mode must be 'warmup' or 'download'")
        self._merge_state(payload)
        state = self._copy_state()
        if bool(state.get("output_dir_auto")):
            auto_dir = _build_task_output_dir(state)
            self._merge_state({"output_dir": auto_dir})
            state = self._copy_state()
        preview_rows = _parse_input_text(state.get("input_text", ""))
        if not preview_rows:
            raise ValueError("No DOI or DOI URL was parsed from the current input.")
        if self.monitor.running():
            raise RuntimeError("A run is already active.")

        run_dir = Path(str(state.get("output_dir") or _build_task_output_dir(state))).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        input_path = run_dir / "web_input.txt"
        input_path.write_text(
            "\n".join(row["raw"] for row in preview_rows) + "\n",
            encoding="utf-8",
        )

        command = self._build_command(state, input_path=input_path, mode=mode)
        self._merge_state({"last_run_dir": str(run_dir)})
        self.monitor.start(command, SKILL_ROOT, run_dir, mode)
        return self.get_state_payload()

    def stop(self) -> dict[str, Any]:
        self.monitor.stop()
        return self.get_state_payload()

    def open_output_dir(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            self._merge_state(payload)
        state = self._copy_state()
        run_state = self.monitor.snapshot()
        run_dir = _resolve_current_run_dir(state, run_state)
        if run_dir:
            _materialize_partial_run_outputs(run_dir)
            target = run_dir / "pdfs"
        else:
            target = Path(str(state.get("output_dir") or _default_output_dir())).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))
        return self.get_state_payload()

    def open_results_csv(self) -> dict[str, Any]:
        state = self._copy_state()
        run_state = self.monitor.snapshot()
        run_dir = _resolve_current_run_dir(state, run_state)
        if not run_dir:
            raise FileNotFoundError("No run directory is available yet.")
        _materialize_partial_run_outputs(run_dir)
        for candidate in (
            run_dir / "combined_download_results.csv",
            run_dir / "download_results.csv",
        ):
            if candidate.exists():
                os.startfile(str(candidate))
                return self.get_state_payload()
        partial_dir = run_dir / "publisher_runs"
        if partial_dir.exists():
            os.startfile(str(partial_dir))
            return self.get_state_payload()
        raise FileNotFoundError(f"No result CSV was found in {run_dir}")

    def open_research_doi_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip().lower()
        key_map = {
            "all": "research_doi_file_all",
            "oa": "research_doi_file_oa",
            "non_oa": "research_doi_file_non_oa",
            "unknown": "research_doi_file_unknown",
            "csv": "research_doi_file_csv",
        }
        if kind not in key_map:
            raise ValueError("Unknown DOI file kind.")
        state = self._copy_state()
        target = Path(str(state.get(key_map[kind]) or "")).expanduser()
        if not target:
            raise FileNotFoundError("DOI list has not been generated yet.")
        if not target.exists():
            raise FileNotFoundError(f"DOI list file was not found: {target}")
        os.startfile(str(target.resolve()))
        return self.get_state_payload()

    def self_test(self) -> dict[str, Any]:
        payload = self.get_state_payload()
        if "preview_rows" not in payload or "run" not in payload or "results" not in payload:
            raise RuntimeError("Web payload is missing required sections.")
        html = self.render_html()
        react_markers = [
            "北大文献智采工作台",
            "/assets/",
            "type=\"module\"",
        ]
        if not all(marker in html for marker in react_markers):
            raise RuntimeError("React frontend is not built or did not render expected controls.")
        return payload

    def _build_command(self, state: dict[str, Any], input_path: Path, mode: str) -> list[str]:
        command = _internal_script_command(
            "run_ligen_script_mode",
            [
            "--input",
            str(input_path),
            "--phase",
            mode,
            "--output-dir",
            str(Path(str(state.get("output_dir") or _default_output_dir())).expanduser().resolve()),
            "--max-parallel-publishers",
            str(state.get("max_parallel_publishers") or "3"),
            "--max-warmup-per-publisher",
            str(state.get("max_warmup_per_publisher") or "1"),
            "--page-settle-seconds",
            str(state.get("page_settle_seconds") or "6"),
            "--sleep-seconds",
            str(state.get("sleep_seconds") or "1.5"),
            "--per-doi-timeout-seconds",
            str(state.get("per_doi_timeout_seconds") or "240"),
            "--per-publisher-timeout-seconds",
            str(state.get("per_publisher_timeout_seconds") or "3600"),
            ],
        )
        if bool(state.get("launch_chrome")):
            command.append("--launch-chrome")
        if bool(state.get("keep_existing_tabs")):
            command.append("--keep-existing-tabs")
        if bool(state.get("resume_existing")):
            command.append("--resume-existing")
        port_map = state.get("port_map") or {}
        for publisher, port in sorted(port_map.items()):
            if str(port).strip():
                command.extend(["--publisher-port", f"{publisher}={str(port).strip()}"])
        return command

    def _load_state(self) -> dict[str, Any]:
        if not STATE_PATH.exists():
            return _default_state()
        try:
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return _default_state()
        state = _default_state()
        state.update(payload if isinstance(payload, dict) else {})
        if str(state.get("research_limit_per_provider") or "").strip() in {"", "20"}:
            state["research_limit_per_provider"] = str(DEFAULT_RESEARCH_HARVEST_CAP)
        state["research_search_strategy"] = _normalize_research_search_strategy(state.get("research_search_strategy"))
        state["research_limit_per_provider"] = str(RESEARCH_STRATEGY_LIMITS[state["research_search_strategy"]])
        if not isinstance(state.get("port_map"), dict):
            state["port_map"] = dict(_default_state()["port_map"])
        for name, port in DEFAULT_PORT_MAP.items():
            state["port_map"].setdefault(name, str(port))
        for key in (
            "research_query_text",
            "research_confirmed_terms_text",
            "research_confirmed_query_text",
            "research_last_keyword_set_id",
            "research_last_run_id",
            "research_title_review_status_text",
            "research_title_review_summary_text",
            "research_title_review_items_json",
            "research_progress_json",
            "research_doi_file_all",
            "research_doi_file_oa",
            "research_doi_file_non_oa",
            "research_doi_file_unknown",
            "research_doi_file_csv",
        ):
            state[key] = _default_state().get(key, "")
        state["research_keywords_confirmed"] = False
        state["research_status_text"] = "等待研究主题"
        state["research_summary_text"] = "先和 Agent 说清楚研究主题；确认关键词后才会生成 DOI 清单。"
        return state

    def _copy_state(self) -> dict[str, Any]:
        with self._state_lock:
            return json.loads(json.dumps(self.state))

    def _merge_state(self, payload: dict[str, Any]) -> None:
        _save_agent_config_from_payload(payload)
        allowed = {
            "input_text",
            "task_name",
            "output_dir",
            "output_dir_auto",
            "page_settle_seconds",
            "sleep_seconds",
            "per_doi_timeout_seconds",
            "per_publisher_timeout_seconds",
            "max_parallel_publishers",
            "max_warmup_per_publisher",
            "launch_chrome",
            "keep_existing_tabs",
            "resume_existing",
            "research_query_text",
            "research_search_strategy",
            "research_limit_per_provider",
            "research_provider_crossref",
            "research_provider_openalex",
            "research_provider_local_manual",
            "research_confirmed_terms_text",
            "research_confirmed_query_text",
            "research_keywords_confirmed",
            "research_title_review_status_text",
            "research_title_review_summary_text",
            "research_title_review_items_json",
            "research_progress_json",
            "research_doi_file_all",
            "research_doi_file_oa",
            "research_doi_file_non_oa",
            "research_doi_file_unknown",
            "research_doi_file_csv",
            "research_last_keyword_set_id",
            "research_last_run_id",
            "research_status_text",
            "research_summary_text",
            "port_map",
            "last_run_dir",
        }
        with self._state_lock:
            for key, value in payload.items():
                if key not in allowed:
                    continue
                if key == "port_map" and isinstance(value, dict):
                    merged = dict(self.state.get("port_map") or {})
                    for publisher, port in value.items():
                        if publisher in DEFAULT_PORT_MAP and str(port).strip():
                            merged[publisher] = str(port).strip()
                    self.state["port_map"] = merged
                    continue
                if key == "research_search_strategy":
                    strategy = _normalize_research_search_strategy(value)
                    self.state[key] = strategy
                    self.state["research_limit_per_provider"] = str(RESEARCH_STRATEGY_LIMITS[strategy])
                    continue
                if isinstance(value, bool):
                    self.state[key] = value
                    continue
                if value is None:
                    continue
                self.state[key] = str(value)
            if not str(self.state.get("output_dir") or "").strip():
                self.state["output_dir"] = _build_task_output_dir(self.state)
            strategy = _normalize_research_search_strategy(self.state.get("research_search_strategy"))
            self.state["research_search_strategy"] = strategy
            self.state["research_limit_per_provider"] = str(RESEARCH_STRATEGY_LIMITS[strategy])
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        STATE_PATH.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _ensure_scholarly_research_providers(self) -> None:
        state = self._copy_state()
        query_text = str(state.get("research_query_text") or "").strip()
        terms_text = str(state.get("research_confirmed_terms_text") or "").strip()
        only_manual = (
            bool(state.get("research_provider_local_manual"))
            and not bool(state.get("research_provider_crossref"))
            and not bool(state.get("research_provider_openalex"))
        )
        if (query_text or terms_text) and only_manual:
            self._merge_state({
                "research_provider_crossref": True,
                "research_provider_openalex": True,
                "research_provider_local_manual": False,
                "research_limit_per_provider": str(state.get("research_limit_per_provider") or str(DEFAULT_RESEARCH_HARVEST_CAP)),
            })

    def _start_research_progress(self, *, provider_ids: list[str], limit: int) -> None:
        payload = {
            "running": True,
            "status": "running",
            "label": "开始分页采集 DOI 候选",
            "provider": "",
            "provider_index": 0,
            "provider_total": len(provider_ids),
            "fetched": 0,
            "cap": limit * max(1, len(provider_ids)) if limit > 0 else 0,
            "percent": 1,
            "started_at": datetime.now().strftime("%H:%M:%S"),
        }
        self._merge_state({
            "research_status_text": "正在分页采集 DOI 候选...",
            "research_progress_json": json.dumps(payload, ensure_ascii=False),
        })

    def _update_research_progress(self, event: dict[str, Any]) -> None:
        current = _parse_json_object(str(self._copy_state().get("research_progress_json") or "{}"))
        provider_index = _safe_int(str(event.get("provider_index") or current.get("provider_index") or "1"), 1)
        provider_total = _safe_int(str(event.get("provider_total") or current.get("provider_total") or "1"), 1)
        fetched = _safe_int(str(event.get("fetched") or "0"), 0)
        cap = _safe_int(str(event.get("cap") or current.get("provider_cap") or current.get("cap") or "0"), 0)
        overall_cap = _safe_int(str(current.get("cap") or (cap * max(1, provider_total) if cap > 0 else "0")), 0)
        completed_before = max(0, provider_index - 1) * max(0, cap)
        overall_fetched = completed_before + fetched if overall_cap <= 0 else min(overall_cap, completed_before + fetched)
        percent = max(1, min(99, round((overall_fetched / max(1, overall_cap)) * 100))) if overall_cap > 0 else min(99, 5 + provider_index * 10)
        provider = str(event.get("display_name") or event.get("provider_id") or current.get("provider") or "")
        cap_text = str(cap) if cap > 0 else "不限量"
        label = f"{provider} 第 {event.get('page') or '?'} 页，已采集 {fetched} / {cap_text}"
        if event.get("event") == "provider_start":
            label = f"开始采集 {provider} ({provider_index}/{provider_total})"
        elif event.get("event") == "provider_done":
            label = f"{provider} 完成，返回 {fetched} 条"
        payload = {
            **current,
            "running": True,
            "status": "running",
            "label": label,
            "provider": provider,
            "provider_index": provider_index,
            "provider_total": provider_total,
            "provider_cap": cap,
            "provider_fetched": fetched,
            "fetched": overall_fetched,
            "cap": overall_cap,
            "percent": percent,
            "reported_total_count": event.get("reported_total_count"),
        }
        self._merge_state({
            "research_status_text": label,
            "research_progress_json": json.dumps(payload, ensure_ascii=False),
        })

    def _refresh_output_dir_if_needed(self) -> None:
        state = self._copy_state()
        if not bool(state.get("output_dir_auto")):
            return
        self._merge_state({"output_dir": _build_task_output_dir(state)})


def _parse_input_text(raw_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        cleaned = cleaned.split("#", 1)[0].strip()
        doi = extract_doi_like(cleaned)
        url = cleaned if cleaned.startswith(("http://", "https://")) else (f"https://doi.org/{doi}" if doi else "")
        rows.append(
            {
                "idx": str(len(rows) + 1),
                "raw": cleaned,
                "doi": doi,
                "publisher": infer_publisher(doi, url),
                "url": url,
            }
        )
    return rows


def _port_status_rows(port_map: dict[str, Any]) -> list[dict[str, str]]:
    merged = {name: str(port) for name, port in DEFAULT_PORT_MAP.items()}
    for publisher, port in (port_map or {}).items():
        merged[publisher] = str(port)
    items = sorted(merged.items())

    def build_row(item: tuple[str, str]) -> dict[str, str]:
        publisher, port_text = item
        try:
            port = int(str(port_text).strip())
        except ValueError:
            return {"publisher": publisher, "port": str(port_text), "status": "invalid port"}
        return {
            "publisher": publisher,
            "port": str(port),
            "status": _port_status(port),
        }

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(items)))) as executor:
        return list(executor.map(build_row, items))


def _port_status(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.18) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        browser = str(payload.get("Browser") or "Chrome session")
        return f"online: {browser}"
    except Exception:
        return "offline"


def _empty_results() -> dict[str, Any]:
    return {
        "message": "No run results yet.",
        "results_csv": "",
        "total_rows": 0,
        "status_counts": {},
        "rows": [],
    }


def _build_progress_snapshot(
    *,
    preview_rows: list[dict[str, str]],
    run_state: dict[str, Any],
    results: dict[str, Any],
) -> dict[str, Any]:
    total = len(preview_rows)
    counts = results.get("status_counts") or {}
    completed = int(results.get("total_rows") or 0)
    downloaded = int(counts.get("downloaded") or 0)
    failed_or_pending = sum(int(value or 0) for key, value in counts.items() if key != "downloaded")
    percent = round((completed / total) * 100, 1) if total > 0 else 0.0
    mode = str(run_state.get("current_mode") or "")
    status = "idle"
    if run_state.get("running"):
        status = f"running_{mode or 'task'}"
    elif completed and completed >= total and total > 0:
        status = "complete"
    elif completed:
        status = "partial"
    label = (
        f"{completed} / {total} processed"
        if total > 0
        else "No parsed rows yet"
    )
    return {
        "status": status,
        "mode": mode,
        "total": total,
        "completed": completed,
        "downloaded": downloaded,
        "failed_or_pending": failed_or_pending,
        "remaining": max(total - completed, 0),
        "percent": percent,
        "label": label,
    }


def _summarize_results(run_dir: Path) -> dict[str, Any]:
    if not run_dir.exists():
        return {
            **_empty_results(),
            "message": f"Run directory does not exist yet: {run_dir}",
        }
    _materialize_partial_run_outputs(run_dir)
    candidates = [
        run_dir / "combined_download_results.csv",
        run_dir / "download_results.csv",
    ]
    target = next((path for path in candidates if path.exists()), None)
    if target is None:
        partial_targets = sorted((run_dir / "publisher_runs").glob("*/download_results.csv"))
        partial_rows: list[dict[str, str]] = []
        for partial_target in partial_targets:
            publisher = partial_target.parent.name
            for row in csv.DictReader(partial_target.open("r", encoding="utf-8-sig", newline="")):
                merged_row = dict(row)
                merged_row.setdefault("publisher", publisher)
                partial_rows.append(merged_row)
        if partial_rows:
            counts = Counter((row.get("status") or "").strip() for row in partial_rows)
            return {
                "message": f"Partial publisher results found in {run_dir / 'publisher_runs'}",
                "results_csv": "",
                "total_rows": len(partial_rows),
                "status_counts": dict(counts),
                "rows": partial_rows[:300],
            }
        return {
            **_empty_results(),
            "message": f"No result CSV found in {run_dir}",
        }
    rows = list(csv.DictReader(target.open("r", encoding="utf-8-sig", newline="")))
    counts = Counter((row.get("status") or "").strip() for row in rows)
    return {
        "message": "",
        "results_csv": str(target),
        "total_rows": len(rows),
        "status_counts": dict(counts),
        "rows": rows[:300],
    }


def _resolve_current_run_dir(state: dict[str, Any], run_state: dict[str, Any]) -> Path | None:
    raw = str(run_state.get("current_run_dir") or state.get("last_run_dir") or state.get("output_dir") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _materialize_partial_run_outputs(run_dir: Path) -> dict[str, Any]:
    publisher_root = run_dir / "publisher_runs"
    if not publisher_root.exists():
        return {"combined_results": 0, "combined_map": 0, "copied_pdfs": 0}

    combined_results: list[dict[str, str]] = []
    combined_map: list[dict[str, str]] = []
    for publisher_dir in sorted(path for path in publisher_root.iterdir() if path.is_dir()):
        results_csv = publisher_dir / "download_results.csv"
        map_csv = publisher_dir / "downloaded_doi_filename_map.csv"
        if results_csv.exists():
            combined_results.extend(_read_csv_rows(results_csv, publisher_dir.name))
        if map_csv.exists():
            combined_map.extend(_read_csv_rows(map_csv, publisher_dir.name))

    if not combined_results and not combined_map:
        return {"combined_results": 0, "combined_map": 0, "copied_pdfs": 0}

    pdf_dir = run_dir / "pdfs"
    copied = _copy_downloaded_pdfs_to_unified_dir(combined_results, combined_map, pdf_dir)
    _write_csv_rows(run_dir / "combined_download_results.csv", combined_results)
    _write_csv_rows(run_dir / "combined_downloaded_doi_filename_map.csv", combined_map)
    return {
        "combined_results": len(combined_results),
        "combined_map": len(combined_map),
        "copied_pdfs": copied,
    }


def _read_csv_rows(path: Path, publisher: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            normalized = {str(key): str(value or "") for key, value in row.items() if key is not None}
            if publisher and not normalized.get("publisher"):
                normalized["publisher"] = publisher
            rows.append(normalized)
    return rows


def _write_csv_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _copy_downloaded_pdfs_to_unified_dir(
    combined_results: list[dict[str, str]],
    combined_map: list[dict[str, str]],
    pdf_dir: Path,
) -> int:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    canonical_by_doi: dict[str, dict[str, str]] = {}
    canonical_by_source: dict[str, dict[str, str]] = {}
    copied = 0

    def ensure_pdf(row: dict[str, str]) -> dict[str, str] | None:
        nonlocal copied
        doi = str(row.get("doi") or "").strip()
        source_text = str(row.get("pdf_path") or "").strip()
        if not doi or not source_text:
            return None
        source_path = Path(source_text)
        if not source_path.exists() or not source_path.is_file():
            return None
        source_key = str(source_path.resolve())
        if doi in canonical_by_doi:
            canonical_by_source[source_key] = canonical_by_doi[doi]
            return canonical_by_doi[doi]
        if source_key in canonical_by_source:
            canonical_by_doi[doi] = canonical_by_source[source_key]
            return canonical_by_source[source_key]

        target_name = _safe_pdf_filename(str(row.get("pdf_filename") or ""), doi, str(row.get("idx") or ""))
        target_path = _dedupe_target_path(pdf_dir / target_name, source_path)
        if not target_path.exists():
            shutil.copy2(source_path, target_path)
            copied += 1
        canonical = {"pdf_filename": target_path.name, "pdf_path": str(target_path)}
        canonical_by_doi[doi] = canonical
        canonical_by_source[source_key] = canonical
        return canonical

    for row in combined_results:
        canonical = ensure_pdf(row)
        if canonical:
            row.update(canonical)
    for row in combined_map:
        canonical = ensure_pdf(row)
        if canonical:
            row.update(canonical)
    return copied


def _safe_pdf_filename(raw_name: str, doi: str, idx: str) -> str:
    name = Path(raw_name).name.strip()
    if not name:
        prefix = idx.strip() or "paper"
        name = f"{prefix}_{doi}.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)


def _dedupe_target_path(target_path: Path, source_path: Path) -> Path:
    if not target_path.exists():
        return target_path
    try:
        if target_path.resolve() == source_path.resolve():
            return target_path
        if target_path.stat().st_size == source_path.stat().st_size:
            return target_path
    except OSError:
        pass
    stem = target_path.stem
    suffix = target_path.suffix or ".pdf"
    for index in range(2, 1000):
        candidate = target_path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    return target_path.with_name(f"{stem}_{int(time.time())}{suffix}")


def _selected_research_provider_ids(state: dict[str, Any]) -> list[str]:
    provider_ids: list[str] = []
    if bool(state.get("research_provider_crossref")):
        provider_ids.append("crossref")
    if bool(state.get("research_provider_openalex")):
        provider_ids.append("openalex")
    if bool(state.get("research_provider_local_manual")):
        provider_ids.append("local_manual")
    return provider_ids


def _normalize_research_search_strategy(value: object) -> str:
    strategy = str(value or "quality").strip().lower()
    if strategy in RESEARCH_STRATEGY_LIMITS:
        return strategy
    return "quality"


def _load_agent_config_file() -> dict[str, str]:
    try:
        if not AGENT_CONFIG_PATH.exists():
            return {}
        payload = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if value is not None}
    except Exception:
        return {}


def _save_agent_config_from_payload(payload: dict[str, Any]) -> None:
    if not any(key in payload for key in ("agent_api_key", "agent_base_url", "agent_model", "agent_clear_api_key")):
        return
    current = _load_agent_config_file()
    if bool(payload.get("agent_clear_api_key")):
        current.pop("api_key", None)
    api_key = str(payload.get("agent_api_key") or "").strip()
    if api_key and "••" not in api_key and api_key.lower() not in {"saved", "configured"}:
        current["api_key"] = api_key
    for source_key, target_key in (("agent_base_url", "base_url"), ("agent_model", "model")):
        if source_key not in payload:
            continue
        value = str(payload.get(source_key) or "").strip()
        if value:
            current[target_key] = value
        else:
            current.pop(target_key, None)
    AGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def _agent_llm_config() -> dict[str, str]:
    saved = _load_agent_config_file()
    api_key = saved.get("api_key", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    base_url = (
        saved.get("base_url", "").strip()
        or os.environ.get("LIGEN_AGENT_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_AGENT_BASE_URL
    ).rstrip("/")
    model = (
        saved.get("model", "").strip()
        or os.environ.get("LIGEN_AGENT_MODEL", "").strip()
        or DEFAULT_AGENT_MODEL
    )
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "retry_attempts": str(_safe_int(os.environ.get("LIGEN_AGENT_RETRY_ATTEMPTS", ""), DEFAULT_AGENT_RETRY_ATTEMPTS)),
        "retry_base_delay_seconds": str(_safe_float(os.environ.get("LIGEN_AGENT_RETRY_BASE_DELAY_SECONDS", ""), DEFAULT_AGENT_RETRY_BASE_DELAY_SECONDS)),
        "request_timeout_seconds": str(_safe_float(os.environ.get("LIGEN_AGENT_REQUEST_TIMEOUT_SECONDS", ""), DEFAULT_AGENT_REQUEST_TIMEOUT_SECONDS)),
    }


def _agent_config_status() -> dict[str, Any]:
    saved = _load_agent_config_file()
    env_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    saved_api_key = saved.get("api_key", "").strip()
    api_key = saved_api_key or env_api_key
    base_url = (
        saved.get("base_url", "").strip()
        or os.environ.get("LIGEN_AGENT_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_AGENT_BASE_URL
    ).rstrip("/")
    model = saved.get("model", "").strip() or os.environ.get("LIGEN_AGENT_MODEL", "").strip() or DEFAULT_AGENT_MODEL
    return {
        "available": bool(api_key),
        "model": model,
        "base_url": base_url,
        "source": "local_settings" if saved_api_key else ("environment" if env_api_key else "missing"),
        "has_saved_key": bool(saved_api_key),
        "required_env": "OPENAI_API_KEY",
        "optional_env": "OPENAI_BASE_URL / LIGEN_AGENT_BASE_URL / LIGEN_AGENT_MODEL",
        "setup_command": 'setx OPENAI_API_KEY "你的_API_Key"',
        "settings_path": str(AGENT_CONFIG_PATH),
        "restart_required": False,
    }


def _build_research_agent_reply(
    *,
    message: str,
    history: list[Any],
    current_brief: str,
) -> dict[str, Any]:
    config = _agent_llm_config()
    if not config:
        return _agent_unavailable_payload("OPENAI_API_KEY is not set.")
    try:
        return _call_research_agent_llm(
            message=message,
            history=history,
            current_brief=current_brief,
            config=config,
        )
    except Exception as exc:
        return _agent_unavailable_payload(f"{type(exc).__name__}: {exc}")


def _agent_unavailable_payload(error: str) -> dict[str, Any]:
    lower_error = error.lower()
    if "timed out" in lower_error or "timeouterror" in lower_error:
        reply = "Research Agent 暂不可用：模型请求超时，已停止等待；请重试或切换快模型。"
    elif "api_key" in lower_error or "401" in lower_error or "unauthorized" in lower_error:
        reply = "Agent 未连接：请先配置 OPENAI_API_KEY，重启本软件后再试。"
    else:
        reply = "Agent 暂不可用：请检查 API Key、Base URL、模型名称和网络连接。"
    return {
        "include_in_brief": False,
        "reply": reply,
        "normalized_requirement": "",
        "task_name_hint": "",
        "replace_brief": False,
        "model": "llm-unavailable",
        "model_error": error,
    }


def _call_research_agent_llm(
    *,
    message: str,
    history: list[Any],
    current_brief: str,
    config: dict[str, str],
) -> dict[str, Any]:
    compact_history = []
    for turn in history[-4:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        text = str(turn.get("text") or "").strip()
        if role in {"user", "agent"} and text:
            compact_history.append({"role": role, "text": text[:180]})

    system_prompt = (
        "You are a terse keyword assistant inside a local DOI app. "
        "Classify whether the latest user message is a real academic literature search requirement or keyword refinement. "
        "A message is a valid research requirement when it contains a technical concept, method, material, disease, task, field, venue constraint, year range, or asks for papers/literature/DOI, even if the wording is loose or has typos. "
        "Examples of valid requirements: 'Transformer 在计算机视觉中的高影响力论文', 'polyurethane waterproof latent curing coating', '近五年 CO2RR Nature/JACS papers'. "
        "When the user gives a method plus a field or task, keep it as the research topic; do not ask them to repeat the topic. "
        "If current_research_brief is not empty, short keyword commands such as 'top5', '给我关键词', or '随便找几个' are keyword refinements, not random chat. "
        "Journal whitelists, venue scopes, year ranges, exclusion criteria, ranking preferences, and source filters are research constraints; include them in the brief even if the research topic is still missing, then ask for the missing topic. "
        "Only explicit decision commands such as '确认关键词', '就用这组关键词', '开始检索', or '导入下载队列' are confirmations; vague acknowledgements such as '好的', '可以', or '嗯' are not confirmations. "
        "Greetings, identity questions, capability questions, model/API questions, random text, and meta chat are not research requirements. "
        "Return JSON only with keys: include_in_brief(boolean), reply(string), normalized_requirement(string), task_name_hint(string). "
        "If include_in_brief is false, normalized_requirement must be empty. "
        "If include_in_brief is true, normalized_requirement must be a compact search brief or keyword list that preserves every concrete constraint from the user: years/time range, field/task, method, material, journals, ranking preference, and exclusions. "
        "Reply in Simplified Chinese by default. Preserve technical terms in English. "
        "Maximum reply length: 80 Chinese characters or 45 English words. "
        "Do not introduce yourself. Do not repeat workflow warnings. Do not mention PDF download unless the user asks. "
        "If user asks for keywords/top N and the message also contains a research topic, set include_in_brief=true and put the topic plus keyword intent in normalized_requirement. "
        "If user explicitly confirms the keyword plan, set include_in_brief=false, normalized_requirement='', and reply with a short confirmation such as '已确认当前关键词方案'."
    )
    user_payload = {
        "current_research_brief": current_brief[:800],
        "recent_history": compact_history,
        "latest_user_message": message,
    }
    body = {
        "model": config["model"],
        "temperature": 0.1,
        "max_tokens": 220,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    url = f"{config['base_url']}/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = max(1.0, _safe_float(config.get("request_timeout_seconds", ""), DEFAULT_AGENT_REQUEST_TIMEOUT_SECONDS))
    with _open_agent_request_with_retry(request, config=config, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    content = str(payload["choices"][0]["message"]["content"]).strip()
    parsed = _parse_agent_json(content)
    return _normalize_agent_payload(parsed, model=config["model"], message=message, current_brief=current_brief)


def _open_agent_request_with_retry(
    request: urllib.request.Request,
    *,
    config: dict[str, str],
    timeout: float,
) -> Any:
    attempts = max(1, min(8, _safe_int(config.get("retry_attempts", ""), DEFAULT_AGENT_RETRY_ATTEMPTS)))
    base_delay = max(0.0, min(8.0, _safe_float(config.get("retry_base_delay_seconds", ""), DEFAULT_AGENT_RETRY_BASE_DELAY_SECONDS)))
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if not _should_retry_agent_error(exc) or attempt >= attempts:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
        if base_delay:
            time.sleep(base_delay * (2 ** (attempt - 1)))
    if last_error:
        raise last_error
    raise RuntimeError("Agent request retry loop exited unexpectedly.")


def _should_retry_agent_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _parse_agent_json(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if match:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Agent model did not return a JSON object.")


def _normalize_agent_payload(
    payload: dict[str, Any],
    *,
    model: str,
    message: str = "",
    current_brief: str = "",
) -> dict[str, Any]:
    include = bool(payload.get("include_in_brief"))
    reply = str(payload.get("reply") or "").strip()
    normalized = str(payload.get("normalized_requirement") or "").strip()
    task_name = str(payload.get("task_name_hint") or "").strip()
    replace_brief = bool(payload.get("replace_brief")) or _is_switch_request(message)
    if not include and _looks_like_research_requirement(message):
        include = True
        normalized = message
    if normalized:
        normalized = _normalize_research_brief(normalized)
    if include:
        normalized = _preserve_user_research_constraints(normalized, message)
    if include and not normalized:
        include = False
    if not reply:
        reply = "已加入简报。" if include else "我是选题研判助手，负责提炼检索关键词。"
    if include and _reply_only_asks_for_more(reply):
        reply = "已记录研究需求。可继续补充期刊、年份或排除条件，也可以生成关键词草案。"
    return {
        "include_in_brief": include,
        "reply": _shorten_agent_reply(reply),
        "normalized_requirement": normalized[:700] if include else "",
        "task_name_hint": task_name[:80],
        "replace_brief": replace_brief,
        "model": model,
    }


def _preserve_user_research_constraints(normalized: str, message: str) -> str:
    original_text = " ".join(str(message or "").split())
    user_text = _normalize_research_brief(original_text)
    if not user_text:
        return normalized
    if not normalized:
        return user_text
    if _has_non_ascii_research_text(original_text) and original_text not in normalized:
        return f"{normalized}；用户原始需求：{original_text}"
    if _has_non_ascii_research_text(user_text) and user_text not in normalized:
        return f"{normalized}；用户原始需求：{user_text}"
    normalized_lower = normalized.lower()
    missing_fragments: list[str] = []
    for fragment in _important_research_fragments(user_text):
        if fragment.lower() not in normalized_lower:
            missing_fragments.append(fragment)
    if not missing_fragments:
        return normalized
    return f"{normalized}；保留用户约束：{'；'.join(missing_fragments)}"


def _has_non_ascii_research_text(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def _reply_only_asks_for_more(reply: str) -> bool:
    text = str(reply or "")
    return bool(re.search(r"请.*(补充|补全|提供|限定|说明|明确)", text))


def _looks_like_research_requirement(message: str) -> bool:
    text = str(message or "").strip()
    if len(text) < 3:
        return False
    lower = text.lower()
    research_markers = [
        "paper", "papers", "literature", "doi", "review", "journal", "nature", "science",
        "jacs", "transformer", "machine learning", "deep learning", "computer vision",
        "polyurethane", "coating", "catalysis", "co2rr",
        "论文", "文献", "期刊", "研究", "关键词", "近五年", "计算机视觉", "高影响力",
    ]
    return any(marker in lower for marker in research_markers)


def _important_research_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    patterns = [
        r"近\s*[一二三四五六七八九十\d]+\s*年",
        r"20\d{2}\s*[-至到]\s*20\d{2}",
        r"Transformer",
        r"computer vision|计算机视觉",
        r"high impact|高影响力|top|顶刊",
        r"Nature|Science|Cell|Joule|JACS|Angewandte|Chemical Reviews|Advanced Materials",
        r"polyurethane|waterproof|latent curing|coating|CO2RR|photocatalysis|electrocatalysis",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = " ".join(match.group(0).split())
            if value and value.lower() not in {item.lower() for item in fragments}:
                fragments.append(value)
    return fragments[:12]


def _shorten_agent_reply(value: str, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，,；;。 ") + "。"


def _normalize_research_brief(value: str) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"^(算了|不用了|不是|不对)[，,。；;\s]*(换成|改成|改为|看|找)?", "", text).strip()
    text = re.sub(r"^(换成|改成|改为)", "", text).strip()
    text = re.sub(r"^(switch\s+to|change\s+to|replace\s+with)\s+", "", text, flags=re.I).strip()
    text = re.sub(r"^(就|那个|这个)[，,。；;\s]*", "", text).strip()
    replacements = {
        "transoer": "Transformer",
        "transfomer": "Transformer",
        "tranformer": "Transformer",
        "trasnformer": "Transformer",
        "transformers": "Transformer",
        "transformer": "Transformer",
        "计算机的": "计算机方向",
    }
    for wrong, right in replacements.items():
        text = re.sub(re.escape(wrong), right, text, flags=re.I)
    return text.strip()


def _keyword_terms_from_brief(brief: str) -> list[str]:
    text = _normalize_research_brief(brief)
    text_for_terms = _normalize_research_brief(_strip_research_filter_text(str(brief or "")))
    lower_terms = text_for_terms.lower()
    terms: list[str] = []

    def add(term: str) -> None:
        cleaned = " ".join(term.strip(" ,;:：，。；").split())
        if _is_filter_only_term(cleaned):
            return
        if cleaned and cleaned.lower() not in {item.lower() for item in terms}:
            terms.append(cleaned)

    if not text_for_terms.strip():
        return []

    if "transformer" in lower_terms:
        add("Transformer")
        if re.search(r"计算机|computer|cs|ai|人工智能|机器学习|深度学习", text_for_terms, re.I):
            add("Transformer architecture")
            add("attention mechanism")
            add("natural language processing")
        if re.search(r"视觉|vision|image|图像", text_for_terms, re.I):
            add("computer vision")
            add("vision transformer")
        else:
            add("attention mechanism")
            add("deep learning")
    if re.search(r"polyurethane|聚氨酯", text_for_terms, re.I):
        add("polyurethane")
    if re.search(r"waterproof|防水", text_for_terms, re.I):
        add("waterproof coating")
    if re.search(r"latent curing|潜伏固化", text_for_terms, re.I):
        add("latent curing")
    if re.search(r"coating|涂层", text_for_terms, re.I):
        add("coating")

    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-&]{2,}(?:\s+[A-Za-z][A-Za-z0-9\-&]{2,}){0,2}", text_for_terms):
        normalized_token = token.lower()
        if normalized_token not in {"the", "and", "for", "with", "from", "paper", "papers", "article", "review"}:
            add(token)

    if re.search(r"近[一二三四五六七八九十\d]+年|20\d{2}|recent|latest", text, re.I) and terms:
        add("recent studies")
    return terms[:10]


def _strip_research_filter_text(text: str) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    filter_markers = (
        "journal whitelist",
        "期刊白名单",
        "venue whitelist",
        "venue scope",
        "source filter",
        "journal filter",
        "优先只收这些",
        "如果数量太少",
        "再放宽到",
        "fallback",
    )
    marker_pattern = "|".join(re.escape(marker) for marker in filter_markers)
    kept_chunks: list[str] = []
    for chunk in re.split(r"\n\s*\n+", cleaned):
        candidate = chunk.strip()
        if not candidate:
            continue
        marker_match = re.search(rf"({marker_pattern})[:：]?", candidate, flags=re.I)
        if marker_match:
            prefix = candidate[: marker_match.start()].strip(" ,;:：，。；")
            if prefix and _looks_like_research_topic(prefix):
                kept_chunks.append(prefix)
            continue
        for venue in sorted(_VENUE_FILTER_TERMS, key=len, reverse=True):
            candidate = re.sub(rf"\b{re.escape(venue)}\b", " ", candidate, flags=re.I)
        candidate = re.sub(
            r"(优先|只收|这些|如果数量太少|再放宽到|顶刊|系列|白名单|期刊|来源|限制|fallback)",
            " ",
            candidate,
            flags=re.I,
        )
        candidate = " ".join(candidate.replace("/", " ").replace("，", " ").replace(",", " ").split())
        if candidate and _looks_like_research_topic(candidate):
            kept_chunks.append(candidate)
    return " ".join(kept_chunks)


def _looks_like_research_topic(text: str) -> bool:
    candidate = " ".join(str(text or "").split())
    if not candidate:
        return False
    if re.search(
        r"transformer|attention|vision|computer|polyurethane|waterproof|coating|curing|catalyst|catalysis|battery|machine learning|deep learning|"
        r"计算机|视觉|图像|材料|涂层|防水|聚氨酯|固化|催化|电催化|光催化|电池|反应|体系|应用|方法",
        candidate,
        re.I,
    ):
        return True
    return bool(re.search(r"[A-Za-z][A-Za-z0-9\-]{3,}", candidate)) and not _is_filter_only_term(candidate)


_VENUE_FILTER_TERMS = {
    "journal whitelist",
    "venue scope",
    "source filter",
    "fallback",
    "nature",
    "nature energy",
    "nature catalysis",
    "nature chemical engineering",
    "nature synthesis",
    "nature materials",
    "nature nanotechnology",
    "nature communications",
    "nature sustainability",
    "nature reviews chemistry",
    "science",
    "science advances",
    "cell",
    "joule",
    "chem",
    "chem catalysis",
    "jacs",
    "angewandte chemie",
    "chemical reviews",
    "chemical society reviews",
    "energy environmental science",
    "energy & environmental science",
    "acs energy letters",
    "acs catalysis",
    "advanced materials",
    "advanced energy materials",
    "applied catalysis b",
    "nano letters",
    "nano energy",
    "matter",
    "chemsuschem",
    "green chemistry",
}


def _is_filter_only_term(term: str) -> bool:
    normalized = " ".join(str(term or "").lower().replace("&", " ").split())
    normalized_amp = " ".join(str(term or "").lower().split())
    if not normalized:
        return True
    normalized_venues = {" ".join(venue.replace("&", " ").split()) for venue in _VENUE_FILTER_TERMS}
    if normalized in normalized_venues or normalized_amp in _VENUE_FILTER_TERMS:
        return True
    return any(normalized.startswith(f"{venue} ") for venue in normalized_venues)


def _is_switch_request(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.search(r"^(算了|不用了|不是|不对)?[，,。；;\s]*(换成|改成|改为)|^(switch\s+to|change\s+to|replace\s+with)\b", text, re.I))


def _resolve_research_query_text(state: dict[str, Any]) -> str:
    research_text = str(state.get("research_query_text") or "").strip()
    if research_text:
        return research_text
    return str(state.get("input_text") or "").strip()


def _merge_research_brief(current: str, addition: str, *, replace: bool = False) -> str:
    cleaned = str(addition or "").strip()
    if not cleaned:
        return str(current or "").strip()
    if replace:
        return cleaned
    existing = str(current or "").strip()
    if not existing:
        return cleaned
    if cleaned.lower() in existing.lower():
        return existing
    return f"{existing}\n\n{cleaned}"


def _parse_terms_text(raw_text: str) -> list[str]:
    terms: list[str] = []
    for part in raw_text.replace(",", "\n").replace(";", "\n").splitlines():
        cleaned = " ".join(part.strip().split())
        if not cleaned:
            continue
        if cleaned.lower() not in {item.lower() for item in terms}:
            terms.append(cleaned)
    return terms


def _slugify_task_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {" ", "-", "_"} else " " for ch in value)
    collapsed = "_".join(part for part in cleaned.strip().split() if part)
    return collapsed[:80] or "task"


def _guess_task_label(state: dict[str, Any]) -> str:
    explicit = str(state.get("task_name") or "").strip()
    if explicit:
        return explicit
    research = str(state.get("research_query_text") or "").strip()
    if research:
        return research[:80]
    parsed_rows = _parse_input_text(str(state.get("input_text") or ""))
    if parsed_rows:
        if len(parsed_rows) == 1 and parsed_rows[0].get("doi"):
            return f"manual_doi_{parsed_rows[0]['doi']}"
        return f"manual_doi_batch_{len(parsed_rows)}"
    return _default_task_name()


def _guess_keyword_label(state: dict[str, Any]) -> str:
    terms = _parse_terms_text(str(state.get("research_confirmed_terms_text") or ""))
    if not terms:
        terms = _parse_terms_text(str(state.get("research_query_text") or ""))
    if not terms:
        return ""
    return "_".join(terms[:6])


def _build_task_output_dir(state: dict[str, Any]) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_label = _guess_task_label(state)
    keyword_label = _guess_keyword_label(state)
    prefix = "topic" if str(state.get("research_query_text") or "").strip() else "manual"
    slug_parts = [_slugify_task_name(task_label)]
    if keyword_label:
        slug_parts.append(f"kw_{_slugify_task_name(keyword_label)}")
    slug = "_".join(part for part in slug_parts if part)
    return str((OUTPUTS_DIR / f"{prefix}_{slug}_{timestamp}").resolve())


def _research_state_payload(state: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "query_text": str(state.get("research_query_text") or ""),
        "search_strategy": _normalize_research_search_strategy(state.get("research_search_strategy")),
        "limit_per_provider": str(state.get("research_limit_per_provider") or str(DEFAULT_RESEARCH_HARVEST_CAP)),
        "provider_ids": _selected_research_provider_ids(state),
        "status_text": str(state.get("research_status_text") or "等待研究需求"),
        "summary_text": str(state.get("research_summary_text") or "先在 Research Agent 中描述论文方向，再生成关键词草案。"),
        "confirmed_terms_text": str(state.get("research_confirmed_terms_text") or ""),
        "confirmed_query_text": str(state.get("research_confirmed_query_text") or ""),
        "keywords_confirmed": bool(state.get("research_keywords_confirmed")),
        "title_review_status_text": str(state.get("research_title_review_status_text") or "尚未校验题名"),
        "title_review_summary_text": str(state.get("research_title_review_summary_text") or "检索 DOI 候选后，可用题名校验判断返回记录是否匹配。"),
        "title_review_items": [],
        "keyword_set_id": str(state.get("research_last_keyword_set_id") or ""),
        "run_id": str(state.get("research_last_run_id") or ""),
        "include_terms": [],
        "provider_stats": [],
        "records": [],
        "records_oa": [],
        "records_non_oa": [],
        "records_unknown_oa": [],
        "oa_summary": {"oa_count": 0, "non_oa_count": 0, "unknown_oa_count": 0},
        "doi_files": {
            "all": str(state.get("research_doi_file_all") or ""),
            "oa": str(state.get("research_doi_file_oa") or ""),
            "non_oa": str(state.get("research_doi_file_non_oa") or ""),
            "unknown": str(state.get("research_doi_file_unknown") or ""),
            "csv": str(state.get("research_doi_file_csv") or ""),
        },
        "agent_config": _agent_config_status(),
    }
    keyword_id_text = payload["keyword_set_id"]
    run_id_text = payload["run_id"]
    try:
        if keyword_id_text:
            store = SQLiteStore(OUTPUTS_DIR / "app_state.db")
            keyword_set = store.get_keyword_set(int(keyword_id_text))
            payload["include_terms"] = list(keyword_set.include_terms)
    except Exception:
        pass
    try:
        if run_id_text:
            payload.update(_load_research_run_snapshot(int(run_id_text)))
    except Exception:
        pass
    try:
        payload["title_review_items"] = json.loads(str(state.get("research_title_review_items_json") or "[]"))
    except Exception:
        payload["title_review_items"] = []
    payload["progress"] = _parse_json_object(str(state.get("research_progress_json") or "{}"))
    return payload


def _normalize_title(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return " ".join(cleaned.split())


def _fetch_crossref_title_for_doi(doi: str) -> str:
    encoded = urllib.parse.quote(doi, safe="")
    url = f"https://api.crossref.org/works/{encoded}"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    message = payload.get("message", {})
    title = message.get("title") or []
    if isinstance(title, list):
        return str(title[0] or "").strip()
    return str(title or "").strip()


def _review_agent_cleanup_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    noise_count = 0
    for record in records:
        doi = str(record.get("doi") or "").strip().lower()
        title = str(record.get("title") or "").strip()
        if _is_review_noise_title(title):
            noise_count += 1
            continue
        key = f"doi:{doi}" if doi else f"title:{_normalize_title(title)}:{record.get('year') or ''}"
        if not key.strip(":") or key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        cleaned.append(record)
    cleaned.sort(key=lambda item: (0 if str(item.get("doi") or "").strip() else 1, str(item.get("year") or "")), reverse=False)
    return {
        "input_count": len(records),
        "output_count": len(cleaned),
        "duplicate_count": duplicate_count,
        "noise_count": noise_count,
        "records": cleaned,
    }


def _is_review_noise_title(title: str) -> bool:
    normalized = _normalize_title(title)
    if not normalized:
        return True
    blocked_exact = {
        "copyright",
        "index",
        "front matter",
        "back matter",
        "table of contents",
        "contents",
        "preface",
        "foreword",
    }
    return normalized in blocked_exact


def _review_record_title(record: dict[str, Any]) -> dict[str, Any]:
    doi = str(record.get("doi") or "").strip().lower()
    observed_title = str(record.get("title") or "").strip()
    if not doi:
        return {
            "doi": doi,
            "provider_id": str(record.get("provider_id") or ""),
            "observed_title": observed_title,
            "reference_title": "",
            "status": "missing_doi",
            "score": 0.0,
            "note": "No DOI available for title review.",
        }
    if not observed_title:
        return {
            "doi": doi,
            "provider_id": str(record.get("provider_id") or ""),
            "observed_title": "",
            "reference_title": "",
            "status": "missing_title",
            "score": 0.0,
            "note": "The provider record does not include a title.",
        }
    try:
        reference_title = _fetch_crossref_title_for_doi(doi)
    except Exception as exc:
        return {
            "doi": doi,
            "provider_id": str(record.get("provider_id") or ""),
            "observed_title": observed_title,
            "reference_title": "",
            "status": "lookup_failed",
            "score": 0.0,
            "note": f"Crossref lookup failed: {type(exc).__name__}",
        }

    normalized_observed = _normalize_title(observed_title)
    normalized_reference = _normalize_title(reference_title)
    score = round(difflib.SequenceMatcher(None, normalized_observed, normalized_reference).ratio(), 3)
    if normalized_observed == normalized_reference:
        status = "exact_match"
        note = "Observed title exactly matches Crossref."
    elif normalized_observed and normalized_reference and (
        normalized_observed in normalized_reference
        or normalized_reference in normalized_observed
        or score >= 0.88
    ):
        status = "likely_match"
        note = "Observed title is very close to Crossref."
    else:
        status = "mismatch"
        note = "Observed title differs from Crossref and should be reviewed."
    return {
        "doi": doi,
        "provider_id": str(record.get("provider_id") or ""),
        "observed_title": observed_title,
        "reference_title": reference_title,
        "status": status,
        "score": score,
        "note": note,
    }


def _load_research_run_snapshot(run_id: int) -> dict[str, Any]:
    store = SQLiteStore(OUTPUTS_DIR / "app_state.db")
    with store.connect() as conn:
        stats_rows = conn.execute(
            """
            select provider_id, display_name, reported_total_count, returned_count, doi_count,
                   download_candidate_count, error_count, status, elapsed_seconds
            from provider_stats
            where search_run_id = ?
            order by id asc
            """,
            (run_id,),
        ).fetchall()
        record_rows = conn.execute(
            """
            select provider_id, provider_item_id, title, doi, url, authors_json, year, venue, abstract, raw_json
            from search_records
            where search_run_id = ?
            order by id asc
            """,
            (run_id,),
        ).fetchall()
    provider_stats = [
        {
            "provider_id": str(row["provider_id"]),
            "display_name": str(row["display_name"]),
            "reported_total_count": row["reported_total_count"],
            "returned_count": int(row["returned_count"]),
            "doi_count": int(row["doi_count"]),
            "download_candidate_count": int(row["download_candidate_count"]),
            "error_count": int(row["error_count"]),
            "status": str(row["status"]),
            "elapsed_seconds": float(row["elapsed_seconds"]),
        }
        for row in stats_rows
    ]
    records = []
    for row in record_rows:
        raw = _parse_json_object(str(row["raw_json"] or "{}"))
        oa = _classify_oa_record(str(row["provider_id"]), raw)
        record = {
            "provider_id": str(row["provider_id"]),
            "provider_item_id": str(row["provider_item_id"]),
            "title": str(row["title"]),
            "doi": str(row["doi"]),
            "url": str(row["url"]),
            "authors": json.loads(str(row["authors_json"] or "[]")),
            "year": str(row["year"]),
            "venue": str(row["venue"]),
            "abstract": str(row["abstract"]),
            "oa_layer": oa["layer"],
            "oa_status": oa["status"],
            "pdf_url": oa["pdf_url"],
            "landing_page_url": oa["landing_page_url"],
        }
        records.append(record)
    oa_records = [record for record in records if record.get("oa_layer") == "oa"]
    non_oa_records = [record for record in records if record.get("oa_layer") == "non_oa"]
    unknown_records = [record for record in records if record.get("oa_layer") == "unknown"]
    return {
        "provider_stats": provider_stats,
        "records": records,
        "records_oa": oa_records,
        "records_non_oa": non_oa_records,
        "records_unknown_oa": unknown_records,
        "oa_summary": {
            "oa_count": len(oa_records),
            "non_oa_count": len(non_oa_records),
            "unknown_oa_count": len(unknown_records),
        },
    }


def _classify_oa_record(provider_id: str, raw: dict[str, Any]) -> dict[str, str]:
    layer = "unknown"
    status = ""
    pdf_url = ""
    landing_page_url = ""
    if provider_id == "openalex":
        open_access = raw.get("open_access") if isinstance(raw.get("open_access"), dict) else {}
        status = str(open_access.get("oa_status") or "")
        if open_access.get("is_oa") is True:
            layer = "oa"
        elif open_access.get("is_oa") is False:
            layer = "non_oa"
        for location_key in ("best_oa_location", "primary_location"):
            location = raw.get(location_key) if isinstance(raw.get(location_key), dict) else {}
            if not pdf_url:
                pdf_url = str(location.get("pdf_url") or "")
            if not landing_page_url:
                landing_page_url = str(location.get("landing_page_url") or "")
            if layer != "oa" and location.get("is_oa") is True:
                layer = "oa"
        if layer == "oa" and not status:
            status = "oa"
    elif provider_id == "crossref":
        for license_item in raw.get("license") or []:
            if not isinstance(license_item, dict):
                continue
            url = str(license_item.get("URL") or license_item.get("url") or "")
            if "creativecommons.org" in url.lower():
                layer = "oa"
                status = "license"
                landing_page_url = landing_page_url or url
                break
        for link_item in raw.get("link") or []:
            if not isinstance(link_item, dict):
                continue
            content_type = str(link_item.get("content-type") or "").lower()
            url = str(link_item.get("URL") or link_item.get("url") or "")
            if "pdf" in content_type and not pdf_url:
                pdf_url = url
        if not status and pdf_url:
            status = "pdf_link_unverified"
    return {
        "layer": layer,
        "status": status or layer,
        "pdf_url": pdf_url,
        "landing_page_url": landing_page_url,
    }


def _write_research_doi_exports(state: dict[str, Any], run_id: int, records: list[dict[str, Any]]) -> dict[str, str]:
    export_dir = Path(str(state.get("output_dir") or _build_task_output_dir(state))).expanduser().resolve()
    export_dir.mkdir(parents=True, exist_ok=True)
    cleaned = _review_agent_cleanup_records(records)
    export_records = cleaned["records"]
    groups = {
        "all": export_records,
        "oa": [record for record in export_records if record.get("oa_layer") == "oa"],
        "non_oa": [record for record in export_records if record.get("oa_layer") != "oa"],
        "unknown": [record for record in export_records if record.get("oa_layer") == "unknown"],
    }
    paths = {
        "all": export_dir / f"run_{run_id}_doi_all.txt",
        "oa": export_dir / f"run_{run_id}_OA_DOI_list.txt",
        "non_oa": export_dir / f"run_{run_id}_non_OA_DOI_list.txt",
        "unknown": export_dir / f"run_{run_id}_doi_unknown_oa.txt",
        "csv": export_dir / f"run_{run_id}_doi_candidates.csv",
    }
    for key, group_records in groups.items():
        paths[key].write_text(_doi_lines(group_records), encoding="utf-8")
    with paths["csv"].open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["oa_layer", "oa_status", "year", "doi", "title", "venue", "provider_id", "url", "pdf_url", "landing_page_url"],
        )
        writer.writeheader()
        for record in export_records:
            writer.writerow({
                "oa_layer": record.get("oa_layer") or "unknown",
                "oa_status": record.get("oa_status") or "",
                "year": record.get("year") or "",
                "doi": record.get("doi") or "",
                "title": record.get("title") or "",
                "venue": record.get("venue") or "",
                "provider_id": record.get("provider_id") or "",
                "url": record.get("url") or "",
                "pdf_url": record.get("pdf_url") or "",
                "landing_page_url": record.get("landing_page_url") or "",
            })
    return {key: str(path) for key, path in paths.items()}


def _doi_lines(records: list[dict[str, Any]]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for record in records:
        doi = str(record.get("doi") or "").strip()
        if doi and doi.lower() not in seen:
            seen.add(doi.lower())
            lines.append(doi)
    return "\n".join(lines) + ("\n" if lines else "")


def _safe_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _safe_float(value: str, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _parse_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


class LigenRequestHandler(BaseHTTPRequestHandler):
    server_version = "LigenLocalWeb/0.1"

    def do_GET(self) -> None:
        controller: LigenWebController = self.server.controller  # type: ignore[attr-defined]
        route_path = urllib.parse.urlparse(self.path).path
        if route_path == "/":
            self._send_html(controller.render_html())
            return
        if route_path.startswith("/assets/"):
            self._send_static_asset(route_path.removeprefix("/assets/"))
            return
        if route_path == "/api/state":
            self._send_json(controller.get_state_payload())
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, f"Unknown route: {route_path}")

    def do_POST(self) -> None:
        controller: LigenWebController = self.server.controller  # type: ignore[attr-defined]
        try:
            payload = self._read_json_body()
            if self.path == "/api/analyze":
                self._send_json(controller.analyze(payload))
                return
            if self.path == "/api/research/agent-turn":
                self._send_json(controller.handle_research_agent_turn(payload))
                return
            if self.path == "/api/research/draft":
                self._send_json(controller.create_research_draft(payload))
                return
            if self.path == "/api/research/search":
                self._send_json(controller.run_research_search(payload))
                return
            if self.path == "/api/research/confirm":
                self._send_json(controller.confirm_research_terms(payload))
                return
            if self.path == "/api/research/confirm-and-search":
                self._send_json(controller.confirm_and_run_research_search(payload))
                return
            if self.path == "/api/research/review-titles":
                self._send_json(controller.review_research_titles(payload))
                return
            if self.path == "/api/research/use-results":
                self._send_json(controller.use_research_results_as_input(payload))
                return
            if self.path == "/api/research/open-doi-file":
                self._send_json(controller.open_research_doi_file(payload))
                return
            if self.path == "/api/start":
                self._send_json(controller.start(payload))
                return
            if self.path == "/api/stop":
                self._send_json(controller.stop())
                return
            if self.path == "/api/open-output":
                self._send_json(controller.open_output_dir(payload))
                return
            if self.path == "/api/open-results":
                self._send_json(controller.open_results_csv())
                return
            self._send_error_json(HTTPStatus.NOT_FOUND, f"Unknown route: {self.path}")
        except FileNotFoundError as exc:
            self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_static_asset(self, relative_path: str) -> None:
        safe_parts = [part for part in relative_path.split("/") if part not in {"", ".", ".."}]
        target: Path | None = None
        assets_root = (WEB_FRONTEND_DIST_DIR / "assets").resolve()
        candidate = (assets_root / Path(*safe_parts)).resolve()
        if (assets_root in candidate.parents or candidate == assets_root) and candidate.exists() and candidate.is_file():
            target = candidate
        if target is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Asset not found.")
            return
        body = target.read_bytes()
        mime_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = False,
) -> None:
    controller = LigenWebController()
    with ThreadingHTTPServer((host, port), LigenRequestHandler) as httpd:
        httpd.controller = controller  # type: ignore[attr-defined]
        url = f"http://{host}:{port}/"
        print(f"Ligen Local Web listening on {url}")
        print(f"Skill root: {SKILL_ROOT}")
        print(f"Outputs dir: {OUTPUTS_DIR}")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
            print("Server stopped.")


def self_test() -> None:
    controller = LigenWebController()
    payload = controller.self_test()
    print("Web self-test OK")
    print(f"Default output dir: {payload.get('output_dir')}")
