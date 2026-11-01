"""歸檔路徑預覽的渲染測試：按鈕要有回饋、表格要出現、選取變更後舊預覽失效。"""

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
from app.engine.path_builder import PathMode
from app.models import QuerySnapshot, QuerySpec
from app.ui import web_app
from app.ui.controller import AppController


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
    seeded = AppController(query_service=None, task_service=None)
    seeded.snapshot = snapshot
    seeded.state = build_candidates(snapshot.records, FilterState()).initial_state(
        year_start=113, year_end=113
    )
    st.session_state["ctrl"] = seeded

controller = st.session_state["ctrl"]
web_app._render_preview(controller, PathMode.A, None)
st.session_state["plan_files"] = len(st.session_state.get("preview")[1].files) if st.session_state.get("preview") else 0
'''


@pytest.fixture(name="preview_app")
def preview_app_fixture() -> AppTest:
    app = AppTest.from_string(
        SCRIPT_TEMPLATE.replace("__FIXTURE__", str(FIXTURE)), default_timeout=60
    )
    app.run()
    return app


def test_preview_requires_click_then_shows_table(preview_app: AppTest) -> None:
    assert not preview_app.exception
    assert len(preview_app.dataframe) == 0, "未按按鈕前不應有表格"

    preview_app.button(key="preview-button").click().run()

    assert not preview_app.exception
    assert len(preview_app.dataframe) == 1, "按「計算預覽」後要出現表格"
    assert preview_app.session_state["plan_files"] > 0


def test_preview_shows_summary(preview_app: AppTest) -> None:
    preview_app.button(key="preview-button").click().run()

    captions = " ".join(c.value for c in preview_app.caption)
    assert "個檔案" in captions and "個資料夾" in captions


def test_preview_is_invalidated_when_selection_changes(preview_app: AppTest) -> None:
    preview_app.button(key="preview-button").click().run()
    assert len(preview_app.dataframe) == 1

    preview_app.session_state["ctrl"].set_selection("subjects", ())
    preview_app.run()

    assert not preview_app.exception
    assert len(preview_app.dataframe) == 0, "選取變更後不應顯示過期預覽"


def test_preview_with_empty_selection_shows_hint(preview_app: AppTest) -> None:
    controller = preview_app.session_state["ctrl"]
    controller.set_selection("exams", ())
    controller.set_selection("categories", ())
    controller.set_selection("subjects", ())

    preview_app.button(key="preview-button").click().run()

    assert not preview_app.exception
    assert len(preview_app.dataframe) == 0
