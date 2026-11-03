"""靜態檢查：`app/` 不得出現未定義名稱。

只在罕見分支才執行的程式碼（例如下載失敗清單）不容易被 AppTest 覆蓋，
一旦漏匯入就會在線上直接拋 NameError。這裡用 pyflakes 掃描全部模組。
"""

from __future__ import annotations

import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"


def test_app_has_no_undefined_names() -> None:
    api = pytest.importorskip(
        "pyflakes.api", reason="需要 pyflakes；請安裝 requirements-dev.txt"
    )
    reporter_module = pytest.importorskip("pyflakes.reporter")

    out, err = io.StringIO(), io.StringIO()
    reporter = reporter_module.Reporter(out, err)
    api.checkRecursive([str(APP_DIR)], reporter)

    messages = (out.getvalue() + err.getvalue()).splitlines()
    undefined = [line for line in messages if "undefined name" in line]

    assert undefined == [], "發現未定義名稱（執行時會拋 NameError）：\n" + "\n".join(
        undefined
    )
