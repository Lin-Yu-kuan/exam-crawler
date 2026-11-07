"""下載失敗清單的 UI 回歸測試。

`_render_task` 的失敗區塊只有在至少一個附件失敗時才會渲染；曾經因為漏匯入
`AssetRole` 而在這個分支拋出 NameError，導致使用者看不到任何失敗原因。
"""

from __future__ import annotations

import pathlib

import pytest
from streamlit.testing.v1 import AppTest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "html" / "result_sample.html"

SCRIPT_TEMPLATE = '''
import pathlib

import streamlit as st

from app.crawler.parser import parse_result_page
from app.engine.filter import FilterState, build_candidates
from app.engine.task_service import TaskState
from app.models import QuerySnapshot, QuerySpec, TaskStatus
from app.ui import web_app
from app.ui.controller import AppController


class _NoNetwork:
    api_version = web_app.EXPECTED_TASK_SERVICE_API_VERSION

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError("UI 不應呼叫 " + name)

        return _boom


class _FailedTaskService(_NoNetwork):
    def __init__(self, state):
        self._state = state

    def get(self, task_id):
        return self._state

    def export_results(self, task_id):
        return ()


html = pathlib.Path(r"__FIXTURE__").read_text(encoding="utf-8")
page = parse_result_page(html)
snapshot = QuerySnapshot(
    snapshot_id="snap-fail",
    cache_key="key-fail",
    spec=QuerySpec(113, 113),
    records=page.records,
    status=page.status,
    years=(),
    acquired_at="2026-11-03T12:00:00+08:00",
    parser_version="html-v1",
    source_url="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
)
state = TaskState(
    task_id="task-fail",
    snapshot_id="snap-fail",
    status=TaskStatus.FAILED,
    attempt=1,
    total=2,
    completed=1,
    skipped=0,
    deduped=0,
    failed=1,
    cancelled=0,
    pending=0,
    started_at="2026-11-03T12:00:00+08:00",
    finished_at="2026-11-03T12:00:05+08:00",
    failures=((r"__ASSET_ID__", "transport-error", "連線中斷：ReadError"),),
)
controller = AppController(
    query_service=_NoNetwork(),
    task_service=_FailedTaskService(state),
    root=r"__ROOT__",
)
controller.snapshot = snapshot
controller.state = build_candidates(snapshot.records, FilterState()).initial_state(
    year_start=113, year_end=113
)
controller.task_id = "task-fail"
web_app._render_task(controller, False)
'''


def _render(asset_id: str, tmp_path: pathlib.Path) -> AppTest:
    script = (
        SCRIPT_TEMPLATE.replace("__FIXTURE__", str(FIXTURE))
        .replace("__ROOT__", str(tmp_path))
        .replace("__ASSET_ID__", asset_id)
    )
    app = AppTest.from_string(script, default_timeout=60)
    app.run()
    return app


@pytest.fixture(scope="module")
def known_asset_id() -> str:
    from app.crawler.parser import parse_result_page

    page = parse_result_page(FIXTURE.read_text(encoding="utf-8"))
    for record in page.records:
        if record.assets:
            return record.assets[0].asset_id
    raise AssertionError("fixture 沒有任何附件")


def _captions(app: AppTest) -> str:
    return " ".join(str(item.value) for item in app.caption)


def test_failure_list_renders_for_known_asset(known_asset_id, tmp_path) -> None:
    """失敗附件存在於快照時，必須顯示角色（試題／答案），不得拋 NameError。"""
    app = _render(known_asset_id, tmp_path)

    assert not app.exception
    assert "考選部伺服器偶有連線逾時" in _captions(app)
    markdown = " ".join(str(item.value) for item in app.markdown)
    assert "官方原始" in markdown
    assert "transport-error" in markdown


def test_failure_list_renders_for_unknown_asset(tmp_path) -> None:
    """失敗附件已不在快照（例如重新查詢過）時，走純文字 fallback，仍不得例外。"""
    app = _render("missing-asset-id", tmp_path)

    assert not app.exception
    assert "考選部伺服器偶有連線逾時" in _captions(app)
    texts = " ".join(str(item.value) for item in app.text)
    assert "missing-asset-id" in texts


LEGACY_STATE_SCRIPT = '''
import types
import streamlit as st
from app.models import TaskStatus
from app.ui import web_app

legacy_state = types.SimpleNamespace(
    task_id="task-legacy",
    status=TaskStatus.COMPLETED,
    total=5,
    completed=5,
    skipped=0,
    deduped=0,
    failed=0,
    cancelled=0,
    group_shared=0,
    relocated=0,
    done=5,
    message="全部完成（5/5）",
)

class _LegacyTaskController:
    api_version = web_app.EXPECTED_CONTROLLER_API_VERSION
    _root = r"__ROOT__"
    snapshot = None
    state = None
    last_error = None

    def progress(self):
        return legacy_state

    def can_download(self):
        return False

    def is_task_active(self):
        return False

    def export_paths(self):
        return ()

controller = _LegacyTaskController()
web_app._render_task(controller, False, root=r"__ROOT__")
'''


def test_render_task_with_legacy_state_without_units_total(tmp_path) -> None:
    """模擬舊版或缺少 units_total 屬性的 TaskState 實例，_render_task 必須平滑降級而不拋 AttributeError。"""
    script = LEGACY_STATE_SCRIPT.replace("__ROOT__", str(tmp_path))
    app = AppTest.from_string(script, default_timeout=60)
    app.run()

    assert not app.exception
    # 確認成功降級以附件總數顯示進度
    assert any("已處理 5／5 份" in str(getattr(p, "text", "")) for p in app.progress)

