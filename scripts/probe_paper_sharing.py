"""試卷群組收斂探測腳本（支援線上查詢或本機 HTML 樣本）。

用法：
  python scripts/probe_paper_sharing.py --year 115 --subject 英文
  python scripts/probe_paper_sharing.py --html tests/fixtures/html/result_sample.html --year 113
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.crawler.moex_client import MoexClient  # noqa: E402
from app.crawler.parser import parse_result_page  # noqa: E402
from app.crawler.rate_limiter import RateLimitConfig, RateLimiter  # noqa: E402
from app.engine.filter import selection_stats  # noqa: E402
from app.models import AssetRole, ExamRecord, paper_group_key  # noqa: E402


def summarize_records(records: tuple[ExamRecord, ...]) -> None:
    stats = selection_stats(records)
    print(f"考科紀錄數：{stats.records}")
    print(f"試題附件數：{stats.question_assets} -> 不重複試題試卷：{stats.question_papers}（共用 {stats.shared_question_files}）")
    print(f"答案附件數：{stats.answer_assets} -> 不重複答案試卷：{stats.answer_papers}")
    print(f"總附件數：{stats.total_files} -> 不重複試卷群組：{stats.unique_papers}（總共用 {stats.shared_files}）")
    print()

    by_exam_records: dict[str, int] = defaultdict(int)
    by_exam_groups: dict[str, set[str]] = defaultdict(set)
    for record in records:
        exam_id = record.exam_name
        for asset in record.assets:
            if asset.official_id and asset.official_id.startswith("moex:"):
                parts = asset.official_id.split(":")
                if len(parts) >= 2:
                    exam_id = parts[1]
                    break
        by_exam_records[exam_id] += 1
        for asset in record.assets:
            if asset.role is AssetRole.QUESTION:
                key = paper_group_key(asset) or f"url:{asset.url}"
                by_exam_groups[exam_id].add(key)

    print(f"{'考試代碼/名稱':<25} {'試卷組數':>8} {'符合筆數':>8}")
    print("-" * 45)
    total_groups = 0
    total_rows = 0
    for exam_id in sorted(by_exam_records):
        groups = len(by_exam_groups[exam_id])
        rows = by_exam_records[exam_id]
        total_groups += groups
        total_rows += rows
        print(f"{exam_id:<25} {groups:>8} {rows:>8}")
    print("-" * 45)
    print(f"{'合計':<25} {total_groups:>8} {total_rows:>8}")


def main() -> int:
    parser = argparse.ArgumentParser(description="統計考畢試題的試卷群組收斂結果")
    parser.add_argument("--year", type=int, default=115, help="民國年度（預設：115）")
    parser.add_argument("--subject", type=str, default="英文", help="科目名稱關鍵字（預設：英文）")
    parser.add_argument("--exam", type=str, default="", help="考試名稱關鍵字（選填）")
    parser.add_argument("--html", type=str, default="", help="離線解析指定 HTML 檔案（不發送網路請求）")
    args = parser.parse_args()

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8")
        page = parse_result_page(html)
        SummarizeRecords = page.records
    else:
        limiter = RateLimiter(RateLimitConfig(max_in_flight=1, min_interval_seconds=1.0))
        client = MoexClient(rate_limiter=limiter)
        page = client.query_year(
            args.year,
            exam_name_keyword=args.exam,
            subject_keyword=args.subject,
        )
        SummarizeRecords = page.records

    print(f"解析狀態：{page.status.value}｜訊息：{page.message}")
    if page.official_counts:
        print(
            f"官方統計：{page.official_counts.exams} 考試／"
            f"{page.official_counts.categories} 類科／"
            f"{page.official_counts.subjects} 科目"
        )
    summarize_records(SummarizeRecords)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

