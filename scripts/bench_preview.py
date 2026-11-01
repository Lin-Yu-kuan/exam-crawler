"""量測歸檔路徑預覽的耗時與資料量（本機快照，不需網路）。"""

from __future__ import annotations

import pathlib
import sqlite3
import time

from app.engine.path_builder import PathMode, build_plan
from app.storage.manifest import ManifestStore

db = pathlib.Path("downloads_dedupe/manifest.db")
store = ManifestStore(db)
snapshot_id = sqlite3.connect(db).execute("select snapshot_id from snapshots limit 1").fetchone()[0]
snapshot = store.load_snapshot(snapshot_id)
assert snapshot is not None

t0 = time.perf_counter()
plan = build_plan(db.parent, snapshot.records, mode=PathMode.A)
t1 = time.perf_counter()
print(f"build_plan：{(t1 - t0) * 1000:.0f} ms｜檔案 {len(plan.files)}｜警告 {len(plan.warnings)}")

paths = [item.relative_path for item in plan.files]
print(f"最長路徑 {max(len(p) for p in paths)} 字｜平均 {sum(map(len, paths)) // len(paths)} 字")
folders = {p.split('/')[0] for p in paths if '/' in p}
print(f"頂層資料夾 {len(folders)} 個｜暫定題型 {sum(1 for i in plan.files if i.is_provisional)} 檔")

rows = [
    {
        "相對路徑": item.relative_path,
        "角色": item.role.value,
        "題型": item.question_type.value,
        "暫定": "是" if item.is_provisional else "",
    }
    for item in plan.files[:200]
]
import pandas as pd  # noqa: E402 - 只為量測轉換成本

t0 = time.perf_counter()
pd.DataFrame(rows)
t1 = time.perf_counter()
print(f"200 列轉 DataFrame：{(t1 - t0) * 1000:.1f} ms")
