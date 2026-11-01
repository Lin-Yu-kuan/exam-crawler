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
from app.ui.controller import CONTROLLER_API_VERSION, DIMENSION_LABELS, AppController

DEFAULT_ROOT = "downloads"
MAX_CHIPS = 40
PREVIEW_ROW_LIMIT = 200
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
        # 設定變更後舊的預覽與選取已不適用
        st.session_state.pop("preview", None)
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

    st.subheader("候選與選取")
    candidates = controller.candidates()
    for dimension in ("exams", "categories", "subjects"):
        label = DIMENSION_LABELS[dimension]
        all_items = getattr(candidates, dimension)
        counts = {item.value: item.count for item in all_items}
        selected = controller.selected_values(dimension)
        selected_set = set(selected)
        visible = controller.visible(dimension)

        with st.expander(
            f"{label}：已選 {len(selected)}／{len(all_items)}",
            expanded=False,
            key=f"exp-{dimension}",
        ):
            st.text_input(
                "快速過濾（只影響可見項目）",
                key=f"quick-{dimension}",
                value=controller.quick_text[dimension],
                on_change=_on_quick_filter,
                args=(controller, dimension),
            )
            cols = st.columns(3)
            if cols[0].button("全選可見", key=f"all-{dimension}"):
                _apply_action(controller, dimension, "select_all")
            if cols[1].button("取消可見", key=f"clear-{dimension}"):
                _apply_action(controller, dimension, "clear")
            if cols[2].button("反選可見", key=f"invert-{dimension}"):
                _apply_action(controller, dimension, "invert")

            chips_key = f"chips-{dimension}"
            shown = [
                item.value for item in visible if item.value in selected_set
            ][:MAX_CHIPS]
            if shown:
                if set(st.session_state.get(chips_key, ())) != set(shown):
                    st.session_state[chips_key] = list(shown)
                st.pills(
                    "已選（點一下可叉除）",
                    options=shown,
                    selection_mode="multi",
                    key=chips_key,
                    format_func=lambda value, counts=counts: f"{value}（{counts.get(value, 0)} 份）",
                    on_change=_on_chips_change,
                    args=(controller, dimension, shown),
                )
                if len(selected) > len(shown):
                    st.caption(
                        f"已選 {len(selected)} 項，只顯示目前可見的前 {len(shown)} 項；"
                        "可用快速過濾縮小範圍。"
                    )
            else:
                st.session_state.pop(chips_key, None)
                st.caption(
                    "目前沒有可見的已選項目（零選取時無法下載）。"
                    if selected
                    else "目前零選取；零選取時無法下載。"
                )

            unselected = [item.value for item in visible if item.value not in selected_set]
            if unselected:
                st.multiselect(
                    "從可見清單加入（可直接輸入搜尋）",
                    options=unselected,
                    key=f"add-{dimension}",
                    format_func=lambda value, counts=counts: f"{value}（{counts.get(value, 0)} 份）",
                    on_change=_on_add_selection,
                    args=(controller, dimension),
                )
            st.caption(f"可見 {len(visible)} 項｜未選 {len(unselected)} 項｜括號內為份數")

    stats = controller.stats()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("考科數", stats.records)
    col2.metric("試題附件", stats.question_assets)
    col3.metric("答案附件", stats.answer_assets)
    col4.metric("實際傳輸（去重）", stats.unique_urls)
    st.caption(
        f"預計歸檔 {stats.total_files} 個檔案（內容相同的附件會共用同一個檔案，實際檔案數可能較少）"
    )


def _on_quick_filter(controller: AppController, dimension: str) -> None:
    controller.quick_text[dimension] = st.session_state.get(f"quick-{dimension}", "")


def _on_chips_change(
    controller: AppController, dimension: str, shown: list[str]
) -> None:
    """叉掉 chips：只把被叉掉的項目移出選取，不重新查詢。"""
    kept = set(st.session_state.get(f"chips-{dimension}", ()))
    removed = tuple(value for value in shown if value not in kept)
    if removed:
        controller.remove_values(dimension, removed)


def _on_add_selection(controller: AppController, dimension: str) -> None:
    """從可見清單加入：僅更新本機選取。"""
    chosen = tuple(st.session_state.get(f"add-{dimension}", ()))
    if not chosen:
        return
    controller.add_values(dimension, chosen)
    st.session_state[f"add-{dimension}"] = []


def _apply_action(controller: AppController, dimension: str, action: str) -> None:
    """全選／取消／反選可見：改完立刻重畫，避免同一輪顯示舊清單。"""
    controller.apply_action(dimension, action)
    _reset_candidate_dimension(dimension)
    st.rerun()


def _reset_candidate_dimension(dimension: str) -> None:
    """按鈕造成外部變更後，讓 chips／加入清單重新初始化。"""
    st.session_state.pop(f"chips-{dimension}", None)
    st.session_state.pop(f"add-{dimension}", None)


def _reset_candidate_widgets() -> None:
    """重新查詢後清除各欄位的 UI 狀態（選取已由控制器重設）。"""
    for dimension in ("exams", "categories", "subjects"):
        for prefix in ("chips", "add", "quick", "exp"):
            st.session_state.pop(f"{prefix}-{dimension}", None)


def _preview_fingerprint(
    controller: AppController, mode: PathMode, template: str | None
) -> tuple:
    """預覽識別碼；若控制器是熱重載前的舊版（缺此方法）則顯示提示而不是爆錯。"""
    method = getattr(controller, "preview_fingerprint", None)
    if method is None:
        st.warning("程式已更新，請重新整理頁面（F5）再使用預覽。")
        return ()
    return method(mode, template)


def _render_preview(controller: AppController, mode: PathMode, template: str | None) -> None:
    if controller.snapshot is None:
        return
    st.subheader("歸檔路徑預覽")
    st.caption("只計算路徑，不會建立或修改任何檔案；題型尚未確認的標示「暫定」。")

    fingerprint = _preview_fingerprint(controller, mode, template)
    stored = st.session_state.get("preview")
    if stored is not None and stored[0] != fingerprint:
        # 選取或歸檔設定改了，舊預覽不再可信
        st.session_state.pop("preview", None)
        stored = None

    if st.button("計算預覽", key="preview-button"):
        with st.spinner("計算中…（選取很多時需要幾秒）"):
            plan = controller.preview(mode, template)
        if plan is None:
            st.error(controller.last_error or "無法計算預覽")
            return
        stored = (fingerprint, plan)
        st.session_state["preview"] = stored
        for warning in plan.warnings[:5]:
            st.warning(warning)

    if stored is None:
        st.caption("還沒有預覽。按「計算預覽」可先確認檔名與資料夾，再決定要不要下載。")
        return

    plan = stored[1]
    if not plan.files:
        st.info("目前沒有可預覽的附件（請確認三個候選清單都至少選取一項）。")
        return

    folders = {
        item.relative_path.split("/")[0]
        for item in plan.files
        if "/" in item.relative_path
    }
    provisional = sum(1 for item in plan.files if item.is_provisional)
    st.caption(
        f"共 {len(plan.files)} 個檔案、{len(folders)} 個資料夾；"
        f"其中 {provisional} 個題型暫定（下載後會依實際內容改名）"
    )
    rows = [
        {
            "相對路徑": item.relative_path,
            "角色": item.role.value,
            "題型": item.question_type.value,
            "暫定": "是" if item.is_provisional else "",
        }
        for item in plan.files[:PREVIEW_ROW_LIMIT]
    ]
    st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
        column_config={"相對路徑": st.column_config.TextColumn(width="large")},
    )
    if len(plan.files) > PREVIEW_ROW_LIMIT:
        st.caption(f"僅顯示前 {PREVIEW_ROW_LIMIT} 筆，共 {len(plan.files)} 筆")


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
    _render_preview(controller, page_settings["mode"], page_settings["template"])
    _render_task(controller, auto_refresh)


if __name__ == "__main__":  # pragma: no cover - 由 streamlit 執行
    main()
