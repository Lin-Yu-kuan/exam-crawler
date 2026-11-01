"""檢視已存檔的查詢結果 HTML 標記結構（離線，不發網路請求）。"""

from __future__ import annotations

import argparse
import collections
import pathlib

from bs4 import BeautifulSoup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/result_113.html")
    parser.add_argument("--rows", type=int, default=6)
    args = parser.parse_args()

    html = pathlib.Path(args.path).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")
    print("table rows:", len(table.find_all("tr")))

    classes = collections.Counter(
        " ".join(tr.get("class", [])) or "<none>" for tr in table.find_all("tr")
    )
    print("tr classes:", classes.most_common(15))

    print("\n=== 前幾列原始標記 ===")
    for idx, tr in enumerate(table.find_all("tr")[: args.rows]):
        raw = str(tr)
        print(f"\n--- tr[{idx}] class={tr.get('class')} len={len(raw)} ---")
        print(raw[:1500].replace("\t", ""))

    print("\n=== 表格外的重要容器 ===")
    for el in soup.select("#ctl00_holderContent_tblExamQand"):
        parent = el.parent
        print("parent tag/id/class:", parent.name, parent.get("id"), parent.get("class"))

    print("\n=== 下載與打包相關表單控制項 ===")
    for el in soup.select("input, a"):
        name = el.get("name") or el.get("href") or ""
        if any(k in str(name) for k in ("DownLoad", "Select", "chk", "File.ashx")):
            print(" ", el.name, el.get("type"), el.get("name"), el.get("id"), str(el.get("href"))[:80], el.get("value"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
