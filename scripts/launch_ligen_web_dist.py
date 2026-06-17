from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


INTERNAL_SCRIPTS = {
    "run_ligen_script_mode": "scripts.run_ligen_script_mode",
    "run_ligen_paper_download_multiport": "scripts.run_ligen_paper_download_multiport",
    "run_ligen_paper_download": "scripts.run_ligen_paper_download",
    "open_publisher_warmup_tabs": "scripts.open_publisher_warmup_tabs",
    "fetch_publisher_pdfs": "scripts.fetch_publisher_pdfs",
}


def _default_app_home() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "PKU Literature Workbench"


def _set_distributable_defaults() -> None:
    os.environ.setdefault("LIGEN_APP_HOME", str(_default_app_home()))
    os.environ.setdefault("LIGEN_AGENT_REQUEST_TIMEOUT_SECONDS", "18")
    os.environ.setdefault("LIGEN_AGENT_RETRY_ATTEMPTS", "3")
    os.environ.setdefault("LIGEN_AGENT_RETRY_BASE_DELAY_SECONDS", "0.5")


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def _pick_port(host: str, requested: int) -> int:
    for port in [requested, *range(8765, 8775)]:
        if _is_port_free(host, port):
            return port
    raise SystemExit("No free local port found in 8765-8774.")


def _run_internal_script(name: str, args: list[str]) -> None:
    module_name = INTERNAL_SCRIPTS.get(name)
    if not module_name:
        choices = ", ".join(sorted(INTERNAL_SCRIPTS))
        raise SystemExit(f"Unknown internal script: {name}. Choices: {choices}")
    sys.argv = [name, *args]
    module = importlib.import_module(module_name)
    main = getattr(module, "main", None)
    if not callable(main):
        raise SystemExit(f"Internal script has no callable main(): {name}")
    main()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch PKU Literature Workbench.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def _extract_internal_invocation(argv: list[str]) -> tuple[str, list[str]] | None:
    for index, value in enumerate(argv):
        if value == "--internal-script":
            if index + 1 >= len(argv):
                raise SystemExit("--internal-script requires a script name.")
            return argv[index + 1], argv[index + 2 :]
        if value.startswith("--internal-script="):
            return value.split("=", 1)[1], argv[index + 1 :]
    return None


def main() -> None:
    _set_distributable_defaults()
    internal = _extract_internal_invocation(sys.argv[1:])
    if internal is not None:
        name, internal_args = internal
        _run_internal_script(name, internal_args)
        return
    args = parse_args()

    from ligen_downloader.web_app import self_test
    from ligen_downloader.web_app import serve

    if args.self_test:
        self_test()
        return
    port = _pick_port(args.host, args.port)
    serve(host=args.host, port=port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
