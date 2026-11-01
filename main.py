"""程式入口：啟動本機 Streamlit 介面（僅監聽 localhost）。

用法：
    python main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent / "app" / "ui" / "web_app.py"
DEFAULT_PORT = 8501


def main(argv: list[str] | None = None) -> int:
    """以 Streamlit 執行本機介面。"""
    args = list(argv if argv is not None else sys.argv[1:])
    from streamlit.web import cli as streamlit_cli

    sys.argv = [
        "streamlit",
        "run",
        str(APP_PATH),
        "--server.address=localhost",
        f"--server.port={DEFAULT_PORT}",
        "--server.headless=true",
        *args,
    ]
    return int(streamlit_cli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
