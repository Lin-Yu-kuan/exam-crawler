"""以小樣本對官方站點驗證查詢流程（P1-3 實測）。

只查詢單一年度，請求數約 3 個（表單 → 迄年 postback → 查詢），全程共用限速器。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.crawler.moex_client import MoexClient
from app.crawler.rate_limiter import RateLimiter
from app.engine.query_service import QueryService
from app.models import QuerySpec
from app.storage.manifest import ManifestStore


def main() -> int:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 113
    limiter = RateLimiter()
    with tempfile.TemporaryDirectory() as tmp:
        store = ManifestStore(pathlib.Path(tmp) / "manifest.db")
        with MoexClient(rate_limiter=limiter) as client:
            service = QueryService(client, store)
            snapshot = service.query(QuerySpec(year, year), force_refresh=True)

        print("狀態:", snapshot.status.value)
        print("訊息:", snapshot.message if hasattr(snapshot, "message") else "")
        print("紀錄數:", len(snapshot.records))
        print("年度診斷:")
        for diag in snapshot.years:
            print(
                f"  {diag.year}: {diag.status.value} 官方={diag.official} 解析={diag.parsed}"
            )
            for warning in diag.warnings:
                print("   -", warning)
        print("請求數:", client.request_count, "postback 數:", client.postback_count)
        print("高峰在途數:", limiter.peak_in_flight)
        print("啟動間隔:", [round(b - a, 2) for a, b in zip(limiter.start_times, limiter.start_times[1:])])
        sample = snapshot.records[0] if snapshot.records else None
        if sample is not None:
            print("樣本紀錄:", sample.record_id, sample.exam_name[:20], sample.category, sample.subject)
            for asset in sample.assets:
                print("   ", asset.role.value, asset.original_label, asset.url.split("?")[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
