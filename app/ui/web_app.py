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
from app.models import ParseStatus, QuerySpec, TaskStatus
from app.storage.manifest import ManifestStore
from app.ui.controller import CONTROLLER_API_VERSION, DIMENSION_LABELS, DIMENSIONS, AppController

def is_cloud_env() -> bool:
    """判斷是否運行於 Streamlit Cloud 或 Linux 無原生視窗的雲端環境。"""
    cloud_env_keys = ("STREAMLIT_SHARING_HOST", "IS_STREAMLIT_CLOUD", "STREAMLIT_SERVER_BASE_URL_PATH")
    return any(os.environ.get(k) for k in cloud_env_keys) or not sys.platform.startswith("win")


DEFAULT_LOCAL_ROOT = r"C:\國考題庫"
DEFAULT_CLOUD_ROOT = str(Path(tempfile.gettempdir()) / "moex_exam_bank")
DEFAULT_ROOT = DEFAULT_CLOUD_ROOT if is_cloud_env() else DEFAULT_LOCAL_ROOT
SYSTEM_DIR_NAME = "_系統資料"


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
    "D（自訂模板）": PathMode.D,
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


EXPECTED_CONTROLLER_API_VERSION = 4


def _reload_submodules_if_needed() -> None:
    """當子模組在開發期間更新但 Streamlit 僅重跑 web_app.py 時，強制重新載入子模組。"""
    global CONTROLLER_API_VERSION, AppController
    if getattr(AppController, "api_version", None) != EXPECTED_CONTROLLER_API_VERSION:
        import importlib
        import app.engine.filter as filter_mod
        import app.ui.controller as ctrl_mod

        importlib.reload(filter_mod)
        importlib.reload(ctrl_mod)
        CONTROLLER_API_VERSION = ctrl_mod.CONTROLLER_API_VERSION
        AppController = ctrl_mod.AppController


def _ensure_controller(settings: dict) -> AppController:
    """設定變更時重建控制器；重跑時沿用既有狀態，避免重複查詢或重複下載。

    另外檢查快取的實例是否屬於「目前的」AppController 類別：開發時熱重載會換掉
    類別物件，舊實例會缺少新方法，必須重建才不會出現 AttributeError。
    """
    _reload_submodules_if_needed()
    key = (
        settings["root"],
        settings["db_path"],
        settings["workers"],
        settings["mode"].value,
        settings["template"],
    )
    cached = st.session_state.get("controller")
    # 熱重載會換掉類別物件，但 session 仍握著舊實例；用介面版本（或「缺方法」）
    # 只要對不上就重建，不能只看型別，因為型別可能仍是同一個舊類別。
    # 若載入中的類別本身也還沒有版本標記（reload 尚未完成），就維持現況以免每次
    # 重跑都重建控制器、丢掉選取。
    class_ready = getattr(AppController, "api_version", None) == CONTROLLER_API_VERSION
    stale = class_ready and cached is not None and (
        not isinstance(cached, AppController)
        or getattr(cached, "api_version", None) != CONTROLLER_API_VERSION
    )
    if stale or st.session_state.get("settings") != key:
        new_ctrl = build_controller(**settings)
        prev_snapshot = getattr(cached, "snapshot", None) if stale else None
        if prev_snapshot is not None:
            from app.engine.filter import FilterState, build_candidates

            new_ctrl.snapshot = prev_snapshot
            new_ctrl.state = build_candidates(
                prev_snapshot.records, FilterState()
            ).initial_state(
                year_start=prev_snapshot.spec.start_year,
                year_end=prev_snapshot.spec.end_year,
            )
        st.session_state["controller"] = new_ctrl
        st.session_state["settings"] = key
        _reset_candidate_widgets()
    return st.session_state["controller"]



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
        if "download_root" not in st.session_state:
            st.session_state["download_root"] = DEFAULT_ROOT
        
        cloud_mode = is_cloud_env()
        if cloud_mode:
            root = st.session_state["download_root"]
            st.info("☁️ 雲端模式：完成後可直接一鍵下載分類打包的 ZIP 題庫包。")
        else:
            root = st.text_input("下載根目錄", key="download_root")
            st.button(
                "📂 選擇目錄…",
                on_click=_on_pick_directory,
                width="stretch",
                key="pick-dir-btn",
            )
            st.caption("下載紀錄由程式自動保存，搬移題庫時請連同整個資料夾一起搬移。")
        mode_label = st.selectbox("歸檔模式", list(MODE_LABELS))
        mode = MODE_LABELS[mode_label]
        template = ""

        if mode is PathMode.D:
            with st.container(border=True):
                st.markdown("##### 📁 自訂目錄結構")
                depth = st.radio(
                    "目錄層次",
                    ["雙層資料夾", "單層資料夾", "平鋪（無資料夾）"],
                    horizontal=True,
                    key="custom-depth",
                )

                folder_options = {
                    "年度 (例: 113)": "{年度}",
                    "科目 (例: 資料結構)": "{科目}",
                    "考試名稱 (例: 高考三級)": "{考試簡稱}",
                    "類科 (例: 資訊處理)": "{類科}",
                }
                subfolder_options = {
                    "考試＋類科 (例: 高考三級_資訊處理)": "{考試簡稱}_{類科}",
                    "類科 (例: 資訊處理)": "{類科}",
                    "科目 (例: 資料結構)": "{科目}",
                    "年度 (例: 113)": "{年度}",
                }

                folders = []
                if depth in ("雙層資料夾", "單層資料夾"):
                    c1 = st.selectbox("第一層分類", list(folder_options.keys()), index=0, key="c-l1")
                    folders.append(folder_options[c1])

                if depth == "雙層資料夾":
                    c2 = st.selectbox("第二層分類", list(subfolder_options.keys()), index=0, key="c-l2")
                    folders.append(subfolder_options[c2])

                fn_items = st.multiselect(
                    "檔名包含欄位（依順序串接）",
                    ["年度", "考試簡稱", "類科", "科目"],
                    default=["科目"],
                    key="c-fn",
                )
                fn_tokens = [f"{{{item}}}" for item in (fn_items or [])]
                fn_prefix = "_".join(fn_tokens)
                filename = f"{fn_prefix}_{{題型}}.pdf" if fn_prefix else "{題型}.pdf"
                template = "/".join(folders + [filename])

                st.caption("歸檔結構預覽")
                st.code(build_tree_preview(root, template), language="text")
        else:
            with st.container(border=True):
                template = DEFAULT_TEMPLATES[mode.name]
                st.caption("歸檔結構預覽")
                st.code(build_tree_preview(root, template), language="text")

        db_path = str(resolve_manifest_path(root))
        with st.expander("進階設定"):
            workers = st.slider("下載併發數", MIN_WORKERS, MAX_WORKERS, DEFAULT_WORKERS)
            auto_refresh = st.checkbox("下載時自動更新進度", value=True)
        st.caption("本工具只在本機執行；查詢與下載需要網路。")
    return (
        dict(
            root=root,
            db_path=db_path,
            workers=workers,
            mode=mode,
            template=template if mode is PathMode.D else None,
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


def _render_query(controller: AppController) -> None:
    recent_specs = controller.recent_query_specs()

    if recent_specs:
        history_options = ["（點選歷史條件自動帶入…）"] + [_format_spec_label(s) for s in recent_specs]
        selected_history = st.selectbox(
            "最近查詢(自動帶入下方篩選條件)",
            options=history_options,
            index=0,
            key="history-selector",
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

    with st.form("query-form"):
        col1, col2 = st.columns(2)
        start = col1.number_input(
            "年度（起，民國）",
            min_value=81,
            max_value=115,
            value=st.session_state.get("q_start", 113),
            key="q_start",
        )
        end = col2.number_input(
            "年度（迄，民國）",
            min_value=81,
            max_value=115,
            value=st.session_state.get("q_end", 113),
            key="q_end",
        )
        col_exam, col_category, col_subject = st.columns(3)
        exam_keyword = col_exam.text_input(
            "考試名稱關鍵字（可留空）",
            value=st.session_state.get("q_exam", ""),
            key="q_exam",
        )
        category_keyword = col_category.text_input(
            "類科關鍵字（可留空）",
            value=st.session_state.get("q_category", ""),
            key="q_category",
        )
        subject_keyword = col_subject.text_input(
            "科目關鍵字（可留空）",
            value=st.session_state.get("q_subject", ""),
            key="q_subject",
        )
        full_fetch = st.checkbox(
            "不套用關鍵字，抓整年度全部考科",
            value=st.session_state.get("q_full", False),
            key="q_full",
            help=(
                "勾選後會忽略上面三個關鍵字，直接向考選部下載整年度資料"
                "（113 年約 3,566 筆、6 MB、約 11 秒）。候選清單最完整，但比較慢；"
                "平常建議先用關鍵字縮小範圍。"
            ),
        )
        force = st.checkbox(
            "重新向官網下載（忽略今日快取）",
            value=False,
            help=(
                "相同條件在 24 小時內會直接用本機存好的結果（0 次請求）。"
                "只有想確認官網有沒有更新時才需要勾選，勾了會重新查詢（約 11 秒）。"
            ),
        )
        submitted = st.form_submit_button("查詢試題", type="primary", width="stretch")

    if submitted:
        spec = (
            QuerySpec(int(start), int(end))
            if full_fetch
            else QuerySpec(
                int(start),
                int(end),
                exam_name_keyword=exam_keyword.strip(),
                category_keyword=category_keyword.strip(),
                subject_keyword=subject_keyword.strip(),
            )
        )
        with st.spinner("正在取得考畢試題清單…"):
            controller.query(spec, force=force)
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
        f"新下載 {state.completed - state.deduped}／共用 {state.deduped}／略過 {state.skipped}"
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
                file_name=f"國考題庫_{state.task_id}_{time.strftime('%Y%m%d_%H%M%S')}.zip",
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
