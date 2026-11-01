"""送出一次考畢試題查詢，觀察結果表格與分頁結構。

僅發送少量請求（GET 查詢頁 → POST 查詢 → 必要時 1 次分頁），
請求間隔至少 1.5 秒，不保存 Cookie。
"""

from __future__ import annotations

import argparse
import pathlib
import random
import time

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) study-tool-source-validation/0.1"
BASE = "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"


def polite_pause() -> None:
    time.sleep(1.5 + random.random())


def collect_form(soup: BeautifulSoup) -> dict[str, str]:
    """收集所有 hidden fields 與 select 目前選取值（模擬瀏覽器行為）。"""
    data: dict[str, str] = {}
    for hidden in soup.select("input[type=hidden]"):
        name = hidden.get("name")
        if name:
            data[name] = hidden.get("value", "")
    for select in soup.select("select"):
        name = select.get("name")
        if not name:
            continue
        selected = select.select_one("option[selected]") or select.select_one("option")
        data[name] = selected.get("value", "") if selected else ""
    return data


def summarize(html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand") or soup.find("table")
    print("table found:", bool(table), "id:", table.get("id") if table else None)
    if table:
        rows = table.find_all("tr")
        print("tr count:", len(rows))
        for idx, row in enumerate(rows[:8]):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            print(f"  row[{idx}] {cells}")
            for a in row.find_all("a"):
                print("      link:", a.get("href"), "|", a.get_text(" ", strip=True)[:30])

    print("\n--- 分頁／其他 postback 目標 ---")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "__doPostBack" in href:
            print("  ", a.get_text(" ", strip=True)[:20], "->", href)
    for el in soup.select("input[type=submit], input[type=button]"):
        print("  submit:", el.get("name"), el.get("value"))

    print("\n--- 頁面提示文字 ---")
    text = soup.get_text("\n", strip=True)
    for line in text.splitlines():
        if any(k in line for k in ("筆", "共", "查無", "無資料", "請選擇", "頁")):
            print("  ", line[:120])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year-start", default="2025")
    parser.add_argument("--year-end", default="2025")
    parser.add_argument("--exam-code", default="")
    parser.add_argument("--out", default="tests/fixtures/source")
    parser.add_argument("--name", default="result")
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        polite_pause()
        page = client.get(BASE, timeout=30)
        print("[GET]", page.status_code, len(page.content))
        soup = BeautifulSoup(page.text, "lxml")
        data = collect_form(soup)

        data["ctl00$holderContent$wUctlExamYearStart$ddlExamYear"] = args.year_start
        data["ctl00$holderContent$wUctlExamYearEnd$ddlExamYear"] = args.year_end
        data["ctl00$holderContent$ddlExamCode"] = args.exam_code
        data["ctl00$holderContent$btnSearch"] = "查詢"
        data["__EVENTTARGET"] = ""
        data["__EVENTARGUMENT"] = ""

        polite_pause()
        resp = client.post(BASE, data=data, timeout=60)
        print("[POST]", resp.status_code, len(resp.content), str(resp.url))

    (out_dir / f"{args.name}.html").write_text(resp.text, encoding="utf-8")
    summarize(resp.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
