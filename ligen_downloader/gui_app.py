from __future__ import annotations

import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
import tkinter as tk
from tkinter import ttk

from .utils import extract_doi_like
from .utils import infer_publisher


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
        if (candidate / "scripts" / "run_ligen_paper_download_multiport.py").exists():
            return candidate.resolve()
    return Path(__file__).resolve().parents[1]


SKILL_ROOT = _resolve_skill_root()
APP_HOME = Path(os.environ.get("LIGEN_APP_HOME", str(SKILL_ROOT)))
OUTPUTS_DIR = APP_HOME / "outputs"
STATE_PATH = OUTPUTS_DIR / "gui_client_state.json"
MULTIPORT_SCRIPT = SKILL_ROOT / "scripts" / "run_ligen_paper_download_multiport.py"
PYTHON_EXE = Path(os.environ.get("LIGEN_PYTHON_EXE", sys.executable))

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


@dataclass
class ParsedInputRow:
    idx: int
    raw: str
    doi: str
    publisher: str
    url: str


class ProcessBridge:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._threads: list[threading.Thread] = []

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, cmd: list[str], workdir: Path) -> None:
        if self.running():
            raise RuntimeError("A run is already active.")
        self.process = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._threads = [
            threading.Thread(target=self._pump, args=("stdout", self.process.stdout), daemon=True),
            threading.Thread(target=self._pump, args=("stderr", self.process.stderr), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        threading.Thread(target=self._watch_exit, daemon=True).start()

    def stop(self) -> None:
        if not self.running():
            return
        assert self.process is not None
        self.process.terminate()

    def _pump(self, stream_name: str, handle) -> None:
        try:
            for line in handle:
                self.queue.put((stream_name, line.rstrip("\n")))
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def _watch_exit(self) -> None:
        assert self.process is not None
        return_code = self.process.wait()
        self.queue.put(("event", f"PROCESS_EXIT::{return_code}"))


class LigenGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Ligen Literature Downloader")
        self.root.geometry("1440x920")
        self.root.minsize(1240, 780)

        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

        self.bridge = ProcessBridge()
        self.state = self._load_state()
        self.parsed_rows: list[ParsedInputRow] = []
        self.current_run_dir: Path | None = None

        self._build_style()
        self._build_vars()
        self._build_ui()
        self._apply_state()
        self.refresh_preview()
        self.refresh_port_status()
        self.root.after(250, self._poll_bridge)

    def _build_style(self) -> None:
        bg = "#f4f1ea"
        card = "#fbfaf7"
        ink = "#1e2430"
        accent = "#1e5eff"
        warm = "#b85c38"
        ok = "#1f7a4d"
        muted = "#697384"

        self.root.configure(bg=bg)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=bg, foreground=ink, fieldbackground="white")
        style.configure("App.TFrame", background=bg)
        style.configure("Card.TFrame", background=card, relief="flat")
        style.configure("Title.TLabel", background=bg, foreground=ink, font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", background=bg, foreground=muted, font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=card, foreground=ink, font=("Segoe UI Semibold", 11))
        style.configure("Accent.TButton", background=accent, foreground="white", padding=(12, 8))
        style.map("Accent.TButton", background=[("active", "#184dd6")])
        style.configure("Warm.TButton", background=warm, foreground="white", padding=(12, 8))
        style.map("Warm.TButton", background=[("active", "#9f4f2f")])
        style.configure("Ghost.TButton", background=card, foreground=ink, padding=(10, 6))
        style.configure("Treeview", rowheight=24, font=("Consolas", 10))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        style.configure("StatusGood.TLabel", background=card, foreground=ok, font=("Segoe UI Semibold", 10))
        style.configure("StatusWarn.TLabel", background=card, foreground=warm, font=("Segoe UI Semibold", 10))
        style.configure("StatusMuted.TLabel", background=card, foreground=muted, font=("Segoe UI", 10))

    def _build_vars(self) -> None:
        self.output_dir_var = tk.StringVar(value=str(OUTPUTS_DIR / f"gui_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        self.page_settle_var = tk.StringVar(value=str(self.state.get("page_settle_seconds", 6.0)))
        self.per_doi_timeout_var = tk.StringVar(value=str(self.state.get("per_doi_timeout_seconds", 240.0)))
        self.per_publisher_timeout_var = tk.StringVar(value=str(self.state.get("per_publisher_timeout_seconds", 3600.0)))
        self.max_parallel_publishers_var = tk.StringVar(value=str(self.state.get("max_parallel_publishers", 3)))
        self.max_warmup_var = tk.StringVar(value=str(self.state.get("max_warmup_per_publisher", 1)))
        self.sleep_seconds_var = tk.StringVar(value=str(self.state.get("sleep_seconds", 1.5)))
        self.keep_existing_tabs_var = tk.BooleanVar(value=self.state.get("keep_existing_tabs", True))
        self.resume_existing_var = tk.BooleanVar(value=self.state.get("resume_existing", True))
        self.launch_chrome_var = tk.BooleanVar(value=self.state.get("launch_chrome", True))
        self.status_var = tk.StringVar(value="Idle")
        self.summary_var = tk.StringVar(value="No run yet.")
        self.preview_count_var = tk.StringVar(value="0 rows")
        self.publisher_count_var = tk.StringVar(value="No publishers detected")
        self.port_vars = {name: tk.StringVar(value=str(self.state.get("port_map", {}).get(name, port))) for name, port in DEFAULT_PORT_MAP.items()}

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Ligen Literature Downloader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Desktop operator console for DOI-list intake, publisher warmup, download control, and run auditing.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 10))

        toolbar = ttk.Frame(outer, style="Card.TFrame", padding=12)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="Analyze Input", command=self.refresh_preview, style="Ghost.TButton").pack(side="left")
        ttk.Button(toolbar, text="Warm Up Sessions", command=lambda: self.start_run("warmup"), style="Warm.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Start Download", command=lambda: self.start_run("download"), style="Accent.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Stop Run", command=self.stop_run, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Refresh Ports", command=self.refresh_port_status, style="Ghost.TButton").pack(side="left", padx=(18, 0))
        ttk.Button(toolbar, text="Open Output Folder", command=self.open_output_dir, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Label(toolbar, textvariable=self.status_var, style="StatusWarn.TLabel").pack(side="right")

        content = ttk.Panedwindow(outer, orient="horizontal")
        content.pack(fill="both", expand=True)

        left = ttk.Frame(content, style="App.TFrame")
        right = ttk.Frame(content, style="App.TFrame")
        content.add(left, weight=3)
        content.add(right, weight=2)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        input_card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        input_card.pack(fill="both", expand=True)

        top = ttk.Frame(input_card, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Input", style="Section.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.preview_count_var, style="StatusMuted.TLabel").pack(side="right")

        ttk.Label(
            input_card,
            text="Paste DOI list, DOI URLs, or mixed lines. One entry per line. No CSV required.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 8))

        input_actions = ttk.Frame(input_card, style="Card.TFrame")
        input_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(input_actions, text="Paste Clipboard", command=self.paste_clipboard, style="Ghost.TButton").pack(side="left")
        ttk.Button(input_actions, text="Load Text/CSV", command=self.load_input_file, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(input_actions, text="Save Input Snapshot", command=self.save_input_snapshot, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Label(input_actions, textvariable=self.publisher_count_var, style="StatusMuted.TLabel").pack(side="right")

        self.input_text = tk.Text(
            input_card,
            height=12,
            wrap="none",
            font=("Consolas", 11),
            bg="white",
            fg="#1f2430",
            insertbackground="#1f2430",
            relief="flat",
            padx=10,
            pady=10,
        )
        self.input_text.pack(fill="x")
        if self.state.get("input_text"):
            self.input_text.insert("1.0", self.state["input_text"])

        preview_label = ttk.Label(input_card, text="Parsed Preview", style="Section.TLabel")
        preview_label.pack(anchor="w", pady=(12, 6))

        columns = ("idx", "doi", "publisher", "url")
        self.preview_tree = ttk.Treeview(input_card, columns=columns, show="headings", height=16)
        for col, width in (("idx", 60), ("doi", 280), ("publisher", 110), ("url", 520)):
            self.preview_tree.heading(col, text=col.upper())
            self.preview_tree.column(col, width=width, anchor="w")
        self.preview_tree.pack(fill="both", expand=True)

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)

        settings_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        ports_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        logs_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        results_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        notebook.add(settings_tab, text="Run Setup")
        notebook.add(ports_tab, text="Publisher Sessions")
        notebook.add(logs_tab, text="Live Console")
        notebook.add(results_tab, text="Run Results")

        self._build_settings_tab(settings_tab)
        self._build_ports_tab(ports_tab)
        self._build_logs_tab(logs_tab)
        self._build_results_tab(results_tab)

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Run Setup", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="Output Folder", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 2))
        folder_row = ttk.Frame(parent, style="Card.TFrame")
        folder_row.grid(row=2, column=0, sticky="ew")
        parent.columnconfigure(0, weight=1)

        folder_entry = ttk.Entry(folder_row, textvariable=self.output_dir_var)
        folder_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="Browse", command=self.choose_output_dir, style="Ghost.TButton").pack(side="left", padx=(8, 0))

        fields = [
            ("Page settle seconds", self.page_settle_var),
            ("Per DOI timeout (s)", self.per_doi_timeout_var),
            ("Per publisher timeout (s)", self.per_publisher_timeout_var),
            ("Max parallel publishers", self.max_parallel_publishers_var),
            ("Max warmup tabs per publisher", self.max_warmup_var),
            ("Sleep seconds between warmup tabs", self.sleep_seconds_var),
        ]
        row_cursor = 3
        for label, var in fields:
            ttk.Label(parent, text=label, style="Subtitle.TLabel").grid(row=row_cursor, column=0, sticky="w", pady=(8, 2))
            ttk.Entry(parent, textvariable=var).grid(row=row_cursor + 1, column=0, sticky="ew")
            row_cursor += 2

        checks = ttk.Frame(parent, style="Card.TFrame")
        checks.grid(row=row_cursor + 1, column=0, sticky="w", pady=(12, 0))
        ttk.Checkbutton(checks, text="Keep existing warmup tabs", variable=self.keep_existing_tabs_var).pack(anchor="w")
        ttk.Checkbutton(checks, text="Resume existing successes", variable=self.resume_existing_var).pack(anchor="w")
        ttk.Checkbutton(checks, text="Launch Chrome if port missing", variable=self.launch_chrome_var).pack(anchor="w")

        ttk.Label(parent, textvariable=self.summary_var, style="StatusMuted.TLabel").grid(row=row_cursor + 2, column=0, sticky="w", pady=(18, 0))

    def _build_ports_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Publisher Sessions", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            parent,
            text="These ports back the Chrome GUI sessions used for warmup, captcha, and manual verification.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 8))

        columns = ("publisher", "port", "status")
        self.port_tree = ttk.Treeview(parent, columns=columns, show="headings", height=18)
        for col, width in (("publisher", 120), ("port", 80), ("status", 220)):
            self.port_tree.heading(col, text=col.upper())
            self.port_tree.column(col, width=width, anchor="w")
        self.port_tree.pack(fill="both", expand=True)

        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        row = 0
        col = 0
        for publisher in DEFAULT_PORT_MAP:
            ttk.Label(grid, text=publisher, style="Subtitle.TLabel").grid(row=row, column=col * 2, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(grid, width=8, textvariable=self.port_vars[publisher]).grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 14), pady=4)
            col += 1
            if col == 3:
                col = 0
                row += 1

    def _build_logs_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Live Console", style="Section.TLabel").pack(anchor="w")
        self.log_text = tk.Text(
            parent,
            height=28,
            wrap="word",
            font=("Consolas", 10),
            bg="#10141b",
            fg="#d4dde8",
            insertbackground="#d4dde8",
            relief="flat",
            padx=10,
            pady=10,
        )
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

    def _build_results_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Run Results", style="Section.TLabel").pack(anchor="w")
        button_row = ttk.Frame(parent, style="Card.TFrame")
        button_row.pack(fill="x", pady=(8, 8))
        ttk.Button(button_row, text="Refresh Results", command=self.refresh_results_summary, style="Ghost.TButton").pack(side="left")
        ttk.Button(button_row, text="Open Combined Results CSV", command=self.open_combined_results, style="Ghost.TButton").pack(side="left", padx=(8, 0))

        self.results_text = tk.Text(
            parent,
            height=28,
            wrap="word",
            font=("Consolas", 10),
            bg="white",
            fg="#1f2430",
            relief="flat",
            padx=10,
            pady=10,
        )
        self.results_text.pack(fill="both", expand=True)

    def _load_state(self) -> dict:
        if not STATE_PATH.exists():
            return {}
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        payload = {
            "input_text": self.input_text.get("1.0", "end").strip(),
            "output_dir": self.output_dir_var.get().strip(),
            "page_settle_seconds": self.page_settle_var.get().strip(),
            "per_doi_timeout_seconds": self.per_doi_timeout_var.get().strip(),
            "per_publisher_timeout_seconds": self.per_publisher_timeout_var.get().strip(),
            "max_parallel_publishers": self.max_parallel_publishers_var.get().strip(),
            "max_warmup_per_publisher": self.max_warmup_var.get().strip(),
            "sleep_seconds": self.sleep_seconds_var.get().strip(),
            "keep_existing_tabs": self.keep_existing_tabs_var.get(),
            "resume_existing": self.resume_existing_var.get(),
            "launch_chrome": self.launch_chrome_var.get(),
            "port_map": {name: var.get().strip() for name, var in self.port_vars.items()},
            "last_run_dir": str(self.current_run_dir) if self.current_run_dir else "",
        }
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_state(self) -> None:
        if self.state.get("output_dir"):
            self.output_dir_var.set(self.state["output_dir"])
        if self.state.get("last_run_dir"):
            self.current_run_dir = Path(self.state["last_run_dir"])

    def paste_clipboard(self) -> None:
        try:
            text = self.root.clipboard_get()
        except Exception:
            messagebox.showwarning("Clipboard", "Clipboard does not contain readable text.")
            return
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self.refresh_preview()

    def load_input_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open DOI input",
            filetypes=[("Supported", "*.txt *.csv"), ("Text", "*.txt"), ("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8-sig")
        if path.lower().endswith(".csv"):
            lines = []
            for row in csv.DictReader(text.splitlines()):
                doi = (row.get("doi") or "").strip()
                url = (row.get("url") or "").strip()
                if doi:
                    lines.append(doi if not url else f"{doi}  # {url}")
            text = "\n".join(lines)
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self.refresh_preview()

    def save_input_snapshot(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save DOI list snapshot",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt")],
            initialdir=str(OUTPUTS_DIR),
        )
        if not path:
            return
        Path(path).write_text(self.input_text.get("1.0", "end").strip() + "\n", encoding="utf-8")
        self._log("stdout", f"Saved input snapshot: {path}")

    def choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Choose output folder", initialdir=str(OUTPUTS_DIR))
        if path:
            self.output_dir_var.set(path)

    def refresh_preview(self) -> None:
        raw_text = self.input_text.get("1.0", "end")
        self.parsed_rows = self._parse_input_text(raw_text)
        for item in self.preview_tree.get_children():
            self.preview_tree.delete(item)
        for row in self.parsed_rows:
            self.preview_tree.insert("", "end", values=(row.idx, row.doi, row.publisher, row.url))
        self.preview_count_var.set(f"{len(self.parsed_rows)} rows")
        counts = Counter(row.publisher for row in self.parsed_rows)
        if counts:
            summary = ", ".join(f"{pub} {count}" for pub, count in sorted(counts.items()))
        else:
            summary = "No publishers detected"
        self.publisher_count_var.set(summary)
        self._save_state()

    def _parse_input_text(self, raw_text: str) -> list[ParsedInputRow]:
        rows: list[ParsedInputRow] = []
        for line in raw_text.splitlines():
            cleaned = line.strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            cleaned = cleaned.split("#", 1)[0].strip()
            doi = extract_doi_like(cleaned)
            url = cleaned if cleaned.startswith(("http://", "https://")) else (f"https://doi.org/{doi}" if doi else "")
            rows.append(
                ParsedInputRow(
                    idx=len(rows) + 1,
                    raw=cleaned,
                    doi=doi,
                    publisher=infer_publisher(doi, url),
                    url=url,
                )
            )
        return rows

    def refresh_port_status(self) -> None:
        for item in self.port_tree.get_children():
            self.port_tree.delete(item)
        for publisher, var in self.port_vars.items():
            port = int(var.get() or DEFAULT_PORT_MAP[publisher])
            status = self._port_status(port)
            self.port_tree.insert("", "end", values=(publisher, port, status))
        self._save_state()

    def _port_status(self, port: int) -> str:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            browser = str(payload.get("Browser") or "Chrome session")
            return f"Listening - {browser}"
        except Exception:
            return "Offline"

    def start_run(self, mode: str) -> None:
        if mode not in {"warmup", "download"}:
            raise ValueError(mode)
        self.refresh_preview()
        if not self.parsed_rows:
            messagebox.showwarning("No input", "Paste at least one DOI or URL before running.")
            return
        run_dir = Path(self.output_dir_var.get().strip()).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        input_path = run_dir / "gui_input.txt"
        input_path.write_text("\n".join(row.raw for row in self.parsed_rows) + "\n", encoding="utf-8")
        self.current_run_dir = run_dir

        cmd = [
            str(PYTHON_EXE),
            str(MULTIPORT_SCRIPT),
            "--input",
            str(input_path),
            "--mode",
            mode,
            "--output-dir",
            str(run_dir),
            "--page-settle-seconds",
            self.page_settle_var.get().strip(),
            "--per-doi-timeout-seconds",
            self.per_doi_timeout_var.get().strip(),
            "--per-publisher-timeout-seconds",
            self.per_publisher_timeout_var.get().strip(),
            "--max-parallel-publishers",
            self.max_parallel_publishers_var.get().strip(),
            "--max-warmup-per-publisher",
            self.max_warmup_var.get().strip(),
            "--sleep-seconds",
            self.sleep_seconds_var.get().strip(),
        ]
        if self.keep_existing_tabs_var.get():
            cmd.append("--keep-existing-tabs")
        if self.resume_existing_var.get():
            cmd.append("--resume-existing")
        if self.launch_chrome_var.get():
            cmd.append("--launch-chrome")
        for publisher, var in self.port_vars.items():
            cmd.extend(["--publisher-port", f"{publisher}={var.get().strip()}"])

        self._log("stdout", "")
        self._log("stdout", f"$ {' '.join(cmd)}")
        self.status_var.set(f"Running: {mode}")
        self.summary_var.set(f"Started {mode} run in {run_dir}")
        self._save_state()
        try:
            self.bridge.start(cmd, SKILL_ROOT)
        except Exception as exc:
            messagebox.showerror("Run failed to start", str(exc))

    def stop_run(self) -> None:
        self.bridge.stop()
        self.status_var.set("Stopping...")

    def _poll_bridge(self) -> None:
        try:
            while True:
                stream_name, payload = self.bridge.queue.get_nowait()
                if stream_name == "event" and payload.startswith("PROCESS_EXIT::"):
                    return_code = int(payload.split("::", 1)[1])
                    self.status_var.set(f"Finished ({return_code})")
                    self.summary_var.set(f"Process finished with exit code {return_code}")
                    self.refresh_port_status()
                    self.refresh_results_summary()
                    self._save_state()
                else:
                    self._log(stream_name, payload)
        except queue.Empty:
            pass
        self.root.after(250, self._poll_bridge)

    def _log(self, stream_name: str, line: str) -> None:
        prefix = "[stderr]" if stream_name == "stderr" else "[log]"
        self.log_text.insert("end", f"{prefix} {line}\n")
        self.log_text.see("end")

    def refresh_results_summary(self) -> None:
        self.results_text.delete("1.0", "end")
        if not self.current_run_dir:
            self.results_text.insert("1.0", "No run folder selected yet.\n")
            return
        run_dir = self.current_run_dir
        combined = run_dir / "combined_download_results.csv"
        if not combined.exists():
            self.results_text.insert("1.0", f"No combined results yet in {run_dir}\n")
            return
        rows = list(csv.DictReader(combined.open("r", encoding="utf-8-sig", newline="")))
        counts = Counter((row.get("status") or "").strip() for row in rows)
        summary_lines = [
            f"Run directory: {run_dir}",
            f"Total rows: {len(rows)}",
            "",
            "Status counts:",
        ]
        summary_lines.extend(f"- {status}: {count}" for status, count in sorted(counts.items()))
        summary_lines.append("")
        summary_lines.append("Rows:")
        for row in rows:
            summary_lines.append(
                f"{row.get('publisher','')}\t{row.get('doi','')}\t{row.get('status','')}\t{row.get('detail','')[:180]}"
            )
        self.results_text.insert("1.0", "\n".join(summary_lines))

    def open_output_dir(self) -> None:
        path = Path(self.output_dir_var.get().strip()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def open_combined_results(self) -> None:
        if not self.current_run_dir:
            return
        target = self.current_run_dir / "combined_download_results.csv"
        if not target.exists():
            messagebox.showinfo("No results", "Combined results CSV does not exist yet.")
            return
        os.startfile(str(target))


def launch_app() -> None:
    root = tk.Tk()
    app = LigenGuiApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: _close_app(root, app))
    root.mainloop()


def _close_app(root: tk.Tk, app: LigenGuiApp) -> None:
    app._save_state()
    root.destroy()


def self_test() -> None:
    root = tk.Tk()
    app = LigenGuiApp(root)
    app.refresh_preview()
    app.refresh_port_status()
    app._save_state()
    root.update_idletasks()
    root.destroy()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        launch_app()
