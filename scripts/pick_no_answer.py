"""列出沒有答案連結（可能為純申論）的科目樣本（離線）。"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from find_subject import parse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="tests/fixtures/source/range_112_113_plain.html")
    parser.add_argument("--exam", default="")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    records = parse(pathlib.Path(args.path).read_text(encoding="utf-8"))
    shown = 0
    for r in records:
        if r["kind"] != "subject" or r["answer"]:
            continue
        if args.exam and args.exam not in r["exam"].replace(" ", ""):
            continue
        print(f"{r['exam'][:30]} | {r['category']} | {r['subject']}")
        print("   Q:", r["question"][0] if r["question"] else None)
        shown += 1
        if shown >= args.limit:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
