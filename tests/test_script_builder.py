"""測試本機下載腳本生成器 (script_builder) 及控制器整合。"""

from __future__ import annotations

from app.engine.script_builder import generate_powershell_script, generate_windows_batch
from app.models import AssetRole, ExamAsset, ExamRecord, QuestionType
from app.ui.controller import AppController


def test_generate_windows_batch_structure() -> None:
    items = [
        ("https://wwwq.moex.gov.tw/exam/q1.pdf", "112年高考/資訊處理/國文/試題.pdf"),
        ("https://wwwq.moex.gov.tw/exam/a1.pdf", "112年高考/資訊處理/國文/答案.pdf"),
    ]
    script = generate_windows_batch(items)

    assert "@echo off" in script
    assert "chcp 65001 > nul" in script
    assert "set \"TARGET_ROOT=%USERPROFILE%\\Downloads\\國考題庫\"" in script
    assert "where curl" in script
    assert 'mkdir "112年高考\\資訊處理\\國文"' in script
    assert 'curl -f -s -S -L -k --retry 3 "https://wwwq.moex.gov.tw/exam/q1.pdf"' in script
    assert "pause" in script


def test_generate_windows_batch_deduplication() -> None:
    """測試相同 URL 在不同路徑時，第二個路徑自動使用 copy 加速。"""
    shared_url = "https://wwwq.moex.gov.tw/exam/common_chinese.pdf"
    items = [
        (shared_url, "112年高考/資訊處理/國文/試題.pdf"),
        (shared_url, "112年高考/人事行政/國文/試題.pdf"),
    ]
    script = generate_windows_batch(items)

    assert "copy /y" in script
    assert "共用附件複製" in script


def test_generate_powershell_script_structure() -> None:
    items = [
        ("https://wwwq.moex.gov.tw/exam/q1.pdf", "112年高考/國文.pdf"),
    ]
    script = generate_powershell_script(items)

    assert "$OutputEncoding = [Console]::OutputEncoding" in script
    assert "$TargetRoot = \"$HOME/Downloads/國考題庫\"" in script
    assert "q1.pdf" in script
    assert "curl.exe -f -s -S -L -k --retry 3" in script


def test_controller_export_script_items() -> None:
    record = ExamRecord(
        year=112,
        exam_name="公務人員高等考試三級考試",
        category="資訊處理",
        subject="國文",
        record_id="rec-1",
        assets=(
            ExamAsset(
                asset_id="a-1",
                role=AssetRole.QUESTION,
                url="https://wwwq.moex.gov.tw/exam/test_q.pdf",
            ),
            ExamAsset(
                asset_id="a-2",
                role=AssetRole.CHOICE_ANSWER,
                url="https://wwwq.moex.gov.tw/exam/test_a.pdf",
            ),
        ),
    )

    class DummyQueryService:
        pass

    class DummyTaskService:
        api_version = 1

        def export_results(self, task_id: str):
            return ()

    controller = AppController(
        query_service=DummyQueryService(),
        task_service=DummyTaskService(),
        root="D:/test_root",
    )
    # 模擬已有已選取紀錄
    controller.selected_records = lambda: [record]

    items = controller.export_script_items()
    assert len(items) == 2
    urls = [it[0] for it in items]
    assert "https://wwwq.moex.gov.tw/exam/test_q.pdf" in urls
    assert "https://wwwq.moex.gov.tw/exam/test_a.pdf" in urls

