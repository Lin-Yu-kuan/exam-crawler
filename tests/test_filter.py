"""篩選與選取測試：AND／OR、候選份數、快速過濾、統計與凍結（離線）。"""

from __future__ import annotations

import pathlib

import pytest

from app.crawler.parser import parse_result_page
from app.engine.filter import (
    SHORT_NAME_VERSION,
    FilterState,
    SelectionError,
    SortOrder,
    apply_selection,
    build_candidates,
    can_download,
    clear_visible,
    expand_exam_query,
    freeze_selection,
    invert_visible,
    matches,
    quick_filter,
    resolve_frozen,
    select_all_visible,
    selection_stats,
    sort_records,
)
from app.models import AssetRole, ExamAsset, ExamRecord, QuestionType, TypeEvidence

EXAM_CHU = "113年公務人員初等考試"


@pytest.fixture(scope="module")
def records(html_dir: pathlib.Path) -> tuple[ExamRecord, ...]:
    page = parse_result_page((html_dir / "result_sample.html").read_text(encoding="utf-8"))
    return page.records


@pytest.fixture(scope="module")
def all_state(records) -> FilterState:
    return build_candidates(records, FilterState()).initial_state()


def pick(state: FilterState, **kwargs) -> FilterState:
    data = {
        "exams": state.exams,
        "categories": state.categories,
        "subjects": state.subjects,
        "year_start": state.year_start,
        "year_end": state.year_end,
    }
    data.update(kwargs)
    return FilterState(**data)


# --- 候選聚合 ----------------------------------------------------------


def test_candidate_counts_match_official_totals(records, all_state) -> None:
    candidates = build_candidates(records, all_state)

    assert len(candidates.exams) == 2
    assert len(candidates.categories) == 75
    assert sum(item.count for item in candidates.exams) == 371
    assert all(item.count > 0 for item in candidates.exams)


def test_candidate_counts_respect_other_columns(records, all_state) -> None:
    """份數＝在其他欄位選取條件下，不重複考科紀錄數。"""
    only_chu = pick(all_state, exams=frozenset({EXAM_CHU}))
    candidates = build_candidates(records, only_chu)

    # 考試候選不受考試欄位自身影響，仍列出全部考試與完整份數
    assert len(candidates.exams) == 2
    counts = {item.value: item.count for item in candidates.exams}
    assert counts[EXAM_CHU] == 56
    assert sum(counts.values()) == 371

    # 類科候選則受考試選取影響，只統計初等考試的紀錄
    chu_categories = [item for item in candidates.categories if item.value.startswith("初等考試_")]
    assert chu_categories, "應保留初等考試的類科"
    assert sum(item.count for item in candidates.categories) == 56


# --- AND／OR -----------------------------------------------------------


def test_and_across_three_columns(records, all_state) -> None:
    state = pick(
        all_state,
        exams=frozenset({EXAM_CHU}),
        categories=frozenset({"初等考試_一般行政"}),
        subjects=frozenset({"國文"}),
    )
    selected = apply_selection(records, state)

    assert len(selected) == 1
    assert selected[0].record_id == "moex:113010:501:0101"


def test_or_within_one_column(records, all_state) -> None:
    state = pick(
        all_state,
        exams=frozenset({EXAM_CHU}),
        categories=frozenset({"初等考試_一般行政"}),
        subjects=frozenset({"國文", "公民與英文"}),
    )
    selected = apply_selection(records, state)

    assert {record.subject for record in selected} == {"國文", "公民與英文"}
    assert len(selected) == 2


def test_zero_selection_returns_nothing_and_blocks_download(records, all_state) -> None:
    state = pick(all_state, subjects=frozenset())
    selected = apply_selection(records, state)

    assert selected == ()
    stats = selection_stats(selected)
    assert can_download(state, stats) is False


def test_partial_text_matching_uses_normalized_keys(records, all_state) -> None:
    """來源在名稱中插入排版空白，比對仍可命中。"""
    state = pick(
        all_state,
        exams=frozenset({EXAM_CHU}),
        categories=frozenset({"一般行政"}),
        subjects=frozenset({"法學"}),
    )
    selected = apply_selection(records, state)

    assert len(selected) >= 1
    assert all("法學" in record.subject for record in selected)


def test_year_range_filter(records, all_state) -> None:
    state = pick(all_state, year_start=114, year_end=115)
    assert apply_selection(records, state) == ()

    state_ok = pick(all_state, year_start=113, year_end=113)
    assert len(apply_selection(records, state_ok)) == 371


# --- 快速過濾與批量操作 ------------------------------------------------


def test_quick_filter_only_returns_visible_items(records, all_state) -> None:
    candidates = build_candidates(records, all_state)
    visible = quick_filter(candidates.subjects, "法學")

    assert 0 < len(visible) < len(candidates.subjects)
    assert all("法學" in item.value for item in visible)
    # 空白關鍵字時不改變任何候選
    assert quick_filter(candidates.subjects, "") == candidates.subjects
    # 快速過濾不應影響選取結果
    assert len(apply_selection(records, all_state)) == 371


def test_select_all_visible_keeps_hidden_selection(records, all_state) -> None:
    candidates = build_candidates(records, all_state)
    visible = quick_filter(candidates.subjects, "法學")
    assert 0 < len(visible) < len(candidates.subjects)

    hidden_value = next(item.value for item in candidates.subjects if item not in visible)
    empty = pick(all_state, subjects=frozenset())
    with_hidden = pick(empty, subjects=frozenset({hidden_value}))

    after = select_all_visible(with_hidden, "subjects", visible)

    assert hidden_value in after.subjects
    assert all(item.value in after.subjects for item in visible)


def test_clear_and_invert_visible(records, all_state) -> None:
    candidates = build_candidates(records, all_state)
    visible = quick_filter(candidates.categories, "一般行政")
    assert visible

    cleared = clear_visible(all_state, "categories", visible)
    assert all(item.value not in cleared.categories for item in visible)
    assert len(cleared.categories) == len(all_state.categories) - len(visible)

    inverted = invert_visible(all_state, "categories", visible)
    assert all(item.value not in inverted.categories for item in visible)
    assert len(inverted.categories) == len(all_state.categories) - len(visible)


def test_invert_adds_visible_items_when_unselected(records, all_state) -> None:
    """反選在「未選取」狀態下會把可見項目加入選取。"""
    candidates = build_candidates(records, all_state)
    visible = quick_filter(candidates.subjects, "國文")
    empty_state = pick(all_state, subjects=frozenset())

    inverted = invert_visible(empty_state, "subjects", visible)

    assert inverted.subjects == frozenset(item.value for item in visible)


# --- 統計 -------------------------------------------------------------


def test_stats_separate_records_assets_and_unique_urls(records, all_state) -> None:
    stats = selection_stats(apply_selection(records, all_state))

    assert stats.records == 371
    assert stats.question_assets == 371
    assert stats.answer_assets == 239
    assert stats.total_files == 610
    assert stats.unique_urls == 610


def _make_record(
    record_id: str,
    *,
    subject: str = "測試科目",
    question_urls: tuple[str, ...] = ("https://example/Q1",),
    answer_url: str | None = "https://example/S1",
) -> ExamRecord:
    evidence = TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="test",
        reason="測試",
        rule_version="test-v1",
    )
    assets = [
        ExamAsset(
            asset_id=f"{record_id}#q{index}",
            official_id=None,
            role=AssetRole.QUESTION,
            original_label="試題",
            url=url,
            observed_at="2026-11-01T00:00:00+08:00",
            source_version=None,
            evidence=evidence,
        )
        for index, url in enumerate(question_urls, start=1)
    ]
    if answer_url:
        assets.append(
            ExamAsset(
                asset_id=f"{record_id}#s1",
                official_id=None,
                role=AssetRole.CHOICE_ANSWER,
                original_label="答案",
                url=answer_url,
                observed_at="2026-11-01T00:00:00+08:00",
                source_version=None,
                evidence=evidence,
            )
        )
    return ExamRecord(
        record_id=record_id,
        official_id=None,
        year=113,
        exam_name=EXAM_CHU,
        exam_short_name=None,
        category="初等考試_一般行政",
        subject=subject,
        level=None,
        source_page="test",
        assets=tuple(assets),
        exam_name_key="113年公務人員初等考試",
        category_key="初等考試_一般行政",
        subject_key=subject,
    )


def test_split_volume_assets_exceed_subject_count() -> None:
    """拆卷：一個考科可有兩個試題附件，附件數大於考科數。"""
    record = _make_record("r1", question_urls=("https://example/Q1", "https://example/Q2"))
    stats = selection_stats((record,))

    assert stats.records == 1
    assert stats.question_assets == 2
    assert stats.total_files == 3
    assert stats.unique_urls == 3


def test_shared_url_counted_once_for_transfer() -> None:
    """共用 URL：歸檔檔數仍為 2，但實際傳輸去重為 1。"""
    shared = "https://example/shared.pdf"
    first = _make_record("r1", subject="科目A", question_urls=(shared,), answer_url=None)
    second = _make_record("r2", subject="科目B", question_urls=(shared,), answer_url=None)

    stats = selection_stats((first, second))

    assert stats.records == 2
    assert stats.total_files == 2
    assert stats.unique_urls == 1


# --- 簡稱回退 ----------------------------------------------------------


def test_short_name_table_expands_known_abbreviation(records, all_state) -> None:
    assert expand_exam_query("高考三級") == ("高等考試三級",)
    assert SHORT_NAME_VERSION == "short-names-v1"


def test_short_name_fallback_uses_raw_string(records, all_state) -> None:
    assert expand_exam_query("不存在的簡稱") == ("不存在的簡稱",)

    state = pick(
        all_state,
        exams=frozenset({"不存在的簡稱"}),
        categories=frozenset({"一般行政"}),
        subjects=frozenset({"國文"}),
    )
    assert apply_selection(records, state) == ()


def test_short_name_matches_real_exam_name(records, all_state) -> None:
    state = pick(
        all_state,
        exams=frozenset({"初考"}),
        categories=frozenset({"一般行政"}),
        subjects=frozenset({"國文"}),
    )
    selected = apply_selection(records, state)
    assert len(selected) == 1
    assert selected[0].exam_name == EXAM_CHU


# --- 排序與凍結 --------------------------------------------------------


def test_sorting_is_stable_by_record_id(records, all_state) -> None:
    selected = apply_selection(records, all_state)
    by_year = sort_records(selected, SortOrder.YEAR_THEN_EXAM)
    by_exam = sort_records(selected, SortOrder.EXAM_THEN_YEAR)

    assert len(by_year) == len(by_exam) == 371
    assert by_year[0].record_id.startswith("moex:")
    keys = [(r.year, r.exam_name_key, r.category_key, r.subject_key, r.record_id) for r in by_year]
    assert keys == sorted(keys)


def test_freeze_and_resolve_roundtrip(records, all_state) -> None:
    selected = apply_selection(records, all_state)[:3]
    frozen = freeze_selection(selected, snapshot_id="snap-1", frozen_at="2026-11-01T12:00:00+08:00")

    assert frozen.record_ids == tuple(record.record_id for record in selected)
    assert frozen.snapshot_id == "snap-1"
    resolved = resolve_frozen(records, frozen)
    assert [record.record_id for record in resolved] == list(frozen.record_ids)


def test_freeze_rejects_empty_selection(records) -> None:
    with pytest.raises(SelectionError):
        freeze_selection((), snapshot_id="snap-1")


def test_resolve_frozen_rejects_missing_record(records, all_state) -> None:
    frozen = freeze_selection(apply_selection(records, all_state)[:1], snapshot_id="snap-1")
    broken = type(frozen)(record_ids=("missing-id",), snapshot_id="snap-1", frozen_at="now")

    with pytest.raises(SelectionError):
        resolve_frozen(records, broken)


def test_matches_requires_all_columns(records, all_state) -> None:
    record = records[0]
    assert matches(record, all_state) is True
    assert matches(record, pick(all_state, subjects=frozenset())) is False
