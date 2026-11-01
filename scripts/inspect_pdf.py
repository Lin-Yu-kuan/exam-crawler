"""檢視 PDF 首頁文字與題型線索（離線）。

只讀第一頁，輸出前 300 個字元，供 P1-4 的題型規則設計參考。
"""

from __future__ import annotations

import argparse
import pathlib

from pypdf import PdfReader


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--chars", type=int, default=300)
    args = parser.parse_args()

    for raw in args.paths:
        path = pathlib.Path(raw)
        print(f"\n=== {path.name} ({path.stat().st_size} bytes) ===")
        try:
            reader = PdfReader(str(path))
            print("pages:", len(reader.pages))
            text = (reader.pages[0].extract_text() or "").strip()
        except Exception as exc:  # 診斷用途
            print("讀取失敗:", type(exc).__name__, exc)
            continue
        print(f"首頁文字長度: {len(text)}")
        print("前 300 字元:")
        print(repr(text[: args.chars]))
        for keyword in ("測驗題", "申論題", "選擇題", "單一選擇題", "作文", "公文", "答案"):
            if keyword in text[: args.chars]:
                print(f"  含關鍵字: {keyword}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
