"""候選區塊的渲染測試：以 AppTest 實際執行 Streamlit 元件（離線，用 fixture HTML）。

驗證「300px 滾動清單表格」、「多向互相收斂」與統計卡片正常呈現，
且任何篩選操作都只改動記憶體狀態、不呼叫網路服務。
"""

from __future__ import annotations

import pathlib

import pytest

from streamlit.testing.v1 import AppTest

from app.models import QuerySpec

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
    api_version = 3

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
    assert "不重複試題" in metrics
    assert int(metrics["不重複試題"]) <= int(metrics["試題附件"])


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

def test_history_selector_does_not_revert_modified_form_inputs() -> None:
    history_script = '''
import streamlit as st
from app.models import QuerySpec
from app.ui import web_app

class _DummyStore:
    def list_recent_specs(self, limit=10):
        return (
            QuerySpec(112, 112, exam_name_keyword="高考", category_keyword="資訊", subject_keyword="資料結構"),
        )

class _DummyController:
    last_error = None
    snapshot = None

    def __init__(self):
        self._store = _DummyStore()
        self.submitted_spec = None

    def recent_query_specs(self, limit=10):
        return self._store.list_recent_specs(limit=limit)

    def query(self, spec, force=False):
        self.submitted_spec = spec

if "ctrl" not in st.session_state:
    st.session_state["ctrl"] = _DummyController()

controller = st.session_state["ctrl"]
web_app._render_query(controller)
'''
    app = AppTest.from_string(history_script, default_timeout=30)
    app.run()
    assert not app.exception

    # 1. 預設狀態
    assert app.selectbox[0].key == "history-selector-0"
    assert app.text_input(key="q_subject").value == ""

    # 2. 選擇歷史紀錄
    app.selectbox[0].select("112年｜考試：高考、類科：資訊、科目：資料結構").run()
    assert not app.exception

    # 驗證歷史值已帶入，且選單 key 遞增並自動歸位回到提示「（點選歷史條件自動帶入…）」
    assert app.text_input(key="q_subject").value == "資料結構"
    assert app.selectbox[0].key == "history-selector-1"
    assert app.selectbox[0].value == "（點選歷史條件自動帶入…）"

    # 3. 使用者手動修改科目關鍵字為「演算法」並送出查詢
    app.text_input(key="q_subject").input("演算法")
    app.button[0].click().run()
    assert not app.exception

    # 驗證送出的條件為修改後的「演算法」，而不是被跳回「資料結構」
    ctrl = app.session_state["ctrl"]
    assert ctrl.submitted_spec is not None
    assert ctrl.submitted_spec.subject_keyword == "演算法"
    assert app.text_input(key="q_subject").value == "演算法"


def test_unapplied_condition_change_is_warned() -> None:
    """年度改了卻沒按「查詢試題」時，必須標明結果仍屬上一次的條件。"""
    script = '''
import streamlit as st
from app.models import ParseStatus, QuerySnapshot, QuerySpec
from app.ui import web_app


def _snapshot(spec):
    return QuerySnapshot(
        snapshot_id=f"snap-{spec.start_year}-{spec.end_year}",
        cache_key="key",
        spec=spec,
        records=(),
        status=ParseStatus.OK,
        years=(),
        acquired_at="2026-11-03T19:00:00+08:00",
        parser_version="test",
        source_url="https://example.invalid",
    )


class _DummyController:
    last_error = None

    def __init__(self):
        self.snapshot = _snapshot(QuerySpec(114, 114, subject_keyword="資料結構"))
        self.submitted_spec = None

    def recent_query_specs(self, limit=10):
        return ()

    def query_scope_text(self):
        spec = self.snapshot.spec
        parts = [f"年度 {spec.start_year}–{spec.end_year}"]
        if spec.subject_keyword:
            parts.append(f"科目含「{spec.subject_keyword}」")
        return "｜".join(parts)

    def query(self, spec, force=False):
        self.submitted_spec = spec
        self.snapshot = _snapshot(spec)


for key, value in {"q_start": 114, "q_end": 114, "q_subject": "資料結構"}.items():
    if key not in st.session_state:
        st.session_state[key] = value
if "ctrl" not in st.session_state:
    st.session_state["ctrl"] = _DummyController()

web_app._render_query(st.session_state["ctrl"])
'''
    app = AppTest.from_string(script, default_timeout=30)
    app.run()
    assert not app.exception
    assert not [w for w in app.warning if "尚未查詢" in w.value], "條件相符時不應提示"

    # 只改迄年、還沒送出查詢：結果仍屬 114–114，必須明確提示
    app.number_input(key="q_end").set_value(115).run()
    assert not app.exception
    warnings = [w.value for w in app.warning if "尚未查詢" in w.value]
    assert warnings, "改動年度後必須提示條件尚未查詢"
    assert "114–115年" in warnings[0]
    assert "114年｜科目：資料結構" in warnings[0]
    assert any("年度 114–114" in m.value for m in app.markdown if "筆紀錄" in m.value)

    # 按下查詢後，送出的條件就是表單上的 114–115，提示應消失
    app.button[0].click().run()
    assert not app.exception
    ctrl = app.session_state["ctrl"]
    assert ctrl.submitted_spec == QuerySpec(114, 115, subject_keyword="資料結構")
    assert not [w for w in app.warning if "尚未查詢" in w.value]
    assert any("年度 114–115" in m.value for m in app.markdown if "筆紀錄" in m.value)


def test_common_subject_query_mode_calls_query_common_subject() -> None:
    """切換至「共同科目快速查詢」模式並送出時，應呼叫 controller.query_common_subject。"""
    script = '''
import streamlit as st
from app.ui import web_app

class _DummyController:
    last_error = None
    snapshot = None

    def __init__(self):
        self.common_calls = []
        self.normal_calls = []

    def recent_query_specs(self, limit=10):
        return ()

    def query(self, spec, force=False):
        self.normal_calls.append((spec, force))

    def query_common_subject(self, start, end, preset_key, *, exam_name_keyword="", force=False):
        self.common_calls.append((start, end, preset_key, exam_name_keyword, force))

if "ctrl" not in st.session_state:
    st.session_state["ctrl"] = _DummyController()

web_app._render_query(st.session_state["ctrl"])
'''
    app = AppTest.from_string(script, default_timeout=30)
    app.run()
    assert not app.exception

    app.radio(key="q_mode").set_value("共同科目快速查詢").run()
    assert not app.exception

    app.selectbox(key="q_common_preset").select("law_knowledge").run()
    assert not app.exception

    app.button[0].click().run()
    assert not app.exception

    ctrl = app.session_state["ctrl"]
    assert ctrl.normal_calls == []
    assert ctrl.common_calls == [(113, 113, "law_knowledge", "", False)]

