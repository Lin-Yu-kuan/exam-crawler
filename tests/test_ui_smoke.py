"""UI 入口煙霧測試：模組可匯入、不會在匯入時啟動服務。"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _module_ast(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_web_app_exposes_main_and_controller_builder() -> None:
    from app.ui import web_app

    assert callable(web_app.main)
    assert callable(web_app.build_controller)
    assert web_app.DEFAULT_ROOT == web_app.DEFAULT_LOCAL_ROOT


def test_web_app_has_no_import_time_side_effects() -> None:
    """匯入時不得執行 Streamlit 指令或建立服務（只在函式內呼叫）。"""
    tree = _module_ast(ROOT / "app" / "ui" / "web_app.py")
    for node in tree.body:
        assert not isinstance(node, ast.Call), f"模組層出現呼叫：{ast.dump(node)[:80]}"


def test_web_app_delegates_business_logic_to_controller() -> None:
    tree = _module_ast(ROOT / "app" / "ui" / "web_app.py")
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert "AppController" in imported
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")
    # 業務規則不應在 UI 中重寫：不出現直接的字串比對或統計計算
    assert "apply_selection(" not in source
    assert "selection_stats(" not in source


def test_query_form_has_category_and_subject_keyword_fields() -> None:
    """第一階段要有考試名稱、類科、科目三個關鍵字欄位。"""
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")

    assert "考試名稱關鍵字" in source
    assert "類科關鍵字" in source
    assert "科目關鍵字" in source
    assert "category_keyword=category_keyword.strip()" in source
    assert "subject_keyword=subject_keyword.strip()" in source


def test_candidate_ui_uses_300px_table_and_full_fetch() -> None:
    """候選區塊要有 300px 固定高度滾動表格；多向收斂支援；查詢表單要有全抓選項。"""
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")

    assert "不套用關鍵字，抓整年度全部考科" in source
    assert "st.data_editor(" in source
    assert "height=300" in source
    assert "CheckboxColumn" in source
    assert "faceted_candidates" in source
    assert "st.pills(" not in source, "長文字標籤海應已移除"
    assert "載入更多" not in source, "分頁會讓瀏覽器端的搜尋找不到未載入項目"
    assert "sel-{dimension}" not in source, "舊的單一多選清單應已移除"


def test_query_form_wording_matches_user_feedback() -> None:
    """便條紙 #1–#4：說明文字移除、兩個選項白話化、按鈕改「查詢試題」。"""
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")

    assert "關鍵字會先送官方進階查詢" not in source, "#1 整段說明要拿掉"
    assert "重新向官網下載（忽略今日快取）" in source, "#3 要白話"
    assert "強制重新查詢" not in source
    assert 'st.form_submit_button("查詢試題"' in source, "#4 按鈕改「查詢試題」"
    assert "尚未取得資料。按「查詢試題」" in source
    # 兩個勾選項都要有說明
    assert source.count("help=(") >= 2


def test_download_progress_separates_new_and_skipped() -> None:
    """重跑時要分得出來「新下載」「共用」與「略過」，不能全部寫成完成。"""
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")

    assert "新下載 {state.completed - state.deduped}" in source
    assert "共用 {state.deduped}" in source
    assert "略過 {state.skipped}" in source


def test_entry_point_uses_localhost() -> None:
    import main as entry

    assert entry.APP_PATH.exists()
    assert entry.DEFAULT_PORT == 8501
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "--server.address=localhost" in source
    assert "import streamlit" not in source.split("def main")[0], "匯入入口時不應載入 Streamlit"


def test_entry_point_builds_streamlit_arguments(monkeypatch) -> None:
    """入口應以 streamlit run 啟動並只綁定 localhost。"""
    import streamlit.web.cli as streamlit_cli

    import main as entry

    captured: dict[str, list[str]] = {}

    def fake_main() -> int:
        captured["argv"] = list(entry.sys.argv)
        return 0

    monkeypatch.setattr(streamlit_cli, "main", fake_main)
    assert entry.main(["--server.port=9000"]) == 0

    argv = captured["argv"]
    assert argv[:2] == ["streamlit", "run"]
    assert argv[2].endswith("web_app.py")
    assert "--server.address=localhost" in argv
    assert "--server.headless=true" in argv
    assert argv[-1] == "--server.port=9000"


def test_manifest_managed_by_system_and_migrated_automatically(tmp_path: pathlib.Path) -> None:
    from app.ui.web_app import SYSTEM_DIR_NAME, resolve_manifest_path

    # 情境 1: 乾淨目錄，自動解析為 _系統資料/manifest.db
    clean_root = tmp_path / "clean_repo"
    db_path = resolve_manifest_path(clean_root)
    assert db_path == clean_root / SYSTEM_DIR_NAME / "manifest.db"
    assert (clean_root / SYSTEM_DIR_NAME).is_dir()

    # 情境 2: 既有目錄原本有 downloads/manifest.db 與 -wal/-shm，自動無損搬移
    legacy_root = tmp_path / "legacy_repo"
    legacy_root.mkdir()
    legacy_db = legacy_root / "manifest.db"
    legacy_wal = legacy_root / "manifest.db-wal"
    legacy_shm = legacy_root / "manifest.db-shm"
    legacy_db.write_text("dummy-db-content", encoding="utf-8")
    legacy_wal.write_text("dummy-wal-content", encoding="utf-8")
    legacy_shm.write_text("dummy-shm-content", encoding="utf-8")

    migrated_db = resolve_manifest_path(legacy_root)
    assert migrated_db == legacy_root / SYSTEM_DIR_NAME / "manifest.db"
    assert migrated_db.is_file()
    assert migrated_db.read_text(encoding="utf-8") == "dummy-db-content"
    assert not legacy_db.exists(), "舊路徑的 manifest.db 應已搬移"

    migrated_wal = legacy_root / SYSTEM_DIR_NAME / "manifest.db-wal"
    migrated_shm = legacy_root / SYSTEM_DIR_NAME / "manifest.db-shm"
    assert migrated_wal.is_file() and migrated_wal.read_text(encoding="utf-8") == "dummy-wal-content"
    assert migrated_shm.is_file() and migrated_shm.read_text(encoding="utf-8") == "dummy-shm-content"
    assert not legacy_wal.exists()
    assert not legacy_shm.exists()


def test_database_input_removed_from_sidebar() -> None:
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")
    assert 'st.text_input("資料庫檔案"' not in source, "不讓使用者手動輸入資料庫路徑"
    assert "下載紀錄由程式自動保存，搬移題庫時請連同整個資料夾一起搬移。" in source



def test_directory_picker_uses_native_dialog_and_returns_normalized_path(monkeypatch) -> None:
    import subprocess
    from app.ui import web_app

    class DummyCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: DummyCompleted("C:/Users/Public/ExamBank\n"),
    )
    chosen = web_app.pick_directory_dialog(r"C:\國考題庫")
    assert chosen == r"C:\Users\Public\ExamBank"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: DummyCompleted(""),
    )
    assert web_app.pick_directory_dialog(r"C:\國考題庫") is None

    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")
    assert "📂 選擇目錄…" in source
