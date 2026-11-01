"""把已擷取的原始頁面整理成去敏感化 fixture 與預期解析結果（離線）。

原始擷取放在 tests/fixtures/source/（不納入版本控制），
輸出放在 tests/fixtures/html/ 與 tests/fixtures/expected/。
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
from probe_source import mask_tokens  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "tests" / "fixtures" / "source"
HTML_OUT = ROOT / "tests" / "fixtures" / "html"
EXP_OUT = ROOT / "tests" / "fixtures" / "expected"

# 選入縮減樣本的考試代碼（初等考試、關務人員考試）
KEEP_CODES = {"113010", "113040"}


def write_html(name: str, html: str) -> pathlib.Path:
    HTML_OUT.mkdir(parents=True, exist_ok=True)
    path = HTML_OUT / name
    path.write_text(mask_tokens(html), encoding="utf-8")
    return path


def trim_result(source: pathlib.Path, keep_codes: set[str], stats_text: str) -> str:
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "lxml")
    table = soup.select_one("#ctl00_holderContent_tblExamQand")
    current_code = None
    drop = []
    for tr in table.find_all("tr", recursive=False):
        level1 = tr.find("td", class_="level1")
        if level1 is not None:
            link = level1.find("a", href=True)
            m = re.search(r"code=(\d+)", link["href"]) if link else None
            current_code = m.group(1) if m else None
        if current_code not in keep_codes and level1 is not None:
            drop.append(tr)
        elif current_code not in keep_codes and level1 is None:
            # 屬於被排除考試的後續列：透過所屬 checkbox 名稱判斷
            box = tr.find("input", attrs={"type": "checkbox"})
            name = box.get("name", "") if box else ""
            m = re.search(r"chk_(\d{6})_", name)
            code = m.group(1) if m else None
            if code is None or code not in keep_codes:
                drop.append(tr)
    for tr in drop:
        tr.decompose()

    stats = soup.select_one("#ctl00_holderContent_lblStatistics")
    if stats is not None:
        stats.string = stats_text
    return str(soup)


def count_records(html: str) -> tuple[int, int, int]:
    records = parse(html)
    exams = len({r["exam"] for r in records if r["kind"] == "subject"})
    categories = len({(r["exam"], r["category"]) for r in records if r["kind"] == "category"})
    subjects = sum(1 for r in records if r["kind"] == "subject")
    return exams, categories, subjects


def main() -> int:
    HTML_OUT.mkdir(parents=True, exist_ok=True)
    EXP_OUT.mkdir(parents=True, exist_ok=True)

    # 1) 表單頁
    for src_name, out_name in [
        ("search_page.html", "search_form.html"),
        ("full_form.html", "full_form.html"),
    ]:
        write_html(out_name, (SRC / src_name).read_text(encoding="utf-8"))

    # 2) 正常結果（縮減為 2 場考試；統計文字與實際列數一致）
    subset = trim_result(SRC / "result_113.html", KEEP_CODES, "共 0 考試 0 類科 0 科目")
    n_exam, n_cat, n_subj = count_records(subset)
    subset = trim_result(
        SRC / "result_113.html", KEEP_CODES, f"共 {n_exam} 考試 {n_cat} 類科 {n_subj} 科目"
    )
    write_html("result_sample.html", subset)

    # 3) 官方統計與實際列數不符的情境（故意多報 3 個科目）
    small = trim_result(SRC / "result_113.html", {"113010"}, "共 0 考試 0 類科 0 科目")
    m_exam, m_cat, m_subj = count_records(small)
    mismatch = trim_result(
        SRC / "result_113.html",
        {"113010"},
        f"共 {m_exam} 考試 {m_cat} 類科 {m_subj + 3} 科目",
    )
    write_html("result_stats_mismatch.html", mismatch)

    # 4) 無結果
    write_html("result_noresult.html", (SRC / "adv_noresult.html").read_text(encoding="utf-8"))

    # 5) 超過官方 6000 筆上限
    write_html(
        "result_limit_exceeded.html",
        (SRC / "range_112_113_postback.html").read_text(encoding="utf-8"),
    )

    # 6) 錯誤頁（HTTP 200 但實際為找不到頁面）
    error_bytes = (SRC / "error_notfound.html").read_bytes()
    (HTML_OUT / "error_notfound.html").write_bytes(error_bytes)

    # 7) 預期解析結果
    records = parse(subset)
    subjects = [r for r in records if r["kind"] == "subject"]
    categories = [r for r in records if r["kind"] == "category"]
    exams = [r for r in records if r["kind"] == "exam_asset"]
    expected = {
        "fixture": "result_sample.html",
        "generated_by": "scripts/build_fixtures.py",
        "parser_reference": "scripts/find_subject.py（P1-2 前的參考解析）",
        "counts": {
            "exam_assets": len(exams),
            "categories": len(categories),
            "subjects": len(subjects),
            "subjects_with_answer": sum(1 for s in subjects if s["answer"]),
            "subjects_without_answer": sum(1 for s in subjects if not s["answer"]),
        },
        "records": records,
        "pdf_fixtures": {},
    }

    pdf_dir = ROOT / "tests" / "fixtures" / "pdf"
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        data = pdf.read_bytes()
        expected["pdf_fixtures"][pdf.name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "magic": data[:8].decode("latin-1"),
        }

    (EXP_OUT / "result_sample.expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(expected["counts"], ensure_ascii=False))
    print("pdf fixtures:", len(expected["pdf_fixtures"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
