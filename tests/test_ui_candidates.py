"""候選區塊的渲染測試：以 AppTest 實際執行 Streamlit 元件（離線，用 fixture HTML）。

驗證「300px 滾動清單表格」、「多向互相收斂」與統計卡片正常呈現，
且任何篩選操作都只改動記憶體狀態、不呼叫網路服務。
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
from app.models import QuerySnapshot, QuerySpec
from app.ui import web_app
from app.ui.controller import AppController


class _NoNetwork:
    api_version = 1

    def __getattr__(self, name):  # 任何呼叫都代表越界使用網路
        def _boom(*args, **kwargs):
            raise AssertionError("UI 不應呼叫 " + name)

        return _boom


if "ctrl" not in st.session_state:
    html = pathlib.Path(r"__FIXTURE__").read_text(encoding="utf-8")
    page = parse_result_page(html)
    snapshot = QuerySnapshot(
        snapshot_id="snap-ui",
        cache_key="key-ui",
        spec=QuerySpec(113, 113),
        records=page.records,
        status=page.status,
        years=(),
        acquired_at="2026-11-01T12:00:00+08:00",
        parser_version="html-v1",
        source_url="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
    )
    seeded = AppController(query_service=_NoNetwork(), task_service=_NoNetwork())
    seeded.snapshot = snapshot
    seeded.state = build_candidates(snapshot.records, FilterState()).initial_state(
        year_start=113, year_end=113
    )
    st.session_state["ctrl"] = seeded

controller = st.session_state["ctrl"]
web_app._render_candidates(controller)
web_app._render_task(controller, False)
st.session_state["exams_total"] = len(controller.faceted_candidates().exams)
st.session_state["categories_total"] = len(controller.faceted_candidates().categories)
st.session_state["subjects_total"] = len(controller.faceted_candidates().subjects)
'''


@pytest.fixture(name="candidates_app")
def candidates_app_fixture() -> AppTest:
    app = AppTest.from_string(
        SCRIPT_TEMPLATE.replace("__FIXTURE__", str(FIXTURE)), default_timeout=60
    )
    app.run()
    return app


def test_candidates_block_renders_three_tables(candidates_app: AppTest) -> None:
    assert not candidates_app.exception
    # 三個維度都應該渲染出 data_editor 300px 捲軸表格
    assert len(candidates_app.dataframe) == 3


def test_candidates_block_has_full_items(candidates_app: AppTest) -> None:
    assert not candidates_app.exception
    assert candidates_app.session_state["exams_total"] > 0
    assert candidates_app.session_state["categories_total"] > 0
    assert candidates_app.session_state["subjects_total"] > 0


def test_metric_row_reflects_initial_selection(candidates_app: AppTest) -> None:
    assert not candidates_app.exception
    metrics = {m.label: m.value for m in candidates_app.metric}
    assert "已選考科" in metrics
    assert int(metrics["已選考科"]) > 0
    assert "試題附件" in metrics
    assert "答案附件" in metrics


def test_none_button_sets_zero_selection_and_disables_download() -> None:
    app = AppTest.from_string(
        SCRIPT_TEMPLATE.replace("__FIXTURE__", str(FIXTURE)), default_timeout=60
    )
    app.run()
    assert not app.exception

    # 點擊科目欄的「全不選」按鈕
    none_btn = next(b for b in app.button if b.key == "none-btn-subjects")
    none_btn.click().run()
    assert not app.exception

    metrics = {m.label: m.value for m in app.metric}
    assert int(metrics["已選考科"]) == 0
    ctrl = app.session_state["ctrl"]
    assert ctrl.active_filters["subjects"] == set()
    assert ctrl.can_download() is False
