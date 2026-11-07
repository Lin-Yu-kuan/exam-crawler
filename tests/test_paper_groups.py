"""試卷群組鍵（paper_group_key）與共同科目預設家族（common_subject_values）單元測試。"""

from __future__ import annotations

from app.engine.filter import (
    COMMON_SUBJECT_PRESETS,
    common_subject_values,
    matches_common_subject_preset,
)
from app.models import (
    AssetRole,
    AssetScope,
    ExamAsset,
    ExamRecord,
    paper_group_key,
)


def _asset(
    *,
    asset_id: str = "a1",
    role: AssetRole = AssetRole.QUESTION,
    scope: AssetScope = AssetScope.SUBJECT,
    official_id: str | None = "moex:115080:301:0401:Q1",
    url: str = "https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx?code=115080&c=301&s=0401&t=Q&q=1",
) -> ExamAsset:
    return ExamAsset(
        asset_id=asset_id,
        official_id=official_id,
        role=role,
        original_label="試題" if role is AssetRole.QUESTION else "答案",
        url=url,
        observed_at="2026-11-07T00:00:00+08:00",
        source_version=None,
        evidence=None,  # type: ignore[arg-type]
        scope=scope,
    )


def _record(
    *,
    record_id: str = "r1",
    exam_code: str = "115080",
    category_code: str = "301",
    subject_code: str = "0401",
    subject: str = "法學知識與英文（包括中華民國憲法、法學緒論、英文）",
) -> ExamRecord:
    q = _asset(
        asset_id=f"{record_id}#q1",
        role=AssetRole.QUESTION,
        official_id=f"moex:{exam_code}:{category_code}:{subject_code}:Q1",
        url=f"https://example.test/{exam_code}/{category_code}/{subject_code}/Q1",
    )
    return ExamRecord(
        record_id=record_id,
        official_id=f"moex:{exam_code}:{category_code}:{subject_code}",
        year=115,
        exam_name="115年公務人員高等考試三級考試暨普通考試",
        exam_short_name=None,
        category=f"{category_code} 一般行政",
        subject=subject,
        level=None,
        source_page="https://example.test",
        assets=(q,),
        exam_name_key="115年公務人員高等考試三級考試暨普通考試",
        category_key=f"{category_code} 一般行政",
        subject_key=subject,
    )


def test_same_exam_and_subject_across_categories_share_paper_group_key() -> None:
    a1 = _asset(official_id="moex:115080:301:0401:Q1")
    a2 = _asset(official_id="moex:115080:302:0401:Q1")
    assert paper_group_key(a1) == "paper:115080:0401:Q1"
    assert paper_group_key(a2) == "paper:115080:0401:Q1"
    assert paper_group_key(a1) == paper_group_key(a2)


def test_different_subject_codes_in_same_exam_must_not_merge() -> None:
    """115080 高考三級(0401)與普考(0114)同在考試代碼 115080 下，但科目代碼不同，絕不可合併。"""
    gaokao = _asset(official_id="moex:115080:301:0401:Q1")
    pukao = _asset(official_id="moex:115080:401:0114:Q1")
    assert paper_group_key(gaokao) == "paper:115080:0401:Q1"
    assert paper_group_key(pukao) == "paper:115080:0114:Q1"
    assert paper_group_key(gaokao) != paper_group_key(pukao)


def test_different_attachment_type_or_sequence_must_not_merge() -> None:
    q1 = _asset(official_id="moex:115080:301:0401:Q1")
    q2 = _asset(official_id="moex:115080:301:0401:Q2")
    s1 = _asset(official_id="moex:115080:301:0401:S1", role=AssetRole.CHOICE_ANSWER)
    m1 = _asset(official_id="moex:115080:301:0401:M1", role=AssetRole.CHOICE_ANSWER)
    assert len({
        paper_group_key(q1),
        paper_group_key(q2),
        paper_group_key(s1),
        paper_group_key(m1),
    }) == 4


def test_missing_official_id_or_exam_scope_returns_none() -> None:
    no_id = _asset(official_id=None)
    malformed = _asset(official_id="moex:115080:0401:Q1")
    exam_zip = _asset(
        role=AssetRole.OTHER,
        scope=AssetScope.EXAM,
        official_id="moex:115080:::A1",
    )
    exam_type_in_subject = _asset(official_id="moex:115080:301:0401:B1")
    assert paper_group_key(no_id) is None
    assert paper_group_key(malformed) is None
    assert paper_group_key(exam_zip) is None
    assert paper_group_key(exam_type_in_subject) is None


def test_common_subject_presets_match_expected_and_exclude_false_positives() -> None:
    assert set(COMMON_SUBJECT_PRESETS) == {"chinese", "law_knowledge", "civics", "english"}

    # 國文家族：涵蓋國文(...)，但排除「外國文（英文）」
    assert matches_common_subject_preset("國文", "chinese")
    assert matches_common_subject_preset("國文（作文與測驗）", "chinese")
    assert matches_common_subject_preset("國文(包括公文格式用語)", "chinese")
    assert not matches_common_subject_preset("外國文（英文）", "chinese")
    assert not matches_common_subject_preset("外國文（日文；兼試基礎專業日文）", "chinese")

    # 法學知識家族：涵蓋法學知識與英文、法學知識、法學大意，但排除「社政法規大意」
    assert matches_common_subject_preset(
        "法學知識與英文（包括中華民國憲法、法學緒論、英文）", "law_knowledge"
    )
    assert matches_common_subject_preset(
        "法學知識（包括中華民國憲法、法學緒論）", "law_knowledge"
    )
    assert matches_common_subject_preset("法學大意", "law_knowledge")
    assert not matches_common_subject_preset("社政法規大意", "law_knowledge")

    # 公民家族：涵蓋公民與英文、公民，排除公民教育專題
    assert matches_common_subject_preset("公民與英文", "civics")
    assert matches_common_subject_preset("公民", "civics")
    assert not matches_common_subject_preset("公民教育與社會科學", "civics")

    # 英文家族：涵蓋英文、英文(...)、外國文(英文...)，排除「輪機工程（包括…輪機英文）」與「法學知識與英文」
    assert matches_common_subject_preset("英文", "english")
    assert matches_common_subject_preset("英文（包括作文、翻譯與應用文）", "english")
    assert matches_common_subject_preset("外國文（英文）", "english")
    assert matches_common_subject_preset("外國文(英文)(包括作文、翻譯與應用文)", "english")
    assert not matches_common_subject_preset(
        "輪機工程（包括推進裝置、輔機、輪機英文）", "english"
    )
    assert not matches_common_subject_preset(
        "法學知識與英文（包括中華民國憲法、法學緒論、英文）", "english"
    )


def test_common_subject_values_extracts_matching_subject_names_from_records() -> None:
    records = (
        _record(subject_code="0101", subject="國文（作文與測驗）"),
        _record(subject_code="0201", subject="外國文（英文）"),
        _record(subject_code="0301", subject="輪機工程（包括推進裝置、輔機、輪機英文）"),
        _record(subject_code="0401", subject="英文"),
    )
    assert common_subject_values(records, "chinese") == frozenset({"國文（作文與測驗）"})
    assert common_subject_values(records, "english") == frozenset({"外國文（英文）", "英文"})

