"""本機 Streamlit 介面：查詢、候選、路徑預覽、下載進度、取消與重試。

畫面只負責呈現與轉送操作；查詢、篩選、命名與下載都在 app/engine。
本模組不在匯入時啟動任何服務，也不直接操作背景執行緒。
"""

from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from app.crawler.moex_client import MoexClient
from app.crawler.rate_limiter import RateLimitConfig, RateLimiter
from app.engine.downloader import Downloader
from app.engine.path_builder import DEFAULT_TEMPLATES, PathMode
from app.engine.query_service import QueryService
from app.engine.task_service import DEFAULT_WORKERS, MAX_WORKERS, MIN_WORKERS, TaskService
from app.models import ParseStatus, QuerySpec, TaskStatus
from app.storage.manifest import ManifestStore
from app.ui.controller import CONTROLLER_API_VERSION, DIMENSION_LABELS, DIMENSIONS, AppController

DEFAULT_ROOT = "downloads"
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


def _ensure_controller(settings: dict) -> AppController:
    """設定變更時重建控制器；重跑時沿用既有狀態，避免重複查詢或重複下載。

    另外檢查快取的實例是否屬於「目前的」AppController 類別：開發時熱重載會換掉
    類別物件，舊實例會缺少新方法，必須重建才不會出現 AttributeError。
    """
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
        st.session_state["controller"] = build_controller(**settings)
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
        st.subheader("下載設定")
        root = st.text_input("下載根目錄", DEFAULT_ROOT)
        db_path = st.text_input("資料庫檔案", str(Path(root) / "manifest.db"))
        workers = st.slider("下載併發數", MIN_WORKERS, MAX_WORKERS, DEFAULT_WORKERS)
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

                st.caption("即時目錄樹狀圖預覽：")
                st.code(build_tree_preview(root, template), language="text")
        else:
            with st.container(border=True):
                template = DEFAULT_TEMPLATES[mode.name]
                st.caption("即時目錄樹狀圖預覽：")
                st.code(build_tree_preview(root, template), language="text")

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
        submitted = st.form_submit_button("開始過濾", type="primary")

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
        st.caption("尚未取得資料。按「開始過濾」後會顯示候選清單與下載統計。")
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

    has_active_filters = any(controller.active_filters[dim] for dim in DIMENSIONS)
    col_desc, col_reset = st.columns([4, 1])
    if has_active_filters:
        if col_reset.button("清除所有篩選（還原全部）", key="reset-all-filters"):
            controller.clear_all_filters()
            st.rerun()

    candidates = controller.faceted_candidates()
    table_cols = st.columns(3)

    for idx, dimension in enumerate(DIMENSIONS):
        label = DIMENSION_LABELS[dimension]
        items = getattr(candidates, dimension)
        active = controller.active_filters[dimension]

        with table_cols[idx]:
            header_text = f"**{label}**（可選 {len(items)} 項"
            if active and active != {"__NONE__"}:
                header_text += f"，已篩選 {len(active)} 項）"
            else:
                header_text += "，未限制）"
            st.markdown(header_text)

            btn_col1, btn_col2 = st.columns(2)
            if active:
                if btn_col1.button("恢復全部", key=f"clear-btn-{dimension}"):
                    controller.clear_dimension_filters(dimension)
                    st.rerun()
            else:
                if btn_col1.button("全不選", key=f"none-btn-{dimension}"):
                    controller.set_dimension_filters(dimension, {"__NONE__"})
                    st.rerun()

            is_none_selected = active == {"__NONE__"}
            table_data = [
                {
                    "選取": False if is_none_selected else (item.value in active if active else True),
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
                if len(current_selected) == len(items) or len(current_selected) == 0:
                    controller.clear_dimension_filters(dimension)
                else:
                    controller.set_dimension_filters(dimension, current_selected)
                st.rerun()

    stats = controller.stats()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("考科數", stats.records)
    col2.metric("試題附件", stats.question_assets)
    col3.metric("答案附件", stats.answer_assets)
    col4.metric("實際傳輸（去重）", stats.unique_urls)
    st.caption(
        f"預計歸檔 {stats.total_files} 個檔案（內容相同的附件會共用同一個檔案，實際檔案數可能較少）"
    )


def _reset_candidate_widgets() -> None:
    """重新查詢後清除各欄位的 UI 狀態。"""
    for dimension in DIMENSIONS:
        st.session_state.pop(f"table-editor-{dimension}", None)


def _render_task(controller: AppController, auto_refresh: bool) -> None:
    st.subheader("下載")
    disabled = not controller.can_download()
    if st.button("開始下載", type="primary", disabled=disabled):
        state = controller.start_download()
        if controller.last_error:
            st.error(controller.last_error)
        elif state is not None:
            st.success(f"已建立任務 {state.task_id}")
            st.rerun()
    if disabled:
        if controller.snapshot is None:
            st.caption("💡 請先點擊上方的「開始過濾」取得試題清單。")
        elif not controller.snapshot.is_complete:
            st.caption("💡 試題清單未完整取得，請重新點擊「開始過濾」。")
        else:
            st.caption("💡 目前未選取任何考科，請在上方勾選欲下載的科目。")

    state = controller.progress()
    if state is None:
        return

    total = max(1, state.total)
    st.progress(min(1.0, state.done / total))
    st.write(
        f"任務 {state.task_id}｜{state.status.value}｜{state.message}"
        f"｜第 {state.attempt} 次嘗試"
    )
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

    if state.failures:
        with st.expander(f"失敗附件（{len(state.failures)}）"):
            for asset_id, code, message in state.failures[:20]:
                st.text(f"{asset_id}｜{code}｜{message}")
    if state.unsupported:
        with st.expander(f"不支援的附件（{len(state.unsupported)}）"):
            for asset_id, reason in state.unsupported[:20]:
                st.text(f"{asset_id}｜{reason}")
    with st.expander("事件紀錄"):
        for event in state.events[-30:]:
            st.text(f"{event.at}｜{event.kind}｜{event.message}")

    if state.status.is_active and auto_refresh:
        time.sleep(1.0)
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="國考刷題神器", layout="wide")
    st.markdown(
        "<style>[data-testid='stDeployButton'] { display: none !important; }</style>",
        unsafe_allow_html=True,
    )
    st.title("國考刷題神器")
    st.caption("考選部考畢試題查詢、篩選與歸檔（本機執行，預設僅監聽 localhost）")

    page_settings, auto_refresh = _sidebar()
    controller = _ensure_controller(page_settings)
    _render_reconcile(controller)
    _render_query(controller)
    _render_candidates(controller)
    _render_task(controller, auto_refresh)


if __name__ == "__main__":  # pragma: no cover - 由 streamlit 執行
    main()
