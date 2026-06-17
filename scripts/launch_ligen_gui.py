from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ligen_downloader.gui_app import launch_app
from ligen_downloader.gui_app import self_test


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        launch_app()
