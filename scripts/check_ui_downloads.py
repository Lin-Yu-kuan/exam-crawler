"""實機驗證輔助：核對 downloads 目錄與 manifest 內容。"""

from __future__ import annotations

import pathlib
import sqlite3

ROOT = pathlib.Path("downloads")
con = sqlite3.connect(ROOT / "manifest.db")

pdfs = sorted(ROOT.rglob("*.pdf"))
print("PDF 檔數:", len(pdfs))
for path in pdfs:
    print("  ", path.relative_to(ROOT), path.stat().st_size)

print("attachments 總數:", con.execute("select count(*) from attachments").fetchone()[0])
for status, count in con.execute(
    "select status, count(*) from attachments group by status"
):
    print("  狀態", status, count)

print("最新 12 筆附件：")
for row in con.execute(
    "select status, size, question_type, substr(path, 1, 60) from attachments order by rowid desc limit 12"
):
    print("  ", row)

print("快照數:", con.execute("select count(*) from snapshots").fetchone()[0])
con.close()
