"""依考試／科目關鍵字挑出樣本附件的 URL（離線）。"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from find_subject import parse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/result_112_113.html")
    parser.add_argument("--exam", default="")
    parser.add_argument("--subject", default="")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    records = parse(pathlib.Path(args.path).read_text(encoding="utf-8"))
    exam_re = re.compile(args.exam) if args.exam else None
    subj_re = re.compile(args.subject) if args.subject else None

    shown = 0
    for r in records:
        if r["kind"] != "subject":
            continue
        if exam_re and not exam_re.search(r["exam"].replace(" ", "")):
            continue
        if subj_re and not subj_re.search(r["subject"]):
            continue
        print(f"{r['exam'][:40]} | {r['category']} | {r['subject']}")
        for url in r["question"]:
            print("   Q:", url)
        for url in r["answer"]:
            print("   A:", url)
        shown += 1
        if shown >= args.limit:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
