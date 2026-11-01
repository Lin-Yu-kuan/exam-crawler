"""驗證官方進階查詢（類科／科目關鍵字）與年度 postback 的組合（P2-6 Phase A）。

請求數：GET 表單 → 切換進階 → 迄年 postback → btnQuery（共 4 個）。
輸出：回應大小、統計文字、解析筆數、耗時。
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.crawler.parser import (
    EXAM_CODE_CONTROL,
    YEAR_END_CONTROL,
    YEAR_START_CONTROL,
    parse_result_page,
    parse_search_form,
)
from app.models import SOURCE_PAGE

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) kaoshi-practice-tool/0.1"
IBTN_FULL = "ctl00$holderContent$ibtnFull"
BTN_QUERY = "ctl00$holderContent$btnQuery"
DISPLAY_MODE = "ctl00$holderContent$ddlExamDisplayMode1"
SUBJECT_KW = "ctl00$holderContent$txtSubjectName"
CATEGORY_KW = "ctl00$holderContent$txtCategoryName"
EXAM_KW = "ctl00$holderContent$txtExamSetName"
ROC_OFFSET = 1911


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=113)
    parser.add_argument("--subject", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--exam", default="")
    args = parser.parse_args()

    hidden: dict[str, str] = {}
    values: dict[str, str] = {}
    started = time.perf_counter()
    requests = 0

    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=False, timeout=60) as client:
        def pause() -> None:
            time.sleep(1.2 + random.random())

        def post(data: dict[str, str], label: str) -> httpx.Response:
            nonlocal requests, hidden, values
            pause()
            payload = {**hidden, **values, **data}
            response = client.post(SOURCE_PAGE, data=payload)
            requests += 1
            soup = BeautifulSoup(response.text, "lxml")
            form = parse_search_form(response.text, base_url=SOURCE_PAGE)
            hidden = dict(form.hidden_fields)
            values = dict(form.select_values)
            print(f"  {label}: HTTP {response.status_code}, {len(response.content)} bytes")
            return response

        pause()
        page = client.get(SOURCE_PAGE, timeout=30)
        requests += 1
        form = parse_search_form(page.text, base_url=SOURCE_PAGE)
        hidden = dict(form.hidden_fields)
        values = dict(form.select_values)
        print(f"  GET 簡易表單: {len(page.content)} bytes, 文字欄位={form.text_fields}")

        response = post({IBTN_FULL: "切換至進階查詢", "__EVENTTARGET": "", "__EVENTARGUMENT": ""}, "切換進階")

        values[YEAR_END_CONTROL] = str(args.year + ROC_OFFSET)
        post({YEAR_END_CONTROL: str(args.year + ROC_OFFSET), "__EVENTTARGET": YEAR_END_CONTROL,
              "__EVENTARGUMENT": ""}, "迄年 postback")

        values[EXAM_CODE_CONTROL] = ""
        values[YEAR_START_CONTROL] = str(args.year + ROC_OFFSET)
        query_data = {
            YEAR_START_CONTROL: str(args.year + ROC_OFFSET),
            EXAM_CODE_CONTROL: "",
            DISPLAY_MODE: "0",
            BTN_QUERY: "查詢",
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
        }
        if args.subject:
            query_data[SUBJECT_KW] = args.subject
        if args.category:
            query_data[CATEGORY_KW] = args.category
        if args.exam:
            query_data[EXAM_KW] = args.exam
        response = post(query_data, "btnQuery（關鍵字）")

    elapsed = time.perf_counter() - started
    parsed = parse_result_page(response.text)
    print(f"\n總請求數：{requests}｜總耗時：{elapsed:.2f}s｜回應大小：{len(response.content)} bytes")
    print(f"統計文字：{parsed.stats_text}")
    print(f"官方：{parsed.official_counts}")
    print(f"解析：{parsed.parsed_counts}")
    print(f"狀態：{parsed.status.value}")
    if parsed.warnings:
        for warning in parsed.warnings[:3]:
            print("  警告：", warning)

    subjects = {record.subject for record in parsed.records}
    print(f"科目樣本（前 8）：{sorted(subjects)[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
