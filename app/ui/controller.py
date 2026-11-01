"""UI 控制器：把使用者操作轉成 engine 呼叫，讓 Streamlit 畫面不含業務邏輯。

畫面重跑（rerun）時，控制器保存在 session 狀態中，查詢結果與任務 ID 不會重複產生。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from app.engine.filter import (
    Candidate,
    CandidateSets,
    FilterState,
    SelectionError,
    SelectionStats,
    SortOrder,
    apply_selection,
    build_candidates,
    can_download,
    clear_visible,
    freeze_selection,
    invert_visible,
    quick_filter,
    select_all_visible,
    selection_stats,
    sort_records,
)
from app.engine.path_builder import PathBuildError, PathMode, PathPlan, build_plan
from app.engine.query_service import QueryCancelled
from app.engine.task_service import SnapshotIncompleteError, TaskServiceError, TaskState
from app.models import ExamRecord, QuerySnapshot, QuerySpec
from app.storage.manifest import ReconcileReport

#: 官方年度選項範圍（民國年；實測 1992（81）～2026（115））
MIN_YEAR = 81
MAX_YEAR = 115
DIMENSIONS = ("exams", "categories", "subjects")
DIMENSION_LABELS = {"exams": "考試", "categories": "類科", "subjects": "科目"}


class QueryServiceProtocol(Protocol):
    def query(  # pragma: no cover - 介面
        self,
        spec: QuerySpec,
        *,
        force_refresh: bool = False,
        on_progress: Callable | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> QuerySnapshot:
        ...


class TaskServiceProtocol(Protocol):
    def submit(  # pragma: no cover - 介面
        self, snapshot: QuerySnapshot, frozen
    ) -> TaskState:
        ...

    def get(self, task_id: str) -> TaskState:  # pragma: no cover - 介面
        ...

    def cancel(self, task_id: str) -> TaskState:  # pragma: no cover - 介面
        ...

    def retry_failed(self, task_id: str, *, snapshot: QuerySnapshot) -> TaskState:  # pragma: no cover
        ...


def validate_query_inputs(start_year: int, end_year: int) -> list[str]:
    """檢查年度輸入；回傳錯誤訊息清單（空清單代表通過）。"""
    errors: list[str] = []
    if start_year <= 0 or end_year <= 0:
        errors.append("年度必須是正的民國年")
        return errors
    if start_year > end_year:
        errors.append("起年不得大於迄年")
    if start_year < MIN_YEAR or end_year > MAX_YEAR:
        errors.append(f"年度需介於民國 {MIN_YEAR}～{MAX_YEAR} 年")
    return errors


class AppController:
    """介面狀態與 engine 呼叫的協調者。"""

    def __init__(
        self,
        *,
        query_service: QueryServiceProtocol,
        task_service: TaskServiceProtocol,
        store=None,
        root: str | Path = "downloads",
    ) -> None:
        self._query_service = query_service
        self._task_service = task_service
        self._store = store
        self._root = Path(root)
        self.snapshot: QuerySnapshot | None = None
        self.state: FilterState | None = None
        self.task_id: str | None = None
        self.last_error: str | None = None
        self.quick_text = {dimension: "" for dimension in DIMENSIONS}
        self.reconcile_report: ReconcileReport | None = None
        self._reconciled = False

    # --- 查詢 ---------------------------------------------------------
    def query(
        self,
        spec: QuerySpec,
        *,
        force: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> QuerySnapshot | None:
        """執行查詢並重置選取；錯誤時保留先前狀態並記錄訊息。"""
        self.last_error = None
        errors = validate_query_inputs(spec.start_year, spec.end_year)
        if errors:
            self.last_error = "；".join(errors)
            return None
        try:
            snapshot = self._query_service.query(
                spec, force_refresh=force, should_cancel=should_cancel
            )
        except QueryCancelled:
            self.last_error = "查詢已取消，未取得完整結果"
            return None
        except Exception as exc:  # 來源錯誤需顯示可採取動作，不可靜默
            self.last_error = f"查詢失敗：{exc}"
            return None

        self.snapshot = snapshot
        self.task_id = None
        candidates = build_candidates(snapshot.records, FilterState())
        self.state = candidates.initial_state(
            year_start=spec.start_year, year_end=spec.end_year
        )
        return snapshot

    # --- 候選與選取 ---------------------------------------------------
    def candidates(self) -> CandidateSets:
        if self.snapshot is None or self.state is None:
            return CandidateSets((), (), ())
        return build_candidates(self.snapshot.records, self.state)

    def visible(self, dimension: str) -> tuple[Candidate, ...]:
        """依快速過濾文字回傳可見候選（只影響顯示）。"""
        if dimension not in DIMENSIONS:
            raise ValueError(f"未知欄位：{dimension}")
        return quick_filter(
            getattr(self.candidates(), dimension), self.quick_text.get(dimension, "")
        )

    def apply_action(self, dimension: str, action: str) -> None:
        """對「目前可見」候選執行全選／全部取消／反選。"""
        if self.state is None:
            return
        visible = self.visible(dimension)
        if action == "select_all":
            self.state = select_all_visible(self.state, dimension, visible)
        elif action == "clear":
            self.state = clear_visible(self.state, dimension, visible)
        elif action == "invert":
            self.state = invert_visible(self.state, dimension, visible)
        else:
            raise ValueError(f"未知操作：{action}")

    def set_selection(self, dimension: str, values: tuple[str, ...]) -> None:
        """以多選清單直接指定選取值。"""
        if dimension not in DIMENSIONS:
            raise ValueError(f"未知欄位：{dimension}")
        if self.state is None:
            return
        data = {
            "exams": self.state.exams,
            "categories": self.state.categories,
            "subjects": self.state.subjects,
            "year_start": self.state.year_start,
            "year_end": self.state.year_end,
        }
        data[dimension] = frozenset(values)
        self.state = FilterState(**data)

    def selected_records(self) -> tuple[ExamRecord, ...]:
        if self.snapshot is None or self.state is None:
            return ()
        return sort_records(
            apply_selection(self.snapshot.records, self.state), SortOrder.YEAR_THEN_EXAM
        )

    def stats(self) -> SelectionStats:
        return selection_stats(self.selected_records())

    def can_download(self) -> bool:
        if self.snapshot is None or not self.snapshot.is_complete or self.state is None:
            return False
        return can_download(self.state, self.stats())

    # --- 路徑預覽 -----------------------------------------------------
    def preview(self, mode: PathMode = PathMode.A, template: str | None = None) -> PathPlan | None:
        """計算暫定歸檔路徑（題型未確認者標示暫定）。"""
        self.last_error = None
        records = self.selected_records()
        if not records:
            self.last_error = "沒有選取任何紀錄，無法預覽路徑"
            return None
        try:
            return build_plan(self._root, records, mode=mode, template=template)
        except PathBuildError as exc:
            self.last_error = f"路徑設定錯誤：{exc}"
            return None

    # --- 任務 ---------------------------------------------------------
    def start_download(self) -> TaskState | None:
        """開始下載；重複點擊不會建立第二個任務。"""
        self.last_error = None
        if self.snapshot is None or self.state is None:
            self.last_error = "請先完成查詢"
            return None
        if not self.snapshot.is_complete:
            self.last_error = f"快照未完整（{self.snapshot.message}），已停用下載"
            return None

        current = self.progress()
        if self.task_id is not None and current is not None and current.status.is_active:
            return current

        records = self.selected_records()
        if not records:
            self.last_error = "沒有選取任何紀錄（零選取），請先勾選候選項目"
            return None
        if not self.can_download():
            self.last_error = "三個候選清單都必須至少選取一個項目"
            return None

        try:
            frozen = freeze_selection(
                records, snapshot_id=self.snapshot.snapshot_id
            )
        except SelectionError as exc:
            self.last_error = str(exc)
            return None

        try:
            state = self._task_service.submit(self.snapshot, frozen)
        except (SnapshotIncompleteError, TaskServiceError, PathBuildError) as exc:
            self.last_error = f"無法開始下載：{exc}"
            return None
        self.task_id = state.task_id
        return state

    def progress(self) -> TaskState | None:
        if self.task_id is None:
            return None
        try:
            return self._task_service.get(self.task_id)
        except Exception:  # 任務可能屬於先前的程序，視為不存在
            return None

    def cancel(self) -> TaskState | None:
        if self.task_id is None:
            return None
        try:
            return self._task_service.cancel(self.task_id)
        except TaskServiceError as exc:
            self.last_error = str(exc)
            return None

    def retry(self) -> TaskState | None:
        if self.task_id is None or self.snapshot is None:
            return None
        try:
            return self._task_service.retry_failed(self.task_id, snapshot=self.snapshot)
        except TaskServiceError as exc:
            self.last_error = str(exc)
            return None

    # --- 啟動核對 -----------------------------------------------------
    def reconcile(self) -> ReconcileReport | None:
        """啟動時核對一次：修復中斷、清理暫存、列出未追蹤檔案。"""
        if self._reconciled or self._store is None:
            return self.reconcile_report
        self._reconciled = True
        try:
            self.reconcile_report = self._store.reconcile(self._root)
        except Exception as exc:
            self.last_error = f"啟動核對失敗：{exc}"
            self.reconcile_report = None
        return self.reconcile_report
