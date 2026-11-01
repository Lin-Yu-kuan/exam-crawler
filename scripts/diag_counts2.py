"""診斷：找出沒有自身科目 checkbox 的科目列（離線，逐列掃描）。"""

from __future__ import annotations

import argparse
import pathlib
import re

from bs4 import BeautifulSoup

SUBJECT_BOX = re.compile(r"^ctl00\$holderContent\$chk_(\d{6})_(\d{3})_(\d{4})$")
CATEGORY_BOX = re.compile(r"^ctl00\$holderContent\$chk_(\d{6})_(\d{3})$")


def direct_boxes(tr) -> list[str]:
    return [b.get("name", "") for b in tr.find_all("input", attrs={"type": "checkbox"})]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/result_113.html")
    args = parser.parse_args()

    soup = BeautifulSoup(pathlib.Path(args.path).read_text(encoding="utf-8"), "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")

    exam = category = ""
    no_own_box: list[str] = []
    category_without_box: list[str] = []
    subject_rows = 0

    for tr in table.find_all("tr", recursive=False):
        boxes = direct_boxes(tr)
        level1 = tr.find("td", class_="level1")
        level2 = tr.find("td", class_="level2")
        label = tr.find("label", class_="exam-title")

        if level1 is not None:
            texts = [td.get_text(" ", strip=True) for td in level1.find_all("td")]
            exam = next((t for t in texts if t and "試題" not in t and "答案" not in t), exam)
            continue
        if level2 is not None:
            cat_label = level2.find("label")
            if cat_label:
                category = cat_label.get_text(" ", strip=True)
                if not any(CATEGORY_BOX.match(b) for b in boxes):
                    category_without_box.append(f"{exam} / {category}")
            continue
        if label is None:
            continue

        subject_rows += 1
        name = label.get_text(" ", strip=True)
        if not any(SUBJECT_BOX.match(b) for b in boxes):
            links = [a.get("href") for a in tr.find_all("a", href=True)]
            no_own_box.append(f"{exam} / {category} / {name} | boxes={boxes} | links={links[:2]}")

    print("科目列（exam-title）總數:", subject_rows)
    print("沒有自身科目 checkbox 的科目列:", len(no_own_box))
    for line in no_own_box[:8]:
        print("   ", line[:240])
    print("沒有類科 checkbox 的類科:", len(category_without_box), category_without_box[:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
