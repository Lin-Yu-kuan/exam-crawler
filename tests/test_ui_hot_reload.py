"""更新相容性保護：不相容的 session 提示重啟，不替換服務。"""

from __future__ import annotations

import pathlib
import pytest

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


class LegacyService:
    api_version = 0
    def export_results(self, task_id):
        return ()

class MissingMethodService:
    api_version = 1

class CurrentControllerWithOldService:
    api_version = CONTROLLER_API_VERSION
    task_id = "running-task"
    _task_service = LegacyService()
    def export_paths(self):
        raise AssertionError("不應匯出舊任務")

mode = "__MODE__"

if "seeded" not in st.session_state:
    if mode == "missing_api":
        st.session_state["controller"] = StaleController()
    elif mode == "old_api":
        st.session_state["controller"] = OldApiController()
    elif mode in ("old_service", "missing_method"):
        cached = CurrentControllerWithOldService()
        if mode == "missing_method":
            cached._task_service = MissingMethodService()
        st.session_state["controller"] = cached
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


@pytest.mark.parametrize("mode", ["missing_api", "old_api", "old_service", "missing_method"])
def test_incompatible_session_stops_without_rebuilding(tmp_path, mode):
    app = _run(tmp_path, mode)
    assert not app.exception
    assert len(app.error) == 1
    assert "重新啟動應用程式" in app.error[0].value
    cached = app.session_state["controller"]
    app.run()
    assert not app.exception
    assert app.session_state["controller"] is cached
    assert app.session_state["settings"] is None
    assert not (tmp_path / "downloads" / "manifest.db").exists()
    if mode in ("old_service", "missing_method"):
        assert cached.task_id == "running-task"


def test_fresh_runtime_and_compatible_rerun_reuse_controller(tmp_path):
    app = _run(tmp_path, "fresh")
    assert not app.exception
    assert not app.error
    assert app.session_state["api_ok"] is True
    cached = app.session_state["controller"]
    app.run()
    assert not app.exception
    assert app.session_state["controller"] is cached


def test_old_loaded_service_class_stops_before_building(monkeypatch):
    from types import SimpleNamespace
    from app.ui import web_app

    class Stopped(Exception):
        pass

    errors = []
    def stop():
        raise Stopped()

    monkeypatch.setattr(web_app, "TaskService", SimpleNamespace(api_version=0))
    monkeypatch.setattr(web_app.st, "error", errors.append)
    monkeypatch.setattr(web_app.st, "stop", stop)
    with pytest.raises(Stopped):
        web_app._require_compatible_runtime()
    assert errors == [web_app.RESTART_REQUIRED_MESSAGE]
