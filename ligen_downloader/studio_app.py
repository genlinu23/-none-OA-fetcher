from __future__ import annotations

import csv
import json
import os
import queue
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
import tkinter as tk
from tkinter import ttk
import urllib.request

from .app import KeywordService
from .app import SearchWorkflow
from .gui_app import DEFAULT_PORT_MAP
from .gui_app import OUTPUTS_DIR
from .gui_app import ProcessBridge
from .storage import SQLiteStore
from .utils import extract_doi_like
from .utils import infer_publisher


STATE_PATH = OUTPUTS_DIR / "studio_client_state.json"


def _resolve_skill_root() -> Path:
    env_root = os.environ.get("LIGEN_SKILL_ROOT", "").strip()
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.extend(
        [
            Path(__file__).resolve().parents[1],
            Path(sys.executable).resolve().parent,
            Path(sys.executable).resolve().parent.parent,
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


SKILL_ROOT = _resolve_skill_root()
SCRIPT_MODE = SKILL_ROOT / "scripts" / "run_ligen_script_mode.py"
ANALYZE_RUN = SKILL_ROOT / "scripts" / "analyze_ligen_download_run.py"
PYTHON_EXE = _resolve_python_exe()


class LigenStudioApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Ligen Studio")
        self.root.geometry("1360x860")
        self.root.minsize(1120, 720)

        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.bridge = ProcessBridge()
        self.app_store = SQLiteStore(OUTPUTS_DIR / "app_state.db")
        self.keyword_service = KeywordService(self.app_store)
        self.search_workflow = SearchWorkflow(self.app_store)
        self.rows: list[dict[str, str]] = []
        self.current_run_dir: Path | None = None

        self._build_vars()
        self._build_style()
        self._build_layout()
        self._apply_state()
        self.analyze_input()
        self.refresh_ports()
        self.root.after(200, self._poll_bridge)

    def _build_vars(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir_var = tk.StringVar(
            value=str(self.state.get("output_dir") or (OUTPUTS_DIR / f"studio_run_{timestamp}"))
        )
        self.phase_var = tk.StringVar(value=self.state.get("phase", "warmup"))
        self.status_var = tk.StringVar(value="Ready")
        self.summary_var = tk.StringVar(value="Paste DOI lines, then analyze the batch.")
        self.count_var = tk.StringVar(value="0 items")
        self.command_var = tk.StringVar(value="")
        self.launch_chrome_var = tk.BooleanVar(value=bool(self.state.get("launch_chrome", True)))
        self.resume_var = tk.BooleanVar(value=bool(self.state.get("resume_existing", True)))
        self.keep_tabs_var = tk.BooleanVar(value=bool(self.state.get("keep_existing_tabs", True)))
        self.dry_run_var = tk.BooleanVar(value=bool(self.state.get("dry_run", False)))
        self.max_parallel_var = tk.StringVar(value=str(self.state.get("max_parallel_publishers", "3")))
        self.max_warmup_var = tk.StringVar(value=str(self.state.get("max_warmup_per_publisher", "1")))
        self.page_settle_var = tk.StringVar(value=str(self.state.get("page_settle_seconds", "6")))
        self.per_doi_timeout_var = tk.StringVar(value=str(self.state.get("per_doi_timeout_seconds", "240")))
        self.research_status_var = tk.StringVar(value="Draft a research query, then lock and search.")
        self.research_summary_var = tk.StringVar(value="No research search run yet.")
        self.research_limit_var = tk.StringVar(value=str(self.state.get("research_limit_per_provider", "20")))
        self.research_crossref_var = tk.BooleanVar(value=bool(self.state.get("research_provider_crossref", True)))
        self.research_openalex_var = tk.BooleanVar(value=bool(self.state.get("research_provider_openalex", True)))
        self.research_local_var = tk.BooleanVar(value=bool(self.state.get("research_provider_local_manual", False)))
        self.port_vars = {
            publisher: tk.StringVar(
                value=str(self.state.get("port_map", {}).get(publisher, default_port))
            )
            for publisher, default_port in DEFAULT_PORT_MAP.items()
        }

    def _build_style(self) -> None:
        self.colors = {
            "canvas": "#ebe7dd",
            "rail": "#17202a",
            "panel": "#fffdf8",
            "panel_alt": "#f7f4ed",
            "ink": "#17202a",
            "muted": "#68717d",
            "accent": "#0f766e",
            "accent_hover": "#115e59",
            "warning": "#b45309",
            "danger": "#b91c1c",
            "line": "#d8d1c3",
        }
        self.root.configure(bg=self.colors["canvas"])
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 9), background=self.colors["canvas"], foreground=self.colors["ink"])
        style.configure("Canvas.TFrame", background=self.colors["canvas"])
        style.configure("Rail.TFrame", background=self.colors["rail"])
        style.configure("Panel.TFrame", background=self.colors["panel"])
        style.configure("Soft.TFrame", background=self.colors["panel_alt"])
        style.configure("Title.TLabel", background=self.colors["rail"], foreground="#fff7ed", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Rail.TLabel", background=self.colors["rail"], foreground="#d9e2e4", font=("Microsoft YaHei UI", 9))
        style.configure("PanelTitle.TLabel", background=self.colors["panel"], foreground=self.colors["ink"], font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("SoftTitle.TLabel", background=self.colors["panel_alt"], foreground=self.colors["ink"], font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Muted.TLabel", background=self.colors["panel"], foreground=self.colors["muted"])
        style.configure("SoftMuted.TLabel", background=self.colors["panel_alt"], foreground=self.colors["muted"])
        style.configure("Status.TLabel", background=self.colors["rail"], foreground="#a7f3d0", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", background=self.colors["accent"], foreground="white", padding=(14, 9), borderwidth=0)
        style.map("Primary.TButton", background=[("active", self.colors["accent_hover"])])
        style.configure("Warn.TButton", background=self.colors["warning"], foreground="white", padding=(14, 9), borderwidth=0)
        style.map("Warn.TButton", background=[("active", "#92400e")])
        style.configure("Ghost.TButton", background=self.colors["panel_alt"], foreground=self.colors["ink"], padding=(10, 7), borderwidth=0)
        style.map("Ghost.TButton", background=[("active", "#eee7d8")])
        style.configure("TNotebook", background=self.colors["panel"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 8), background=self.colors["panel_alt"])
        style.map("TNotebook.Tab", background=[("selected", self.colors["panel"])])
        style.configure("Treeview", rowheight=26, font=("Cascadia Mono", 9), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_layout(self) -> None:
        shell = ttk.Frame(self.root, style="Canvas.TFrame", padding=14)
        shell.pack(fill="both", expand=True)

        rail = ttk.Frame(shell, style="Rail.TFrame", padding=18)
        rail.pack(side="left", fill="y")
        rail.configure(width=250)
        rail.pack_propagate(False)

        ttk.Label(rail, text="Ligen Studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(rail, text="Literature download workbench", style="Rail.TLabel").pack(anchor="w", pady=(2, 24))
        ttk.Label(rail, text="Current phase", style="Rail.TLabel").pack(anchor="w")
        self.phase_selector = ttk.Combobox(
            rail,
            textvariable=self.phase_var,
            values=["warmup", "download"],
            state="readonly",
            width=20,
        )
        self.phase_selector.pack(anchor="w", fill="x", pady=(6, 16))
        self.phase_selector.bind("<<ComboboxSelected>>", lambda _event: self.update_command_preview())

        ttk.Button(rail, text="鍒嗘瀽杈撳叆", command=self.analyze_input, style="Ghost.TButton").pack(fill="x", pady=(0, 8))
        ttk.Button(rail, text="鍚姩褰撳墠闃舵", command=self.start_phase, style="Primary.TButton").pack(fill="x", pady=(0, 8))
        ttk.Button(rail, text="鍋滄浠诲姟", command=self.stop_run, style="Warn.TButton").pack(fill="x", pady=(0, 20))
        ttk.Label(rail, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w", pady=(4, 8))
        ttk.Label(rail, textvariable=self.summary_var, style="Rail.TLabel", wraplength=210).pack(anchor="w")

        rail_footer = ttk.Frame(rail, style="Rail.TFrame")
        rail_footer.pack(side="bottom", fill="x")
        ttk.Button(rail_footer, text="鎵撳紑杈撳嚭鐩綍", command=self.open_output_dir, style="Ghost.TButton").pack(fill="x", pady=(0, 8))
        ttk.Button(rail_footer, text="鍒锋柊绔彛", command=self.refresh_ports, style="Ghost.TButton").pack(fill="x")

        body = ttk.Frame(shell, style="Canvas.TFrame")
        body.pack(side="left", fill="both", expand=True, padx=(14, 0))

        top = ttk.Frame(body, style="Panel.TFrame", padding=14)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="鎵规杈撳叆", style="PanelTitle.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.count_var, style="Muted.TLabel").pack(side="right")

        main = ttk.Panedwindow(body, orient="horizontal")
        main.pack(fill="both", expand=True)
        left = ttk.Frame(main, style="Panel.TFrame", padding=14)
        right = ttk.Frame(main, style="Panel.TFrame", padding=14)
        main.add(left, weight=3)
        main.add(right, weight=2)

        self._build_input_area(left)
        self._build_control_area(right)

    def _build_input_area(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent, style="Panel.TFrame")
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Paste Clipboard", command=self.paste_clipboard, style="Ghost.TButton").pack(side="left")
        ttk.Button(actions, text="Load File", command=self.load_file, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Save Snapshot", command=self.save_snapshot, style="Ghost.TButton").pack(side="left", padx=(8, 0))

        self.input_text = tk.Text(
            parent,
            height=10,
            wrap="none",
            font=("Cascadia Mono", 10),
            bg="#fffaf0",
            fg=self.colors["ink"],
            insertbackground=self.colors["ink"],
            relief="flat",
            padx=12,
            pady=12,
        )
        self.input_text.pack(fill="x")
        self.input_text.insert("1.0", self.state.get("input_text", ""))

        ttk.Label(parent, text="瑙ｆ瀽棰勮", style="PanelTitle.TLabel").pack(anchor="w", pady=(14, 8))
        columns = ("idx", "publisher", "doi", "url")
        self.preview_tree = ttk.Treeview(parent, columns=columns, show="headings")
        for column, width in (("idx", 56), ("publisher", 110), ("doi", 260), ("url", 460)):
            self.preview_tree.heading(column, text=column.upper())
            self.preview_tree.column(column, width=width, anchor="w")
        self.preview_tree.pack(fill="both", expand=True)

    def _build_control_area(self, parent: ttk.Frame) -> None:
        tabs = ttk.Notebook(parent)
        tabs.pack(fill="both", expand=True)
        research = ttk.Frame(tabs, style="Panel.TFrame", padding=10)
        setup = ttk.Frame(tabs, style="Panel.TFrame", padding=10)
        sessions = ttk.Frame(tabs, style="Panel.TFrame", padding=10)
        console = ttk.Frame(tabs, style="Panel.TFrame", padding=10)
        results = ttk.Frame(tabs, style="Panel.TFrame", padding=10)
        tabs.add(research, text="Research Search")
        tabs.add(setup, text="杩愯璁剧疆")
        tabs.add(sessions, text="Browser Sessions")
        tabs.add(console, text="瀹炴椂鏃ュ織")
        tabs.add(results, text="缁撴灉")

        self._build_research_search_tab(research)
        self._build_setup_tab(setup)
        self._build_sessions_tab(sessions)
        self._build_console_tab(console)
        self._build_results_tab(results)

    def _build_setup_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, text="杈撳嚭鐩綍", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        folder = ttk.Frame(parent, style="Panel.TFrame")
        folder.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        folder.columnconfigure(0, weight=1)
        ttk.Entry(folder, textvariable=self.output_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder, text="閫夋嫨", command=self.choose_output_dir, style="Ghost.TButton").grid(row=0, column=1, padx=(8, 0))

        fields = [
            ("鏈€澶у苟琛屽嚭鐗堢ぞ", self.max_parallel_var),
            ("姣忓嚭鐗堢ぞ棰勭儹椤垫暟", self.max_warmup_var),
            ("椤甸潰绋冲畾绛夊緟绉掓暟", self.page_settle_var),
            ("鍗曠瘒瓒呮椂绉掓暟", self.per_doi_timeout_var),
        ]
        for index, (label, variable) in enumerate(fields, start=2):
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=index * 2, column=0, sticky="w", pady=(4, 2))
            ttk.Entry(parent, textvariable=variable).grid(row=index * 2 + 1, column=0, sticky="ew", pady=(0, 6))

        checks = ttk.Frame(parent, style="Soft.TFrame", padding=10)
        checks.grid(row=12, column=0, sticky="ew", pady=(8, 12))
        ttk.Checkbutton(checks, text="缂虹鍙ｆ椂鑷姩鍚姩 Chrome", variable=self.launch_chrome_var, command=self.update_command_preview).pack(anchor="w")
        ttk.Checkbutton(checks, text="Keep warmup tabs open", variable=self.keep_tabs_var, command=self.update_command_preview).pack(anchor="w")
        ttk.Checkbutton(checks, text="Reuse existing successful results", variable=self.resume_var, command=self.update_command_preview).pack(anchor="w")
        ttk.Checkbutton(checks, text="Dry run锛屽彧鎵撳嵃鍛戒护", variable=self.dry_run_var, command=self.update_command_preview).pack(anchor="w")

        ttk.Label(parent, text="鍛戒护棰勮", style="PanelTitle.TLabel").grid(row=13, column=0, sticky="w")
        self.command_text = tk.Text(parent, height=5, wrap="word", bg="#f8fafc", fg=self.colors["ink"], relief="flat", padx=10, pady=8)
        self.command_text.grid(row=14, column=0, sticky="nsew", pady=(6, 0))
        parent.rowconfigure(14, weight=1)

    def _build_sessions_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Port status", style="PanelTitle.TLabel").pack(anchor="w")
        columns = ("publisher", "port", "status")
        self.port_tree = ttk.Treeview(parent, columns=columns, show="headings", height=10)
        for column, width in (("publisher", 120), ("port", 80), ("status", 240)):
            self.port_tree.heading(column, text=column.upper())
            self.port_tree.column(column, width=width, anchor="w")
        self.port_tree.pack(fill="x", pady=(8, 12))

        grid = ttk.Frame(parent, style="Panel.TFrame")
        grid.pack(fill="both", expand=True)
        for index, publisher in enumerate(DEFAULT_PORT_MAP):
            row, col = divmod(index, 3)
            ttk.Label(grid, text=publisher, style="Muted.TLabel").grid(row=row, column=col * 2, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(grid, width=8, textvariable=self.port_vars[publisher]).grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=4)

    def _build_console_tab(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(
            parent,
            wrap="word",
            font=("Cascadia Mono", 9),
            bg="#111827",
            fg="#d1d5db",
            insertbackground="#d1d5db",
            relief="flat",
            padx=12,
            pady=12,
        )
        self.log_text.pack(fill="both", expand=True)

    def _build_results_tab(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent, style="Panel.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="鍒锋柊缁撴灉", command=self.refresh_results, style="Ghost.TButton").pack(side="left")
        ttk.Button(actions, text="鎵撳紑缁撴灉 CSV", command=self.open_results_csv, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Analyze failures", command=lambda: self.analyze_current_run(show_alert=True), style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open analysis report", command=self.open_analysis_report, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        self.results_text = tk.Text(parent, wrap="word", font=("Cascadia Mono", 9), bg="white", fg=self.colors["ink"], relief="flat", padx=12, pady=12)
        self.results_text.pack(fill="both", expand=True)

    def _build_research_search_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(8, weight=1)
        ttk.Label(parent, text="Research topic or locked query", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.research_text = tk.Text(
            parent,
            height=5,
            wrap="word",
            font=("Cascadia Mono", 10),
            bg="#fffaf0",
            fg=self.colors["ink"],
            insertbackground=self.colors["ink"],
            relief="flat",
            padx=10,
            pady=8,
        )
        self.research_text.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        self.research_text.insert("1.0", self.state.get("research_query_text", ""))

        provider_box = ttk.Frame(parent, style="Soft.TFrame", padding=10)
        provider_box.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(provider_box, text="Providers", style="SoftTitle.TLabel").pack(side="left", padx=(0, 12))
        ttk.Checkbutton(provider_box, text="Crossref", variable=self.research_crossref_var).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(provider_box, text="OpenAlex", variable=self.research_openalex_var).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(provider_box, text="Manual DOI/List", variable=self.research_local_var).pack(side="left", padx=(0, 10))
        ttk.Label(provider_box, text="Limit", style="SoftMuted.TLabel").pack(side="left", padx=(18, 4))
        ttk.Entry(provider_box, textvariable=self.research_limit_var, width=6).pack(side="left")

        actions = ttk.Frame(parent, style="Panel.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(actions, text="Create Draft", command=self.create_research_keyword_draft, style="Ghost.TButton").pack(side="left")
        ttk.Button(actions, text="Lock Keywords and Search", command=self.lock_and_run_research_search, style="Primary.TButton").pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.research_status_var, style="Muted.TLabel").pack(side="right")

        ttk.Label(parent, textvariable=self.research_summary_var, style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 8))

        ttk.Label(parent, text="Provider counts", style="PanelTitle.TLabel").grid(row=5, column=0, sticky="w")
        count_columns = ("provider", "reported", "returned", "doi", "candidates", "status", "seconds")
        self.provider_count_tree = ttk.Treeview(parent, columns=count_columns, show="headings", height=4)
        for column, width in (
            ("provider", 110),
            ("reported", 80),
            ("returned", 80),
            ("doi", 70),
            ("candidates", 90),
            ("status", 180),
            ("seconds", 70),
        ):
            self.provider_count_tree.heading(column, text=column.upper())
            self.provider_count_tree.column(column, width=width, anchor="w")
        self.provider_count_tree.grid(row=6, column=0, sticky="ew", pady=(6, 12))

        ttk.Label(parent, text="Deduped records", style="PanelTitle.TLabel").grid(row=7, column=0, sticky="w")
        record_columns = ("provider", "year", "doi", "title")
        self.research_record_tree = ttk.Treeview(parent, columns=record_columns, show="headings")
        for column, width in (("provider", 90), ("year", 60), ("doi", 190), ("title", 520)):
            self.research_record_tree.heading(column, text=column.upper())
            self.research_record_tree.column(column, width=width, anchor="w")
        self.research_record_tree.grid(row=8, column=0, sticky="nsew", pady=(6, 0))

    def create_research_keyword_draft(self):
        query_text = self._research_query_text()
        if not query_text:
            messagebox.showwarning("Research Search", "Enter a research topic or query first.")
            return
        keyword_set = self.keyword_service.create_draft(query_text)
        self._last_research_keyword_set_id = keyword_set.id
        self.research_status_var.set(f"Draft #{keyword_set.id} created with {len(keyword_set.include_terms)} terms.")
        self.research_summary_var.set("Draft created. Review the query, then lock and search.")
        self._save_state()

    def lock_and_run_research_search(self):
        query_text = self._research_query_text()
        if not query_text:
            messagebox.showwarning("Research Search", "Enter a research topic or query first.")
            return
        provider_ids = self._selected_research_provider_ids()
        if not provider_ids:
            messagebox.showwarning("Research Search", "Select at least one provider.")
            return
        keyword_set = self.keyword_service.create_draft(query_text)
        assert keyword_set.id is not None
        locked = self.keyword_service.lock(keyword_set.id)
        self._last_research_keyword_set_id = locked.id
        limit = self._safe_int(self.research_limit_var.get(), 20)
        self.research_status_var.set("Searching providers...")
        self.root.update_idletasks()
        try:
            run_id, result = self.search_workflow.run(locked, provider_ids=provider_ids, limit_per_provider=limit)
        except Exception as exc:
            self.research_status_var.set("Search failed.")
            messagebox.showerror("Research Search", str(exc))
            return

        self._fill_provider_count_tree(result.provider_stats)
        self._fill_research_record_tree(result.records)
        self.research_status_var.set(f"Search run #{run_id} saved.")
        self.research_summary_var.set(
            f"Raw {result.raw_total} | Unique {result.unique_count} | "
            f"Duplicates {result.duplicate_count} | Overlap {result.overlap_count} | "
            f"Download candidates {result.download_candidate_count}"
        )
        self._save_state()

    def _research_query_text(self) -> str:
        if hasattr(self, "research_text"):
            text = self.research_text.get("1.0", "end").strip()
            if text:
                return text
        if hasattr(self, "input_text"):
            return self.input_text.get("1.0", "end").strip()
        return ""

    def _selected_research_provider_ids(self) -> list[str]:
        provider_ids: list[str] = []
        if self.research_crossref_var.get():
            provider_ids.append("crossref")
        if self.research_openalex_var.get():
            provider_ids.append("openalex")
        if self.research_local_var.get():
            provider_ids.append("local_manual")
        return provider_ids

    def _fill_provider_count_tree(self, provider_stats) -> None:
        for item in self.provider_count_tree.get_children():
            self.provider_count_tree.delete(item)
        for stat in provider_stats:
            self.provider_count_tree.insert(
                "",
                "end",
                values=(
                    stat.display_name,
                    stat.reported_total_count if stat.reported_total_count is not None else "-",
                    stat.returned_count,
                    stat.doi_count,
                    stat.download_candidate_count,
                    stat.status,
                    stat.elapsed_seconds,
                ),
            )

    def _fill_research_record_tree(self, records) -> None:
        for item in self.research_record_tree.get_children():
            self.research_record_tree.delete(item)
        for record in records[:200]:
            self.research_record_tree.insert(
                "",
                "end",
                values=(record.provider_id, record.year, record.doi, record.title[:240]),
            )

    def _load_state(self) -> dict[str, object]:
        if not STATE_PATH.exists():
            return {}
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        payload = {
            "input_text": self.input_text.get("1.0", "end").strip() if hasattr(self, "input_text") else "",
            "output_dir": self.output_dir_var.get(),
            "phase": self.phase_var.get(),
            "launch_chrome": self.launch_chrome_var.get(),
            "resume_existing": self.resume_var.get(),
            "keep_existing_tabs": self.keep_tabs_var.get(),
            "dry_run": self.dry_run_var.get(),
            "max_parallel_publishers": self.max_parallel_var.get(),
            "max_warmup_per_publisher": self.max_warmup_var.get(),
            "page_settle_seconds": self.page_settle_var.get(),
            "per_doi_timeout_seconds": self.per_doi_timeout_var.get(),
            "research_query_text": self.research_text.get("1.0", "end").strip() if hasattr(self, "research_text") else "",
            "research_limit_per_provider": self.research_limit_var.get(),
            "research_provider_crossref": self.research_crossref_var.get(),
            "research_provider_openalex": self.research_openalex_var.get(),
            "research_provider_local_manual": self.research_local_var.get(),
            "port_map": {publisher: variable.get() for publisher, variable in self.port_vars.items()},
            "last_run_dir": str(self.current_run_dir or ""),
        }
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_state(self) -> None:
        last_run_dir = str(self.state.get("last_run_dir") or "")
        if last_run_dir:
            self.current_run_dir = Path(last_run_dir)

    def paste_clipboard(self) -> None:
        try:
            text = self.root.clipboard_get()
        except Exception:
            messagebox.showwarning("Clipboard", "No readable text was found in the clipboard.")
            return
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self.analyze_input()

    def load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="閫夋嫨 DOI/TXT/CSV 鏂囦欢",
            filetypes=[("Supported", "*.txt *.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        input_path = Path(path)
        if input_path.suffix.lower() == ".csv":
            rows = list(csv.DictReader(input_path.open("r", encoding="utf-8-sig", newline="")))
            text = "\n".join((row.get("doi") or row.get("url") or "").strip() for row in rows)
        else:
            text = input_path.read_text(encoding="utf-8-sig")
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self.analyze_input()

    def save_snapshot(self) -> None:
        path = filedialog.asksaveasfilename(
            title="淇濆瓨杈撳叆蹇収",
            initialdir=str(OUTPUTS_DIR),
            defaultextension=".txt",
            filetypes=[("Text", "*.txt")],
        )
        if path:
            Path(path).write_text(self.input_text.get("1.0", "end").strip() + "\n", encoding="utf-8")

    def choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="閫夋嫨杈撳嚭鐩綍", initialdir=str(OUTPUTS_DIR))
        if path:
            self.output_dir_var.set(path)
            self.update_command_preview()

    def analyze_input(self) -> None:
        self.rows = []
        for raw_line in self.input_text.get("1.0", "end").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.split("#", 1)[0].strip()
            doi = extract_doi_like(line)
            url = line if line.startswith(("http://", "https://")) else (f"https://doi.org/{doi}" if doi else "")
            self.rows.append(
                {
                    "idx": str(len(self.rows) + 1),
                    "publisher": infer_publisher(doi, url),
                    "doi": doi,
                    "url": url,
                    "raw": line,
                }
            )

        for item in self.preview_tree.get_children():
            self.preview_tree.delete(item)
        for row in self.rows:
            self.preview_tree.insert("", "end", values=(row["idx"], row["publisher"], row["doi"], row["url"]))

        counts = Counter(row["publisher"] for row in self.rows)
        if counts:
            publishers = ", ".join(f"{publisher} {count}" for publisher, count in sorted(counts.items()))
            self.summary_var.set(publishers)
        else:
            self.summary_var.set("Paste DOI lines, then analyze the batch.")
        self.count_var.set(f"{len(self.rows)} items")
        self.update_command_preview()
        self._save_state()

    def refresh_ports(self) -> None:
        for item in self.port_tree.get_children():
            self.port_tree.delete(item)
        for publisher, variable in self.port_vars.items():
            port = self._safe_int(variable.get(), DEFAULT_PORT_MAP[publisher])
            self.port_tree.insert("", "end", values=(publisher, port, self._port_status(port)))
        self._save_state()

    def _port_status(self, port: int) -> str:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.25) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            return f"online: {payload.get('Browser', 'Chrome')}"
        except Exception:
            return "offline"

    def update_command_preview(self) -> None:
        command = self._build_command(input_path=Path("<input_snapshot>"))
        text = subprocess.list2cmdline(command)
        if hasattr(self, "command_text"):
            self.command_text.delete("1.0", "end")
            self.command_text.insert("1.0", text)
        self.command_var.set(text)

    def start_phase(self) -> None:
        self.analyze_input()
        if not self.rows:
            messagebox.showwarning("No input", "Paste or load a DOI/URL list first.")
            return
        if self.bridge.running():
            messagebox.showinfo("Running", "A task is already running.")
            return
        run_dir = Path(self.output_dir_var.get()).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        input_path = run_dir / "studio_input.txt"
        input_path.write_text("\n".join(row["raw"] for row in self.rows) + "\n", encoding="utf-8")
        self.current_run_dir = run_dir

        command = self._build_command(input_path=input_path)
        self._log(f"$ {subprocess.list2cmdline(command)}")
        self.status_var.set(f"Running {self.phase_var.get()}")
        self._save_state()
        try:
            self.bridge.start(command, SKILL_ROOT)
        except Exception as exc:
            messagebox.showerror("鍚姩澶辫触", str(exc))
            self.status_var.set("Failed to start")

    def _build_command(self, input_path: Path) -> list[str]:
        command = [
            str(PYTHON_EXE),
            str(SCRIPT_MODE),
            "--input",
            str(input_path),
            "--phase",
            self.phase_var.get(),
            "--output-dir",
            self.output_dir_var.get(),
            "--max-parallel-publishers",
            self.max_parallel_var.get(),
            "--max-warmup-per-publisher",
            self.max_warmup_var.get(),
            "--page-settle-seconds",
            self.page_settle_var.get(),
            "--per-doi-timeout-seconds",
            self.per_doi_timeout_var.get(),
        ]
        if self.launch_chrome_var.get():
            command.append("--launch-chrome")
        if self.keep_tabs_var.get():
            command.append("--keep-existing-tabs")
        if self.resume_var.get():
            command.append("--resume-existing")
        if self.dry_run_var.get():
            command.append("--dry-run")
        for publisher, variable in self.port_vars.items():
            port = variable.get().strip()
            if port:
                command.extend(["--publisher-port", f"{publisher}={port}"])
        return command

    def stop_run(self) -> None:
        self.bridge.stop()
        self.status_var.set("Stopping")

    def _poll_bridge(self) -> None:
        try:
            while True:
                stream_name, payload = self.bridge.queue.get_nowait()
                if stream_name == "event" and payload.startswith("PROCESS_EXIT::"):
                    code = payload.split("::", 1)[1]
                    self.status_var.set(f"Finished ({code})")
                    self.analyze_current_run(show_alert=True)
                    self.refresh_results()
                    self.refresh_ports()
                    self._save_state()
                else:
                    prefix = "ERR " if stream_name == "stderr" else ""
                    self._log(prefix + payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_bridge)

    def _log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def refresh_results(self) -> None:
        self.results_text.delete("1.0", "end")
        run_dir = self.current_run_dir or Path(self.output_dir_var.get()).expanduser().resolve()
        analysis_report = run_dir / "analysis" / "download_run_status_report.md"
        analysis_summary = run_dir / "analysis" / "download_run_status_summary.json"
        if analysis_report.exists():
            self.results_text.insert("1.0", analysis_report.read_text(encoding="utf-8", errors="replace"))
            return
        combined = run_dir / "combined_download_results.csv"
        single = run_dir / "download_results.csv"
        target = combined if combined.exists() else single
        if not target.exists():
            self.results_text.insert("1.0", f"杩樻病鏈夌粨鏋滄枃浠躲€俓n褰撳墠杈撳嚭鐩綍: {run_dir}\n")
            return
        rows = list(csv.DictReader(target.open("r", encoding="utf-8-sig", newline="")))
        counts = Counter((row.get("status") or "").strip() for row in rows)
        lines = [f"Results: {target}", f"Total: {len(rows)}", ""]
        lines.extend(f"{status or 'blank'}: {count}" for status, count in sorted(counts.items()))
        lines.append("")
        for row in rows[:200]:
            lines.append(
                f"{row.get('publisher','')}\t{row.get('status','')}\t{row.get('doi','')}\t{row.get('pdf_path','')}"
            )
        if analysis_summary.exists():
            lines.extend(["", f"Analysis summary: {analysis_summary}"])
        self.results_text.insert("1.0", "\n".join(lines))

    def analyze_current_run(self, *, show_alert: bool = False) -> None:
        run_dir = self.current_run_dir or Path(self.output_dir_var.get()).expanduser().resolve()
        if not ANALYZE_RUN.exists():
            if show_alert:
                messagebox.showwarning("Analysis unavailable", f"Analyzer script not found:\n{ANALYZE_RUN}")
            return
        input_path = run_dir / "studio_input.txt"
        command = [str(PYTHON_EXE), str(ANALYZE_RUN), "--run-dir", str(run_dir)]
        if input_path.exists():
            command.extend(["--input", str(input_path)])
        try:
            completed = subprocess.run(
                command,
                cwd=str(SKILL_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except Exception as exc:
            if show_alert:
                messagebox.showwarning("Analysis failed", str(exc))
            return
        if completed.returncode != 0:
            if show_alert:
                messagebox.showwarning("Analysis failed", completed.stderr or completed.stdout or "Unknown analyzer error")
            return
        if completed.stdout.strip():
            self._log("[analysis] " + completed.stdout.strip().splitlines()[-1])
        if show_alert:
            self._show_analysis_alert(run_dir)

    def _show_analysis_alert(self, run_dir: Path) -> None:
        summary_path = run_dir / "analysis" / "download_run_status_summary.json"
        if not summary_path.exists():
            return
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            return
        category_counts = summary.get("category_counts") or {}
        publisher_counts = summary.get("publisher_category_counts") or {}
        downloaded = summary.get("downloaded_unique_doi_count", 0)
        failed = summary.get("failed_or_pending_unique_doi_count", 0)
        auth_count = int(category_counts.get("auth_or_entitlement") or 0)
        acs_count = int(category_counts.get("acs_needs_verification") or 0)
        elsevier_institution_count = int(category_counts.get("elsevier_needs_institution_verification") or 0)
        elsevier_pdf_count = int(category_counts.get("elsevier_view_pdf_not_materialized") or 0)
        elsevier_no_subscription_count = int(category_counts.get("elsevier_no_subscription") or 0)
        robot_captcha_count = int(category_counts.get("publisher_robot_captcha") or 0)
        manual_count = int(category_counts.get("manual_verification") or 0)
        login_count = int(category_counts.get("manual_login_or_institution") or 0)
        timeout_count = int(category_counts.get("site_unreachable_or_network_timeout") or 0)
        site_error_count = int(category_counts.get("publisher_site_error") or 0)
        fetch_count = int(category_counts.get("browser_fetch_or_cors") or 0)

        messages = [f"Downloaded: {downloaded}", f"Failed/pending: {failed}"]
        if auth_count or acs_count or elsevier_institution_count or manual_count or robot_captcha_count or login_count:
            affected = [
                publisher
                for publisher, counts in publisher_counts.items()
                if int((counts or {}).get("auth_or_entitlement") or 0)
                or int((counts or {}).get("acs_needs_verification") or 0)
                or int((counts or {}).get("elsevier_needs_institution_verification") or 0)
                or int((counts or {}).get("manual_verification") or 0)
                or int((counts or {}).get("publisher_robot_captcha") or 0)
                or int((counts or {}).get("manual_login_or_institution") or 0)
            ]
            messages.extend(
                [
                    "",
                    "Needs browser/campus verification:",
                    ", ".join(affected) if affected else "publisher tabs",
                    "Open the warmup Chrome windows, confirm VPN/campus access, pass captcha/login, then rerun failed rows.",
                ]
            )
        if robot_captcha_count:
            messages.extend(
                [
                    "",
                    f"Publisher robot/captcha: {robot_captcha_count}",
                    "Complete the visible Are you a robot / captcha challenge in the publisher Chrome tab, then rerun only those failed rows.",
                ]
            )
        if elsevier_pdf_count:
            messages.extend(
                [
                    "",
                    f"Elsevier View PDF needs materialization: {elsevier_pdf_count}",
                    "Click View PDF once in ScienceDirect so it generates the temporary pdf.sciencedirectassets.com main.pdf URL, then rerun Elsevier.",
                ]
            )
        if elsevier_no_subscription_count:
            messages.extend(
                [
                    "",
                    f"Elsevier no subscription: {elsevier_no_subscription_count}",
                    "Peking University is verified, but ScienceDirect says these items are not subscribed. Skip automatic retry and use manual alternative access if needed.",
                ]
            )
        if timeout_count:
            messages.extend(
                [
                    "",
                    f"Site/network timeout: {timeout_count}",
                    "The site did not respond from this network. Check VPN/proxy/DNS or retry later; this is not a permissions conclusion.",
                ]
            )
        if site_error_count:
            messages.extend(
                [
                    "",
                    f"Publisher server errors: {site_error_count}",
                    "The publisher returned 502/503/504-style errors. Retry later or open DOI manually.",
                ]
            )
        if fetch_count:
            messages.extend(
                [
                    "",
                    f"Browser fetch/CORS issues: {fetch_count}",
                    "Chrome or the site returned HTML/network errors instead of a PDF. Retry after VPN/network check; if repeated, open DOI manually.",
                ]
            )
        messages.extend(["", f"Report: {run_dir / 'analysis' / 'download_run_status_report.md'}"])
        messagebox.showinfo("Download analysis", "\n".join(messages))

    def open_analysis_report(self) -> None:
        run_dir = self.current_run_dir or Path(self.output_dir_var.get()).expanduser().resolve()
        report = run_dir / "analysis" / "download_run_status_report.md"
        if not report.exists():
            self.analyze_current_run(show_alert=False)
        if report.exists():
            os.startfile(str(report))
            return
        messagebox.showinfo("Analysis", "No analysis report is available yet.")

    def open_output_dir(self) -> None:
        path = Path(self.output_dir_var.get()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def open_results_csv(self) -> None:
        run_dir = self.current_run_dir or Path(self.output_dir_var.get()).expanduser().resolve()
        candidates = [run_dir / "combined_download_results.csv", run_dir / "download_results.csv"]
        for candidate in candidates:
            if candidate.exists():
                os.startfile(str(candidate))
                return
        messagebox.showinfo("Results", "No result CSV is available yet.")

    def _safe_int(self, value: str, fallback: int) -> int:
        try:
            return int(value)
        except ValueError:
            return fallback


def launch_app() -> None:
    root = tk.Tk()
    app = LigenStudioApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: _close(root, app))
    root.mainloop()


def _close(root: tk.Tk, app: LigenStudioApp) -> None:
    app._save_state()
    root.destroy()


def self_test() -> None:
    root = tk.Tk()
    app = LigenStudioApp(root)
    app.analyze_input()
    app.refresh_ports()
    root.update_idletasks()
    app._save_state()
    root.destroy()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        launch_app()
