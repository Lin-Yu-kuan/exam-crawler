"""本機效能取樣（離線）：快取命中、候選建置與選取。

只使用既有 manifest 快照，不發送網路請求。輸出供驗收紀錄引用。
"""

from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine.filter import FilterState, apply_selection, build_candidates, selection_stats
from app.engine.query_service import QueryService
from app.models import QuerySpec
from app.storage.manifest import ManifestStore


class _NoNetworkClient:
    def search(self, *, year_roc: int, exam_code: str = "") -> str:  # pragma: no cover
        raise AssertionError("效能取樣不應發出網路請求")


def main() -> int:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 113
    db = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "downloads" / "e2e" / "manifest.db"
    store = ManifestStore(db)
    service = QueryService(_NoNetworkClient(), store, cache_ttl_seconds=24 * 3600)
    key = service.cache_key(QuerySpec(year, year))

    started = time.perf_counter()
    snapshot = store.find_fresh(key, ttl_seconds=24 * 3600)
    cache_ms = (time.perf_counter() - started) * 1000
    if snapshot is None:
        print(f"找不到 {year} 年快照，請先執行 scripts/e2e_check.py")
        return 1

    started = time.perf_counter()
    candidates = build_candidates(snapshot.records, FilterState())
    state = candidates.initial_state()
    build_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    selected = apply_selection(snapshot.records, state)
    select_ms = (time.perf_counter() - started) * 1000

    stats = selection_stats(selected)
    print(f"樣本：{year} 年｜紀錄 {len(snapshot.records)}｜類科候選 {len(candidates.categories)}"
          f"｜科目候選 {len(candidates.subjects)}")
    print(f"快取命中（含讀回 {len(snapshot.records)} 筆）：{cache_ms:.0f} ms")
    print(f"建立候選（含初始全選）：{build_ms:.0f} ms")
    print(f"套用選取（三欄全選）：{select_ms:.0f} ms")
    print(f"統計：考科 {stats.records}、試題 {stats.question_assets}、答案 {stats.answer_assets}、"
          f"檔案 {stats.total_files}、傳輸去重 {stats.unique_urls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
