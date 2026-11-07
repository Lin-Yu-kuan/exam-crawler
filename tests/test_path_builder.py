"""路徑建構測試：模板、清理、碰撞、長路徑與根目錄邊界。"""

from __future__ import annotations

import pathlib
import subprocess
import unicodedata
from dataclasses import replace

import pytest

from app.crawler.parser import parse_result_page
from app.engine.path_builder import (
    DEFAULT_MAX_SEGMENT_BYTES,
    DEFAULT_TEMPLATES,
    MISSING_LEVEL,
    PathBuildError,
    PathEscapeError,
    PathMode,
    TemplateError,
    build_plan,
    collapse_shared_paper_paths,
    ensure_within_root,
    is_within_root,
    preview_lines,
    sanitize_segment,
    validate_template,
)
from app.models import AssetRole, AssetScope, ExamAsset, ExamRecord, QuestionType, TypeEvidence

RECORD_ID = "moex:113010:501:0101"


@pytest.fixture(scope="module")
def records(html_dir: pathlib.Path) -> tuple[ExamRecord, ...]:
    page = parse_result_page((html_dir / "result_sample.html").read_text(encoding="utf-8"))
    return page.records


@pytest.fixture(scope="module")
def guowen(records) -> ExamRecord:
    return next(record for record in records if record.record_id == RECORD_ID)


def question_asset(record: ExamRecord) -> ExamAsset:
    return record.assets_by_role(AssetRole.QUESTION)[0]


def answer_asset(record: ExamRecord) -> ExamAsset:
    return record.assets_by_role(AssetRole.CHOICE_ANSWER)[0]


def plan_for(root: pathlib.Path, record: ExamRecord, **kwargs):
    return build_plan(root, (record,), **kwargs)


# --- 預設模板 ----------------------------------------------------------


def test_mode_a_uses_subject_directory(tmp_path, guowen) -> None:
    plan = plan_for(tmp_path, guowen, mode=PathMode.A)
    relative = plan.files[0].relative_path

    assert relative.startswith("國文/113_")
    assert relative.endswith("_試題.pdf")
    assert "/" not in relative[relative.index("/") + 1 :]
    assert plan.template == DEFAULT_TEMPLATES["A"]


def test_mode_b_uses_year_and_exam(tmp_path, guowen) -> None:
    plan = plan_for(tmp_path, guowen, mode=PathMode.B)
    relative = plan.files[0].relative_path

    assert relative.startswith("113/")
    assert "113年公務人員初等考試" in relative
    assert relative.endswith("_試題.pdf")


def test_mode_c_is_flat(tmp_path, guowen) -> None:
    plan = plan_for(tmp_path, guowen, mode=PathMode.C)
    relative = plan.files[0].relative_path

    assert "/" not in relative
    assert relative.startswith("113_")
    assert relative.endswith("_試題.pdf")


# --- 變數契約 ----------------------------------------------------------


def test_all_variables_render(tmp_path, guowen) -> None:
    template = (
        "{年度標籤}_{年度}_{考試名稱}_{考試簡稱}_{類科}_{科目}_{等別}_{題型類別}_{題型}.pdf"
    )
    plan = plan_for(tmp_path, guowen, mode=PathMode.D, template=template)
    name = plan.files[0].relative_path

    assert name.startswith("113年_113_")
    assert MISSING_LEVEL in name
    assert name.endswith("_未確認_試題.pdf")


def test_short_name_falls_back_to_full_name(tmp_path, guowen) -> None:
    template = "{考試簡稱}_{題型}.pdf"
    plan = plan_for(tmp_path, guowen, mode=PathMode.D, template=template)

    assert plan.files[0].relative_path.startswith("113年公務人員初等考試_")


def test_question_type_tokens(tmp_path, guowen) -> None:
    asset = question_asset(guowen)
    expected = {
        QuestionType.ESSAY: "申論試題",
        QuestionType.CHOICE: "測驗試題",
        QuestionType.MIXED: "混合試題",
        QuestionType.UNKNOWN: "試題",
    }
    for question_type, token in expected.items():
        plan = build_plan(
            tmp_path, (guowen,), mode=PathMode.C, asset_types={asset.asset_id: question_type}
        )
        assert plan.files[0].relative_path.endswith(f"_{token}.pdf")


def test_answer_always_named_measurement_answer(tmp_path, guowen) -> None:
    answer = answer_asset(guowen)
    plan = build_plan(
        tmp_path,
        (guowen,),
        mode=PathMode.C,
        asset_types={answer.asset_id: QuestionType.MIXED},
        record_types={guowen.record_id: QuestionType.CHOICE},
    )
    answer_file = next(item for item in plan.files if item.asset_id == answer.asset_id)

    assert answer_file.relative_path.endswith("_測驗答案.pdf")
    assert answer_file.is_provisional is False


def test_answer_type_category_uses_record_type(tmp_path, guowen) -> None:
    template = "{題型類別}_{題型}.pdf"
    plan = build_plan(
        tmp_path,
        (guowen,),
        mode=PathMode.D,
        template=template,
        record_types={guowen.record_id: QuestionType.ESSAY},
    )
    answer_file = next(item for item in plan.files if item.role is AssetRole.CHOICE_ANSWER)

    assert answer_file.relative_path == "申論題_測驗答案.pdf"


def test_answer_type_category_unknown_without_record_type(tmp_path, guowen) -> None:
    template = "{題型類別}_{題型}.pdf"
    plan = build_plan(tmp_path, (guowen,), mode=PathMode.D, template=template)
    answer_file = next(item for item in plan.files if item.role is AssetRole.CHOICE_ANSWER)

    assert answer_file.relative_path == "未確認_測驗答案.pdf"


# --- 模板驗證 ----------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "{科目}/{年度}.pdf",  # 缺少 {題型}
        "{科目}/{年度}_{題型}.txt",  # 非 .pdf
        "{科目}/{未知變數}_{題型}.pdf",  # 未知變數
        "{科目}/{年度:04d}_{題型}.pdf",  # 格式運算
        "/abs/{題型}.pdf",  # 絕對路徑
        "../{題型}.pdf",  # 目錄逃逸
        "a//{題型}.pdf",  # 空目錄段
        "a/./{題型}.pdf",  # 目前目錄
        "C:/x/{題型}.pdf",  # 磁碟機
        "a\\b_{題型}.pdf",  # 反斜線
        "",  # 空白模板
    ],
)
def test_invalid_templates_are_rejected(template: str) -> None:
    with pytest.raises(TemplateError):
        validate_template(template)


def test_valid_custom_template(tmp_path, guowen) -> None:
    template = "{年度標籤}/{考試名稱}/{科目}_{題型}.pdf"
    validate_template(template)
    plan = plan_for(tmp_path, guowen, mode=PathMode.D, template=template)

    assert plan.files[0].relative_path.startswith("113年/113年公務人員初等考試/國文_")


def test_mode_d_requires_template(tmp_path, guowen) -> None:
    with pytest.raises(TemplateError):
        build_plan(tmp_path, (guowen,), mode=PathMode.D)


# --- 清理 --------------------------------------------------------------


def test_sanitize_removes_illegal_and_control_characters() -> None:
    cleaned = sanitize_segment('行政法<考>\x00:"試"|題?*')

    assert cleaned.startswith("行政法_考")
    assert "試" in cleaned and "題" in cleaned
    assert not any(char in cleaned for char in '<>:"/\\|?*')
    assert all(unicodedata.category(char)[0] != "C" for char in cleaned)


def test_sanitize_strips_trailing_dots_and_spaces() -> None:
    assert sanitize_segment("行政法. . ") == "行政法"


def test_sanitize_prefixes_reserved_names() -> None:
    assert sanitize_segment("CON") == "_CON"
    assert sanitize_segment("com1.pdf") == "_com1.pdf"


def test_sanitize_rejects_empty_names() -> None:
    for value in ("", "   ", "...", " . . "):
        with pytest.raises(PathBuildError):
            sanitize_segment(value)


def test_sanitize_normalizes_fullwidth_characters() -> None:
    assert sanitize_segment("ＡＢＣ：１２３") == "ABC_123"


def test_slash_in_data_value_is_cleaned_not_split(tmp_path) -> None:
    record = make_record("r1", subject="民法/刑法")
    plan = plan_for(tmp_path, record, mode=PathMode.C)

    assert "/" not in plan.files[0].relative_path
    assert "民法_刑法" in plan.files[0].relative_path


# --- 碰撞與拆卷 --------------------------------------------------------


def _evidence() -> TypeEvidence:
    return TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="test",
        reason="測試",
        rule_version="test-v1",
    )


def _asset(asset_id: str, url: str, role: AssetRole = AssetRole.QUESTION) -> ExamAsset:
    return ExamAsset(
        asset_id=asset_id,
        official_id=None,
        role=role,
        original_label="試題" if role is AssetRole.QUESTION else "答案",
        url=url,
        observed_at="2026-11-01T00:00:00+08:00",
        source_version=None,
        evidence=_evidence(),
    )


def make_record(
    record_id: str,
    *,
    subject: str = "測試科目",
    category: str = "測試_一般行政",
    assets: tuple[ExamAsset, ...] | None = None,
) -> ExamRecord:
    return ExamRecord(
        record_id=record_id,
        official_id=None,
        year=113,
        exam_name="113年測試考試",
        exam_short_name=None,
        category=category,
        subject=subject,
        level=None,
        source_page="test",
        assets=assets or (_asset(f"{record_id}#q1", "https://example/q1"),),
        exam_name_key="113年測試考試",
        category_key=category,
        subject_key=subject,
    )


def test_split_volume_adds_stable_short_ids(tmp_path) -> None:
    assets = (
        _asset("r1#q1", "https://example/q1"),
        _asset("r1#q2", "https://example/q2"),
        _asset("r1#s1", "https://example/s1", AssetRole.CHOICE_ANSWER),
    )
    record = make_record("r1", assets=assets)
    plan = plan_for(tmp_path, record, mode=PathMode.C)
    paths = [item.relative_path for item in plan.files]

    assert any(p.endswith("_q1_試題.pdf") for p in paths)
    assert any(p.endswith("_q2_試題.pdf") for p in paths)
    assert any(p.endswith("_測驗答案.pdf") for p in paths)
    assert len(set(paths)) == 3


def test_case_insensitive_collision_is_disambiguated(tmp_path) -> None:
    first = make_record("r1", subject="ABC")
    second = make_record("r2", subject="abc")
    plan = build_plan(tmp_path, (first, second), mode=PathMode.C)

    paths = [item.relative_path for item in plan.files]
    assert paths[0].casefold() != paths[1].casefold()
    assert plan.files[1].disambiguated is True
    assert any("碰撞" in warning for warning in plan.warnings)


def test_identical_subjects_do_not_overwrite(tmp_path) -> None:
    first = make_record("r1", category="測試_A")
    second = make_record("r2", category="測試_A")
    plan = build_plan(tmp_path, (first, second), mode=PathMode.C)

    assert len({item.relative_path.casefold() for item in plan.files}) == 2


# --- 長路徑 ------------------------------------------------------------


def test_long_name_is_shortened_with_stable_id(tmp_path) -> None:
    record = make_record("r1", subject="很長的科目名稱" * 30)
    plan = plan_for(tmp_path, record, mode=PathMode.C, max_total_length=120)
    relative = plan.files[0].relative_path

    assert relative.endswith(".pdf")
    assert len(str(pathlib.Path(tmp_path).resolve())) + 1 + len(relative) <= 120


def test_segment_length_limit_applies(tmp_path) -> None:
    record = make_record("r1", subject="長" * 200)
    plan = plan_for(tmp_path, record, mode=PathMode.C, max_segment_length=40)

    for segment in plan.files[0].relative_path.split("/"):
        assert len(segment) <= 40


def test_segment_byte_limit_applies_to_cjk(tmp_path) -> None:
    record = make_record("r1", subject="長" * 200)
    plan = plan_for(tmp_path, record, mode=PathMode.C)

    for segment in plan.files[0].relative_path.split("/"):
        assert len(segment.encode("utf-8")) <= DEFAULT_MAX_SEGMENT_BYTES


def test_100_char_cjk_segment_is_byte_shortened() -> None:
    cleaned = sanitize_segment("年" * 100)

    assert len(cleaned) <= 100
    assert len(cleaned.encode("utf-8")) <= DEFAULT_MAX_SEGMENT_BYTES


def test_long_exam_name_directory_stays_under_byte_limit(tmp_path) -> None:
    long_name = (
        "115年公務人員特種考試警察人員考試、"
        "115年公務人員特種考試一般警察人員考試、"
        "115年公務人員特種考試國家安全局國家安全情報人員考試、"
        "115年公務人員特種考試移民行政人員考試"
    )
    record = replace(make_record("r1"), exam_name=long_name, exam_name_key=long_name)
    plan = plan_for(tmp_path, record, mode=PathMode.B)

    for segment in plan.files[0].relative_path.split("/"):
        assert len(segment.encode("utf-8")) <= DEFAULT_MAX_SEGMENT_BYTES


# --- 根目錄邊界 --------------------------------------------------------


def test_normal_target_is_within_root(tmp_path) -> None:
    assert is_within_root(tmp_path, tmp_path / "a" / "b.pdf") is True


def test_parent_traversal_is_rejected(tmp_path) -> None:
    assert is_within_root(tmp_path, tmp_path / ".." / "escape.pdf") is False
    with pytest.raises(PathEscapeError):
        ensure_within_root(tmp_path, tmp_path / ".." / "escape.pdf")


def test_junction_escape_is_detected(tmp_path) -> None:
    """Windows junction 指向根目錄外時，解析後必須被判定為逃逸。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    link = root / "linked"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip(f"無法建立 junction：{result.stderr.strip() or result.stdout.strip()}")

    assert is_within_root(root, link / "file.pdf") is False


def test_build_plan_creates_no_files(tmp_path) -> None:
    record = make_record("r1")
    build_plan(tmp_path, (record,), mode=PathMode.A)

    assert list(tmp_path.iterdir()) == []


def test_preview_marks_provisional_question(tmp_path) -> None:
    record = make_record("r1")
    plan = plan_for(tmp_path, record, mode=PathMode.C)
    lines = preview_lines(plan)

    assert "暫定" in lines[0]
    assert plan.files[0].is_provisional is True


def test_confirmed_type_is_not_provisional(tmp_path) -> None:
    record = make_record("r1")
    plan = build_plan(
        tmp_path,
        (record,),
        mode=PathMode.C,
        asset_types={"r1#q1": QuestionType.CHOICE},
    )

    assert plan.files[0].is_provisional is False
    assert plan.files[0].relative_path.endswith("_測驗試題.pdf")


def test_combined_exam_is_smart_simplified(tmp_path) -> None:
    """多合一合併超長考試名稱依類科精確配對子特考，絕不產生雜湊碼截斷。"""
    long_name = (
        "105年公務人員特種考試關務人員考試、"
        "105年公務人員特種考試身心障礙人員考試、"
        "105年國軍上校以上軍官轉任公務人員考試"
    )
    record = replace(
        make_record("r1", category="關務特考三等考試_資訊處理", subject="資料結構"), year=105,
        exam_name=long_name,
        exam_name_key=long_name,
    )
    plan = build_plan(tmp_path, (record,), mode=PathMode.A)
    path = plan.files[0].relative_path

    assert path == "資料結構/105_105年公務人員特種考試關務人員考試_關務特考三等考試_資訊處理_試題.pdf"


def test_middle_truncation_preserves_tail(tmp_path) -> None:
    """超長檔名段在極端截斷時仍保留尾端類科與題型。"""
    long_subject = "超長科目名稱" * 25
    record = make_record("r1", subject=long_subject, category="資訊處理")
    plan = build_plan(tmp_path, (record,), mode=PathMode.C)
    filename = plan.files[0].relative_path

    assert filename.endswith("_試題.pdf")
    assert len(filename.encode("utf-8")) <= 200


def test_collapse_shared_paper_paths_keeps_first_per_paper_group(tmp_path) -> None:
    evidence = TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="test",
        reason="測試",
        rule_version="test-v1",
    )
    q1 = ExamAsset(
        asset_id="r1#q1",
        official_id="moex:115080:301:0401:Q1",
        role=AssetRole.QUESTION,
        original_label="試題",
        url="https://example.test/exam?code=115080&c=301&s=0401&t=Q&q=1",
        observed_at="2026-11-07T00:00:00+08:00",
        source_version=None,
        evidence=evidence,
    )
    q2 = ExamAsset(
        asset_id="r2#q1",
        official_id="moex:115080:302:0401:Q1",
        role=AssetRole.QUESTION,
        original_label="試題",
        url="https://example.test/exam?code=115080&c=302&s=0401&t=Q&q=1",
        observed_at="2026-11-07T00:00:00+08:00",
        source_version=None,
        evidence=evidence,
    )
    r1 = ExamRecord(
        record_id="r1",
        official_id="moex:115080:301:0401",
        year=115,
        exam_name="115年高考三級",
        exam_short_name=None,
        category="301 一般行政",
        subject="法學知識與英文",
        level=None,
        source_page="test",
        assets=(q1,),
        exam_name_key="115年高考三級",
        category_key="301 一般行政",
        subject_key="法學知識與英文",
    )
    r2 = ExamRecord(
        record_id="r2",
        official_id="moex:115080:302:0401",
        year=115,
        exam_name="115年高考三級",
        exam_short_name=None,
        category="302 一般民政",
        subject="法學知識與英文",
        level=None,
        source_page="test",
        assets=(q2,),
        exam_name_key="115年高考三級",
        category_key="302 一般民政",
        subject_key="法學知識與英文",
    )
    raw_plan = build_plan(tmp_path, (r1, r2), mode=PathMode.A)
    assert len(raw_plan.files) == 2

    collapsed = collapse_shared_paper_paths(raw_plan, (r1, r2))
    assert len(collapsed.files) == 1
    assert collapsed.files[0].asset_id == q1.asset_id
    assert any("1 個附件與其他類科共用同一份試卷" in w for w in collapsed.warnings)
