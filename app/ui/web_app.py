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
from app.ui.controller import DIMENSION_LABELS, AppController

DEFAULT_ROOT = "downloads"
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
    """設定變更時重建控制器；重跑時沿用既有狀態，避免重複查詢或重複下載。"""
    key = (
        settings["root"],
        settings["db_path"],
        settings["workers"],
        settings["mode"].value,
        settings["template"],
    )
    if st.session_state.get("settings") != key:
        st.session_state["controller"] = build_controller(**settings)
        st.session_state["settings"] = key
        # 設定變更後舊的預覽與選取已不適用
        st.session_state.pop("preview", None)
        for dimension in ("exams", "categories", "subjects"):
            st.session_state.pop(f"sel-{dimension}", None)
            st.session_state.pop(f"quick-{dimension}", None)
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
        exam_keyword = st.text_input("考試名稱關鍵字（可留空）", "")
        force = st.checkbox("強制重新查詢（忽略快取）", value=False)
        submitted = st.form_submit_button("查詢", type="primary")

    if submitted:
        spec = QuerySpec(
            int(start), int(end), exam_name_keyword=exam_keyword.strip()
        )
        with st.spinner("查詢中…"):
            controller.query(spec, force=force)
        if controller.last_error:
            st.error(controller.last_error)

    snapshot = controller.snapshot
    if snapshot is None:
        st.caption("尚未查詢。查詢後會顯示候選清單與下載統計。")
        return

    stamp = "（快取）" if snapshot.cached else "（本次查詢）"
    st.write(
        f"取得時間：{snapshot.acquired_at}{stamp}｜狀態：**{snapshot.status.value}**"
        f"｜紀錄 {len(snapshot.records)} 筆"
    )
    if not snapshot.is_complete:
        st.error(f"{snapshot.message}；已停用批量下載，請重試或縮小條件。")
    elif snapshot.status is ParseStatus.OK_WITH_DIFF:
        st.warning(f"{snapshot.message}；下載仍可使用，但請確認統計差異是否可接受。")
    for warning in snapshot.warnings[:5]:
        st.warning(warning)


def _render_candidates(controller: AppController) -> None:
    if controller.snapshot is None or controller.state is None:
        return

    st.subheader("候選與選取")
    candidates = controller.candidates()
    for dimension in ("exams", "categories", "subjects"):
        label = DIMENSION_LABELS[dimension]
        all_items = getattr(candidates, dimension)
        visible = controller.visible(dimension)
        selected = getattr(controller.state, dimension)
        with st.expander(f"{label}：已選 {len(selected)}／{len(all_items)}", expanded=False):
            st.text_input(
                "快速過濾（只影響可見項目）",
                key=f"quick-{dimension}",
                value=controller.quick_text[dimension],
                on_change=_on_quick_filter,
                args=(controller, dimension),
            )
            cols = st.columns(3)
            if cols[0].button("全選可見", key=f"all-{dimension}"):
                controller.apply_action(dimension, "select_all")
                _sync_widget(controller, dimension)
            if cols[1].button("取消可見", key=f"clear-{dimension}"):
                controller.apply_action(dimension, "clear")
                _sync_widget(controller, dimension)
            if cols[2].button("反選可見", key=f"invert-{dimension}"):
                controller.apply_action(dimension, "invert")
                _sync_widget(controller, dimension)

            options = [item.value for item in all_items]
            counts = {item.value: item.count for item in all_items}
            st.multiselect(
                label,
                options,
                default=[value for value in selected if value in options],
                key=f"sel-{dimension}",
                format_func=lambda value: f"{value}（{counts.get(value, 0)} 份）",
                on_change=_on_selection_change,
                args=(controller, dimension),
            )
            st.caption(f"目前可見 {len(visible)} 項")

    stats = controller.stats()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("考科數", stats.records)
    col2.metric("試題附件", stats.question_assets)
    col3.metric("答案附件", stats.answer_assets)
    col4.metric("實際傳輸（去重）", stats.unique_urls)
    st.caption(f"預計歸檔 {stats.total_files} 個檔案")


def _on_quick_filter(controller: AppController, dimension: str) -> None:
    controller.quick_text[dimension] = st.session_state.get(f"quick-{dimension}", "")


def _on_selection_change(controller: AppController, dimension: str) -> None:
    controller.set_selection(dimension, tuple(st.session_state.get(f"sel-{dimension}", ())))


def _sync_widget(controller: AppController, dimension: str) -> None:
    if controller.state is not None:
        st.session_state[f"sel-{dimension}"] = sorted(
            getattr(controller.state, dimension)
        )


def _render_preview(controller: AppController, mode: PathMode, template: str | None) -> None:
    if controller.snapshot is None:
        return
    st.subheader("歸檔路徑預覽")
    if st.button("計算預覽"):
        plan = controller.preview(mode, template)
        if plan is None:
            st.error(controller.last_error or "無法計算預覽")
            return
        st.session_state["preview"] = plan
        for warning in plan.warnings[:5]:
            st.warning(warning)

    plan = st.session_state.get("preview")
    if plan is not None:
        rows = [
            {
                "相對路徑": item.relative_path,
                "角色": item.role.value,
                "題型": item.question_type.value,
                "暫定": "是" if item.is_provisional else "",
            }
            for item in plan.files[:200]
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if len(plan.files) > 200:
            st.caption(f"僅顯示前 200 筆，共 {len(plan.files)} 筆")


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
    cols[2].caption(f"完成 {state.done}／失敗 {state.failed}／取消 {state.cancelled}")

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
    _render_preview(controller, page_settings["mode"], page_settings["template"])
    _render_task(controller, auto_refresh)


if __name__ == "__main__":  # pragma: no cover - 由 streamlit 執行
    main()
