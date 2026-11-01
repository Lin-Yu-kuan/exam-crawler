"""端到端驗收（小樣本）：真實查詢 → 篩選 → 預覽 → 下載 → 略過 → 修復 → 離線快照。

僅發送少量請求（單年度查詢 3 個、附件下載 2 個），全程共用限速器。
輸出為驗收紀錄草稿，不含任何 Cookie 或 token。

用法：
    python scripts/e2e_check.py            # 113 年度、初等考試一般行政國文
    python scripts/e2e_check.py --year 112
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.crawler.moex_client import MoexClient
from app.crawler.rate_limiter import RateLimiter
from app.engine.downloader import Downloader, DownloadTarget
from app.engine.filter import FilterState, build_candidates
from app.engine.path_builder import PathMode, build_plan
from app.engine.query_service import QueryService
from app.models import AssetRole, QuerySpec
from app.storage.manifest import ManifestStore

WORK = ROOT / "downloads" / "e2e"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=113)
    parser.add_argument("--exam", default="初等考試")
    parser.add_argument("--category", default="一般行政")
    parser.add_argument("--subject", default="國文")
    args = parser.parse_args()

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    store = ManifestStore(WORK / "manifest.db")
    limiter = RateLimiter()

    print("== 1. 線上查詢 ==")
    client = MoexClient(rate_limiter=limiter)
    service = QueryService(client, store)
    started = time.perf_counter()
    snapshot = service.query(QuerySpec(args.year, args.year), force_refresh=True)
    elapsed = time.perf_counter() - started
    print(f"狀態：{snapshot.status.value}｜{snapshot.message}")
    print(f"紀錄：{len(snapshot.records)}｜請求數：{client.request_count}｜耗時：{elapsed:.2f}s")
    for diag in snapshot.years:
        print(f"  {diag.year}: 官方={diag.official} 解析={diag.parsed}")
    for warning in snapshot.warnings[:3]:
        print("  警告：", warning)

    print("\n== 2. 離線篩選與預覽 ==")
    candidates = build_candidates(snapshot.records, FilterState())
    state = candidates.initial_state()
    exam_value = next(c.value for c in candidates.exams if args.exam in c.value)
    category_value = next(
        c.value for c in candidates.categories if args.category in c.value
    )
    subject_value = next(c.value for c in candidates.subjects if c.value == args.subject)
    from app.engine.filter import apply_selection, selection_stats

    selection = FilterState(
        exams=frozenset({exam_value}),
        categories=frozenset({category_value}),
        subjects=frozenset({subject_value}),
    )
    records = apply_selection(snapshot.records, selection)
    stats = selection_stats(records)
    print(f"考科：{len(records)}｜試題：{stats.question_assets}｜答案：{stats.answer_assets}"
          f"｜檔案：{stats.total_files}｜傳輸去重：{stats.unique_urls}")
    for record in records:
        print(f"  {record.record_id} {record.category} / {record.subject}")

    plan = build_plan(WORK, records, mode=PathMode.A)
    for item in plan.files:
        print(f"  預覽：{item.relative_path}{'（暫定）' if item.is_provisional else ''}")

    print("\n== 3. 真實下載 ==")
    import httpx

    download_requests: list[str] = []

    def counting_client() -> httpx.Client:
        return httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(10.0, read=30.0),
            event_hooks={"request": [lambda request: download_requests.append(str(request.url))]},
        )

    downloader = Downloader(
        root=WORK,
        store=store,
        rate_limiter=limiter,
        snapshot_id=snapshot.snapshot_id,
        client_factory=counting_client,
    )
    targets = [
        DownloadTarget(
            record=record,
            asset=asset,
            relative_path=next(
                item.relative_path for item in plan.files if item.asset_id == asset.asset_id
            ),
        )
        for record in records
        for asset in record.assets
        if asset.role in (AssetRole.QUESTION, AssetRole.CHOICE_ANSWER)
    ]
    for target in targets:
        result = downloader.download(target)
        print(f"  {target.asset.role.value}：{result.status.value}"
              f"｜{result.relative_path}｜{result.size_bytes} bytes"
              f"｜sha256={ (result.sha256 or '')[:12] }")

    print("\n== 4. 檔案與 manifest 核對 ==")
    for path in sorted(WORK.rglob("*.pdf")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"  {path.relative_to(WORK)}｜{path.stat().st_size} bytes｜sha256={digest[:12]}")
    for row in store.list_attachments():
        print(f"  manifest：{row.asset_id}｜{row.status.value}｜{row.path}"
              f"｜v{row.version}｜{row.question_type}")

    print("\n== 5. 重新執行（應全部略過） ==")
    before = len(download_requests)
    for target in targets:
        result = downloader.download(target)
        print(f"  {result.status.value}｜skipped={result.skipped}")
    print(f"  期間新增下載請求：{len(download_requests) - before}")

    print("\n== 6. 刪除其中一個檔案後重新執行（應修復） ==")
    victim = next(WORK.rglob("*_試題.pdf"))
    victim.unlink()
    before = len(download_requests)
    repaired = [downloader.download(target) for target in targets]
    print("  " + "｜".join(f"{r.status.value}{'(skip)' if r.skipped else ''}" for r in repaired))
    print(
        f"  期間新增下載請求：{len(download_requests) - before}｜檔案已回復：{victim.exists()}"
    )

    print("\n== 7. 離線快照操作（不發請求） ==")
    offline_client = MoexClient(rate_limiter=limiter)
    cached = service.query(QuerySpec(args.year, args.year))
    print(f"  快取命中：{cached.cached}｜紀錄：{len(cached.records)}"
          f"｜離線 Client 請求數：{offline_client.request_count}")
    offline_client.close()
    client.close()

    print("\n== 8. 啟動核對 ==")
    report = store.reconcile(WORK)
    print(f"  補記完成 {len(report.adopted)}｜標記中斷 {len(report.interrupted)}"
          f"｜清除暫存 {len(report.removed_parts)}｜未追蹤 {len(report.untracked_pdfs)}")

    print("\n== 9. 任務服務流程（統計差異快照應可下載） ==")
    from app.engine.filter import freeze_selection
    from app.engine.task_service import TaskService

    task_service = TaskService(store=store, downloader=downloader, root=WORK, max_workers=2)
    frozen = freeze_selection(records, snapshot_id=snapshot.snapshot_id)
    state = task_service.submit(snapshot, frozen)
    final = task_service.wait(state.task_id, timeout=60)
    print(f"  任務狀態：{final.status.value}｜{final.message}")
    print(f"  已完成 {final.completed}｜略過 {final.skipped}｜失敗 {final.failed}")
    if final.failures:
        print("  失敗：", final.failures[:3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
