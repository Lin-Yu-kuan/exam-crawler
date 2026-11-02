"""國考刷題神器 - Streamlit 進入點。

供 Streamlit Community Cloud 或 streamlit run 根目錄直接啟動使用。
自動確保專案根目錄加入 sys.path，以避免 ModuleNotFoundError: No module named 'app'。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 將專案根目錄加入 Python 搜尋路徑
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.ui.web_app import main

if __name__ == "__main__":
    main()
