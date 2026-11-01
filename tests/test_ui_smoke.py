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
