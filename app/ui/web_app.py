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
from app.engine.path_builder import PathMode
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


def _sidebar() -> tuple[dict, bool]:
    with st.sidebar:
        st.subheader("下載設定")
        root = st.text_input("下載根目錄", DEFAULT_ROOT)
        db_path = st.text_input("資料庫檔案", str(Path(root) / "manifest.db"))
        workers = st.slider("下載併發數", MIN_WORKERS, MAX_WORKERS, DEFAULT_WORKERS)
        mode_label = st.selectbox("歸檔模式", list(MODE_LABELS))
        template = ""
        if MODE_LABELS[mode_label] is PathMode.D:
            template = st.text_input(
                "自訂模板（須以 {題型}.pdf 結尾）",
                "{年度}/{考試名稱}_{類科}/{科目}_{題型}.pdf",
            )
        auto_refresh = st.checkbox("下載時自動更新進度", value=True)
        st.caption("本工具只在本機執行；查詢與下載需要網路。")
    return (
        dict(
            root=root,
            db_path=db_path,
            workers=workers,
            mode=MODE_LABELS[mode_label],
            template=template or None,
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


def _render_query(controller: AppController) -> None:
    with st.form("query-form"):
        col1, col2 = st.columns(2)
        start = col1.number_input("年度（起，民國）", min_value=81, max_value=115, value=113)
        end = col2.number_input("年度（迄，民國）", min_value=81, max_value=115, value=113)
        col_exam, col_category, col_subject = st.columns(3)
        exam_keyword = col_exam.text_input("考試名稱關鍵字（可留空）", "")
        category_keyword = col_category.text_input("類科關鍵字（可留空）", "")
        subject_keyword = col_subject.text_input("科目關鍵字（可留空）", "")
        full_fetch = st.checkbox(
            "不套用關鍵字，抓整年度全部考科",
            value=False,
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
        f"取得時間：{snapshot.acquired_at}{stamp}｜狀態：**{snapshot.status.value}**"
        f"｜紀錄 {len(snapshot.records)} 筆"
    )
    st.caption(f"查詢範圍：{controller.query_scope_text()}")
    if not snapshot.is_complete:
        st.error(f"{snapshot.message}；已停用批量下載，請重試或縮小條件。")
    elif snapshot.status is ParseStatus.OK_WITH_DIFF:
        st.warning(f"{snapshot.message}；下載仍可使用，但請確認統計差異是否可接受。")
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
        st.caption("需先取得完整快照，且三個候選清單都至少選取一項。")

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
