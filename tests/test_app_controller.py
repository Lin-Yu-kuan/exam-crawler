"""UI 控制器測試：輸入檢查、零選取、未完整快照、重跑去重與任務操作（離線）。"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from app.crawler.parser import parse_result_page
from app.engine.path_builder import PathMode
from app.engine.query_service import QueryCancelled
from app.engine.task_service import CleanupReport, ReorganizeReport, TaskState
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
    api_version = 3

    def export_results(self, task_id):
        return ()

    def __init__(self) -> None:
        self.submissions: list[tuple[str, tuple[str, ...]]] = []
        self.status = TaskStatus.DOWNLOADING
        self.cancelled = 0
        self.retried = 0
        self.reorganized: tuple[str, tuple[str, ...]] | None = None
        self.cleaned = 0

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

    def reorganize(self, snapshot, frozen) -> ReorganizeReport:
        self.reorganized = (snapshot.snapshot_id, tuple(frozen.record_ids))
        return ReorganizeReport(moved=3, unchanged=1, shared=0, failed=0)

    def cleanup_duplicate_untracked(self) -> CleanupReport:
        self.cleaned += 1
        return CleanupReport(removed=("old/dup.pdf",), kept=())


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
    controller, query, _ = make_controller(html_dir)

    snapshot = controller.query(QuerySpec(113, 113))

    assert snapshot is not None
    assert controller.state is not None
    assert len(controller.state.exams) == 2
    assert len(controller.state.categories) == 75
    assert controller.can_download() is True
    assert controller.stats().records == 371


@pytest.mark.parametrize("version,exporter", [(None, None), (0, lambda task_id: ()), (1, None)])
def test_incompatible_task_service_blocks_submission_retry_and_export(html_dir, version, exporter):
    from app.engine.task_service import TaskServiceError
    from app.ui.controller import RESTART_REQUIRED_MESSAGE

    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    tasks.api_version = version
    tasks.export_results = exporter
    controller.task_id = "already-running"
    assert not controller.can_download()
    assert controller.start_download() is None
    assert controller.last_error == RESTART_REQUIRED_MESSAGE
    assert controller.retry() is None
    assert tasks.submissions == []
    assert tasks.retried == 0
    assert controller.task_id == "already-running"
    with pytest.raises(TaskServiceError, match="重新啟動"):
        controller.export_paths()


def test_constructor_reports_legacy_service():
    from app.ui.controller import RESTART_REQUIRED_MESSAGE

    controller = AppController(query_service=FakeQueryService(), task_service=object())
    assert controller.last_error == RESTART_REQUIRED_MESSAGE


def test_export_uses_task_results_after_selection_changes(html_dir, monkeypatch):
    from app.engine.downloader import DownloadResult
    from app.models import AttachmentStatus

    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.start_download()
    task_id = controller.task_id
    seen = []

    def export_results(requested_id):
        seen.append(requested_id)
        return (DownloadResult("asset", AttachmentStatus.COMPLETED, relative_path="原任務.pdf"),)

    monkeypatch.setattr(tasks, "export_results", export_results, raising=False)
    controller.set_selection("subjects", ())
    assert controller.export_paths() == ("原任務.pdf",)
    assert seen == [task_id]


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
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.quick_text["subjects"] = "法學"
    visible = controller.visible("subjects")

    assert 0 < len(visible) < len(controller.candidates().subjects)

    controller.set_selection("subjects", ())
    controller.apply_action("subjects", "select_all")

    assert controller.state.subjects == frozenset(item.value for item in visible)
    assert controller.stats().records > 0


def test_invert_and_clear_visible(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.quick_text["categories"] = "一般行政"
    visible = controller.visible("categories")

    controller.apply_action("categories", "clear")
    assert all(item.value not in controller.state.categories for item in visible)

    controller.apply_action("categories", "invert")
    assert all(item.value in controller.state.categories for item in visible)


# --- 預覽 --------------------------------------------------------------


def test_preview_returns_plan(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.set_selection("exams", ("113年公務人員初等考試",))
    controller.set_selection("categories", ("初等考試_一般行政",))
    controller.set_selection("subjects", ("國文",))

    plan = controller.preview(PathMode.A)

    assert plan is not None
    assert len(plan.files) == 2
    assert plan.files[0].relative_path.endswith("_試題.pdf")


def test_preview_with_invalid_template_sets_error(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    plan = controller.preview(PathMode.D, template="{未知}_{題型}.pdf")

    assert plan is None
    assert "路徑設定錯誤" in (controller.last_error or "")


def test_preview_fingerprint_tracks_selection_and_mode(html_dir) -> None:
    """選取、年度、歸檔模式或模板任一變動，預覽就必須失效。"""
    controller, query, _ = make_controller(html_dir)
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
    controller, query, _ = make_controller(html_dir)

    controller.query(
        QuerySpec(112, 113, category_keyword="一般行政", subject_keyword="國文")
    )
    scope = controller.query_scope_text()

    assert "年度 112–113" in scope
    assert "類科含「一般行政」" in scope
    assert "科目含「國文」" in scope


def test_query_scope_text_without_keywords(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    assert controller.query_scope_text() == "年度 113–113"


def test_controller_reorganize_and_cleanup(html_dir) -> None:
    controller, _, tasks = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    report = controller.reorganize()
    assert report is not None
    assert report.moved == 3
    assert tasks.reorganized is not None
    assert tasks.reorganized[0] == "snap-1"

    cleanup = controller.cleanup_duplicates()
    assert cleanup is not None
    assert cleanup.removed == ("old/dup.pdf",)
    assert tasks.cleaned == 1


# --- 候選 chips 與快取 ------------------------------------------------


def test_add_and_remove_values(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.set_selection("subjects", ())

    controller.add_values("subjects", ("國文",))
    controller.add_values("subjects", ("國文",))
    assert controller.selected_values("subjects") == ("國文",)

    controller.remove_values("subjects", ("國文",))
    assert controller.selected_values("subjects") == ()


def test_candidates_cache_reused_until_selection_changes(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    first = controller.candidates()
    assert controller.candidates() is first, "選取未變時應沿用快取"

    controller.set_selection("subjects", ("國文",))
    assert controller.candidates() is not first


def test_candidates_prune_selections_that_left_the_pool(html_dir) -> None:
    """取消某考試後，該考試專屬的類科／科目應自動移出選取。"""
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    before = len(controller.state.categories)
    exam = controller.selected_values("exams")[0]

    controller.remove_values("exams", (exam,))
    candidates = controller.candidates()

    assert len(controller.state.categories) == len(candidates.categories)
    assert len(controller.state.subjects) == len(candidates.subjects)
    assert len(controller.state.categories) < before


def test_query_resets_quick_filter_text(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))
    controller.quick_text["subjects"] = "法學"

    controller.query(QuerySpec(113, 113))

    assert controller.quick_text["subjects"] == ""


def test_selected_values_rejects_unknown_dimension(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
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


def test_deselect_last_item_sets_zero_selection_and_blocks_download(html_dir) -> None:
    """取消最後一個勾選時應變為零選取並禁用下載，而非恢復全選。"""
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    # 先只勾選單一科目
    controller.set_dimension_filters("subjects", {"國文"})
    assert controller.stats().records > 0
    assert controller.can_download() is True

    # 取消最後一個勾選（傳入空集合）
    controller.set_dimension_filters("subjects", set())
    assert controller.active_filters["subjects"] == set()
    assert controller.state is not None
    assert controller.state.subjects == frozenset()
    assert controller.stats().records == 0
    assert controller.can_download() is False


def test_re_query_resets_active_filters(html_dir) -> None:
    """重新查詢成功後，必須清除舊的 active_filters，避免操作另一欄時因殘留條件變成 0 筆。"""
    controller, query, _ = make_controller(html_dir)
    controller.query(QuerySpec(113, 113))

    # 模擬前一次查詢留下的篩選條件
    controller.set_dimension_filters("subjects", {"不存在於新查詢的舊科目"})
    assert controller.stats().records == 0

    # 重新查詢成功後，active_filters 應全部重設為 None（不限）
    controller.query(QuerySpec(113, 113))
    assert controller.active_filters == {"exams": None, "categories": None, "subjects": None}
    assert controller.stats().records == 371

    # 此時操作另一欄（例如勾選初等考試），不會被舊科目條件影響而變成 0 筆
    controller.set_dimension_filters("exams", {"113年公務人員初等考試"})
    assert controller.stats().records == 56


def test_query_common_subject_filters_exact_subject_family(html_dir) -> None:
    """共同科目快速查詢：查詢後自動將 subjects 維度收斂為匹配的共同科目家族名稱。"""
    controller, query, _ = make_controller(html_dir)

    snapshot = controller.query_common_subject(113, 113, "chinese", exam_name_keyword="初等")
    assert snapshot is not None
    assert query.calls[-1] == QuerySpec(
        113, 113, exam_name_keyword="初等", subject_keyword="國文"
    )
    assert controller.active_filters["subjects"] is not None
    assert all(
        name == "國文" or name.startswith("國文（") or name.startswith("國文(")
        for name in controller.active_filters["subjects"]
    )
    assert "外國文（英文）" not in controller.active_filters["subjects"]


def test_query_common_subject_rejects_unknown_preset(html_dir) -> None:
    controller, query, _ = make_controller(html_dir)
    assert controller.query_common_subject(113, 113, "unknown_family") is None
    assert controller.last_error is not None
    assert "未知共同科目家族" in controller.last_error


def test_preview_collapses_shared_paper_paths(html_dir) -> None:
    from app.engine.filter import FilterState
    from app.models import AssetRole, AssetScope, ExamAsset, ExamRecord, QuestionType, TypeEvidence

    evidence = TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="test",
        reason="測試",
        rule_version="test-v1",
    )
    controller, query, _ = make_controller(html_dir)
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
        category_key="301一般行政",
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
        category_key="302一般民政",
        subject_key="法學知識與英文",
    )
    query.snapshot = QuerySnapshot(
        snapshot_id="snap-preview",
        cache_key="key-preview",
        spec=QuerySpec(115, 115),
        records=(r1, r2),
        status=ParseStatus.OK,
        years=(),
        acquired_at="2026-11-07T00:00:00+08:00",
        parser_version="test",
        source_url="https://example.test",
    )
    controller.query(QuerySpec(115, 115))

    plan = controller.preview(mode=PathMode.A)
    assert plan is not None
    assert len(plan.files) == 1
    assert any("共用同一份試卷" in w for w in plan.warnings)
