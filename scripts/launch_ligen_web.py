from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ligen_downloader.web_app import DEFAULT_HOST
from ligen_downloader.web_app import DEFAULT_PORT
from ligen_downloader.web_app import self_test
from ligen_downloader.web_app import serve


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the local web UI for the Ligen DOI downloader.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host. Default: {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port. Default: {DEFAULT_PORT}")
    parser.add_argument("--open-browser", action="store_true", help="Open the local site in the default browser after startup.")
    parser.add_argument("--self-test", action="store_true", help="Run a lightweight startup self-test and exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    serve(host=args.host, port=args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    main()
