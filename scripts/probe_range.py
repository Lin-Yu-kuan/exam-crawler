"""驗證年度起迄欄位的實際行為（需不需要 postback）。

用法：
    python scripts/probe_range.py --start 2023 --end 2024 --mode plain
    python scripts/probe_range.py --start 2023 --end 2024 --mode postback
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
START_CTL = "ctl00$holderContent$wUctlExamYearStart$ddlExamYear"
END_CTL = "ctl00$holderContent$wUctlExamYearEnd$ddlExamYear"


def pause() -> None:
    time.sleep(1.5 + random.random())


def collect(soup: BeautifulSoup) -> dict[str, str]:
    data: dict[str, str] = {}
    for hidden in soup.select("input[type=hidden]"):
        if hidden.get("name"):
            data[hidden.get("name")] = hidden.get("value", "")
    for select in soup.select("select"):
        name = select.get("name")
        if not name:
            continue
        chosen = select.select_one("option[selected]") or select.select_one("option")
        data[name] = chosen.get("value", "") if chosen else ""
    return data


def report(html: str, label: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    stats = soup.select_one("#ctl00_holderContent_lblStatistics")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")
    exams = []
    if table:
        for level1 in table.select("td.level1"):
            texts = [td.get_text(" ", strip=True) for td in level1.find_all("td")]
            name = next((t for t in texts if t and "試題" not in t and "答案" not in t), "")
            if name:
                exams.append(name)
    print(f"[{label}] statistics:", stats.get_text(" ", strip=True) if stats else None)
    print(f"[{label}] exams:", len(exams))
    for name in exams[:6]:
        print("   ", name[:60])
    years = sorted({name[:3] for name in exams if name[:3].isdigit()})
    print(f"[{label}] 出現的年度:", years)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--mode", choices=["plain", "postback"], default="plain")
    parser.add_argument("--out", default="tests/fixtures/source")
    parser.add_argument("--name", default="range")
    args = parser.parse_args()

    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        pause()
        page = client.get(BASE, timeout=30)
        data = collect(BeautifulSoup(page.text, "lxml"))

        if args.mode == "postback":
            data[END_CTL] = args.end
            data[START_CTL] = args.start
            data["__EVENTTARGET"] = END_CTL
            data["__EVENTARGUMENT"] = ""
            pause()
            page = client.post(BASE, data=data, timeout=60)
            data = collect(BeautifulSoup(page.text, "lxml"))

        data[START_CTL] = args.start
        data[END_CTL] = args.end
        data["ctl00$holderContent$btnSearch"] = "查詢"
        data["__EVENTTARGET"] = ""
        data["__EVENTARGUMENT"] = ""
        pause()
        resp = client.post(BASE, data=data, timeout=180)

    print("[POST]", resp.status_code, len(resp.content))
    pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(args.out) / f"{args.name}_{args.mode}.html").write_text(resp.text, encoding="utf-8")
    report(resp.text, args.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
