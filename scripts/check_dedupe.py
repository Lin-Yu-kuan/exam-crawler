"""驗證內容去重：列出檔案與 manifest 的 dedupe_of 對應。"""

from __future__ import annotations

import pathlib
import sqlite3

ROOT = pathlib.Path("downloads_dedupe")
con = sqlite3.connect(ROOT / "manifest.db")

pdfs = sorted(ROOT.rglob("*.pdf"))
print("實體 PDF 檔數:", len(pdfs))
for path in pdfs:
    print("  ", path.relative_to(ROOT), path.stat().st_size)

print("\n附件紀錄：")
for row in con.execute(
    "select asset_id, status, size, dedupe_of, path from attachments"
    " order by rowid"
):
    print(f"  {row[0][:28]:30} {row[1]:4} {row[2]:>7}  dedupe_of={row[3] or '-'}")
    print(f"    {row[4]}")

print(
    "\n共用檔案筆數:",
    con.execute("select count(*) from attachments where dedupe_of is not null").fetchone()[0],
)
print(
    "不重複路徑數:",
    con.execute("select count(distinct path) from attachments").fetchone()[0],
)
con.close()
