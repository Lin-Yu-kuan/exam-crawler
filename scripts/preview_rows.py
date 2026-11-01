"""輸出查詢結果表格前幾列的原始標記（離線分析用，輸出到檔案）。"""

from __future__ import annotations

import argparse
import pathlib

from bs4 import BeautifulSoup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/result_113.html")
    parser.add_argument("--rows", type=int, default=8)
    parser.add_argument("--chars", type=int, default=1200)
    parser.add_argument("--out", default="tests/fixtures/source/_rows_preview.txt")
    args = parser.parse_args()

    html = pathlib.Path(args.path).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")
    lines: list[str] = []

    lines.append(f"tr classes: {sorted({' '.join(tr.get('class', [])) or '<none>' for tr in table.find_all('tr')})}")
    lines.append(f"total tr: {len(table.find_all('tr'))}")
    for idx, tr in enumerate(table.find_all("tr")[: args.rows]):
        raw = " ".join(str(tr).split())
        lines.append(f"\n--- tr[{idx}] class={tr.get('class')} len={len(raw)} ---\n{raw[: args.chars]}")

    # 找「共 N 考試 M 類科 K 科目」所在元素
    for el in soup.find_all(string=lambda s: s and "考試" in s and "類科" in s and "科目" in s):
        parent = el.parent
        lines.append(f"\n--- 總數元素: <{parent.name} id={parent.get('id')} class={parent.get('class')}> {el.strip()[:120]}")

    pathlib.Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"written {args.out} ({len(lines)} blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
