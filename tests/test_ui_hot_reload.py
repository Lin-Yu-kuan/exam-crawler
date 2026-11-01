"""熱重載保護：session 內的控制器若屬舊版（缺介面版本），必須自動重建。

開發時 Streamlit 會重新載入模組，但 session_state 仍持有舊實例；舊實例沒有新方法
（例如 preview_fingerprint），會在畫面上變成 AttributeError。
"""

from __future__ import annotations

import pathlib

from streamlit.testing.v1 import AppTest

ROOT = pathlib.Path(__file__).resolve().parent.parent

SCRIPT = '''
import streamlit as st

from app.ui import web_app
from app.ui.controller import CONTROLLER_API_VERSION


class StaleController:  # 模擬改版前、連 api_version 都沒有的實例
    pass


class OldApiController:  # 模擬有 api_version 但落後的實例
    api_version = CONTROLLER_API_VERSION - 1


mode = "__MODE__"

if "seeded" not in st.session_state:
    if mode == "missing_api":
        st.session_state["controller"] = StaleController()
    elif mode == "old_api":
        st.session_state["controller"] = OldApiController()
    st.session_state["settings"] = None
    st.session_state["seeded"] = True

settings = dict(
    root=r"__ROOT__",
    db_path=r"__DB__",
    workers=1,
    mode=web_app.MODE_LABELS["A（依科目）"],
    template=None,
)
controller = web_app._ensure_controller(settings)
st.session_state["rebuilt"] = type(controller).__name__
st.session_state["api_ok"] = (
    getattr(controller, "api_version", None) == CONTROLLER_API_VERSION
)
'''


def _run(tmp_path: pathlib.Path, mode: str) -> AppTest:
    script = (
        SCRIPT.replace("__MODE__", mode)
        .replace("__ROOT__", str(tmp_path / "downloads"))
        .replace("__DB__", str(tmp_path / "downloads" / "manifest.db"))
    )
    app = AppTest.from_string(script, default_timeout=60)
    app.run()
    return app


def test_stale_controller_without_api_version_is_rebuilt(tmp_path) -> None:
    app = _run(tmp_path, "missing_api")

    assert not app.exception
    assert app.session_state["rebuilt"] == "AppController"
    assert app.session_state["api_ok"] is True


def test_controller_with_old_api_version_is_rebuilt(tmp_path) -> None:
    app = _run(tmp_path, "old_api")

    assert not app.exception
    assert app.session_state["rebuilt"] == "AppController"
    assert app.session_state["api_ok"] is True
