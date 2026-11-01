"""P1-1 fixture 自我檢查（離線，不發網路請求）。

用法：python scripts/verify_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from find_subject import parse  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "tests" / "fixtures" / "html"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "result_sample.expected.json"
PDF = ROOT / "tests" / "fixtures" / "pdf"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def main() -> int:
    print("[1] 結果樣本統計與解析筆數")
    html = (HTML / "result_sample.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    stats = soup.select_one("#ctl00_holderContent_lblStatistics").get_text(" ", strip=True)
    records = parse(html)
    subjects = [r for r in records if r["kind"] == "subject"]
    categories = {r["category"] for r in records if r["kind"] == "category"}
    exams = {r["exam"] for r in subjects}
    numbers = [int(n) for n in re.findall(r"\d+", stats)]
    check(len(exams) == numbers[0], f"考試數 {len(exams)} == 統計 {numbers[0]}")
    check(len(categories) == numbers[1], f"類科數 {len(categories)} == 統計 {numbers[1]}")
    check(len(subjects) == numbers[2], f"科目數 {len(subjects)} == 統計 {numbers[2]}")

    print("[2] 關鍵紀錄")
    by_control = {r["control"]: r for r in subjects}
    guowen = by_control.get("ctl00$holderContent$chk_113010_501_0101")
    check(guowen is not None and guowen["subject"] == "國文", "找到 113010 初等一般行政 國文")
    check(bool(guowen and guowen["question"] and guowen["answer"]), "國文同時有試題與答案連結")
    xingzhengfa = by_control.get("ctl00$holderContent$chk_113040_101_0301")
    check(xingzhengfa is not None and xingzhengfa["subject"] == "行政法", "找到 113040 關務 行政法")
    check(bool(xingzhengfa and xingzhengfa["answer"]) is False, "關務行政法無答案連結（維持未確認題型）")
    two_digit = by_control.get("ctl00$holderContent$chk_113020_301_11")
    check(two_digit is None, "縮減樣本不含 113020（科目代碼 2 位數的考試）")

    print("[3] 統計不一致樣本")
    mismatch = (HTML / "result_stats_mismatch.html").read_text(encoding="utf-8")
    mstats = BeautifulSoup(mismatch, "lxml").select_one("#ctl00_holderContent_lblStatistics")
    mnumbers = [int(n) for n in re.findall(r"\d+", mstats.get_text(" ", strip=True))]
    msubjects = [r for r in parse(mismatch) if r["kind"] == "subject"]
    check(mnumbers[2] != len(msubjects), f"統計 {mnumbers[2]} 與實際 {len(msubjects)} 不符（預期不一致）")

    print("[4] 無結果／上限／錯誤頁")
    noresult = BeautifulSoup((HTML / "result_noresult.html").read_text(encoding="utf-8"), "lxml")
    check("查無結果" in noresult.get_text(" ", strip=True), "無結果頁含「查無結果」")
    limit = BeautifulSoup((HTML / "result_limit_exceeded.html").read_text(encoding="utf-8"), "lxml")
    check("6000" in limit.get_text(" ", strip=True), "上限頁含 6000 筆提示")
    error_text = (HTML / "error_notfound.html").read_text(encoding="utf-8", errors="replace")
    check("您所輸入的網址並不存在" in error_text, "錯誤頁為「網址不存在」頁面（HTTP 200）")

    print("[5] 表單 fixture 的 token 遮罩")
    for name in ("search_form.html", "full_form.html", "result_sample.html"):
        text = (HTML / name).read_text(encoding="utf-8")
        masked = text.count("MASKED")
        leftover = [
            m.group(0)[:40]
            for m in re.finditer(r'name="__[A-Z]+"\s+[^>]*?value="[^<"]{20,}', text)
        ]
        check(masked >= 5 and not leftover, f"{name} token 已遮罩（{masked} 個）")

    print("[6] PDF 樣本")
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    for name, meta in expected["pdf_fixtures"].items():
        path = PDF / name
        if not path.exists():
            check(False, f"{name} 存在")
            continue
        data = path.read_bytes()
        check(data[:4] == b"%PDF", f"{name} 具有 PDF 檔頭")
        check(len(data) == meta["bytes"], f"{name} 大小 {len(data)} 相符")
        check(hashlib.sha256(data).hexdigest() == meta["sha256"], f"{name} SHA-256 相符")

    print()
    if failures:
        print(f"驗證失敗 {len(failures)} 項")
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
