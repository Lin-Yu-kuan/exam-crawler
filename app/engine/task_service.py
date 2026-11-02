"""任務服務：串接凍結快照、選擇、命名、下載與狀態回報。

邊界與契約（SPEC §3.2、§6、§7）：
- 未完整快照不得開始下載。
- 同一快照與同一組凍結紀錄在執行中只會有一個任務（重複提交防護）。
- 下載階段使用有界派送（同時提交數 ≤ max_workers），取消後不再派發新工作。
- 工作執行緒**不直接呼叫 UI**：狀態以 `TaskState` 快照與事件提供，
  由 UI 輪詢或透過注入的監聽器接收。
"""

from __future__ import annotations

import hashlib
import threading
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from app.engine.downloader import DownloadResult, DownloadTarget
from app.engine.filter import FrozenSelection, resolve_frozen
from app.engine.path_builder import (
    ANSWER_TOKEN,
    FILENAME_TYPE_TOKENS,
    PathBuildError,
    PathMode,
    build_plan,
)
from app.models import (
    AssetRole,
    AttachmentStatus,
    ExamAsset,
    ExamRecord,
    PdfInspection,
    QuerySnapshot,
    TaskStatus,
)
from app.storage.manifest import ManifestStore

MIN_WORKERS = 1
MAX_WORKERS = 4
DEFAULT_WORKERS = 2
TYPE_TOKENS = tuple(FILENAME_TYPE_TOKENS.values()) + (ANSWER_TOKEN,)
SUPPORTED_ROLES = (AssetRole.QUESTION, AssetRole.CHOICE_ANSWER)


class TaskServiceError(Exception):
    """任務服務的可預期錯誤。"""


class SnapshotIncompleteError(TaskServiceError):
    """快照未完整，禁止批量下載。"""


class TaskNotFound(TaskServiceError):
    """找不到指定任務。"""


class TaskStateError(TaskServiceError):
    """任務目前狀態不允許該操作（例如執行中不能重試）。"""


class DownloaderProtocol(Protocol):
    """任務服務需要的下載器介面。"""

    def download(  # pragma: no cover - 介面
        self,
        target: DownloadTarget,
        *,
        should_cancel: Callable[[], bool] | None = None,
        force: bool = False,
    ) -> DownloadResult:
        ...


@dataclass(frozen=True)
class TaskEvent:
    """任務事件；供 UI 顯示進度，不包含 UI 呼叫。"""

    at: str
    kind: str
    message: str
    asset_id: str | None = None


@dataclass(frozen=True)
class TaskState:
    """任務的不可變狀態快照。"""

    task_id: str
    snapshot_id: str
    status: TaskStatus
    attempt: int
    total: int
    completed: int
    skipped: int
    deduped: int
    failed: int
    cancelled: int
    pending: int
    started_at: str | None
    finished_at: str | None
    events: tuple[TaskEvent, ...] = ()
    failures: tuple[tuple[str, str, str], ...] = ()
    unsupported: tuple[tuple[str, str], ...] = ()

    @property
    def done(self) -> int:
        return self.completed + self.skipped

    @property
    def is_partial(self) -> bool:
        return self.status is TaskStatus.PARTIAL

    @property
    def message(self) -> str:
        if self.status is TaskStatus.COMPLETED:
            if self.skipped or self.deduped:
                extras: list[str] = []
                if self.deduped:
                    extras.append(f"{self.deduped} 個與既有檔案內容相同（共用檔案）")
                if self.skipped:
                    extras.append(f"略過 {self.skipped} 個已存在檔案")
                return f"全部完成（{self.done}/{self.total}，其中 {'、'.join(extras)}）"
            return f"全部完成（{self.done}/{self.total}）"
        if self.status is TaskStatus.PARTIAL:
            return f"部分完成（成功 {self.done}，失敗 {self.failed}，取消 {self.cancelled}）"
        if self.status is TaskStatus.CANCELLED:
            return f"已取消（已完成 {self.done}/{self.total}）"
        if self.status is TaskStatus.FAILED:
            return f"下載失敗（{self.failed} 個附件）"
        return f"{self.status.value}（{self.done}/{self.total}）"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class _Task:
    """執行期可變狀態；所有存取都在服務的鎖內。"""

    task_id: str
    snapshot_id: str
    record_ids: tuple[str, ...]
    targets: list[DownloadTarget]
    provisional: dict[str, str]
    unsupported: list[tuple[str, str]]
    attempt: int = 1
    status: TaskStatus = TaskStatus.PENDING
    cancel_requested: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    completed: int = 0
    skipped: int = 0
    deduped: int = 0
    failed: int = 0
    cancelled: int = 0
    events: list[TaskEvent] = field(default_factory=list)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    reserved: dict[str, str] = field(default_factory=dict)
    asset_roles: dict[str, AssetRole] = field(default_factory=dict)
    successful_results: dict[str, DownloadResult] = field(default_factory=dict)

    @property
    def pending(self) -> int:
        return max(
            0, len(self.targets) - self.completed - self.skipped - self.failed - self.cancelled
        )

    def state(self) -> TaskState:
        return TaskState(
            task_id=self.task_id,
            snapshot_id=self.snapshot_id,
            status=self.status,
            attempt=self.attempt,
            total=len(self.targets),
            completed=self.completed,
            skipped=self.skipped,
            deduped=self.deduped,
            failed=self.failed,
            cancelled=self.cancelled,
            pending=self.pending,
            started_at=self.started_at,
            finished_at=self.finished_at,
            events=tuple(self.events),
            failures=tuple(self.failures),
            unsupported=tuple(self.unsupported),
        )


class TaskService:
    """任務建立、取消與重試；狀態轉移一律在鎖內完成。"""

    def __init__(
        self,
        *,
        store: ManifestStore,
        downloader: DownloaderProtocol,
        root: str | Path,
        mode: PathMode = PathMode.A,
        template: str | None = None,
        max_workers: int = DEFAULT_WORKERS,
        state_listener: Callable[[TaskState], None] | None = None,
        task_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not MIN_WORKERS <= max_workers <= MAX_WORKERS:
            raise ValueError(f"併發數必須介於 {MIN_WORKERS}～{MAX_WORKERS}")
        self._store = store
        self._downloader = downloader
        self._root = Path(root)
        self._mode = mode
        self._template = template
        self._max_workers = max_workers
        self._listener = state_listener
        self._id_factory = task_id_factory or (
            lambda: f"task-{hashlib.sha256(str(threading.get_ident()).encode() + _now_iso().encode()).hexdigest()[:12]}"
        )
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}
        self._threads: dict[str, threading.Thread] = {}

    # --- 對外 API -----------------------------------------------------
    def submit(self, snapshot: QuerySnapshot, frozen: FrozenSelection) -> TaskState:
        """建立任務；未完整快照與重複提交都在此攔下。"""
        if not snapshot.is_complete:
            raise SnapshotIncompleteError(
                f"快照未完整（{snapshot.message}），請重新查詢或縮小條件後再下載"
            )
        records = resolve_frozen(snapshot.records, frozen)
        provisional, unsupported, roles = self._plan_paths(records)
        if not provisional:
            raise TaskServiceError("沒有可下載的附件")

        with self._lock:
            for task in self._tasks.values():
                if (
                    task.snapshot_id == snapshot.snapshot_id
                    and task.record_ids == tuple(frozen.record_ids)
                    and task.status.is_active
                ):
                    return task.state()

            task_id = self._id_factory()
            task = _Task(
                task_id=task_id,
                snapshot_id=snapshot.snapshot_id,
                record_ids=tuple(frozen.record_ids),
                targets=[],
                provisional=provisional,
                unsupported=unsupported,
                asset_roles=roles,
                reserved={
                    _path_key(path): asset_id for asset_id, path in provisional.items()
                },
            )
            task.targets = self._build_targets(records, task)
            task.events.append(
                TaskEvent(at=_now_iso(), kind="created", message=f"建立任務（{len(task.targets)} 個附件）")
            )
            self._tasks[task_id] = task
            thread = threading.Thread(
                target=self._run, args=(task, snapshot), name=f"{task_id}-runner", daemon=True
            )
            self._threads[task_id] = thread
            state = task.state()
            thread.start()
        self._notify(state)
        return state

    def cancel(self, task_id: str) -> TaskState:
        with self._lock:
            task = self._task(task_id)
            if not task.status.is_active:
                return task.state()
            task.cancel_requested = True
            task.status = TaskStatus.CANCELLING
            task.events.append(
                TaskEvent(at=_now_iso(), kind="cancelling", message="已要求取消，停止派發新工作")
            )
            state = task.state()
        self._notify(state)
        return state

    def retry_failed(self, task_id: str, *, snapshot: QuerySnapshot) -> TaskState:
        """只重試尚未完成的附件。"""
        with self._lock:
            task = self._task(task_id)
            if task.status.is_active:
                raise TaskStateError("任務仍在執行中，無法重試")
            remaining_targets = [
                target for target in task.targets if self._needs_download(target.asset.asset_id)
            ]
            if not remaining_targets:
                raise TaskStateError("沒有需要重試的附件")
            task.targets = remaining_targets
            task.attempt += 1
            task.status = TaskStatus.PENDING
            task.cancel_requested = False
            task.completed = task.skipped = task.failed = task.cancelled = 0
            task.deduped = 0
            task.failures.clear()
            task.finished_at = None
            task.reserved = {
                _path_key(path): asset_id for asset_id, path in task.provisional.items()
            }
            task.events.append(
                TaskEvent(
                    at=_now_iso(),
                    kind="retry",
                    message=f"第 {task.attempt} 次嘗試，僅重試 {len(remaining_targets)} 個未完成附件",
                )
            )
            thread = threading.Thread(
                target=self._run, args=(task, snapshot), name=f"{task_id}-retry", daemon=True
            )
            self._threads[task_id] = thread
            state = task.state()
            thread.start()
        self._notify(state)
        return state

    def get(self, task_id: str) -> TaskState:
        with self._lock:
            return self._task(task_id).state()

    def list_tasks(self) -> tuple[TaskState, ...]:
        with self._lock:
            return tuple(task.state() for task in self._tasks.values())

    def export_results(self, task_id: str) -> tuple[DownloadResult, ...]:
        """取得本任務累積成功結果；重試不清除先前成果。"""
        with self._lock:
            task = self._task(task_id)
            if task.status.is_active:
                raise TaskStateError("任務仍在執行中，請完成後再匯出")
            return tuple(task.successful_results[key] for key in sorted(task.successful_results))

    def wait(self, task_id: str, timeout: float = 10.0) -> TaskState:
        """測試與 CLI 用：等待任務結束。"""
        thread = self._threads.get(task_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(task_id)

    # --- 內部 ---------------------------------------------------------
    def _task(self, task_id: str) -> _Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFound(f"找不到任務：{task_id}")
        return task

    def _needs_download(self, asset_id: str) -> bool:
        row = self._store.get_attachment(asset_id)
        return row is None or row.status not in (
            AttachmentStatus.COMPLETED,
            AttachmentStatus.SKIPPED,
        )

    def _plan_paths(
        self, records: tuple[ExamRecord, ...]
    ) -> tuple[dict[str, str], list[tuple[str, str]], dict[str, AssetRole]]:
        """以暫定題型計算整批路徑，並回報不支援的附件。"""
        plan = build_plan(self._root, records, mode=self._mode, template=self._template)
        planned = {item.asset_id: item.relative_path for item in plan.files}
        provisional: dict[str, str] = {}
        roles: dict[str, AssetRole] = {}
        unsupported: list[tuple[str, str]] = []
        for record in records:
            for asset in record.assets:
                if asset.role not in SUPPORTED_ROLES:
                    unsupported.append((asset.asset_id, "非 PDF 或未確認角色，MVP 不支援"))
                    continue
                relative = planned.get(asset.asset_id)
                if relative is None:
                    unsupported.append((asset.asset_id, "無法計算歸檔路徑"))
                    continue
                provisional[asset.asset_id] = relative
                roles[asset.asset_id] = asset.role
        return provisional, unsupported, roles

    def _build_targets(
        self, records: tuple[ExamRecord, ...], task: _Task
    ) -> list[DownloadTarget]:
        targets: list[DownloadTarget] = []
        for record in records:
            for asset in record.assets:
                relative = task.provisional.get(asset.asset_id)
                if relative is None:
                    continue
                targets.append(
                    DownloadTarget(
                        record=record,
                        asset=asset,
                        relative_path=relative,
                        resolve_path=self._resolver_for(task, asset.asset_id),
                    )
                )
        return targets

    def _resolver_for(
        self, task: _Task, asset_id: str
    ) -> Callable[[PdfInspection], str]:
        """建立附件專屬的命名解析器（嗅探後才決定最終檔名）。"""

        def resolve(inspection: PdfInspection) -> str:
            with self._lock:
                provisional = task.provisional.get(asset_id, "")
                if not provisional:
                    raise PathBuildError(f"缺少暫定路徑：{asset_id}")
                if task.asset_roles.get(asset_id) is not AssetRole.QUESTION:
                    return provisional
                token = FILENAME_TYPE_TOKENS[inspection.evidence.question_type]
                candidate = _replace_type_token(provisional, token)
                return self._claim_path(task, candidate, asset_id)

        return resolve

    @staticmethod
    def _claim_path(task: _Task, candidate: str, asset_id: str) -> str:
        """在任務內保證唯一（大小寫與相容正規化不敏感）。"""
        key = _path_key(candidate)
        owner = task.reserved.get(key)
        if owner is None or owner == asset_id:
            task.reserved[key] = asset_id
            return candidate
        for _ in range(3):
            candidate = _insert_hash(candidate, hashlib.sha256(asset_id.encode()).hexdigest()[:8])
            key = _path_key(candidate)
            owner = task.reserved.get(key)
            if owner is None or owner == asset_id:
                task.reserved[key] = asset_id
                return candidate
        raise PathBuildError("無法為附件取得唯一檔名")

    def _run(self, task: _Task, snapshot: QuerySnapshot) -> None:
        with self._lock:
            task.status = TaskStatus.DOWNLOADING
            task.started_at = task.started_at or _now_iso()
        self._notify(task.state())

        try:
            self._dispatch(task)
        finally:
            with self._lock:
                self._finalize(task)
                state = task.state()
            self._notify(state)

    def _dispatch(self, task: _Task) -> None:
        pending = list(task.targets)
        index = 0
        with ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix=f"dl-{task.task_id}"
        ) as pool:
            inflight: dict = {}
            while True:
                while (
                    len(inflight) < self._max_workers
                    and index < len(pending)
                    and not task.cancel_requested
                ):
                    target = pending[index]
                    index += 1
                    inflight[pool.submit(self._download_one, task, target)] = target

                if not inflight:
                    break

                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for future in done:
                    inflight.pop(future)
                    result = future.result()
                    self._record_result(task, result)

        if task.cancel_requested:
            for target in pending[index:]:
                self._mark_cancelled(task, target, "取消後未派發")

    def _download_one(self, task: _Task, target: DownloadTarget) -> DownloadResult:
        if task.cancel_requested:
            return DownloadResult(
                asset_id=target.asset.asset_id,
                status=AttachmentStatus.CANCELLED,
                error_code="cancelled",
                error_message="取消後未派發",
            )
        try:
            return self._downloader.download(
                target, should_cancel=lambda: task.cancel_requested
            )
        except Exception as exc:  # 單一附件失敗不得中斷其他結果收集
            return DownloadResult(
                asset_id=target.asset.asset_id,
                status=AttachmentStatus.FAILED,
                error_code="unexpected-error",
                error_message=f"{type(exc).__name__}: {exc}",
            )

    def _record_result(self, task: _Task, result: DownloadResult) -> None:
        with self._lock:
            if result.is_success:
                task.successful_results[result.asset_id] = result
            if result.skipped or result.status is AttachmentStatus.SKIPPED:
                task.skipped += 1
            elif result.status is AttachmentStatus.COMPLETED:
                task.completed += 1
                if result.shared:
                    task.deduped += 1
            elif result.status is AttachmentStatus.CANCELLED:
                task.cancelled += 1
            else:
                task.failed += 1
                task.failures.append(
                    (result.asset_id, result.error_code or "unknown", result.error_message or "")
                )
            task.events.append(
                TaskEvent(
                    at=_now_iso(),
                    kind=result.status.value,
                    message=(
                        f"{result.status.value}：{result.relative_path or result.error_message or ''}"
                        f"{'（內容相同，共用檔案）' if result.shared else ''}"
                    ),
                    asset_id=result.asset_id,
                )
            )
            state = task.state()
        self._notify(state)

    def _mark_cancelled(self, task: _Task, target: DownloadTarget, message: str) -> None:
        self._store.mark_attachment_started(
            asset_id=target.asset.asset_id,
            snapshot_id=task.snapshot_id,
            record_id=target.record.record_id,
            url=target.asset.url,
            role=target.asset.role.value,
        )
        self._store.fail_attachment(
            asset_id=target.asset.asset_id,
            error_code="cancelled",
            error_message=message,
            status=AttachmentStatus.CANCELLED,
        )
        self._record_result(
            task,
            DownloadResult(
                asset_id=target.asset.asset_id,
                status=AttachmentStatus.CANCELLED,
                error_code="cancelled",
                error_message=message,
            ),
        )

    def _finalize(self, task: _Task) -> None:
        task.finished_at = _now_iso()
        if task.cancel_requested:
            task.status = TaskStatus.CANCELLED
        elif task.failed == 0 and task.cancelled == 0:
            task.status = TaskStatus.COMPLETED
        elif task.completed + task.skipped > 0:
            task.status = TaskStatus.PARTIAL
        else:
            task.status = TaskStatus.FAILED
        task.events.append(
            TaskEvent(at=task.finished_at, kind="finished", message=task.state().message)
        )

    def _notify(self, state: TaskState) -> None:
        if self._listener is not None:
            self._listener(state)


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path.replace("\\", "/")).casefold()


def _replace_type_token(relative: str, token: str) -> str:
    """把檔名結尾的題型詞換成實際嗅探結果。"""
    stem, suffix = relative[:-4], relative[-4:]
    for known in TYPE_TOKENS:
        if stem.endswith(known):
            return f"{stem[: -len(known)]}{token}{suffix}"
    return f"{stem}_{token}{suffix}"


def _insert_hash(relative: str, token_hash: str) -> str:
    stem, suffix = relative[:-4], relative[-4:]
    for known in TYPE_TOKENS:
        if stem.endswith(known):
            return f"{stem[: -len(known)]}{token_hash}_{stem[-len(known):]}{suffix}"
    return f"{stem}_{token_hash}{suffix}"
