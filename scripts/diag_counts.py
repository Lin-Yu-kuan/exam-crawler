"""診斷解析筆數與官方統計的差異（離線）。"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re

from bs4 import BeautifulSoup

SUBJECT_BOX = re.compile(r"^ctl00\$holderContent\$chk_(\d{6})_(\d{3})_(\d{4})$")
CATEGORY_BOX = re.compile(r"^ctl00\$holderContent\$chk_(\d{6})_(\d{3})$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/result_113.html")
    args = parser.parse_args()

    soup = BeautifulSoup(pathlib.Path(args.path).read_text(encoding="utf-8"), "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")

    stats = soup.select_one("#ctl00_holderContent_lblStatistics")
    print("official:", stats.get_text(" ", strip=True) if stats else None)

    boxes = [i.get("name", "") for i in table.find_all("input", attrs={"type": "checkbox"})]
    subj_names = [b for b in boxes if SUBJECT_BOX.match(b)]
    cat_names = [b for b in boxes if CATEGORY_BOX.match(b)]
    print("checkbox 總數:", len(boxes))
    print("科目 checkbox 出現次數:", len(subj_names), "去重:", len(set(subj_names)))
    print("類科 checkbox 出現次數:", len(cat_names), "去重:", len(set(cat_names)))

    dup_subj = [n for n, c in collections.Counter(subj_names).items() if c > 1]
    dup_cat = [n for n, c in collections.Counter(cat_names).items() if c > 1]
    print("重複科目 control:", len(dup_subj), dup_subj[:5])
    print("重複類科 control:", len(dup_cat), dup_cat[:5])

    labels = table.find_all("label", class_="exam-title")
    print("exam-title 標籤數:", len(labels))

    # 有 exam-title 但該列找不到 checkbox 的情形
    missing = []
    for label in labels:
        row = label.find_parent("tr")
        while row is not None and row.find("input", attrs={"type": "checkbox"}) is None:
            row = row.find_parent("tr")
        if row is None:
            missing.append(label.get_text(" ", strip=True))
    print("找不到 checkbox 的科目標籤:", len(missing), missing[:10])

    # 科目 checkbox 存在但沒有 exam-title 標籤
    label_controls = set()
    for label in labels:
        for_id = label.get("for")
        if for_id:
            label_controls.add(for_id.replace("ctl00_holderContent_", "ctl00$holderContent$"))
    unnamed = [n for n in set(subj_names) if n not in label_controls]
    print("沒有標籤的科目 checkbox:", len(unnamed), unnamed[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
