"""UI 控制器測試：輸入檢查、零選取、未完整快照、重跑去重與任務操作（離線）。"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from app.crawler.parser import parse_result_page
from app.engine.path_builder import PathMode
from app.engine.query_service import QueryCancelled
from app.engine.task_service import TaskState
from app.models import (
    ParseStatus,
    QuerySnapshot,
    QuerySpec,
    TaskStatus,
)
from app.ui.controller import (
    MAX_YEAR,
    MIN_YEAR,
    AppController,
    validate_query_inputs,
)


def make_snapshot(html_dir: pathlib.Path, *, status: ParseStatus = ParseStatus.OK) -> QuerySnapshot:
    page = parse_result_page((html_dir / "result_sample.html").read_text(encoding="utf-8"))
    return QuerySnapshot(
        snapshot_id="snap-1",
        cache_key="key-1",
        spec=QuerySpec(113, 113),
        records=page.records,
        status=status,
        years=(),
        acquired_at="2026-11-01T12:00:00+08:00",
        parser_version="html-v1",
        source_url="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
    )


class FakeQueryService:
    def __init__(self, snapshot: QuerySnapshot | None = None, error: Exception | None = None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[QuerySpec] = []

    def query(self, spec, *, force_refresh=False, on_progress=None, should_cancel=None):
        self.calls.append(spec)
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        return replace(self.snapshot, spec=spec)


class FakeTaskService:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, tuple[str, ...]]] = []
        self.status = TaskStatus.DOWNLOADING
        self.cancelled = 0
        self.retried = 0

    def submit(self, snapshot, frozen) -> TaskState:
        self.submissions.append((snapshot.snapshot_id, tuple(frozen.record_ids)))
        return self._state()

    def _state(self) -> TaskState:
        return TaskState(
            task_id="task-1",
            snapshot_id="snap-1",
            status=self.status,
            attempt=1,
            total=len(self.submissions[-1][1]) if self.submissions else 0,
            completed=0,
            skipped=0,
            deduped=0,
            failed=0,
            cancelled=0,
            pending=0,
            started_at="2026-11-01T12:00:00+08:00",
            finished_at=None,
        )

    def get(self, task_id: str) -> TaskState:
        return self._state()

    def cancel(self, task_id: str) -> TaskState:
        self.cancelled += 1
        self.status = TaskStatus.CANCELLED
        return self._state()

    def retry_failed(self, task_id: str, *, snapshot) -> TaskState:
        self.retried += 1
        self.status = TaskStatus.DOWNLOADING
        return self._state()


def make_controller(html_dir: pathlib.Path, **snapshot_kwargs) -> tuple[AppController, FakeQueryService, FakeTaskService]:
    query = FakeQueryService(make_snapshot(html_dir, **snapshot_kwargs))
    tasks = FakeTaskService()
    return AppController(query_service=query, task_service=tasks, root="C:/downloads"), query, tasks


# --- 輸入檢查 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [(113, 113), (MIN_YEAR, MAX_YEAR), (112, 114)],
)
def test_valid_year_inputs(start: int, end: int) -> None:
    assert validate_query_inputs(start, end) == []


@pytest.mark.parametrize(
    ("start", "end", "keyword"),
    [
        (114, 113, "起年不得大於迄年"),
        (70, 113, "年度需介於"),
        (113, 200, "年度需介於"),
        (0, 113, "正的民國年"),
    ],
)
def test_invalid_year_inputs(start: int, end: int, keyword: str) -> None:
    errors = validate_query_inputs(start, end)
    assert errors and any(keyword in message for message in errors)


def test_query_spec_rejects_reversed_years() -> None:
    """模型層先擋一次：起年不得大於迄年。"""
    with pytest.raises(ValueError):
        QuerySpec(114, 113)


def test_query_rejects_out_of_range_years(html_dir) -> None:
    """控制器在送出前檢查年度範圍，不呼叫查詢服務。"""
    controller, query, _ = make_controller(html_dir)

    result = controller.query(QuerySpec(70, 113))

    assert result is None
    assert query.calls == []
    assert "年度需介於" in (controller.last_error or "")


# --- 查詢與候選 --------------------------------------------------------


def test_query_initialises_all_candidates(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)

    snapshot = controller.query(QuerySpec(113, 113))

    assert snapshot is not None
    assert controller.state is not None
    assert len(controller.state.exams) == 2
    assert len(controller.state.categories) == 75
    assert controller.can_download() is True
    assert controller.stats().records == 371


def test_query_error_keeps_previous_state(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    query.error = QueryCancelled()

    result = controller.query(QuerySpec(113, 113))

    assert result is None
    assert "取消" in (controller.last_error or "")
    assert controller.snapshot is not None, "查詢失敗不得清空既有結果"


# --- 零選取與不完整快照 ------------------------------------------------


def test_zero_selection_blocks_download(html_dir) -> None:
    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.set_selection("subjects", ())

    assert controller.can_download() is False
    assert controller.start_download() is None
    assert "零選取" in (controller.last_error or "")
    assert tasks.submissions == []


def test_incomplete_snapshot_blocks_download(html_dir) -> None:
    controller, _, tasks = make_controller(html_dir, status=ParseStatus.INCOMPLETE)
    controller.query(QuerySpec(113, 113))

    assert controller.can_download() is False
    assert controller.start_download() is None
    assert "未完整" in (controller.last_error or "")
    assert tasks.submissions == []


def test_stats_difference_snapshot_allows_download(html_dir) -> None:
    """「完整（統計差異）」仍可下載，但狀態會保留差異訊息。"""
    controller, _, tasks = make_controller(html_dir, status=ParseStatus.OK_WITH_DIFF)
    controller.query(QuerySpec(113, 113))

    assert controller.can_download() is True
    assert "差異" in (controller.snapshot.message if controller.snapshot else "")
    assert controller.start_download() is not None
    assert len(tasks.submissions) == 1


# --- 快速過濾與可見項目操作 --------------------------------------------


def test_quick_filter_only_affects_visible(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.quick_text["subjects"] = "法學"
    visible = controller.visible("subjects")

    assert 0 < len(visible) < len(controller.candidates().subjects)

    controller.set_selection("subjects", ())
    controller.apply_action("subjects", "select_all")

    assert controller.state.subjects == frozenset(item.value for item in visible)
    assert controller.stats().records > 0


def test_invert_and_clear_visible(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.quick_text["categories"] = "一般行政"
    visible = controller.visible("categories")

    controller.apply_action("categories", "clear")
    assert all(item.value not in controller.state.categories for item in visible)

    controller.apply_action("categories", "invert")
    assert all(item.value in controller.state.categories for item in visible)


# --- 預覽 --------------------------------------------------------------


def test_preview_returns_plan(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.set_selection("exams", ("113年公務人員初等考試",))
    controller.set_selection("categories", ("初等考試_一般行政",))
    controller.set_selection("subjects", ("國文",))

    plan = controller.preview(PathMode.A)

    assert plan is not None
    assert len(plan.files) == 2
    assert plan.files[0].relative_path.endswith("_試題.pdf")


def test_preview_with_invalid_template_sets_error(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    plan = controller.preview(PathMode.D, template="{未知}_{題型}.pdf")

    assert plan is None
    assert "路徑設定錯誤" in (controller.last_error or "")


def test_preview_fingerprint_tracks_selection_and_mode(html_dir) -> None:
    """選取、年度、歸檔模式或模板任一變動，預覽就必須失效。"""
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    baseline = controller.preview_fingerprint(PathMode.A, None)

    assert controller.preview_fingerprint(PathMode.A, None) == baseline
    assert controller.preview_fingerprint(PathMode.B, None) != baseline
    assert controller.preview_fingerprint(PathMode.D, "{科目}.pdf") != baseline

    controller.set_selection("subjects", ("國文",))
    assert controller.preview_fingerprint(PathMode.A, None) != baseline


# --- 任務：重複點擊、取消、重試 ----------------------------------------


def test_repeated_start_does_not_create_second_task(html_dir) -> None:
    """UI 重跑或重複點擊時，只會有一個任務。"""
    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    first = controller.start_download()
    second = controller.start_download()

    assert first is not None and second is not None
    assert first.task_id == second.task_id
    assert len(tasks.submissions) == 1


def test_cancel_and_retry_delegate(html_dir) -> None:
    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.start_download()

    cancelled = controller.cancel()
    assert cancelled is not None and tasks.cancelled == 1

    tasks.status = TaskStatus.PARTIAL
    retried = controller.retry()
    assert retried is not None and tasks.retried == 1


def test_query_scope_text_includes_keywords(html_dir) -> None:
    """查詢範圍應顯示年度與已套用的類科／科目關鍵字。"""
    controller, _, _ = make_controller(html_dir)

    controller.query(
        QuerySpec(112, 113, category_keyword="一般行政", subject_keyword="國文")
    )
    scope = controller.query_scope_text()

    assert "年度 112–113" in scope
    assert "類科含「一般行政」" in scope
    assert "科目含「國文」" in scope


def test_query_scope_text_without_keywords(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    assert controller.query_scope_text() == "年度 113–113"


# --- 候選 chips 與快取 ------------------------------------------------


def test_add_and_remove_values(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.set_selection("subjects", ())

    controller.add_values("subjects", ("國文",))
    controller.add_values("subjects", ("國文",))
    assert controller.selected_values("subjects") == ("國文",)

    controller.remove_values("subjects", ("國文",))
    assert controller.selected_values("subjects") == ()


def test_candidates_cache_reused_until_selection_changes(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    first = controller.candidates()
    assert controller.candidates() is first, "選取未變時應沿用快取"

    controller.set_selection("subjects", ("國文",))
    assert controller.candidates() is not first


def test_candidates_prune_selections_that_left_the_pool(html_dir) -> None:
    """取消某考試後，該考試專屬的類科／科目應自動移出選取。"""
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    before = len(controller.state.categories)
    exam = controller.selected_values("exams")[0]

    controller.remove_values("exams", (exam,))
    candidates = controller.candidates()

    assert len(controller.state.categories) == len(candidates.categories)
    assert len(controller.state.subjects) == len(candidates.subjects)
    assert len(controller.state.categories) < before


def test_query_resets_quick_filter_text(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.quick_text["subjects"] = "法學"

    controller.query(QuerySpec(113, 113))

    assert controller.quick_text["subjects"] == ""


def test_selected_values_rejects_unknown_dimension(html_dir) -> None:
    controller, _, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    with pytest.raises(ValueError):
        controller.selected_values("unknown")


def test_progress_reports_partial_completion(html_dir) -> None:
    """部分完成時介面可取得狀態與訊息（不顯示成全部完成）。"""
    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.start_download()
    tasks.status = TaskStatus.PARTIAL

    state = controller.progress()

    assert state is not None
    assert state.status is TaskStatus.PARTIAL
    assert "部分完成" in state.message


def test_progress_after_restart_returns_none(html_dir) -> None:
    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.start_download()

    def broken(task_id: str) -> TaskState:
        raise RuntimeError("任務不存在")

    tasks.get = broken  # type: ignore[method-assign]

    assert controller.progress() is None
