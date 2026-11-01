"""離線解析已存檔的查詢結果，找出可用來當 fixture 的樣本。

輸出：符合關鍵字的科目（含考試、類科、科目、試題／答案 URL、卷次 q）。
這個檔案是探測用，正式解析器會在 P1-2 建立於 app/crawler/parser.py。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import urllib.parse

from bs4 import BeautifulSoup

BASE = "https://wwwq.moex.gov.tw/exam/"


def parse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")
    records: list[dict] = []
    seen: set[str] = set()

    exam_text = ""
    category_text = ""
    for tr in table.find_all("tr"):
        level1 = tr.find("td", class_="level1")
        level2 = tr.find("td", class_="level2")
        title = tr.find("label", class_="exam-title")

        if level1 is not None:
            cells = [td.get_text(" ", strip=True) for td in level1.find_all("td")]
            exam_text = next((c for c in cells if c and "試題" not in c and "答案" not in c), "")
            extra = [
                {"label": a.get_text(" ", strip=True).replace("\n", ""), "href": a.get("href")}
                for a in level1.find_all("a")
            ]
            if extra:
                records.append({"kind": "exam_asset", "exam": exam_text, "links": extra})
            continue

        if level2 is not None:
            box = level2.find("input", attrs={"type": "checkbox"})
            label = level2.find("label")
            node = label or level2.find("span")
            if node is None:
                continue
            control = box.get("name") if box is not None else None
            if control is not None:
                if control in seen:
                    continue
                seen.add(control)
            category_text = node.get_text(" ", strip=True)
            records.append(
                {"kind": "category", "exam": exam_text, "category": category_text,
                 "control": control}
            )
            continue

        if title is None:
            continue
        box = tr.find("input", attrs={"type": "checkbox"})
        if box is None or box.get("name") in seen:
            continue
        seen.add(box.get("name"))

        links: dict[str, list[str]] = {}
        for a in tr.find_all("a", href=True):
            href = a["href"]
            if "wHandExamQandA_File.ashx" not in href:
                continue
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(href.replace("&amp;", "&")).query)
            role = {"Q": "question", "S": "answer"}.get(query.get("t", [""])[0])
            if role:
                absolute = urllib.parse.urljoin(BASE, href.replace("&amp;", "&"))
                links.setdefault(role, [])
                if absolute not in links[role]:
                    links[role].append(absolute)

        records.append(
            {
                "kind": "subject",
                "exam": exam_text,
                "category": category_text,
                "subject": title.get_text(" ", strip=True),
                "control": box.get("name"),
                "question": links.get("question", []),
                "answer": links.get("answer", []),
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/result_113.html")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    records = parse(pathlib.Path(args.path).read_text(encoding="utf-8"))
    subjects = [r for r in records if r["kind"] == "subject"]

    if args.stats:
        exams = {r["exam"] for r in subjects}
        categories = {(r["exam"], r["category"]) for r in subjects}
        no_answer = sum(1 for r in subjects if not r["answer"])
        multi_q = sum(1 for r in subjects if len(r["question"]) > 1)
        cat_records = [r for r in records if r["kind"] == "category"]
        print(f"records={len(records)} subjects={len(subjects)} exams={len(exams)} "
              f"categories={len(categories)} category_rows={len(cat_records)} "
              f"no_answer={no_answer} multi_question_urls={multi_q}")
        vol2 = sum(1 for r in subjects for u in r["question"] if "&q=2" in u)
        print(f"出現 q=2 的試題連結數: {vol2}")
        for r in records:
            if r["kind"] == "exam_asset":
                print("  exam_asset:", r["exam"], r["links"])

    pattern = re.compile(args.keyword) if args.keyword else None
    shown = 0
    for r in subjects:
        if pattern and not pattern.search(r["subject"]):
            continue
        print(json.dumps(r, ensure_ascii=False))
        shown += 1
        if shown >= args.limit:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
