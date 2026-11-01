"""用真實快照重跑歸檔路徑預覽，重現「預覽不能用」的問題。

用法：`python scripts/check_preview.py [資料庫路徑]`
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import traceback

from app.engine.path_builder import PathMode, build_plan
from app.storage.manifest import ManifestStore

db = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "downloads_dedupe/manifest.db")
root = db.parent

con = sqlite3.connect(db)
snapshot_ids = [row[0] for row in con.execute("select snapshot_id from snapshots")]
con.close()
print(f"資料庫：{db}｜快照數 {len(snapshot_ids)}")

store = ManifestStore(db)
for snapshot_id in snapshot_ids[:1]:
    snapshot = store.load_snapshot(snapshot_id)
    assert snapshot is not None
    print(f"快照 {snapshot_id[:20]}｜紀錄 {len(snapshot.records)} 筆")
    try:
        plan = build_plan(root, snapshot.records, mode=PathMode.A)
    except Exception:  # noqa: BLE001 - 這裡就是要看真正的例外
        print("預覽失敗：")
        traceback.print_exc()
        continue
    print(f"預覽成功：{len(plan.files)} 個檔案，警告 {len(plan.warnings)} 筆")
    for item in plan.files[:3]:
        print("  ", item.relative_path)
