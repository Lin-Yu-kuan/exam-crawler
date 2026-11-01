"""分步探測官方站點：查詢、進階查詢、附件下載。

每一步都是獨立子命令，方便逐步執行並控制請求量（每次間隔 1.5～2.5 秒）。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import random
import re
import time

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) study-tool-source-validation/0.1"
BASE = "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"


def pause() -> None:
    time.sleep(1.5 + random.random())


def collect_form(soup: BeautifulSoup) -> dict[str, str]:
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


def save(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print("saved:", path, len(text))


def describe_controls(soup: BeautifulSoup, keyword: str = "") -> None:
    for el in soup.select("input, select, textarea"):
        name = el.get("name") or ""
        if keyword and keyword not in name:
            continue
        kind = el.get("type") or el.name
        if el.name == "select":
            opts = [(o.get("value", ""), o.get_text(" ", strip=True)[:24]) for o in el.select("option")]
            print(f"  select {name} ({len(opts)} options) e.g. {opts[:3]}")
        else:
            print(f"  {kind} {name} value={str(el.get('value'))[:40]}")


def cmd_search(args: argparse.Namespace) -> None:
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        pause()
        page = client.get(BASE, timeout=30)
        soup = BeautifulSoup(page.text, "lxml")
        data = collect_form(soup)
        data.update(
            {
                "ctl00$holderContent$wUctlExamYearStart$ddlExamYear": args.start,
                "ctl00$holderContent$wUctlExamYearEnd$ddlExamYear": args.end,
                "ctl00$holderContent$ddlExamCode": args.exam_code,
                "ctl00$holderContent$btnSearch": "查詢",
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
            }
        )
        pause()
        resp = client.post(BASE, data=data, timeout=180)
    print("[POST]", resp.status_code, len(resp.content))
    save(pathlib.Path(args.out) / f"{args.name}.html", resp.text)
    out = BeautifulSoup(resp.text, "lxml")
    stats = out.select_one("#ctl00_holderContent_lblStatistics")
    print("statistics:", stats.get_text(" ", strip=True) if stats else None)
    rows = out.select("#ctl00_holderContent_tblExamQand tr", recursive=False)
    print("outer rows:", len(rows))


def cmd_full(args: argparse.Namespace) -> None:
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        pause()
        page = client.get(BASE, timeout=30)
        soup = BeautifulSoup(page.text, "lxml")
        data = collect_form(soup)
        data["ctl00$holderContent$ibtnFull"] = "切換至進階查詢"
        data["__EVENTTARGET"] = ""
        data["__EVENTARGUMENT"] = ""
        pause()
        resp = client.post(BASE, data=data, timeout=60)
    print("[POST btnFull]", resp.status_code, len(resp.content))
    save(pathlib.Path(args.out) / f"{args.name}.html", resp.text)
    out = BeautifulSoup(resp.text, "lxml")
    print("--- 進階查詢表單控制項 ---")
    describe_controls(out, "holderContent")
    print("--- 提示文字 ---")
    for line in out.get_text("\n", strip=True).splitlines():
        if any(k in line for k in ("進階", "簡稱", "類科", "科目", "年度")):
            print("  ", line[:100])


def cmd_advsearch(args: argparse.Namespace) -> None:
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        pause()
        page = client.get(BASE, timeout=30)
        soup = BeautifulSoup(page.text, "lxml")
        data = collect_form(soup)
        data["ctl00$holderContent$ibtnFull"] = "切換至進階查詢"
        data["__EVENTTARGET"] = ""
        data["__EVENTARGUMENT"] = ""
        pause()
        full = client.post(BASE, data=data, timeout=60)
        full_soup = BeautifulSoup(full.text, "lxml")
        form2 = collect_form(full_soup)
        form2.update(
            {
                "ctl00$holderContent$wUctlExamYearStart$ddlExamYear": args.start,
                "ctl00$holderContent$wUctlExamYearEnd$ddlExamYear": args.end,
                "ctl00$holderContent$ddlExamCode": args.exam_code,
                "ctl00$holderContent$txtExamSetName": args.exam_name,
                "ctl00$holderContent$txtCategoryName": args.category,
                "ctl00$holderContent$txtSubjectName": args.subject,
                "ctl00$holderContent$ddlExamDisplayMode1": args.display_mode,
                "ctl00$holderContent$btnQuery": "查詢",
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
            }
        )
        pause()
        resp = client.post(BASE, data=form2, timeout=180)
    print("[POST btnQuery]", resp.status_code, len(resp.content))
    save(pathlib.Path(args.out) / f"{args.name}.html", resp.text)
    out = BeautifulSoup(resp.text, "lxml")
    stats = out.select_one("#ctl00_holderContent_lblStatistics")
    print("statistics:", stats.get_text(" ", strip=True) if stats else None)
    table = out.select_one("#ctl00_holderContent_tblExamQand")
    print("outer rows:", len(table.find_all("tr", recursive=False)) if table else 0)
    text = out.get_text("\n", strip=True)
    for line in text.splitlines():
        if any(k in line for k in ("查無", "無資料", "共 ", "請選擇")):
            print("  msg:", line[:100])


def cmd_download(args: argparse.Namespace) -> None:
    # 刻意使用全新的 Client（不帶任何 Cookie），驗證是否需要 Session
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        pause()
        resp = client.get(args.url, timeout=120)
        body = resp.content
    print("[GET]", resp.status_code, len(body), str(resp.url))
    for key in ("content-type", "content-length", "content-disposition", "etag",
                "last-modified", "set-cookie", "cache-control"):
        if key in resp.headers:
            value = resp.headers[key]
            print(f"  {key}: {value[:120]}")
    print("  redirects:", [str(r.url) for r in resp.history])
    print("  magic:", body[:8], "sha256:", hashlib.sha256(body).hexdigest()[:16])

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if body[:4] == b"%PDF" else pathlib.Path(args.name).suffix or ".bin"
    target = out_dir / f"{args.name}{suffix}"
    target.write_bytes(body)
    print("  saved:", target, len(body))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("--start", default="2025")
    s.add_argument("--end", default="2025")
    s.add_argument("--exam-code", default="")
    s.add_argument("--name", default="result")
    s.add_argument("--out", default="tests/fixtures/source")
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("full")
    f.add_argument("--name", default="full_form")
    f.add_argument("--out", default="tests/fixtures/source")
    f.set_defaults(func=cmd_full)

    a = sub.add_parser("advsearch")
    a.add_argument("--start", default="2025")
    a.add_argument("--end", default="2025")
    a.add_argument("--exam-code", default="")
    a.add_argument("--exam-name", default="")
    a.add_argument("--category", default="")
    a.add_argument("--subject", default="")
    a.add_argument("--display-mode", default="0")
    a.add_argument("--name", default="adv_result")
    a.add_argument("--out", default="tests/fixtures/source")
    a.set_defaults(func=cmd_advsearch)

    d = sub.add_parser("download")
    d.add_argument("--url", required=True)
    d.add_argument("--name", required=True)
    d.add_argument("--out", default="tests/fixtures/pdf")
    d.set_defaults(func=cmd_download)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
