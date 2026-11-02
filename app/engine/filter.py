"""篩選、候選聚合與選取（純函式，不做 I/O）。

契約（SPEC §3.1–3.2）：
- 單一欄位內多選採 OR，三個欄位之間採 AND；空欄位不限制。
- 比對使用正規化鍵（NFKC、去空白），原始文字仍保留給顯示。
- 簡稱採版本化明確對照表；未命中時按原字串比對，不做模糊推測。
- 候選旁的「份數」＝在其他欄位選取條件下，不重複考科紀錄數。
- 全選／取消／反選只作用於目前可見的候選項目。
- 選取確認後凍結紀錄 ID，之後不再重新查詢。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.models import AssetRole, ExamRecord, compact_key

#: 簡稱對照表版本；調整對照內容時必須遞增
SHORT_NAME_VERSION = "short-names-v1"

#: 版本化簡稱對照表（僅收錄常用且語意明確者，未收錄者回退為原字串比對）
SHORT_NAME_TABLE: dict[str, tuple[str, ...]] = {
    "初考": ("初等考試",),
    "初等": ("初等考試",),
    "高考": ("高等考試",),
    "高考一級": ("高等考試一級",),
    "高考二級": ("高等考試二級",),
    "高考三級": ("高等考試三級",),
    "普考": ("普通考試",),
    "地特": ("地方政府公務人員",),
    "身障": ("身心障礙人員",),
    "原民": ("原住民族",),
    "關務": ("關務人員",),
    "警察": ("警察人員",),
    "司法官": ("司法官",),
    "律師": ("律師考試",),
    "調查局": ("法務部調查局調查人員",),
}


class SortOrder(str, Enum):
    """排序方式；一律以紀錄 ID 作穩定次排序。"""

    YEAR_THEN_EXAM = "年度+考試名稱"
    EXAM_THEN_YEAR = "考試名稱+年度"


@dataclass(frozen=True)
class FilterState:
    """目前的篩選與選取狀態。

    三個集合存放**候選顯示值**；空集合代表該欄位零選取（禁止下載）。
    """

    exams: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    subjects: frozenset[str] = frozenset()
    year_start: int | None = None
    year_end: int | None = None

    def with_years(self, start: int | None, end: int | None) -> FilterState:
        return FilterState(self.exams, self.categories, self.subjects, start, end)


@dataclass(frozen=True)
class Candidate:
    """候選項目；value 為顯示值，count 為目前條件下的考科紀錄數。"""

    value: str
    key: str
    count: int


@dataclass(frozen=True)
class CandidateSets:
    exams: tuple[Candidate, ...]
    categories: tuple[Candidate, ...]
    subjects: tuple[Candidate, ...]

    def initial_state(self, *, year_start: int | None = None, year_end: int | None = None) -> FilterState:
        """依 SPEC，初始選取全部候選。"""
        return FilterState(
            exams=frozenset(item.value for item in self.exams),
            categories=frozenset(item.value for item in self.categories),
            subjects=frozenset(item.value for item in self.subjects),
            year_start=year_start,
            year_end=year_end,
        )


@dataclass(frozen=True)
class SelectionStats:
    """選取結果的統計；考科數、附件數與實際傳輸去重分開計算。"""

    records: int
    exams: int
    categories: int
    question_assets: int
    answer_assets: int
    other_assets: int
    total_files: int
    unique_urls: int

    @property
    def has_records(self) -> bool:
        return self.records > 0


@dataclass(frozen=True)
class FrozenSelection:
    """確認後凍結的選取：只保留紀錄 ID，不重新查詢。"""

    record_ids: tuple[str, ...]
    snapshot_id: str
    frozen_at: str
    short_name_version: str = SHORT_NAME_VERSION


class SelectionError(Exception):
    """凍結選取與快照不一致時拋出。"""


def _within_years(record: ExamRecord, start: int | None, end: int | None) -> bool:
    if start is not None and record.year < start:
        return False
    if end is not None and record.year > end:
        return False
    return True


def expand_exam_query(value: str) -> tuple[str, ...]:
    """把簡稱展開為可支援的比對字串；未命中時回退為原字串。"""
    key = compact_key(value)
    expansions = SHORT_NAME_TABLE.get(key)
    if expansions:
        return tuple(compact_key(item) for item in expansions)
    return (key,)


@dataclass(frozen=True)
class _Matcher:
    """預先正規化的比對器；空鍵集合代表不限制。"""

    exam_keys: frozenset[str] = frozenset()
    category_keys: frozenset[str] = frozenset()
    subject_keys: frozenset[str] = frozenset()
    year_start: int | None = None
    year_end: int | None = None

    def match(self, record: ExamRecord) -> bool:
        if not _within_years(record, self.year_start, self.year_end):
            return False
        if self.exam_keys and not self._match_exam(record):
            return False
        if self.category_keys and not self._contains(self.category_keys, record.category_key):
            return False
        if self.subject_keys and not self._contains(self.subject_keys, record.subject_key):
            return False
        return True

    def _match_exam(self, record: ExamRecord) -> bool:
        short_key = compact_key(record.exam_short_name) if record.exam_short_name else ""
        for key in self.exam_keys:
            if key in record.exam_name_key:
                return True
            if short_key and key in short_key:
                return True
        return False

    @staticmethod
    def _contains(keys: frozenset[str], value_key: str) -> bool:
        return any(key in value_key for key in keys)


def _build_matcher(
    *,
    exams: frozenset[str],
    categories: frozenset[str],
    subjects: frozenset[str],
    year_start: int | None,
    year_end: int | None,
) -> _Matcher:
    exam_keys: set[str] = set()
    for value in exams:
        exam_keys.update(key for key in expand_exam_query(value) if key)
    return _Matcher(
        exam_keys=frozenset(exam_keys),
        category_keys=frozenset(compact_key(value) for value in categories),
        subject_keys=frozenset(compact_key(value) for value in subjects),
        year_start=year_start,
        year_end=year_end,
    )


def matches(record: ExamRecord, state: FilterState) -> bool:
    """判斷單筆紀錄是否同時符合三個欄位（AND）與年度條件。

    對外語意：任何一個欄位零選取即代表「沒有可下載的結果」。
    """
    if not state.exams or not state.categories or not state.subjects:
        return False
    return _build_matcher(
        exams=state.exams,
        categories=state.categories,
        subjects=state.subjects,
        year_start=state.year_start,
        year_end=state.year_end,
    ).match(record)


def apply_selection(
    records: tuple[ExamRecord, ...], state: FilterState
) -> tuple[ExamRecord, ...]:
    """回傳符合條件的紀錄（只包含真實存在的紀錄）。"""
    if not state.exams or not state.categories or not state.subjects:
        return ()
    matcher = _build_matcher(
        exams=state.exams,
        categories=state.categories,
        subjects=state.subjects,
        year_start=state.year_start,
        year_end=state.year_end,
    )
    return tuple(record for record in records if matcher.match(record))


def records_matching_keywords(
    records: tuple[ExamRecord, ...],
    *,
    exam_name: str = "",
    category: str = "",
    subject: str = "",
) -> tuple[ExamRecord, ...]:
    """第一階段關鍵字過濾（純函式）。

    與官方伺服端前篩互補：官方做原始字串比對，這裡用正規化鍵（NFKC、去空白）
    再確認一次，避免全形或排版空白造成漏抓；空關鍵字代表不限制。
    """
    exam_keys = tuple(key for key in expand_exam_query(exam_name) if key) if exam_name else ()
    category_key = compact_key(category) if category else ""
    subject_key = compact_key(subject) if subject else ""

    matched: list[ExamRecord] = []
    for record in records:
        if exam_keys and not any(key in record.exam_name_key for key in exam_keys):
            continue
        if category_key and category_key not in record.category_key:
            continue
        if subject_key and subject_key not in record.subject_key:
            continue
        matched.append(record)
    return tuple(matched)


def _count_by(
    records: tuple[ExamRecord, ...], attribute: str
) -> dict[str, tuple[str, int]]:
    """依指定欄位統計紀錄數；回傳 key -> (顯示值, 筆數)。"""
    counts: dict[str, tuple[str, int]] = {}
    for record in records:
        value = getattr(record, attribute)
        key = compact_key(value)
        previous = counts.get(key)
        counts[key] = (value, 1 if previous is None else previous[1] + 1)
    return counts


def build_candidates(records: tuple[ExamRecord, ...], state: FilterState) -> CandidateSets:
    """依其他欄位目前的選取條件，計算三個候選清單與份數。"""

    def pool(**constraints: frozenset[str]) -> tuple[ExamRecord, ...]:
        matcher = _build_matcher(
            exams=constraints.get("exams", frozenset()),
            categories=constraints.get("categories", frozenset()),
            subjects=constraints.get("subjects", frozenset()),
            year_start=state.year_start,
            year_end=state.year_end,
        )
        return tuple(record for record in records if matcher.match(record))

    exams = _count_by(pool(categories=state.categories, subjects=state.subjects), "exam_name")
    categories = _count_by(pool(exams=state.exams, subjects=state.subjects), "category")
    subjects = _count_by(pool(exams=state.exams, categories=state.categories), "subject")

    return CandidateSets(
        exams=tuple(
            Candidate(value=value, key=key, count=count)
            for key, (value, count) in sorted(exams.items(), key=lambda item: item[1][0])
        ),
        categories=tuple(
            Candidate(value=value, key=key, count=count)
            for key, (value, count) in sorted(categories.items(), key=lambda item: item[1][0])
        ),
        subjects=tuple(
            Candidate(value=value, key=key, count=count)
            for key, (value, count) in sorted(subjects.items(), key=lambda item: item[1][0])
        ),
    )


def quick_filter(
    candidates: tuple[Candidate, ...], text: str
) -> tuple[Candidate, ...]:
    """快速文字過濾：只影響可見項目，不改變選取。"""
    key = compact_key(text)
    if not key:
        return candidates
    return tuple(item for item in candidates if key in item.key)


def select_all_visible(
    state: FilterState, dimension: str, visible: tuple[Candidate, ...]
) -> FilterState:
    """把可見候選全部加入選取（不動到不可見項目）。"""
    current = set(getattr(state, dimension))
    current.update(item.value for item in visible)
    return _replace_dimension(state, dimension, frozenset(current))


def clear_visible(
    state: FilterState, dimension: str, visible: tuple[Candidate, ...]
) -> FilterState:
    """取消可見候選的選取（不動到不可見項目）。"""
    current = set(getattr(state, dimension))
    current.difference_update(item.value for item in visible)
    return _replace_dimension(state, dimension, frozenset(current))


def invert_visible(
    state: FilterState, dimension: str, visible: tuple[Candidate, ...]
) -> FilterState:
    """反選可見候選（不動到不可見項目）。"""
    current = set(getattr(state, dimension))
    for item in visible:
        if item.value in current:
            current.discard(item.value)
        else:
            current.add(item.value)
    return _replace_dimension(state, dimension, frozenset(current))


def _replace_dimension(state: FilterState, dimension: str, values: frozenset[str]) -> FilterState:
    if dimension not in ("exams", "categories", "subjects"):
        raise ValueError(f"未知的候選欄位：{dimension}")
    return FilterState(
        exams=values if dimension == "exams" else state.exams,
        categories=values if dimension == "categories" else state.categories,
        subjects=values if dimension == "subjects" else state.subjects,
        year_start=state.year_start,
        year_end=state.year_end,
    )


def selection_stats(records: tuple[ExamRecord, ...]) -> SelectionStats:
    """計算考科數、各角色附件數、歸檔檔數與實際傳輸去重。"""
    urls: set[str] = set()
    question = answer = other = 0
    for record in records:
        for asset in record.assets:
            urls.add(asset.url)
            if asset.role is AssetRole.QUESTION:
                question += 1
            elif asset.role is AssetRole.CHOICE_ANSWER:
                answer += 1
            else:
                other += 1
    return SelectionStats(
        records=len(records),
        exams=len({record.exam_name for record in records}),
        categories=len({(record.exam_name, record.category) for record in records}),
        question_assets=question,
        answer_assets=answer,
        other_assets=other,
        total_files=question + answer + other,
        unique_urls=len(urls),
    )


def can_download(state: FilterState, stats: SelectionStats) -> bool:
    """任一必要候選清單零選取或沒有紀錄時，禁止下載。"""
    return bool(state.exams) and bool(state.categories) and bool(state.subjects) and stats.has_records


def sort_records(
    records: tuple[ExamRecord, ...], order: SortOrder = SortOrder.YEAR_THEN_EXAM
) -> tuple[ExamRecord, ...]:
    """依指定欄位排序，並以紀錄 ID 作穩定次排序。"""
    if order is SortOrder.YEAR_THEN_EXAM:
        key = lambda record: (record.year, record.exam_name_key, record.category_key,
                              record.subject_key, record.record_id)
    else:
        key = lambda record: (record.exam_name_key, record.year, record.category_key,
                              record.subject_key, record.record_id)
    return tuple(sorted(records, key=key))


def freeze_selection(
    records: tuple[ExamRecord, ...],
    *,
    snapshot_id: str,
    frozen_at: str | None = None,
) -> FrozenSelection:
    """凍結選取：只保存紀錄 ID，下載階段不再重新查詢。"""
    if not records:
        raise SelectionError("沒有可凍結的紀錄（零選取）")
    timestamp = frozen_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return FrozenSelection(
        record_ids=tuple(record.record_id for record in records),
        snapshot_id=snapshot_id,
        frozen_at=timestamp,
    )


def resolve_frozen(
    records: tuple[ExamRecord, ...], frozen: FrozenSelection
) -> tuple[ExamRecord, ...]:
    """依凍結的紀錄 ID 取回紀錄；缺少任何一筆即報錯，不靜默忽略。"""
    index = {record.record_id: record for record in records}
    missing = [record_id for record_id in frozen.record_ids if record_id not in index]
    if missing:
        raise SelectionError(f"快照中找不到凍結的紀錄：{missing[:5]}")
    return tuple(index[record_id] for record_id in frozen.record_ids)


def build_faceted_candidates(
    records: tuple[ExamRecord, ...],
    *,
    selected_exams: frozenset[str] = frozenset(),
    selected_categories: frozenset[str] = frozenset(),
    selected_subjects: frozenset[str] = frozenset(),
    year_start: int | None = None,
    year_end: int | None = None,
) -> CandidateSets:
    """依多向交叉篩選計算三個維度的候選清單與份數。

    任一集合為空代表該維度不設限；計算任一維度之候選時，受另外兩個維度的篩選條件約束。
    """
    def pool(
        exam_filter: frozenset[str],
        cat_filter: frozenset[str],
        sub_filter: frozenset[str],
    ) -> tuple[ExamRecord, ...]:
        matcher = _build_matcher(
            exams=exam_filter,
            categories=cat_filter,
            subjects=sub_filter,
            year_start=year_start,
            year_end=year_end,
        )
        return tuple(record for record in records if matcher.match(record))

    exams_pool = pool(frozenset(), selected_categories, selected_subjects)
    cats_pool = pool(selected_exams, frozenset(), selected_subjects)
    subs_pool = pool(selected_exams, selected_categories, frozenset())

    exams = _count_by(exams_pool, "exam_name")
    categories = _count_by(cats_pool, "category")
    subjects = _count_by(subs_pool, "subject")

    return CandidateSets(
        exams=tuple(
            Candidate(value=value, key=key, count=count)
            for key, (value, count) in sorted(exams.items(), key=lambda item: (-item[1][1], item[1][0]))
        ),
        categories=tuple(
            Candidate(value=value, key=key, count=count)
            for key, (value, count) in sorted(categories.items(), key=lambda item: (-item[1][1], item[1][0]))
        ),
        subjects=tuple(
            Candidate(value=value, key=key, count=count)
            for key, (value, count) in sorted(subjects.items(), key=lambda item: (-item[1][1], item[1][0]))
        ),
    )


def apply_faceted_selection(
    records: tuple[ExamRecord, ...],
    *,
    selected_exams: frozenset[str] = frozenset(),
    selected_categories: frozenset[str] = frozenset(),
    selected_subjects: frozenset[str] = frozenset(),
    year_start: int | None = None,
    year_end: int | None = None,
) -> tuple[ExamRecord, ...]:
    """回傳同時符合三維度主動篩選條件的考科紀錄。空集合代表不限。"""
    matcher = _build_matcher(
        exams=selected_exams,
        categories=selected_categories,
        subjects=selected_subjects,
        year_start=year_start,
        year_end=year_end,
    )
    return tuple(record for record in records if matcher.match(record))
