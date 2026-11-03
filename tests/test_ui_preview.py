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
    """雲端根目錄須為國考題庫_YYYYMMDD_HHMM，且歸檔結構預覽顯示同一路徑。"""
    monkeypatch.setattr(web_app, "is_cloud_env", lambda: True)
    monkeypatch.setattr(web_app.tempfile, "tempdir", str(tmp_path))
    script = '''
import re
import streamlit as st
from app.ui import web_app

settings, _ = web_app._sidebar()
st.session_state["root"] = settings["root"]
st.session_state["preview"] = web_app.build_tree_preview(
    settings["root"], web_app.DEFAULT_TEMPLATES["A"]
)
'''
    app = AppTest.from_string(script, default_timeout=30)
    app.run()
    assert not app.exception

    root = pathlib.Path(app.session_state["root"])
    assert root.parent == tmp_path
    assert re.fullmatch(r"國考題庫_\d{8}_\d{4}", root.name)
    assert root.is_dir()
    assert root.name in app.session_state["preview"]


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
