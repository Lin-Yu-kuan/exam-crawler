"""本機 Streamlit 介面：查詢、候選、路徑預覽、下載進度、取消與重試。

畫面只負責呈現與轉送操作；查詢、篩選、命名與下載都在 app/engine。
本模組不在匯入時啟動任何服務，也不直接操作背景執行緒。
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# 確保專案根目錄在 sys.path 中，防範在任何目錄或雲端直接以 streamlit run app/ui/web_app.py 啟動時發生 ModuleNotFoundError
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app.crawler.moex_client import MoexClient
from app.crawler.rate_limiter import RateLimitConfig, RateLimiter
from app.engine.downloader import Downloader
from app.engine.path_builder import DEFAULT_TEMPLATES, PathMode
from app.engine.query_service import QueryService
from app.engine.task_service import DEFAULT_WORKERS, MAX_WORKERS, MIN_WORKERS, TaskService, TaskServiceError
from app.models import AssetRole, ParseStatus, QuerySpec, TaskStatus
from app.storage.manifest import ManifestStore
from app.ui.controller import (
    CONTROLLER_API_VERSION,
    DIMENSION_LABELS,
    DIMENSIONS,
    AppController,
    validate_query_inputs,
)

def is_cloud_env() -> bool:
    """判斷是否運行於 Streamlit Cloud 或 Linux 無原生視窗的雲端環境。"""
    cloud_env_keys = ("STREAMLIT_SHARING_HOST", "IS_STREAMLIT_CLOUD", "STREAMLIT_SERVER_BASE_URL_PATH")
    return any(os.environ.get(k) for k in cloud_env_keys) or not sys.platform.startswith("win")


DEFAULT_LOCAL_ROOT = r"C:\國考題庫"
DEFAULT_CLOUD_ROOT = str(Path(tempfile.gettempdir()) / "moex_exam_bank")
DEFAULT_ROOT = DEFAULT_CLOUD_ROOT if is_cloud_env() else DEFAULT_LOCAL_ROOT
SYSTEM_DIR_NAME = "_系統資料"
CLOUD_ROOT_PREFIX = "國考題庫"


def cloud_root_name(stamp: str | None = None) -> str:
    """雲端題庫根目錄名稱：國考題庫_YYYYMMDD_HHMM。"""
    return f"{CLOUD_ROOT_PREFIX}_{stamp or time.strftime('%Y%m%d_%H%M')}"


def new_cloud_root() -> str:
    """每次工作階段使用專屬暫存根目錄，避免不同使用者的題庫與下載紀錄互相混用。"""
    return str(Path(tempfile.gettempdir()) / cloud_root_name())


def zip_file_name(root: str, task_id: str) -> str:
    """雲端 ZIP 與根目錄同名（國考題庫_YYYYMMDD_HHMM.zip）；本機保留任務識別。"""
    if is_cloud_env():
        return f"{Path(root).name}.zip"
    return f"國考題庫_{task_id}_{time.strftime('%Y%m%d_%H%M%S')}.zip"


def pick_directory_dialog(initial_dir: str) -> str | None:
    """呼叫作業系統內建的資料夾選擇器；取消或失敗時回傳 None。"""
    start_path = Path(initial_dir) if initial_dir else Path(DEFAULT_ROOT)
    while not start_path.exists() and start_path.parent != start_path:
        start_path = start_path.parent
    init_str = str(start_path) if start_path.exists() else "C:\\"

    script = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "try:\n"
        "    import ctypes\n"
        "    ctypes.windll.shcore.SetProcessDpiAwareness(1)\n"
        "except Exception:\n"
        "    pass\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        "selected = filedialog.askdirectory(initialdir=sys.argv[1], title='選擇題庫下載目錄', mustexist=False)\n"
        "root.destroy()\n"
        "if selected:\n"
        "    sys.stdout.write(selected)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script, init_str],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
        chosen = proc.stdout.strip()
        if chosen:
            return str(Path(chosen))
    except Exception:
        return None
    return None


def _on_pick_directory() -> None:
    current = st.session_state.get("download_root", DEFAULT_ROOT)
    chosen = pick_directory_dialog(current)
    if chosen:
        st.session_state["download_root"] = chosen


def resolve_manifest_path(root: str | Path) -> Path:
    """解析題庫資料庫路徑，固定存放在題庫根目錄下的 _系統資料/manifest.db。

    若偵測到舊版存放在題庫根目錄的 manifest.db，自動無損搬移至 _系統資料。
    """
    root_path = Path(root).resolve()
    sys_dir = root_path / SYSTEM_DIR_NAME
    sys_dir.mkdir(parents=True, exist_ok=True)

    target_db = sys_dir / "manifest.db"
    legacy_db = root_path / "manifest.db"

    if legacy_db.exists() and not target_db.exists():
        import shutil

        shutil.move(str(legacy_db), str(target_db))
        for suffix in ("-wal", "-shm"):
            legacy_extra = root_path / f"manifest.db{suffix}"
            target_extra = sys_dir / f"manifest.db{suffix}"
            if legacy_extra.exists() and not target_extra.exists():
                shutil.move(str(legacy_extra), str(target_extra))
    elif not target_db.exists() and str(root) == DEFAULT_ROOT:
        old_default_db = Path("downloads") / SYSTEM_DIR_NAME / "manifest.db"
        if old_default_db.exists():
            import shutil

            shutil.copy2(str(old_default_db), str(target_db))

    return target_db


MAX_CHIPS = 40
MODE_LABELS = {
    "A（依科目）": PathMode.A,
    "B（依年度考試）": PathMode.B,
    "C（平鋪）": PathMode.C,
}


@st.cache_resource(show_spinner=False)
def shared_limiter(max_in_flight: int) -> RateLimiter:
    """全站共用限速器；Streamlit 重跑時沿用同一個實例。"""
    return RateLimiter(RateLimitConfig(max_in_flight=max_in_flight))


def build_controller(
    *,
    root: str,
    db_path: str,
    workers: int,
    mode: PathMode,
    template: str | None,
) -> AppController:
    """組裝 engine 服務；僅在設定變更或首次載入時呼叫。"""
    limiter = shared_limiter(workers)
    store = ManifestStore(db_path)
    query_service = QueryService(MoexClient(rate_limiter=limiter), store)
    downloader = Downloader(root=root, store=store, rate_limiter=limiter)
    task_service = TaskService(
        store=store,
        downloader=downloader,
        root=root,
        mode=mode,
        template=template,
        max_workers=workers,
    )
    return AppController(
        query_service=query_service, task_service=task_service, store=store, root=root
    )


EXPECTED_CONTROLLER_API_VERSION = 6
EXPECTED_TASK_SERVICE_API_VERSION = 2
RESTART_REQUIRED_MESSAGE = "程式已更新，請重新啟動應用程式後重新查詢及下載"


def _require_compatible_runtime(controller=None) -> None:
    """不重載背景工作使用的模組；版本不符時停止本次畫面執行。"""
    compatible = (
        getattr(AppController, "api_version", None) == EXPECTED_CONTROLLER_API_VERSION
        and getattr(TaskService, "api_version", None) == EXPECTED_TASK_SERVICE_API_VERSION
        and callable(getattr(TaskService, "export_results", None))
    )
    if controller is not None:
        service = getattr(controller, "_task_service", None)
        compatible = compatible and (
            getattr(controller, "api_version", None) == EXPECTED_CONTROLLER_API_VERSION
            and callable(getattr(controller, "export_paths", None))
            and getattr(service, "api_version", None) == EXPECTED_TASK_SERVICE_API_VERSION
            and callable(getattr(service, "export_results", None))
        )
    if not compatible:
        st.error(RESTART_REQUIRED_MESSAGE)
        st.stop()


SAMPLE_VARS = {
    "年度": "113",
    "年度標籤": "113年",
    "考試名稱": "高考三級考試",
    "考試簡稱": "高考三級",
    "類科": "資訊處理",
    "科目": "資料結構",
    "等別": "三等",
    "題型類別": "申論題",
    "題型": "申論試題",
}


def _ensure_controller(settings: dict) -> AppController:
    """相容 session 沿用實例；不相容時要求重啟，不替換背景服務。"""
    cached = st.session_state.get("controller")
    _require_compatible_runtime(cached)
    key = (
        settings["root"],
        settings["db_path"],
        settings["workers"],
        settings["mode"].value,
        settings["template"],
    )
    if cached is None or st.session_state.get("settings") != key:
        new_ctrl = build_controller(**settings)
        _require_compatible_runtime(new_ctrl)
        if cached is not None:
            new_ctrl.snapshot = cached.snapshot
            new_ctrl.state = cached.state
            new_ctrl.active_filters = cached.active_filters
            new_ctrl.quick_text = cached.quick_text
        st.session_state["controller"] = new_ctrl
        st.session_state["settings"] = key
    return st.session_state["controller"]



def build_tree_preview(root_dir: str, template: str) -> str:
    """根據模板與範例變數生成目錄結構樹狀文字。"""
    try:
        rendered = template
        for key, val in SAMPLE_VARS.items():
            rendered = rendered.replace(f"{{{key}}}", val)
        segments = [s for s in rendered.split("/") if s]
    except Exception:
        segments = ["範例_申論試題.pdf"]

    lines = [f"📁 {root_dir}/"]
    for i, seg in enumerate(segments):
        indent = "  " * (i + 1)
        is_last = (i == len(segments) - 1)
        icon = "📄" if is_last else "📁"
        lines.append(f"{indent}└── {icon} {seg}")
    return "\n".join(lines)


def _sidebar() -> tuple[dict, bool]:
    with st.sidebar:
        st.subheader("題庫歸檔")
        st.caption("設定一次，專心挑選需要的試題。")
        cloud_mode = is_cloud_env()
        if "download_root" not in st.session_state:
            st.session_state["download_root"] = new_cloud_root() if cloud_mode else DEFAULT_ROOT

        cached = st.session_state.get("controller")
        task_active = False
        if cached is not None:
            check_active = getattr(cached, "is_task_active", None)
            if callable(check_active):
                try:
                    task_active = bool(check_active())
                except Exception:
                    task_active = False
        if task_active:
            st.warning("下載進行中，歸檔設定暫時鎖定；完成或取消後即可變更。")

        if cloud_mode:
            root = st.session_state["download_root"]
            st.info(
                "☁️ 雲端模式：完成後可直接一鍵下載分類打包的 ZIP 題庫包；"
                f"ZIP 檔名為 `{Path(root).name}.zip`。"
            )
        else:
            root = st.text_input("下載根目錄", key="download_root", disabled=task_active)
            st.button(
                "📂 選擇目錄…",
                on_click=_on_pick_directory,
                width="stretch",
                key="pick-dir-btn",
                disabled=task_active,
            )
            st.caption("下載紀錄由程式自動保存，搬移題庫時請連同整個資料夾一起搬移。")
        mode_label = st.selectbox("歸檔模式", list(MODE_LABELS), disabled=task_active)
        mode = MODE_LABELS[mode_label]
        template = DEFAULT_TEMPLATES[mode.name]
        st.caption(
            "變更模式後按「開始下載」或「整理已下載檔案」，"
            "既有檔案會搬到新結構，不會重新下載。"
        )
        with st.container(border=True):
            st.caption("歸檔結構預覽")
            st.code(build_tree_preview(root, template), language="text")

        db_path = str(resolve_manifest_path(root))
        with st.expander("進階設定"):
            workers = st.slider(
                "下載併發數",
                MIN_WORKERS,
                MAX_WORKERS,
                DEFAULT_WORKERS,
                disabled=task_active,
            )
            auto_refresh = st.checkbox("下載時自動更新進度", value=True)
        st.caption("本工具只在本機執行；查詢與下載需要網路。")
    return (
        dict(
            root=root,
            db_path=db_path,
            workers=workers,
            mode=mode,
            template=None,
        ),
        auto_refresh,
    )


def _render_reconcile(controller: AppController) -> None:
    report = controller.reconcile()
    if report is None:
        return
    if report.adopted or report.interrupted or report.removed_parts:
        st.info(
            f"啟動核對：補記完成 {len(report.adopted)}、標記中斷 {len(report.interrupted)}、"
            f"清除暫存 {len(report.removed_parts)}"
        )
    if report.untracked_pdfs:
        st.warning(
            f"發現 {len(report.untracked_pdfs)} 個未追蹤的 PDF（不會自動採用或覆寫）："
            + "、".join(report.untracked_pdfs[:3])
        )
        with st.expander(f"未追蹤的 PDF（{len(report.untracked_pdfs)} 個）"):
            st.caption(
                "可能是舊歸檔模式留下的檔案；「清理重複舊檔」只會刪除內容與已追蹤檔案完全相同者。"
            )
            for relative in report.untracked_pdfs[:50]:
                st.text(relative)


def _format_spec_label(spec: QuerySpec) -> str:
    """將 QuerySpec 轉成白話好懂的摘要標籤。"""
    year_str = f"{spec.start_year}年" if spec.start_year == spec.end_year else f"{spec.start_year}–{spec.end_year}年"
    filters = []
    if spec.exam_name_keyword:
        filters.append(f"考試：{spec.exam_name_keyword}")
    if spec.category_keyword:
        filters.append(f"類科：{spec.category_keyword}")
    if spec.subject_keyword:
        filters.append(f"科目：{spec.subject_keyword}")
    filter_str = "、".join(filters) if filters else "整年度全部考科"
    return f"{year_str}｜{filter_str}"


def _form_spec(
    start: int,
    end: int,
    *,
    exam_name_keyword: str,
    category_keyword: str,
    subject_keyword: str,
    full_fetch: bool,
) -> QuerySpec:
    """把查詢表單的輸入轉成查詢條件；勾選「不套用關鍵字」時忽略三個關鍵字。"""
    if full_fetch:
        return QuerySpec(start, end)
    return QuerySpec(
        start,
        end,
        exam_name_keyword=exam_name_keyword.strip(),
        category_keyword=category_keyword.strip(),
        subject_keyword=subject_keyword.strip(),
    )


def _warn_unapplied_conditions(
    controller: AppController, form_spec: QuerySpec | None
) -> None:
    """表單條件與已查詢條件不同時明確提示，避免把舊結果誤當成新條件的結果。"""
    snapshot = controller.snapshot
    if snapshot is None:
        return
    if form_spec is None:
        st.warning("上方年度條件無效（起年不得大於迄年）；下方顯示的仍是上次查詢的結果。")
        return
    if form_spec.cache_payload() == snapshot.spec.cache_payload():
        return
    st.warning(
        f"上方條件尚未查詢（{_format_spec_label(form_spec)}）；"
        f"下方顯示的是上次查詢（{_format_spec_label(snapshot.spec)}）的結果，"
        "請按「查詢試題」重新取得。"
    )


def _render_query(controller: AppController) -> None:
    if "q_start" not in st.session_state:
        st.session_state["q_start"] = 113
    if "q_end" not in st.session_state:
        st.session_state["q_end"] = 113
    if "q_exam" not in st.session_state:
        st.session_state["q_exam"] = ""
    if "q_category" not in st.session_state:
        st.session_state["q_category"] = ""
    if "q_subject" not in st.session_state:
        st.session_state["q_subject"] = ""
    if "q_full" not in st.session_state:
        st.session_state["q_full"] = False

    recent_specs = controller.recent_query_specs()

    if recent_specs:
        history_options = ["（點選歷史條件自動帶入…）"] + [_format_spec_label(s) for s in recent_specs]
        ver = st.session_state.get("history_ver", 0)
        curr_key = f"history-selector-{ver}"
        selected_history = st.selectbox(
            "最近查詢(自動帶入下方篩選條件)",
            options=history_options,
            index=0,
            key=curr_key,
        )
        if selected_history != "（點選歷史條件自動帶入…）":
            idx = history_options.index(selected_history) - 1
            chosen_spec = recent_specs[idx]
            st.session_state["q_start"] = chosen_spec.start_year
            st.session_state["q_end"] = chosen_spec.end_year
            st.session_state["q_exam"] = chosen_spec.exam_name_keyword or ""
            st.session_state["q_category"] = chosen_spec.category_keyword or ""
            st.session_state["q_subject"] = chosen_spec.subject_keyword or ""
            st.session_state["q_full"] = not bool(chosen_spec.exam_name_keyword or chosen_spec.category_keyword or chosen_spec.subject_keyword)
            st.session_state.pop(curr_key, None)
            st.session_state["history_ver"] = ver + 1
            st.rerun()

    col1, col2 = st.columns(2)
    start = col1.number_input(
        "年度（起，民國）",
        min_value=81,
        max_value=115,
        key="q_start",
    )
    end = col2.number_input(
        "年度（迄，民國）",
        min_value=81,
        max_value=115,
        key="q_end",
    )
    col_exam, col_category, col_subject = st.columns(3)
    exam_keyword = col_exam.text_input(
        "考試名稱關鍵字（可留空）",
        key="q_exam",
    )
    category_keyword = col_category.text_input(
        "類科關鍵字（可留空）",
        key="q_category",
    )
    subject_keyword = col_subject.text_input(
        "科目關鍵字（可留空）",
        key="q_subject",
    )
    full_fetch = st.checkbox(
        "不套用關鍵字，抓整年度全部考科",
        key="q_full",
        help=(
            "勾選後會忽略上面三個關鍵字，直接向考選部下載整年度資料"
            "（113 年約 3,566 筆、6 MB、約 11 秒）。候選清單最完整，但比較慢；"
            "平常建議先用關鍵字縮小範圍。"
        ),
    )
    force = st.checkbox(
        "重新向官網下載（忽略今日快取）",
        key="q_force",
        help=(
            "相同條件在 24 小時內會直接用本機存好的結果（0 次請求）。"
            "只有想確認官網有沒有更新時才需要勾選，勾了會重新查詢（約 11 秒）。"
        ),
    )
    submitted = st.button("查詢試題", type="primary", width="stretch")

    form_spec = (
        _form_spec(
            int(start),
            int(end),
            exam_name_keyword=exam_keyword,
            category_keyword=category_keyword,
            subject_keyword=subject_keyword,
            full_fetch=full_fetch,
        )
        if start <= end
        else None
    )

    if submitted:
        errors = validate_query_inputs(int(start), int(end))
        if errors:
            st.error("；".join(errors))
        elif form_spec is not None:
            with st.spinner("正在取得考畢試題清單…"):
                controller.query(form_spec, force=force)
            _reset_candidate_widgets()
            if controller.last_error:
                st.error(controller.last_error)

    snapshot = controller.snapshot
    if snapshot is None:
        st.caption("尚未取得資料。按「查詢試題」後會顯示候選清單與下載統計。")
        return

    stamp = "（快取）" if snapshot.cached else "（本次查詢）"
    st.write(
        f"**{controller.query_scope_text()}** {stamp}｜"
        f"取得 {len(snapshot.records)} 筆紀錄、{len(snapshot.years)} 個年度"
    )
    _warn_unapplied_conditions(controller, form_spec)
    for warning in snapshot.warnings[:5]:
        st.warning(warning)


def _render_candidates(controller: AppController) -> None:
    if controller.snapshot is None or controller.state is None:
        return

    has_active_filters = any(controller.active_filters[dim] is not None for dim in DIMENSIONS)
    col_desc, col_reset = st.columns([3, 2])
    col_desc.caption("依考試、類科與科目交叉挑選；三欄會同步更新。")
    if has_active_filters:
        if col_reset.button("清除所有篩選（還原全部）", key="reset-all-filters"):
            controller.clear_all_filters()
            _reset_candidate_widgets()
            st.rerun()

    candidates = controller.faceted_candidates()
    table_cols = st.columns(3)

    for idx, dimension in enumerate(DIMENSIONS):
        label = DIMENSION_LABELS[dimension]
        items = getattr(candidates, dimension)
        active = controller.active_filters[dimension]

        with table_cols[idx]:
            header_text = f"**{label}**（可選 {len(items)} 項"
            if active is None:
                header_text += "，全部選取）"
            elif len(active) == 0:
                header_text += "，已選 0 項）"
            else:
                header_text += f"，已篩選 {len(active)} 項）"
            st.markdown(header_text)

            btn_col1, btn_col2 = st.columns(2)
            if active is not None:
                if btn_col1.button("恢復全部", key=f"clear-btn-{dimension}"):
                    controller.clear_dimension_filters(dimension)
                    _reset_candidate_widgets()
                    st.rerun()
                if len(active) > 0 and btn_col2.button("全不選", key=f"none-btn-{dimension}"):
                    controller.set_dimension_filters(dimension, set())
                    _reset_candidate_widgets()
                    st.rerun()
            else:
                if btn_col1.button("全不選", key=f"none-btn-{dimension}"):
                    controller.set_dimension_filters(dimension, set())
                    _reset_candidate_widgets()
                    st.rerun()

            table_data = [
                {
                    "選取": True if active is None else (item.value in active),
                    "名稱": item.value,
                    "份數": str(item.count),
                }
                for item in items
            ]

            editor_key = f"table-editor-{dimension}"
            edited = st.data_editor(
                table_data,
                key=editor_key,
                height=300,
                width="stretch",
                hide_index=True,
                column_config={
                    "選取": st.column_config.CheckboxColumn("選取", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", disabled=True),
                    "份數": st.column_config.TextColumn("份數", disabled=True, width="small"),
                },
            )

            current_selected = {row["名稱"] for row in edited if row.get("選取")}
            previous_selected = {
                row["名稱"] for row in table_data if row.get("選取")
            }
            if current_selected != previous_selected:
                if len(items) > 0 and len(current_selected) == len(items):
                    controller.clear_dimension_filters(dimension)
                else:
                    controller.set_dimension_filters(dimension, current_selected)
                _reset_candidate_widgets()
                st.rerun()



def _reset_candidate_widgets() -> None:
    """重新查詢後清除各欄位的 UI 狀態。"""
    for dimension in DIMENSIONS:
        st.session_state.pop(f"table-editor-{dimension}", None)


def _render_task(controller: AppController, auto_refresh: bool, root: str = DEFAULT_ROOT) -> None:
    if controller.snapshot is not None and controller.state is not None:
        stats = controller.stats()
        cols = st.columns(3)
        for col, label, value in zip(cols, ("已選考科", "試題附件", "答案附件"),
                                     (stats.records, stats.question_assets, stats.answer_assets)):
            col.metric(label, value, border=True)
        st.caption(f"儲存位置：{root}")
        with st.expander("檔案與傳輸明細"):
            st.write(f"預計歸檔 {stats.total_files} 個檔案；實際傳輸（去重）{stats.unique_urls} 份。")
            st.caption("內容相同的附件會共用同一個檔案，實際檔案數可能較少；題型與檔名以下載驗證後為準。")
    disabled = not controller.can_download()
    btn_label = "開始加載" if is_cloud_env() else "開始下載"
    if st.button(btn_label, type="primary", disabled=disabled, width="stretch"):
        state = controller.start_download()
        if controller.last_error:
            st.error(controller.last_error)
        elif state is not None:
            st.success(f"已建立任務 {state.task_id}")
            st.rerun()
    if disabled:
        if controller.snapshot is None:
            st.caption("💡 請先點擊上方的「查詢試題」取得試題清單。")
        elif not controller.snapshot.is_complete:
            st.caption("💡 試題清單未完整取得，請重新點擊「查詢試題」。")
        else:
            st.caption("💡 目前未選取任何考科，請在上方勾選欲下載的科目。")

    with st.expander("🔁 目錄結構整理", expanded=False):
        st.caption(
            "變更歸檔模式後，已下載且完好的檔案會搬到新結構，不會重新下載；"
            "舊的空資料夾會自動清除。共用檔案由擁有者搬移並保留一份。"
        )
        task_active = controller.is_task_active()
        action_cols = st.columns(2)
        if action_cols[0].button(
            "🔁 依目前模式整理已下載檔案",
            disabled=task_active,
            width="stretch",
        ):
            with st.spinner("正在搬移既有檔案…"):
                report = controller.reorganize()
            if controller.last_error:
                st.error(controller.last_error)
            elif report is not None:
                st.success(
                    f"整理完成：搬移 {report.moved}、未變更 {report.unchanged}、"
                    f"共用保留 {report.shared}、失敗 {report.failed}"
                )
                for asset_id, message in report.errors[:20]:
                    st.text(f"{asset_id}｜{message}")
        confirm_cleanup = action_cols[1].checkbox(
            "我確認清理內容重複的舊 PDF", key="cleanup-confirm"
        )
        if action_cols[1].button(
            "🧹 清理重複舊檔",
            disabled=task_active or not confirm_cleanup,
            width="stretch",
        ):
            with st.spinner("正在比對並清理重複檔案…"):
                report = controller.cleanup_duplicates()
            if controller.last_error:
                st.error(controller.last_error)
            elif report is not None:
                st.success(f"已刪除 {len(report.removed)} 個重複舊檔")
                if report.kept:
                    st.caption(
                        f"保留 {len(report.kept)} 個內容不同或無法確認的未追蹤 PDF"
                    )

    state = controller.progress()
    if state is None:
        return

    total = max(1, state.total)
    st.progress(min(1.0, state.done / total), text=f"已處理 {state.done}／{state.total} 份")
    message = f"{state.status.value}｜{state.message}"
    if state.failed:
        st.warning(message)
    elif state.status is TaskStatus.COMPLETED:
        st.success(message)
    else:
        st.info(message)
    cols = st.columns(3)
    if cols[0].button("取消下載", disabled=not state.status.is_active):
        controller.cancel()
        st.rerun()
    if cols[1].button(
        "重試未完成",
        disabled=state.status.is_active or state.status is TaskStatus.COMPLETED,
    ):
        controller.retry()
        st.rerun()
    cols[2].caption(
        f"新下載 {state.completed - state.deduped}／共用 {state.deduped}"
        f"／略過 {max(0, state.skipped - state.relocated)}／搬移 {state.relocated}"
        f"／失敗 {state.failed}／取消 {state.cancelled}"
    )

    # 任務完成或部分完成時，提供整包打包下載（雲端必備，本機亦可方便打包）
    actual_root = str(controller._root) if hasattr(controller, "_root") else root
    if state.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL):
        try:
            zip_bytes, file_count = _create_zip_from_paths(actual_root, controller.export_paths())
        except (OSError, ValueError, TaskServiceError) as exc:
            st.error(f"無法匯出本次任務題庫包：{exc}")
            zip_bytes, file_count = None, 0
        if zip_bytes:
            st.caption(f"本次任務打包 {file_count} 個檔案；包含已存在及共用的檔案。")
            st.download_button(
                label="📦 下載本次任務題庫包 (ZIP 壓縮檔)",
                data=zip_bytes,
                file_name=zip_file_name(actual_root, state.task_id),
                mime="application/zip",
                type="primary",
                width="stretch",
                key=f"download-zip-{state.task_id}-{state.attempt}",
            )

    if state.failures:
        # 建立 asset_id -> (record, asset) 快速查找表
        asset_map = {}
        if controller.snapshot is not None:
            for rec in controller.snapshot.records:
                for a in rec.assets:
                    asset_map[a.asset_id] = (rec, a)

        with st.expander(f"⚠️ 部分附件加載失敗（{len(state.failures)} 項，點此展開官方直連下載）"):
            st.caption("考選部伺服器偶有連線逾時，您可以點擊「重試未完成」，或直接點選下方官方原始連結手動開啟：")
            for asset_id, code, message in state.failures[:30]:
                info = asset_map.get(asset_id)
                if info:
                    rec, asset = info
                    role_text = "試題" if asset.role is AssetRole.QUESTION else "答案"
                    st.markdown(
                        f"- **{rec.year}年 {rec.exam_name}｜{rec.category}｜{rec.subject}（{role_text}）**  \n"
                        f"  狀態：`{code}` - {message}  \n"
                        f"  👉 [🔗 點此以瀏覽器開啟官方原始{role_text}檔]({asset.url})"
                    )
                else:
                    st.text(f"{asset_id}｜{code}｜{message}")
    if state.unsupported:
        with st.expander(f"不支援的附件（{len(state.unsupported)}）"):
            for asset_id, reason in state.unsupported[:20]:
                st.text(f"{asset_id}｜{reason}")
    with st.expander("任務詳細資訊與事件紀錄"):
        st.caption(f"任務 {state.task_id}｜第 {state.attempt} 次嘗試")
        for event in state.events[-30:]:
            st.text(f"{event.at}｜{event.kind}｜{event.message}")

    if state.status.is_active and auto_refresh:
        time.sleep(1.0)
        st.rerun()



def _sanitize_zip_rel_path(rel_path: str, max_bytes: int = 180) -> str:
    """確保 ZIP 內相對路徑之 UTF-8 位元組不超過 Windows zipfldr.dll 之上限 (255 bytes)。
    
    保留前後結構，若過長則在中間段落或檔名主體截斷並加上雜湊，保證 Windows 檔案總管能正常開啟且不碰撞。
    """
    if len(rel_path.encode("utf-8")) <= max_bytes:
        return rel_path

    parts = rel_path.split("/")
    new_parts: list[str] = []
    for i, part in enumerate(parts):
        is_filename = (i == len(parts) - 1)
        suffix = ".pdf" if (is_filename and part.lower().endswith(".pdf")) else ""
        stem = part[:-4] if suffix else part
        
        # 尋找是否包含角色標記（如 _試題、_測驗試題、_申論試題、_測驗答案等）
        role_marker = ""
        if is_filename:
            for marker in ("_測驗試題", "_申論試題", "_混合試題", "_測驗答案", "_試題", "_答案"):
                if marker in stem:
                    role_marker = marker
                    stem = stem[:stem.rfind(marker)]
                    break

        # 若單段 UTF-8 超過 80 位元組，進行縮短
        if len((stem + role_marker).encode("utf-8")) > 80:
            h = hashlib.sha256((stem + role_marker).encode("utf-8")).hexdigest()[:6]
            target_stem = 80 - len(role_marker.encode("utf-8")) - 7
            while len(stem.encode("utf-8")) > max(10, target_stem):
                stem = stem[:-1]
            stem = f"{stem}_{h}"
        new_parts.append(stem + role_marker + suffix)

    res = "/".join(new_parts)
    # 若整體總長度依然超過 max_bytes，進一步縮減檔名主體
    if len(res.encode("utf-8")) > max_bytes and len(new_parts[-1]) > 10:
        filename = new_parts[-1]
        suffix = ".pdf" if filename.lower().endswith(".pdf") else ""
        stem = filename[:-4] if suffix else filename
        h = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:6]
        
        role_marker = ""
        for marker in ("_測驗試題", "_申論試題", "_混合試題", "_測驗答案", "_試題", "_答案"):
            if marker in stem:
                role_marker = marker
                stem = stem[:stem.rfind(marker)]
                break
        
        dir_bytes = len("/".join(new_parts[:-1]).encode("utf-8")) if len(new_parts) > 1 else 0
        target_stem_len = max_bytes - dir_bytes - 1 - len(role_marker.encode("utf-8")) - len(suffix.encode("utf-8")) - 8
        if target_stem_len < 10:
            target_stem_len = 10
            
        while len(stem.encode("utf-8")) > target_stem_len:
            stem = stem[:-1]
            
        new_parts[-1] = f"{stem}_{h}{role_marker}{suffix}"
        res = "/".join(new_parts)
        
    return res


def _create_zip_from_paths(root: str, relative_paths: tuple[str, ...]) -> tuple[bytes | None, int]:
    """只打包明確指定的任務成果；任何無效路徑都停止整包匯出。"""
    root_path = Path(root).resolve()
    pdf_files: set[Path] = set()
    for relative in relative_paths:
        rel = Path(relative)
        p = (root_path / rel).resolve()
        if rel.is_absolute() or not p.is_relative_to(root_path):
            raise ValueError(f"檔案路徑超出下載根目錄：{relative}")
        if SYSTEM_DIR_NAME in rel.parts or ".parts" in rel.parts or p.suffix.lower() != ".pdf":
            raise ValueError(f"不是可匯出的題庫 PDF：{relative}")
        if not p.is_file() or p.stat().st_size == 0:
            raise ValueError(f"檔案遺失或為空，請重新下載：{relative}")
        pdf_files.add(p)
    if not pdf_files:
        return None, 0

    buf = io.BytesIO()
    created_dirs: set[str] = set()
    archive_names: set[str] = set()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(pdf_files):
            raw_rel_path = p.relative_to(root_path).as_posix()
            rel_path = _sanitize_zip_rel_path(raw_rel_path, max_bytes=180)
            if rel_path.casefold() in archive_names:
                raise ValueError(f"ZIP 檔名縮短後衝突：{raw_rel_path}")
            archive_names.add(rel_path.casefold())
            file_bytes = p.read_bytes()
            mtime = time.localtime(p.stat().st_mtime)[:6]

            # 1. 自動為所有各層父資料夾建立標準目錄條目（Directory Entry）
            # Windows Explorer (zipfldr.dll) 嚴格依賴以 '/' 結尾且 external_attr=0x10 的目錄項來構建資料夾樹
            parts = rel_path.split("/")
            for i in range(1, len(parts)):
                dir_path = "/".join(parts[:i]) + "/"
                if dir_path not in created_dirs:
                    created_dirs.add(dir_path)
                    dir_info = zipfile.ZipInfo(filename=dir_path, date_time=mtime)
                    dir_info.create_system = 0  # 0 = Windows FAT，保證與 Windows 檔案總管 100% 相容
                    dir_info.external_attr = 0x10  # FILE_ATTRIBUTE_DIRECTORY
                    dir_info.flag_bits |= 0x800  # UTF-8 檔名旗標
                    zf.writestr(dir_info, b"")

            # 2. 寫入檔案條目
            # 明確設定 create_system=0 (Windows FAT), external_attr=0x20 (Archive), flag_bits |= 0x800 (UTF-8)
            zinfo = zipfile.ZipInfo(filename=rel_path, date_time=mtime)
            zinfo.create_system = 0
            zinfo.external_attr = 0x20  # FILE_ATTRIBUTE_ARCHIVE
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.flag_bits |= 0x800
            zf.writestr(zinfo, file_bytes)

    data = buf.getvalue()
    return data, len(pdf_files)

def _section_heading(number: str, title: str, description: str) -> None:
    """以固定文案顯示一致的流程標題。"""
    st.markdown(
        f'<div class="section-heading"><span>{number}</span><div>'
        f'<h2>{title}</h2><p>{description}</p></div></div>',
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="國考刷題神器", page_icon="📚", layout="wide")
    css = Path(__file__).with_name("styles.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    st.markdown(
        '<div class="hero"><div class="eyebrow">歷屆試題 · 你的備考起點</div>'
        '<h1>國考刷題神器</h1><p>找到歷屆試題，整理成你的題庫。</p>'
        '<div class="workflow"><span>01 查詢試題</span><b>→</b>'
        '<span>02 篩選考科</span><b>→</b><span>03 下載歸檔</span></div></div>',
        unsafe_allow_html=True,
    )
    page_settings, auto_refresh = _sidebar()
    controller = _ensure_controller(page_settings)
    _render_reconcile(controller)
    with st.container(key="query-section"):
        _section_heading("01", "查詢試題", "選擇年度，使用關鍵字縮小搜尋範圍。")
        _render_query(controller)
    if controller.snapshot is not None:
        with st.container(border=True, key="candidate-section"):
            _section_heading("02", "篩選考科", "從查詢結果挑選這次要收進題庫的考科。")
            _render_candidates(controller)
        with st.container(border=True, key="download-section"):
            _section_heading("03", "下載與歸檔", "確認選取內容，再將試題整理到本機。")
            _render_task(controller, auto_refresh, page_settings["root"])
    else:
        st.markdown(
            '<div class="empty-state"><strong>你的下一份練習，從這裡開始</strong>'
            '<p>先查詢試題，再挑選考科；下載時會依左側設定自動歸檔。</p></div>',
            unsafe_allow_html=True,
        )


if __name__ == "__main__":  # pragma: no cover - 由 streamlit 執行
    main()
