"""候選區塊的渲染測試：以 AppTest 實際執行 Streamlit 元件（離線，用 fixture HTML）。

驗證「已選 chips（可叉除）」與「從可見清單加入」兩種操作都存在，
且叉除只改本機選取、不會呼叫查詢服務。
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
st.session_state["first_category"] = controller.visible("categories")[0].value

web_app._render_candidates(controller)
st.session_state["exams_selected"] = sorted(controller.state.exams)
st.session_state["subjects_total"] = len(controller.selected_values("subjects"))
st.session_state["categories_total"] = len(controller.candidates().categories)
st.session_state["categories_selected_count"] = len(controller.state.categories)
'''


@pytest.fixture(name="candidates_app")
def candidates_app_fixture() -> AppTest:
    app = AppTest.from_string(
        SCRIPT_TEMPLATE.replace("__FIXTURE__", str(FIXTURE)), default_timeout=60
    )
    app.run()
    return app


def test_candidates_block_renders_chips_and_add_list(candidates_app: AppTest) -> None:
    assert not candidates_app.exception
    pills = candidates_app.get("pills")
    assert len(pills) == 3, "三個欄位都應有「已選」chips 列"
    assert len(candidates_app.multiselect) == 0, "全選時沒有可加入項目"


def test_add_list_appears_after_clearing_visible(candidates_app: AppTest) -> None:
    candidates_app.button(key="clear-categories").click().run()

    assert not candidates_app.exception
    lists = candidates_app.multiselect
    assert len(lists) == 1
    assert lists[0].label.startswith("從可見清單加入")
    total = candidates_app.session_state["categories_total"]
    assert len(lists[0].options) == total, "加入清單要全部列出，否則輸入搜尋會找不到"


def test_adding_from_list_updates_selection_only(candidates_app: AppTest) -> None:
    candidates_app.button(key="clear-categories").click().run()
    assert candidates_app.session_state["categories_selected_count"] == 0

    chosen = candidates_app.session_state["first_category"]
    candidates_app.multiselect[0].set_value([chosen]).run()

    assert not candidates_app.exception
    assert candidates_app.session_state["categories_selected_count"] == 1


def test_chips_are_labeled_for_removal(candidates_app: AppTest) -> None:
    labels = [pill.label for pill in candidates_app.get("pills")]
    assert labels == ["已選（點一下可叉除）"] * 3


def test_chips_are_capped_at_max(candidates_app: AppTest) -> None:
    """科目選很多時只列前 MAX_CHIPS 項，避免畫面爆掉。"""
    from app.ui.web_app import MAX_CHIPS

    subjects_pills = candidates_app.get("pills")[2]
    assert len(subjects_pills.options) == MAX_CHIPS
    assert candidates_app.session_state["subjects_total"] > MAX_CHIPS


def test_unselecting_chip_removes_only_that_value(candidates_app: AppTest) -> None:
    """叉掉 chip 只改本機選取（網路替身會讓任何呼叫失敗）。"""
    exams_pills = candidates_app.get("pills")[0]
    victim = exams_pills.value[0]
    before = set(candidates_app.session_state["exams_selected"])
    assert victim in before

    exams_pills.unselect(victim).run()

    assert not candidates_app.exception
    assert set(candidates_app.session_state["exams_selected"]) == before - {victim}


def test_metric_row_reflects_initial_selection(candidates_app: AppTest) -> None:
    metrics = {metric.label: metric.value for metric in candidates_app.metric}
    assert metrics["考科數"] != "0"
    assert metrics["試題附件"] != "0"
