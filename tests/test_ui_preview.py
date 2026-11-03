"""歸檔路徑樹狀圖即時連動與模式切換測試。"""

from __future__ import annotations

import hashlib
import pathlib
import re
import pytest

from streamlit.testing.v1 import AppTest

from app.engine.downloader import DownloadResult, DownloadTarget, Downloader
from app.engine.filter import FilterState
from app.engine.path_builder import DEFAULT_TEMPLATES, PathMode
from app.models import (
    AssetRole,
    AttachmentStatus,
    ExamAsset,
    ExamRecord,
    QuerySnapshot,
    QuerySpec,
    QuestionType,
    TypeEvidence,
)
from app.storage.manifest import ManifestStore
from app.ui import web_app
from app.ui.web_app import MODE_LABELS, SAMPLE_VARS, build_tree_preview


def test_build_tree_preview_all_modes() -> None:
    root = r"C:\國考題庫"
    # Mode A (依科目)
    preview_a = build_tree_preview(root, DEFAULT_TEMPLATES["A"])
    assert "資料結構" in preview_a
    assert "113_高考三級考試_資訊處理_申論試題.pdf" in preview_a
    assert preview_a.count("📁") >= 2
    assert preview_a.count("📄") == 1

    # Mode B (依年度考試)
    preview_b = build_tree_preview(root, DEFAULT_TEMPLATES["B"])
    assert "113" in preview_b
    assert "高考三級考試_資訊處理" in preview_b
    assert "資料結構_申論試題.pdf" in preview_b
    assert preview_b.count("📁") >= 3

    # Mode C (平鋪)
    preview_c = build_tree_preview(root, DEFAULT_TEMPLATES["C"])
    assert preview_c.count("📁") == 1  # 只有根目錄
    assert "113_高考三級考試_資訊處理_資料結構_申論試題.pdf" in preview_c


def test_sidebar_cloud_root_uses_timestamped_folder(tmp_path, monkeypatch) -> None:
    """雲端根目錄為國考題庫_YYYYMMDD_HHMMSS；下載前預覽顯示待命名的佔位名稱。"""
    monkeypatch.setattr(web_app, "is_cloud_env", lambda: True)
    monkeypatch.setattr(web_app.tempfile, "tempdir", str(tmp_path))
    script = '''
import streamlit as st
from app.ui import web_app

settings, _ = web_app._sidebar()
st.session_state["root"] = settings["root"]
st.session_state["preview"] = web_app.build_tree_preview(
    web_app.cloud_display_root(settings["root"]), web_app.DEFAULT_TEMPLATES["A"]
)
'''
    app = AppTest.from_string(script, default_timeout=30)
    app.run()
    assert not app.exception

    root = pathlib.Path(app.session_state["root"])
    assert root.parent == tmp_path
    assert re.fullmatch(r"國考題庫_\d{8}_\d{6}", root.name)
    assert root.is_dir()
    assert web_app.CLOUD_ROOT_PLACEHOLDER in app.session_state["preview"]
    assert root.name not in app.session_state["preview"]


def test_cloud_root_is_renamed_when_download_starts(tmp_path, monkeypatch) -> None:
    """按下下載時才以當下時間命名根目錄，並清除尚未使用的空白根目錄。"""
    monkeypatch.setattr(web_app, "is_cloud_env", lambda: True)
    monkeypatch.setattr(web_app.tempfile, "tempdir", str(tmp_path))
    script = '''
import itertools
import streamlit as st
from app.ui import web_app

_stamps = itertools.count(1)
web_app.cloud_root_name = lambda stamp=None: f"國考題庫_20261103_2125{next(_stamps):02d}"

if "session_root" not in st.session_state:
    settings, _ = web_app._sidebar()
    st.session_state["session_root"] = settings["root"]

if "download_root_value" not in st.session_state:
    st.session_state["download_root_value"] = web_app.rotate_cloud_root()
'''
    app = AppTest.from_string(script, default_timeout=30)
    app.run()
    assert not app.exception

    session_root = pathlib.Path(app.session_state["session_root"])
    download_root = pathlib.Path(app.session_state["download_root_value"])
    assert download_root.parent == tmp_path
    assert download_root.name == "國考題庫_20261103_212502"
    assert download_root != session_root
    assert app.session_state[web_app.CLOUD_ROOT_NAMED_KEY] is True
    # 尚未下載的舊根目錄只剩 _系統資料，應被清除
    assert not session_root.exists()


def test_discard_unused_cloud_root_keeps_downloaded_files(tmp_path) -> None:
    """已有 PDF 的根目錄不得被清除。"""
    root = tmp_path / "國考題庫_20261103_212530"
    (root / web_app.SYSTEM_DIR_NAME).mkdir(parents=True)
    (root / "資料結構").mkdir()
    (root / "資料結構" / "試題.pdf").write_bytes(b"%PDF-1.4 dummy")

    web_app._discard_unused_cloud_root(str(root))
    assert (root / "資料結構" / "試題.pdf").is_file()


def test_discard_unused_cloud_root_removes_empty_session_dir(tmp_path) -> None:
    root = tmp_path / "國考題庫_20261103_212500"
    (root / web_app.SYSTEM_DIR_NAME).mkdir(parents=True)

    web_app._discard_unused_cloud_root(str(root))
    assert not root.exists()


def test_cloud_download_click_renames_root_then_starts_task(tmp_path, monkeypatch) -> None:
    """按下「開始下載」後：根目錄改用下載時間命名，任務在新的根目錄啟動。"""
    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "html" / "result_sample.html"
    monkeypatch.setattr(web_app, "is_cloud_env", lambda: True)
    monkeypatch.setattr(web_app.tempfile, "tempdir", str(tmp_path))
    script = '''
import pathlib

import streamlit as st

from app.crawler.parser import parse_result_page
from app.engine.filter import FilterState, build_candidates
from app.engine.task_service import TaskState
from app.models import QuerySnapshot, QuerySpec, TaskStatus
from app.ui import web_app
from app.ui.controller import AppController


def _next_stamp(stamp=None):
    st.session_state["stamp"] = st.session_state.get("stamp", 0) + 1
    return "國考題庫_20261103_2130" + f"{st.session_state['stamp']:02d}"


web_app.cloud_root_name = _next_stamp


class _NoNetwork:
    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError("UI 不應呼叫 " + name)

        return _boom


class _Service(_NoNetwork):
    api_version = web_app.EXPECTED_TASK_SERVICE_API_VERSION

    def __init__(self):
        self.submitted = []

    def submit(self, snapshot, frozen):
        state = TaskState(
            task_id="task-1",
            snapshot_id=snapshot.snapshot_id,
            status=TaskStatus.COMPLETED,
            attempt=1,
            total=0,
            completed=0,
            skipped=0,
            deduped=0,
            failed=0,
            cancelled=0,
            pending=0,
            started_at=None,
            finished_at=None,
        )
        self.submitted.append(state)
        return state

    def get(self, task_id):
        return self.submitted[-1]

    def export_results(self, task_id):
        return ()


if "service" not in st.session_state:
    st.session_state["service"] = _Service()
    st.session_state["initial_root"] = web_app.new_cloud_root()
    pathlib.Path(st.session_state["initial_root"], web_app.SYSTEM_DIR_NAME).mkdir(parents=True)
    st.session_state["download_root"] = st.session_state["initial_root"]

html = pathlib.Path(r"__FIXTURE__").read_text(encoding="utf-8")
page = parse_result_page(html)
snapshot = QuerySnapshot(
    snapshot_id="snap-cloud",
    cache_key="key-cloud",
    spec=QuerySpec(113, 113),
    records=page.records,
    status=page.status,
    years=(),
    acquired_at="2026-11-03T12:00:00+08:00",
    parser_version="html-v1",
    source_url="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
)
root = st.session_state["download_root"]
controller = AppController(
    query_service=_NoNetwork(),
    task_service=st.session_state["service"],
    root=root,
)
controller.snapshot = snapshot
controller.state = build_candidates(snapshot.records, FilterState()).initial_state(
    year_start=113, year_end=113
)
web_app._render_task(controller, False, root)
'''
    app = AppTest.from_string(
        script.replace("__FIXTURE__", str(fixture)), default_timeout=60
    )
    app.run()
    assert not app.exception

    initial_root = pathlib.Path(app.session_state["initial_root"])
    assert initial_root.exists()

    app.button[0].click().run()
    assert not app.exception

    service = app.session_state["service"]
    assert len(service.submitted) == 1, "按下下載後應在同一次操作啟動任務"
    assert web_app.PENDING_DOWNLOAD_KEY not in app.session_state

    download_root = pathlib.Path(app.session_state["download_root"])
    assert download_root.name == "國考題庫_20261103_213002", "根目錄需改用下載當下的時間"
    assert not initial_root.exists(), "未使用的舊根目錄應清除"


def test_sidebar_mode_labels_only_has_three_modes() -> None:
    assert list(MODE_LABELS.keys()) == ["A（依科目）", "B（依年度考試）", "C（平鋪）"]
    assert "D（自訂模板）" not in MODE_LABELS


def test_ensure_controller_preserves_snapshot_and_state(tmp_path: pathlib.Path) -> None:
    script = '''
import pathlib
import streamlit as st
from app.engine.filter import FilterState
from app.engine.path_builder import PathMode
from app.models import QuerySnapshot, QuerySpec
from app.ui import web_app

db_path = r"__DB__"
root = r"__ROOT__"

mode_val = st.session_state.get("test_mode", PathMode.A)

settings = {
    "root": root,
    "db_path": db_path,
    "workers": 4,
    "mode": mode_val,
    "template": None,
}

ctrl = web_app._ensure_controller(settings)

if "seeded_snap" not in st.session_state:
    dummy_snapshot = QuerySnapshot(
        snapshot_id="snap-1",
        cache_key="key-1",
        spec=QuerySpec(113, 113),
        records=(),
        status=1,
        years=(),
        acquired_at="2026-11-03T12:00:00",
        parser_version="html-v1",
        source_url="https://wwwq.moex.gov.tw",
    )
    ctrl.snapshot = dummy_snapshot
    ctrl.state = FilterState(exams={"高考三級"}, categories={"資訊處理"}, subjects={"資料結構"})
    ctrl.active_filters = {"exams": {"高考三級"}, "categories": {"資訊處理"}, "subjects": {"資料結構"}}
    st.session_state["seeded_snap"] = True
'''
    db_file = tmp_path / "manifest.db"
    code = script.replace("__DB__", str(db_file)).replace("__ROOT__", str(tmp_path))
    app = AppTest.from_string(code, default_timeout=30)
    app.run()
    assert not app.exception

    # 首次執行，確認已有 snapshot
    ctrl1 = app.session_state["controller"]
    assert ctrl1.snapshot.snapshot_id == "snap-1"
    assert ctrl1.state.subjects == {"資料結構"}

    # 切換為 Mode B 並重跑
    app.session_state["test_mode"] = PathMode.B
    app.run()
    assert not app.exception

    ctrl2 = app.session_state["controller"]
    assert ctrl2 is not ctrl1
    # 驗證新控制器已保存舊控制器的 snapshot 與篩選狀態
    assert ctrl2.snapshot.snapshot_id == "snap-1"
    assert ctrl2.state.subjects == {"資料結構"}
    assert ctrl2.active_filters["subjects"] == {"資料結構"}


def test_downloader_relinks_to_new_mode_path_when_mode_changes(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "bank"
    root.mkdir()
    store = ManifestStore(tmp_path / "manifest.db")

    # 建立舊模式 A 的檔案與 manifest 紀錄
    old_rel = "資料結構/113_高考三級_資訊處理_申論試題.pdf"
    old_file = root / "資料結構" / "113_高考三級_資訊處理_申論試題.pdf"
    old_file.parent.mkdir(parents=True)
    pdf_content = b"%PDF-1.4 dummy test content"
    old_file.write_bytes(pdf_content)
    sha256 = hashlib.sha256(pdf_content).hexdigest()

    asset_id = "asset-test-1"
    store.mark_attachment_started(
        asset_id=asset_id,
        snapshot_id="snap-1",
        record_id="rec-1",
        url="https://wwwq.moex.gov.tw/test.pdf",
        role=AssetRole.QUESTION.value,
    )
    store.begin_attachment_verify(
        asset_id=asset_id, path=old_rel, sha256=sha256, size=len(pdf_content), version=1
    )
    store.complete_attachment(
        asset_id=asset_id,
        path=old_rel,
        sha256=sha256,
        size=len(pdf_content),
        version=1,
        question_type="申論試題",
    )

    # 模擬新任務請求 Mode B 的新路徑
    new_rel = "113/高考三級_資訊處理/資料結構_申論試題.pdf"
    asset = ExamAsset(
        asset_id=asset_id,
        official_id="test",
        role=AssetRole.QUESTION,
        original_label="試題",
        url="https://wwwq.moex.gov.tw/test.pdf",
        observed_at="2026-11-03T12:00:00",
        source_version=1,
        evidence=TypeEvidence(
            question_type=QuestionType.ESSAY,
            source="test",
            reason="驗證",
            rule_version="test-v1",
        ),
    )
    rec = ExamRecord(
        record_id="rec-1",
        official_id="113010",
        year=113,
        exam_name="高考三級",
        exam_short_name="高考三級",
        category="資訊處理",
        subject="資料結構",
        level="三等",
        source_page="1",
        assets=(asset,),
    )
    target = DownloadTarget(
        record=rec,
        asset=asset,
        relative_path=new_rel,
        resolve_path=lambda inspection: new_rel,
    )

    downloader = Downloader(root=str(root), store=store)
    result = downloader.download(target)

    # 驗證回傳的相對路徑已是新路徑，且檔案已搬到新目錄下
    assert result.relative_path == new_rel
    assert result.relocated is True
    assert result.skipped is True
    assert (root / "113" / "高考三級_資訊處理" / "資料結構_申論試題.pdf").is_file()
    assert (root / "113" / "高考三級_資訊處理" / "資料結構_申論試題.pdf").read_bytes() == pdf_content
    # 舊檔與舊空資料夾必須移除，不能留下新舊兩份
    assert not old_file.exists()
    assert not old_file.parent.exists()
    # manifest 記錄也同步更新至新路徑
    row = store.get_attachment(asset_id)
    assert row is not None
    assert row.path == new_rel
