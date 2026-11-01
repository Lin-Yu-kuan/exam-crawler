"""解析器與模型契約測試（離線，僅使用 fixture 與合成 HTML）。"""

from __future__ import annotations

import ast
import pathlib

import pytest
from conftest import (
    category_row,
    error_page,
    exam_row,
    result_page,
    subject_row,
)

from app.crawler.parser import (
    YEAR_END_CONTROL,
    decode_html,
    parse_result_page,
    parse_search_form,
)
from app.models import (
    ASSET_ENDPOINT,
    AssetRole,
    AssetScope,
    ParseStatus,
    QuestionType,
    compact_key,
    normalize_text,
)

OBSERVED_AT = "2026-11-01T12:00:00+08:00"


@pytest.fixture(scope="module")
def sample_page(html_dir: pathlib.Path):
    return parse_result_page(
        (html_dir / "result_sample.html").read_text(encoding="utf-8"),
        observed_at=OBSERVED_AT,
    )


def test_real_fixture_is_complete(sample_page) -> None:
    """真實樣本：統計與實際列數一致時為完整快照。"""
    assert sample_page.status is ParseStatus.OK
    assert sample_page.official_counts is not None
    assert sample_page.official_counts.exams == 2
    assert sample_page.official_counts.categories == 75
    assert sample_page.official_counts.subjects == 371
    assert sample_page.parsed_counts.subjects == len(sample_page.records) == 371
    assert sample_page.is_complete is True
    assert sample_page.warnings == ()


def test_record_fields_and_stable_id(sample_page) -> None:
    """官方識別碼優先作為穩定 ID，並保留正規化比對鍵。"""
    record = next(r for r in sample_page.records if r.record_id == "moex:113010:501:0101")
    assert record.official_id == "moex:113010:501:0101"
    assert record.year == 113
    assert record.exam_name == "113年公務人員初等考試"
    assert compact_key(record.category) == compact_key("初等考試_一般行政")
    assert record.subject == "國文"
    assert record.exam_short_name is None and record.level is None
    assert record.source_page.endswith("wFrmExamQandASearch.aspx")
    assert record.observed_at == OBSERVED_AT


def test_question_and_answer_assets(sample_page) -> None:
    """同考科的試題與答案各自成為附件，URL 指回官方端點。"""
    record = next(r for r in sample_page.records if r.record_id == "moex:113010:501:0101")
    roles = [asset.role for asset in record.assets]
    assert roles == [AssetRole.QUESTION, AssetRole.CHOICE_ANSWER]

    question, answer = record.assets
    assert question.url.startswith(ASSET_ENDPOINT)
    assert "t=Q" in question.url and "q=1" in question.url
    assert answer.url.startswith(ASSET_ENDPOINT) and "t=S" in answer.url
    assert question.asset_id != answer.asset_id
    assert question.official_id == "moex:113010:501:0101:Q1"

    # HTML 階段不得判定題型
    assert question.evidence.question_type is QuestionType.UNKNOWN
    assert question.evidence.conflicted is False
    assert question.scope is AssetScope.SUBJECT


def test_missing_answer_keeps_unknown(sample_page) -> None:
    """沒有答案連結不能推論純申論，題型維持未確認。"""
    record = next(r for r in sample_page.records if r.record_id == "moex:113040:101:0301")
    assert record.subject == "行政法"
    assert [asset.role for asset in record.assets] == [AssetRole.QUESTION]
    assert "不能推論" in record.assets[0].evidence.reason
    assert record.assets[0].evidence.question_type is QuestionType.UNKNOWN


def test_exam_level_assets(sample_page) -> None:
    """考試層級附件（整場測驗答案、盲用電腦 zip）不得混入考科附件。"""
    scopes = {asset.scope for asset in sample_page.exam_assets}
    assert AssetScope.EXAM in scopes
    assert all(asset.scope is AssetScope.EXAM for asset in sample_page.exam_assets)
    answer = next(
        a for a in sample_page.exam_assets if a.role is AssetRole.CHOICE_ANSWER
    )
    assert "t=A" in answer.url
    zip_asset = next(
        a for a in sample_page.exam_assets if a.content_hint == "zip"
    )
    assert zip_asset.role is AssetRole.OTHER


def test_corrected_answer_asset(sample_page) -> None:
    """t=M 為官方「更正答案」，屬於測驗答案角色並保留原始標籤。"""
    record = next(r for r in sample_page.records if r.record_id == "moex:113010:501:0202")
    roles = [asset.role for asset in record.assets]
    assert roles == [AssetRole.QUESTION, AssetRole.CHOICE_ANSWER, AssetRole.CHOICE_ANSWER]
    labels = [asset.original_label for asset in record.assets]
    assert "更正答案" in labels
    corrected = next(a for a in record.assets if a.original_label == "更正答案")
    assert "t=M" in corrected.url
    assert corrected.asset_id == f"{record.record_id}#m1"


def test_years_and_compact_keys(sample_page) -> None:
    """全部紀錄都應取得民國年度，比對鍵不含排版空白。"""
    assert {record.year for record in sample_page.records} == {113}
    for record in sample_page.records:
        assert " " not in record.subject_key
        assert record.subject_key == compact_key(record.subject)


def test_stats_mismatch_is_complete_with_warning(html_dir: pathlib.Path) -> None:
    """只有統計文字與列數不一致時：仍屬可用快照，但保留警告與兩組數字。"""
    page = parse_result_page(
        (html_dir / "result_stats_mismatch.html").read_text(encoding="utf-8"),
        observed_at=OBSERVED_AT,
    )
    assert page.status is ParseStatus.OK_WITH_DIFF
    assert page.is_complete is True
    assert page.official_counts is not None
    assert page.official_counts.subjects == page.parsed_counts.subjects + 3
    assert any("統計不符" in warning for warning in page.warnings)
    assert "差異" in page.message


def test_no_result_page(html_dir: pathlib.Path) -> None:
    """「查無結果」是真正的零結果。"""
    page = parse_result_page(
        (html_dir / "result_noresult.html").read_text(encoding="utf-8"),
        observed_at=OBSERVED_AT,
    )
    assert page.status is ParseStatus.NO_RESULT
    assert page.records == ()
    assert page.stats_text == "查無結果"


def test_limit_exceeded_page(html_dir: pathlib.Path) -> None:
    """超過官方 6,000 筆上限時不得當成零結果。"""
    page = parse_result_page(
        (html_dir / "result_limit_exceeded.html").read_text(encoding="utf-8"),
        observed_at=OBSERVED_AT,
    )
    assert page.status is ParseStatus.LIMIT_EXCEEDED
    assert page.records == ()
    assert page.official_counts is None
    assert "上限" in page.message


def test_error_page_detected(html_dir: pathlib.Path) -> None:
    """HTTP 200 的錯誤頁必須辨識為錯誤頁。"""
    page = parse_result_page(
        (html_dir / "error_notfound.html").read_text(encoding="utf-8"),
        observed_at=OBSERVED_AT,
    )
    assert page.status is ParseStatus.ERROR_PAGE
    assert page.records == ()


def test_structure_changed_without_table() -> None:
    """缺少結果表格屬結構變更，不是零結果。"""
    html = "<html><head><title>考畢試題查詢平臺-考選部</title></head><body></body></html>"
    page = parse_result_page(html, observed_at=OBSERVED_AT)
    assert page.status is ParseStatus.STRUCTURE_CHANGED
    assert page.records == ()


def test_stats_without_rows_is_structure_changed() -> None:
    """統計宣稱有資料但表格沒有列，屬結構變更。"""
    html = result_page("", "共 1 考試 1 類科 5 科目")
    page = parse_result_page(html, observed_at=OBSERVED_AT)
    assert page.status is ParseStatus.STRUCTURE_CHANGED
    assert page.parsed_counts.subjects == 0
    assert page.official_counts is not None


def test_multi_volume_creates_multiple_question_assets() -> None:
    """合成：同一科多份試題（拆卷，q=1、q=2）產生多個試題附件。"""
    rows = (
        exam_row("113年測試考試", "113999", with_zip=False)
        + category_row("113999", "501", "測試_一般行政")
        + subject_row("113999", "501", "0101", "測試科目", question_queries=("1", "2"))
    )
    page = parse_result_page(result_page(rows, "共 1 考試 1 類科 1 科目\n"), observed_at=OBSERVED_AT)
    assert page.status is ParseStatus.OK
    record = page.records[0]
    questions = record.assets_by_role(AssetRole.QUESTION)
    assert len(questions) == 2
    assert {asset.asset_id for asset in questions} == {
        f"{record.record_id}#q1",
        f"{record.record_id}#q2",
    }
    assert questions[0].url.endswith("&q=1") and questions[1].url.endswith("&q=2")


def test_duplicate_rows_are_merged() -> None:
    """合成：同一考科列重複出現時合併，附件不重複，並記錄重複列數。"""
    rows = (
        exam_row("113年測試考試", "113999", with_zip=False)
        + category_row("113999", "501", "測試_一般行政")
        + subject_row("113999", "501", "0101", "測試科目", question_queries=("1",))
        + subject_row("113999", "501", "0101", "測試科目", question_queries=("1", "2"))
    )
    page = parse_result_page(
        result_page(rows, "共 1 考試 1 類科 2 科目\n"), observed_at=OBSERVED_AT
    )
    assert page.status is ParseStatus.INCOMPLETE  # 統計寫 2 科，實際 1 科
    assert page.parsed_counts.duplicate_rows == 1
    assert len(page.records) == 1
    record = page.records[0]
    assert len(record.assets_by_role(AssetRole.QUESTION)) == 2


def test_shared_url_between_records_kept() -> None:
    """合成：兩筆紀錄共用同一 URL 時各自保留，傳輸去重留給下載層。"""
    shared = subject_row("113999", "501", "0101", "測試科目A")
    # 第二筆紀錄的類科不同，但指向同一個官方檔案（模擬來源共用附件）
    shared_again = subject_row("113999", "502", "0101", "測試科目B").replace(
        "c=502", "c=501"
    )
    rows = (
        exam_row("113年測試考試", "113999", with_zip=False)
        + category_row("113999", "501", "測試_一般行政")
        + shared
        + category_row("113999", "502", "測試_社會行政")
        + shared_again
    )
    page = parse_result_page(
        result_page(rows, "共 1 考試 2 類科 2 科目\n"), observed_at=OBSERVED_AT
    )
    assert page.status is ParseStatus.OK
    assert len(page.records) == 2
    assert page.records[0].assets[0].url == page.records[1].assets[0].url
    assert page.records[0].record_id != page.records[1].record_id


def test_category_row_without_checkbox() -> None:
    """伺服端篩選後的類科列沒有 checkbox，仍要能取得類科文字。"""
    rows = (
        exam_row("113年測試考試", "113999", with_zip=False)
        + category_row("113999", "501", "測試_一般行政", with_checkbox=False)
        + subject_row("113999", "501", "0101", "測試科目")
    )
    page = parse_result_page(result_page(rows, "共 1 考試 1 類科 1 科目\n"), observed_at=OBSERVED_AT)
    assert page.status is ParseStatus.OK
    assert page.records[0].category == "測試_一般行政"
    assert page.records[0].official_id == "moex:113999:501:0101"


def test_error_page_helper_matches_parser() -> None:
    """conftest 的錯誤頁與真實樣本同樣被辨識。"""
    page = parse_result_page(error_page(), observed_at=OBSERVED_AT)
    assert page.status is ParseStatus.ERROR_PAGE


def test_parser_has_no_network_or_file_io() -> None:
    """解析器不得引入網路或檔案 I/O。"""
    source_path = pathlib.Path(__file__).resolve().parent.parent / "app" / "crawler" / "parser.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"httpx", "requests", "socket", "urllib3", "sqlite3", "subprocess", "shutil"}
    assert not (imported & forbidden), f"解析器引入了禁止的模組：{imported & forbidden}"

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called
    assert "print" not in called


def test_search_form_parsing(html_dir: pathlib.Path) -> None:
    """查詢表單需回收全部 hidden fields、年度／考試選項與 postback 目標。"""
    form = parse_search_form((html_dir / "search_form.html").read_text(encoding="utf-8"))
    for required in (
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__VIEWSTATEENCRYPTED",
        "__EVENTVALIDATION",
        "__EVENTTARGET",
        "__EVENTARGUMENT",
        "__LASTFOCUS",
    ):
        assert required in form.hidden_fields
    assert len(form.year_options) == 35
    assert form.year_options[0].value == "2026"
    assert form.year_options[0].text == "115"
    assert form.exam_options[0].value == ""
    assert form.exam_options[0].text.startswith("所有考試簡稱")
    # 年度（迄）變更必須透過 postback 才生效
    assert YEAR_END_CONTROL in form.postback_targets
    assert form.action.endswith("wFrmExamQandASearch.aspx")


def test_advanced_form_fields(html_dir: pathlib.Path) -> None:
    """進階查詢的關鍵字欄位需可辨識。"""
    form = parse_search_form((html_dir / "full_form.html").read_text(encoding="utf-8"))
    assert "ctl00$holderContent$txtSubjectName" in form.text_fields
    assert "ctl00$holderContent$txtCategoryName" in form.text_fields
    assert "ctl00$holderContent$txtExamSetName" in form.text_fields


def test_decode_html_respects_charset() -> None:
    """依 Content-Type 指定的編碼解碼，不忽略錯誤。"""
    body = "<title>您所輸入的網址並不存在！</title>".encode("big5")
    text, encoding = decode_html(body, "text/html; charset=big5")
    assert encoding == "big5"
    assert "您所輸入的網址並不存在" in text


def test_normalize_helpers() -> None:
    """NFKC、首尾去空白、連續空白合併；比對鍵額外移除空白。"""
    raw = "  初等考試_一般\u3000 行政　"
    assert normalize_text(raw) == "初等考試_一般 行政"
    assert compact_key(raw) == "初等考試_一般行政"
    assert compact_key("ＡＢＣ１２３") == "abc123"
