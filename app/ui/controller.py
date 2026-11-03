"""UI 控制器：把使用者操作轉成 engine 呼叫，讓 Streamlit 畫面不含業務邏輯。

畫面重跑（rerun）時，控制器保存在 session 狀態中，查詢結果與任務 ID 不會重複產生。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol
from app.engine.downloader import DownloadResult

from app.engine.filter import (
    apply_faceted_selection,
    build_faceted_candidates,
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

#: 控制器對外介面版本；UI 以此判斷 session 內的實例是否過期（熱重載後必須重建）。
#: 只要新增／修改 AppController 的公開方法就必須 +1。
CONTROLLER_API_VERSION = 6
EXPECTED_TASK_SERVICE_API_VERSION = 1
RESTART_REQUIRED_MESSAGE = "程式已更新，請重新啟動應用程式後重新查詢及下載"


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
    api_version: int

    def export_results(self, task_id: str) -> tuple[DownloadResult, ...]:
        ...

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

    api_version = CONTROLLER_API_VERSION

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
        self._candidates_cache: tuple[tuple, CandidateSets] | None = None
        self.active_filters: dict[str, set[str] | None] = {dimension: None for dimension in DIMENSIONS}
        if not self.task_service_compatible():
            self.last_error = RESTART_REQUIRED_MESSAGE

    def task_service_compatible(self) -> bool:
        """檢查實際服務實例，不能僅依控制器版本判定。"""
        return (
            getattr(self._task_service, "api_version", None) == EXPECTED_TASK_SERVICE_API_VERSION
            and callable(getattr(self._task_service, "export_results", None))
        )

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
        self.quick_text = {dimension: "" for dimension in DIMENSIONS}
        self.active_filters = {dimension: None for dimension in DIMENSIONS}
        self._candidates_cache = None
        candidates = build_candidates(snapshot.records, FilterState())
        self.state = candidates.initial_state(
            year_start=spec.start_year, year_end=spec.end_year
        )
        return snapshot

    def recent_query_specs(self, limit: int = 10) -> tuple[QuerySpec, ...]:
        """讀取歷史查詢條件（供介面快速選取回填）。"""
        if self._store is None or not hasattr(self._store, "list_recent_specs"):
            return ()
        try:
            return self._store.list_recent_specs(limit=limit)
        except Exception:
            return ()


    # --- 多向互相收斂選取 ---------------------------------------------
    @staticmethod
    def _as_frozenset(values: set[str] | None) -> frozenset[str] | None:
        return None if values is None else frozenset(values)

    def faceted_candidates(self) -> CandidateSets:
        """多向互相收斂候選清單；母體永遠完整保存。"""
        if self.snapshot is None:
            return CandidateSets((), (), ())
        spec = self.snapshot.spec
        return build_faceted_candidates(
            self.snapshot.records,
            selected_exams=self._as_frozenset(self.active_filters["exams"]),
            selected_categories=self._as_frozenset(self.active_filters["categories"]),
            selected_subjects=self._as_frozenset(self.active_filters["subjects"]),
            year_start=spec.start_year,
            year_end=spec.end_year,
        )

    def set_dimension_filters(self, dimension: str, selected_values: set[str] | None) -> None:
        """更新單一維度的主動篩選條件（None 代表不限，空集合代表零選取），並同步更新底層 state。"""
        if dimension not in DIMENSIONS:
            raise ValueError(f"未知欄位：{dimension}")
        self.active_filters[dimension] = None if selected_values is None else set(selected_values)
        self._sync_state_from_filters()

    def clear_dimension_filters(self, dimension: str) -> None:
        """清空單一維度篩選（恢復為不限）。"""
        self.set_dimension_filters(dimension, None)

    def clear_all_filters(self) -> None:
        """清空所有維度篩選（全部恢復原貌）。"""
        for dim in DIMENSIONS:
            self.active_filters[dim] = None
        self._sync_state_from_filters()

    def _sync_state_from_filters(self) -> None:
        """將多向收斂條件同步至 state，確保相容下載與既有介面。"""
        if self.snapshot is None:
            return
        candidates = self.faceted_candidates()
        spec = self.snapshot.spec

        def resolve_dim(dim: str, items: tuple[Candidate, ...]) -> frozenset[str]:
            active = self.active_filters[dim]
            available = {item.value for item in items}
            if active is None:
                return frozenset(available)
            return frozenset(active & available)

        self.state = FilterState(
            exams=resolve_dim("exams", candidates.exams),
            categories=resolve_dim("categories", candidates.categories),
            subjects=resolve_dim("subjects", candidates.subjects),
            year_start=spec.start_year,
            year_end=spec.end_year,
        )

    # --- 候選與選取 ---------------------------------------------------
    def candidates(self) -> CandidateSets:
        """依目前選取計算候選（以選取狀態快取，避免每次重跑都重算）。

        選取其他欄位後，某些已選值可能已不在候選池內（例如取消某考試後，
        該考試專屬的類科）。這裡會把它們從選取中移除，讓「已選 N／總數」
        永遠一致；移除後重新計算一次，必要時重複直到穩定（最多 3 輪）。
        """
        if self.snapshot is None or self.state is None:
            return CandidateSets((), (), ())
        key = self._selection_key()
        if self._candidates_cache is not None and self._candidates_cache[0] == key:
            return self._candidates_cache[1]
        computed = build_candidates(self.snapshot.records, self.state)
        for _ in range(3):
            if not self._prune_orphans(computed):
                break
            computed = build_candidates(self.snapshot.records, self.state)
        self._candidates_cache = (self._selection_key(), computed)
        return computed

    def _prune_orphans(self, candidates: CandidateSets) -> bool:
        """把不在候選池的已選值移除；有變動時回傳 True。"""
        state = self.state
        if state is None:
            return False
        data = {
            "exams": state.exams,
            "categories": state.categories,
            "subjects": state.subjects,
            "year_start": state.year_start,
            "year_end": state.year_end,
        }
        changed = False
        for dimension in DIMENSIONS:
            available = {item.value for item in getattr(candidates, dimension)}
            current = data[dimension]
            kept = current & available
            if kept != current:
                data[dimension] = kept
                changed = True
        if changed:
            self.state = FilterState(**data)
        return changed


    def _selection_key(self) -> tuple:
        state = self.state
        if state is None or self.snapshot is None:
            return ()
        return (
            self.snapshot.snapshot_id,
            tuple(sorted(state.exams)),
            tuple(sorted(state.categories)),
            tuple(sorted(state.subjects)),
            state.year_start,
            state.year_end,
        )

    def selected_values(self, dimension: str) -> tuple[str, ...]:
        """目前已選的候選值（排序後，供 chips 顯示）。"""
        if self.state is None:
            return ()
        if dimension not in DIMENSIONS:
            raise ValueError(f"未知欄位：{dimension}")
        return tuple(sorted(getattr(self.state, dimension)))

    def add_values(self, dimension: str, values: tuple[str, ...]) -> None:
        """把指定候選加入選取（供「從清單加入」使用）。"""
        if self.state is None or not values:
            return
        merged = set(getattr(self.state, dimension)) | set(values)
        self.set_selection(dimension, tuple(merged))

    def remove_values(self, dimension: str, values: tuple[str, ...]) -> None:
        """把指定候選移出選取（供「叉掉 chips」使用）。"""
        if self.state is None or not values:
            return
        remaining = set(getattr(self.state, dimension)) - set(values)
        self.set_selection(dimension, tuple(remaining))

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

    def query_scope_text(self) -> str:
        """顯示本次查詢範圍（年度與已套用的關鍵字）。"""
        if self.snapshot is None:
            return "尚未查詢"
        spec = self.snapshot.spec
        parts = [f"年度 {spec.start_year}–{spec.end_year}"]
        if spec.exam_code:
            parts.append(f"考試代碼 {spec.exam_code}")
        for label, value in (
            ("考試名稱", spec.exam_name_keyword),
            ("類科", spec.category_keyword),
            ("科目", spec.subject_keyword),
        ):
            if value:
                parts.append(f"{label}含「{value}」")
        return "｜".join(parts)

    def can_download(self) -> bool:
        if not self.task_service_compatible():
            return False
        if self.snapshot is None or not self.snapshot.is_complete or self.state is None:
            return False
        return can_download(self.state, self.stats())

    # --- 路徑預覽 -----------------------------------------------------
    def preview_fingerprint(
        self, mode: PathMode = PathMode.A, template: str | None = None
    ) -> tuple:
        """預覽結果的識別碼：選取、年度、歸檔模式或模板任一變動即失效。"""
        state = self.state
        if state is None or self.snapshot is None:
            return ()
        return (
            self.snapshot.snapshot_id,
            mode.value,
            template or "",
            state.year_start,
            state.year_end,
            tuple(sorted(state.exams)),
            tuple(sorted(state.categories)),
            tuple(sorted(state.subjects)),
        )

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
        if not self.task_service_compatible():
            self.last_error = RESTART_REQUIRED_MESSAGE
            return None
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

    def export_paths(self) -> tuple[str, ...]:
        """依任務成果匯出，不使用目前畫面上的篩選條件。"""
        if not self.task_service_compatible():
            raise TaskServiceError(RESTART_REQUIRED_MESSAGE)
        if self.task_id is None:
            return ()
        results = self._task_service.export_results(self.task_id)
        if any(not result.relative_path for result in results):
            raise TaskServiceError("任務成果缺少檔案路徑，無法匯出")
        return tuple(sorted({result.relative_path for result in results if result.relative_path}))

    def export_script_items(
        self, mode: PathMode = PathMode.A, template: str | None = None
    ) -> list[tuple[str, str]]:
        """產生目前選取考科的 (url, relative_path) 清單，供腳本生成器使用。"""
        plan = self.preview(mode=mode, template=template)
        if plan is None:
            return []
        records = self.selected_records()
        url_map: dict[tuple[str, str], str] = {}
        for r in records:
            for a in r.assets:
                url_map[(r.record_id, a.asset_id)] = a.url
        items: list[tuple[str, str]] = []
        for f in plan.files:
            url = url_map.get((f.record_id, f.asset_id))
            if url:
                items.append((url, f.relative_path))
        return items

    def cancel(self) -> TaskState | None:
        if self.task_id is None:
            return None
        try:
            return self._task_service.cancel(self.task_id)
        except TaskServiceError as exc:
            self.last_error = str(exc)
            return None

    def retry(self) -> TaskState | None:
        if not self.task_service_compatible():
            self.last_error = RESTART_REQUIRED_MESSAGE
            return None
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
