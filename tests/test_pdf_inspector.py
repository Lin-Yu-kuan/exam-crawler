"""PDF 檢查器測試：有效性、題型證據、掃描檔與逾時（離線）。"""

from __future__ import annotations

import pathlib

import pytest
from pypdf import PdfWriter

from app.engine.pdf_inspector import (
    MAX_PREVIEW_CHARS,
    RULE_VERSION,
    classify_first_page_text,
    inspect_pdf,
    is_pdf_magic,
)
from app.models import PdfStatus, QuestionType


# --- 題型規則（純函式） ------------------------------------------------


def test_explicit_mixed_wording() -> None:
    evidence = classify_first_page_text(
        "112年公務人員高等考試三級考試試題\n本科目試題包括作文及測驗兩部分，請妥適分配各題作答時間。"
    )
    assert evidence.question_type is QuestionType.MIXED
    assert evidence.source == "pdf:explicit-mixed"
    assert evidence.conflicted is False
    assert evidence.rule_version == RULE_VERSION


def test_choice_by_exclusive_wording() -> None:
    evidence = classify_first_page_text("本科目全部為測驗題，請以 2B 鉛筆劃記。")
    assert evidence.question_type is QuestionType.CHOICE
    assert evidence.source == "pdf:exclusive-choice"


def test_essay_by_exclusive_wording() -> None:
    evidence = classify_first_page_text("本試題全部為申論題，請依題號作答。")
    assert evidence.question_type is QuestionType.ESSAY
    assert evidence.source == "pdf:exclusive-essay"


def test_choice_by_question_counts() -> None:
    """題數加總等於總題數時具排他性（初等國文樣本的實際文字）。"""
    evidence = classify_first_page_text(
        "※注意：本試題共 45 題，其中單選題 35 題，複選題 10 題，各題答案須用 2B 鉛筆在試卡上依題號清楚劃記"
    )
    assert evidence.question_type is QuestionType.CHOICE
    assert evidence.source == "pdf:choice-count"
    assert "45" in evidence.reason


def test_count_mismatch_is_insufficient() -> None:
    evidence = classify_first_page_text("本試題共 45 題，其中單選題 20 題。")
    assert evidence.question_type is QuestionType.UNKNOWN
    assert evidence.source == "pdf:insufficient-evidence"
    assert "證據不足" in evidence.reason


def test_single_choice_hint_alone_is_not_enough() -> None:
    """SPEC §4.1：單獨出現「單一選擇題」不能證明純題型。"""
    evidence = classify_first_page_text(
        "一、單一選擇題（每題 2 分）\n1 下列何者正確？\n2 下列何者錯誤？"
    )
    assert evidence.question_type is QuestionType.UNKNOWN
    assert "單選題" in evidence.reason or "選擇題" in evidence.reason


def test_conflicting_exclusive_wording() -> None:
    evidence = classify_first_page_text(
        "本科目全部為測驗題。\n本試題全部為申論題。"
    )
    assert evidence.question_type is QuestionType.UNKNOWN
    assert evidence.conflicted is True
    assert evidence.source == "pdf:conflicting-exclusive"


def test_mixed_wins_over_exclusive_but_marks_conflict() -> None:
    evidence = classify_first_page_text(
        "本科目試題包括作文及測驗兩部分。\n本科目全部為測驗題。"
    )
    assert evidence.question_type is QuestionType.MIXED
    assert evidence.conflicted is True


def test_empty_text_is_no_text_layer() -> None:
    evidence = classify_first_page_text("   ")
    assert evidence.question_type is QuestionType.UNKNOWN
    assert evidence.source == "pdf:no-text-layer"


# --- 檔案層級檢查 ------------------------------------------------------


def test_real_mixed_pdf(pdf_dir: pathlib.Path) -> None:
    """真實樣本：高考三級國文（作文與測驗）判定為混合題。"""
    result = inspect_pdf(pdf_dir / "112090_301_0101_Q.pdf")

    assert result.status is PdfStatus.VALID
    assert result.reason_code == "ok"
    assert result.page_count == 4
    assert result.evidence.question_type is QuestionType.MIXED
    assert result.has_text is True
    assert len(result.text_preview) <= MAX_PREVIEW_CHARS
    assert len(result.sha256) == 64
    assert result.size_bytes > 0


def test_real_choice_pdf_by_counts(pdf_dir: pathlib.Path) -> None:
    """真實樣本：初等國文以題數加總判定為測驗題，且只取首頁。"""
    result = inspect_pdf(pdf_dir / "113010_501_0101_Q.pdf")

    assert result.status is PdfStatus.VALID
    assert result.page_count == 8
    assert result.evidence.question_type is QuestionType.CHOICE
    assert result.text_length <= MAX_PREVIEW_CHARS
    assert "單選題" in result.text_preview


def test_real_essay_pdf_without_exclusive_wording(pdf_dir: pathlib.Path) -> None:
    """真實樣本：政治學為申論式版面，但無排他敘述 → 維持未確認。"""
    result = inspect_pdf(pdf_dir / "112090_301_0305_Q.pdf")

    assert result.status is PdfStatus.VALID
    assert result.page_count == 1
    assert result.evidence.question_type is QuestionType.UNKNOWN
    assert result.reason_code == "ok"
    assert result.evidence.source == "pdf:insufficient-evidence"


def test_scanned_pdf_has_no_text_layer(tmp_path: pathlib.Path) -> None:
    """沒有文字層的 PDF：有效但題型未確認，仍可歸檔。"""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    path = tmp_path / "scanned.pdf"
    with path.open("wb") as handle:
        writer.write(handle)

    result = inspect_pdf(path)

    assert result.status is PdfStatus.VALID
    assert result.reason_code == "no-text-layer"
    assert result.has_text is False
    assert result.evidence.question_type is QuestionType.UNKNOWN
    assert result.evidence.source == "pdf:no-text-layer"


def test_html_disguised_as_pdf_is_invalid(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "fake.pdf"
    path.write_text("<html><body>請先登入</body></html>", encoding="utf-8")

    result = inspect_pdf(path)

    assert result.status is PdfStatus.INVALID
    assert result.reason_code == "not-pdf"
    assert result.page_count is None


def test_truncated_pdf_is_invalid(pdf_dir: pathlib.Path, tmp_path: pathlib.Path) -> None:
    data = (pdf_dir / "112090_301_0305_Q.pdf").read_bytes()
    path = tmp_path / "truncated.pdf"
    path.write_bytes(data[:600])

    result = inspect_pdf(path)

    assert result.status is PdfStatus.INVALID
    assert result.reason_code == "corrupt-pdf"


def test_timeout_is_sniff_failed_not_invalid(pdf_dir: pathlib.Path) -> None:
    """逾時屬「嗅探失敗」，不得與無效 PDF 混為一談。"""
    result = inspect_pdf(pdf_dir / "113010_501_0101_Q.pdf", timeout=0.001)

    assert result.status is PdfStatus.SNIFF_FAILED
    assert result.reason_code == "timeout"
    assert result.evidence.question_type is QuestionType.UNKNOWN


def test_missing_file_is_invalid(tmp_path: pathlib.Path) -> None:
    result = inspect_pdf(tmp_path / "missing.pdf")

    assert result.status is PdfStatus.INVALID
    assert result.reason_code == "file-not-found"


def test_magic_check_accepts_leading_bytes() -> None:
    assert is_pdf_magic(b"\r\n%PDF-1.7 ...")
    assert not is_pdf_magic(b"<html>")
