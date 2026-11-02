"""任務服務測試：完整流程、部分完成、取消、重試與重複提交（離線）。"""

from __future__ import annotations

import ast
import pathlib
import threading
import time

import pytest

from app.crawler.parser import parse_result_page
from app.engine.downloader import DownloadResult, DownloadTarget
from app.engine.filter import freeze_selection
from app.engine.task_service import (
    SnapshotIncompleteError,
    TaskNotFound,
    TaskService,
    TaskState,
    TaskStateError,
)
from app.models import (
    AssetRole,
    AttachmentStatus,
    ExamAsset,
    ExamRecord,
    ParseStatus,
    PdfInspection,
    PdfStatus,
    QuerySnapshot,
    QuerySpec,
    QuestionType,
    TaskStatus,
    TypeEvidence,
)
from app.storage.manifest import ManifestStore
from conftest import synthetic_year_page


class FakeDownloader:
    """可控制的下載器：記錄呼叫、模擬失敗、可擋住以測試取消與重複提交。"""

    def __init__(
        self,
        *,
        store: ManifestStore | None = None,
        failures: set[str] | None = None,
        delay: float = 0.0,
        gate: threading.Event | None = None,
        question_types: dict[str, QuestionType] | None = None,
    ) -> None:
        self.store = store
        self.failures = failures or set()
        self.delay = delay
        self.gate = gate
        self.question_types = question_types or {}
        self.calls: list[str] = []
        self.paths: dict[str, str] = {}
        self.shared: dict[str, str] = {}
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _start(self, target: DownloadTarget) -> None:
        if self.store is None:
            return
        self.store.mark_attachment_started(
            asset_id=target.asset.asset_id,
            snapshot_id="snap-1",
            record_id=target.record.record_id,
            url=target.asset.url,
            role=target.asset.role.value,
        )

    def _finish(self, target: DownloadTarget, result: DownloadResult) -> None:
        if self.store is None:
            return
        if result.status in (AttachmentStatus.COMPLETED, AttachmentStatus.SKIPPED):
            self.store.complete_attachment(
                asset_id=target.asset.asset_id,
                path=result.relative_path or target.relative_path,
                sha256=result.sha256 or "0" * 64,
                size=result.size_bytes,
                version=1,
            )
        else:
            self.store.fail_attachment(
                asset_id=target.asset.asset_id,
                error_code=result.error_code or "unknown",
                error_message=result.error_message or "",
                status=result.status,
            )

    def download(self, target: DownloadTarget, *, should_cancel=None, force: bool = False) -> DownloadResult:
        asset_id = target.asset.asset_id
        with self._lock:
            self.calls.append(asset_id)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self._start(target)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.gate is not None:
                self.gate.wait(5)
            if should_cancel is not None and should_cancel():
                result = DownloadResult(
                    asset_id=asset_id,
                    status=AttachmentStatus.CANCELLED,
                    error_code="cancelled",
                    error_message="測試取消",
                )
                self._finish(target, result)
                return result
            if asset_id in self.failures:
                result = DownloadResult(
                    asset_id=asset_id,
                    status=AttachmentStatus.FAILED,
                    error_code="invalid-pdf",
                    error_message="測試失敗",
                )
                self._finish(target, result)
                return result
            relative = target.relative_path
            if target.resolve_path is not None:
                relative = target.resolve_path(
                    _inspection(self.question_types.get(asset_id, QuestionType.UNKNOWN))
                )
            self.paths[asset_id] = relative
            shared_with = self.shared.get(asset_id)
            result = DownloadResult(
                asset_id=asset_id,
                status=AttachmentStatus.COMPLETED,
                relative_path=relative,
                size_bytes=10,
                sha256="0" * 64,
                dedupe_of=shared_with,
            )
            self._finish(target, result)
            return result
        finally:
            with self._lock:
                self.active -= 1


def _inspection(question_type: QuestionType) -> PdfInspection:
    return PdfInspection(
        status=PdfStatus.VALID,
        reason_code="ok",
        size_bytes=10,
        sha256="0" * 64,
        page_count=1,
        has_text=True,
        text_length=10,
        text_preview="測試",
        evidence=TypeEvidence(
            question_type=question_type,
            source="test",
            reason="測試",
            rule_version="test-v1",
        ),
        elapsed_ms=1.0,
    )


def make_snapshot(records: tuple[ExamRecord, ...], *, status: ParseStatus = ParseStatus.OK) -> QuerySnapshot:
    return QuerySnapshot(
        snapshot_id="snap-1",
        cache_key="key-1",
        spec=QuerySpec(113, 113),
        records=records,
        status=status,
        years=(),
        acquired_at="2026-11-01T12:00:00+08:00",
        parser_version="html-v1",
        source_url="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
    )


@pytest.fixture()
def store(tmp_path) -> ManifestStore:
    return ManifestStore(tmp_path / "manifest.db")


@pytest.fixture()
def records() -> tuple[ExamRecord, ...]:
    page = parse_result_page(synthetic_year_page(113, subjects=2))
    return page.records


def make_service(tmp_path, store, downloader, **kwargs) -> TaskService:
    return TaskService(
        store=store,
        downloader=downloader,
        root=tmp_path / "downloads",
        task_id_factory=kwargs.pop("task_id_factory", lambda: "task-1"),
        **kwargs,
    )


def test_complete_flow(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    downloader = FakeDownloader(store=store)
    service = make_service(tmp_path, store, downloader)

    state = service.submit(snapshot, frozen)
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.COMPLETED
    assert final.completed == 4
    assert final.failed == 0
    assert len(downloader.calls) == 4
    assert final.message.startswith("全部完成")
    assert any(event.kind == "finished" for event in final.events)


def test_shared_content_is_counted_separately(tmp_path, store, records) -> None:
    """內容相同的附件要標示為「共用」，不計入新下載。"""
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    downloader = FakeDownloader(store=store)
    service = make_service(tmp_path, store, downloader)
    asset_ids = [asset.asset_id for record in records for asset in record.assets]
    downloader.shared = {asset_ids[1]: asset_ids[0]}

    state = service.submit(snapshot, frozen)
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.COMPLETED
    assert final.deduped == 1
    assert final.completed == 4
    assert "共用檔案" in final.message
    assert any("共用檔案" in event.message for event in final.events)


def test_incomplete_snapshot_blocks_download(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records, status=ParseStatus.INCOMPLETE)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    downloader = FakeDownloader()
    service = make_service(tmp_path, store, downloader)

    with pytest.raises(SnapshotIncompleteError):
        service.submit(snapshot, frozen)

    assert downloader.calls == []
    assert service.list_tasks() == ()

def test_snapshot_with_stats_difference_still_downloads(tmp_path, store, records) -> None:
    """統計差異（完整但統計不符）不阻擋下載。"""
    snapshot = make_snapshot(records, status=ParseStatus.OK_WITH_DIFF)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    downloader = FakeDownloader(store=store)
    service = make_service(tmp_path, store, downloader)

    state = service.submit(snapshot, frozen)
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.COMPLETED
    assert len(downloader.calls) == 4

def test_duplicate_submission_is_ignored(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    gate = threading.Event()
    downloader = FakeDownloader(store=store, gate=gate)
    service = make_service(tmp_path, store, downloader, max_workers=1)

    first = service.submit(snapshot, frozen)
    second = service.submit(snapshot, frozen)
    gate.set()
    final = service.wait(first.task_id)

    assert second.task_id == first.task_id
    assert len(downloader.calls) == 4, "重複提交不得造成重複下載"
    assert final.status is TaskStatus.COMPLETED


def test_partial_completion_when_answer_fails(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    answer_id = next(
        asset.asset_id for record in records for asset in record.assets
        if asset.role is AssetRole.CHOICE_ANSWER
    )
    downloader = FakeDownloader(store=store, failures={answer_id})
    service = make_service(tmp_path, store, downloader)

    state = service.submit(snapshot, frozen)
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.PARTIAL
    assert final.completed == 3
    assert final.failed == 1
    assert final.message.startswith("部分完成")
    assert final.failures[0][0] == answer_id


def test_retry_only_processes_failed_assets(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    answer_id = next(
        asset.asset_id for record in records for asset in record.assets
        if asset.role is AssetRole.CHOICE_ANSWER
    )
    downloader = FakeDownloader(store=store, failures={answer_id})
    service = make_service(tmp_path, store, downloader)

    state = service.submit(snapshot, frozen)
    first = service.wait(state.task_id)
    assert first.status is TaskStatus.PARTIAL
    first_exports = service.export_results(state.task_id)
    assert first_exports
    assert answer_id not in {result.asset_id for result in first_exports}

    downloader.failures.clear()
    downloader.calls.clear()
    retried = service.retry_failed(state.task_id, snapshot=snapshot)
    final = service.wait(retried.task_id)

    assert final.status is TaskStatus.COMPLETED
    assert final.completed == 1
    assert downloader.calls == [answer_id]
    assert final.attempt == 2
    exports = service.export_results(state.task_id)
    assert {result.asset_id for result in exports} == {result.asset_id for result in first_exports} | {answer_id}
    assert all(result.relative_path for result in exports)


def test_export_is_scoped_to_submitted_selection(tmp_path, store, records):
    snapshot = make_snapshot(records)
    ids = iter(("task-a", "task-b"))
    downloader = FakeDownloader(store=store)
    service = make_service(tmp_path, store, downloader, task_id_factory=lambda: next(ids))
    for selected in (records[:1], records[1:2]):
        task = service.submit(snapshot, freeze_selection(selected, snapshot_id=snapshot.snapshot_id))
        service.wait(task.task_id)
        assert {r.asset_id for r in service.export_results(task.task_id)} == {
            a.asset_id for record in selected for a in record.assets
        }


@pytest.mark.parametrize("status,shared", [(AttachmentStatus.SKIPPED, None), (AttachmentStatus.COMPLETED, "old-asset")])
def test_export_keeps_existing_and_shared_results(tmp_path, store, records, status, shared):
    class ExistingDownloader:
        def download(self, target, **kwargs):
            return DownloadResult(target.asset.asset_id, status, relative_path="既有/共用.pdf", dedupe_of=shared)

    snapshot = make_snapshot(records)
    service = make_service(tmp_path, store, ExistingDownloader())
    task = service.submit(snapshot, freeze_selection(records[:1], snapshot_id=snapshot.snapshot_id))
    service.wait(task.task_id)
    results = service.export_results(task.task_id)
    assert results
    assert all(r.relative_path == "既有/共用.pdf" for r in results)


def test_retry_rejected_while_running(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    gate = threading.Event()
    downloader = FakeDownloader(store=store, gate=gate)
    service = make_service(tmp_path, store, downloader, max_workers=1)

    state = service.submit(snapshot, frozen)
    with pytest.raises(TaskStateError):
        service.retry_failed(state.task_id, snapshot=snapshot)
    gate.set()
    service.wait(state.task_id)


def test_cancel_stops_new_work_and_keeps_completed(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    gate = threading.Event()
    downloader = FakeDownloader(store=store, gate=gate)
    service = make_service(tmp_path, store, downloader, max_workers=1)

    state = service.submit(snapshot, frozen)
    # 等第一個附件進入下載
    for _ in range(100):
        if downloader.calls:
            break
        time.sleep(0.01)

    cancelled = service.cancel(state.task_id)
    assert cancelled.status is TaskStatus.CANCELLING
    gate.set()
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.CANCELLED
    assert final.cancelled >= 3
    assert len(downloader.calls) <= 1, "取消後不得再派發新工作"


def test_cancel_before_start(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    gate = threading.Event()
    downloader = FakeDownloader(store=store, gate=gate)
    service = make_service(tmp_path, store, downloader, max_workers=1)

    state = service.submit(snapshot, frozen)
    service.cancel(state.task_id)
    gate.set()
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.CANCELLED
    assert final.completed == 0


def test_bounded_dispatch_respects_max_workers(tmp_path, store) -> None:
    page = parse_result_page(synthetic_year_page(113, subjects=6))
    records = page.records
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    downloader = FakeDownloader(store=store, delay=0.03)
    service = make_service(tmp_path, store, downloader, max_workers=2)

    state = service.submit(snapshot, frozen)
    final = service.wait(state.task_id)

    assert final.status is TaskStatus.COMPLETED
    assert downloader.peak == 2, "同時在途數應等於設定的併發數且不超過"


def test_invalid_worker_count_is_rejected(tmp_path, store, records) -> None:
    with pytest.raises(ValueError):
        make_service(tmp_path, store, FakeDownloader(store=store), max_workers=0)
    with pytest.raises(ValueError):
        make_service(tmp_path, store, FakeDownloader(store=store), max_workers=5)


def test_sniffed_question_type_changes_filename(tmp_path, store, records) -> None:
    """命名在嗅探後才定案：測驗題會命名為「測驗試題」。"""
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    question_id = next(
        asset.asset_id for record in records for asset in record.assets
        if asset.role is AssetRole.QUESTION
    )
    downloader = FakeDownloader(store=store, question_types={question_id: QuestionType.CHOICE})
    service = make_service(tmp_path, store, downloader)

    state = service.submit(snapshot, frozen)
    service.wait(state.task_id)

    assert downloader.paths[question_id].endswith("_測驗試題.pdf")


def test_unsupported_assets_are_reported(tmp_path, store) -> None:
    evidence = TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="test",
        reason="測試",
        rule_version="test-v1",
    )
    zip_asset = ExamAsset(
        asset_id="r1#other1",
        official_id=None,
        role=AssetRole.OTHER,
        original_label="盲用電腦專用試題",
        url="https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx?t=B&code=113999",
        observed_at="2026-11-01T00:00:00+08:00",
        source_version=None,
        evidence=evidence,
    )
    record = ExamRecord(
        record_id="r1",
        official_id=None,
        year=113,
        exam_name="113年測試考試",
        exam_short_name=None,
        category="測試_一般行政",
        subject="測試科目",
        level=None,
        source_page="test",
        assets=(zip_asset,),
        exam_name_key="113年測試考試",
        category_key="測試_一般行政",
        subject_key="測試科目",
    )
    snapshot = make_snapshot((record,))
    frozen = freeze_selection((record,), snapshot_id=snapshot.snapshot_id)
    downloader = FakeDownloader(store=store)
    service = make_service(tmp_path, store, downloader)

    with pytest.raises(Exception):
        service.submit(snapshot, frozen)
    assert downloader.calls == []


def test_unknown_task_raises(tmp_path, store) -> None:
    service = make_service(tmp_path, store, FakeDownloader(store=store))
    with pytest.raises(TaskNotFound):
        service.get("missing")


def test_completed_message_reports_skipped_files() -> None:
    """重跑時全部略過也要說清楚，避免誤以為又下載了一次。"""
    base = dict(
        task_id="task-1",
        snapshot_id="snap-1",
        status=TaskStatus.COMPLETED,
        attempt=1,
        total=3,
        completed=0,
        skipped=3,
        deduped=0,
        failed=0,
        cancelled=0,
        pending=0,
        started_at=None,
        finished_at=None,
    )

    assert "略過 3" in TaskState(**base).message
    assert "共用" in TaskState(**{**base, "skipped": 0, "completed": 3, "deduped": 3}).message
    assert TaskState(**{**base, "completed": 3, "skipped": 0}).message == "全部完成（3/3）"


def test_service_does_not_import_ui() -> None:
    """工作執行緒不得直接呼叫 UI：任務服務不可依賴 streamlit 或 UI 模組。"""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "app" / "engine" / "task_service.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "streamlit" not in imported
    assert not any(name.startswith("app.ui") for name in imported)


def test_state_listener_receives_progress(tmp_path, store, records) -> None:
    snapshot = make_snapshot(records)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    seen: list[TaskStatus] = []
    downloader = FakeDownloader(store=store)
    service = TaskService(
        store=store,
        downloader=downloader,
        root=tmp_path / "downloads",
        state_listener=lambda state: seen.append(state.status),
        task_id_factory=lambda: "task-1",
    )

    state = service.submit(snapshot, frozen)
    service.wait(state.task_id)

    assert TaskStatus.DOWNLOADING in seen
    assert seen[-1] is TaskStatus.COMPLETED
