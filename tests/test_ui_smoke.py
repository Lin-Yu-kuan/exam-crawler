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
    assert web_app.DEFAULT_ROOT == "downloads"


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


def test_candidate_ui_uses_chips_and_full_fetch() -> None:
    """候選區塊要有 chips（可叉除）與清單加入；查詢表單要有兩個選項。"""
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")

    assert "不套用關鍵字，抓整年度全部考科" in source
    assert "st.pills(" in source
    assert 'selection_mode="multi"' in source
    assert "_on_chips_change" in source
    assert "_on_add_selection" in source
    assert "MAX_CHIPS" in source
    assert "載入更多" not in source, "分頁會讓瀏覽器端的搜尋找不到未載入項目"
    assert "sel-{dimension}" not in source, "舊的單一多選清單應已移除"


def test_query_form_wording_matches_user_feedback() -> None:
    """便條紙 #1–#4：說明文字移除、兩個選項白話化、按鈕改「開始過濾」。"""
    source = (ROOT / "app" / "ui" / "web_app.py").read_text(encoding="utf-8")

    assert "關鍵字會先送官方進階查詢" not in source, "#1 整段說明要拿掉"
    assert "重新向官網下載（忽略今日快取）" in source, "#3 要白話"
    assert "強制重新查詢" not in source
    assert 'st.form_submit_button("開始過濾"' in source, "#4 按鈕改「開始過濾」"
    assert "尚未取得資料。按「開始過濾」" in source
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
