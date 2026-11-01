"""ManifestStore 的內容去重與 schema 遷移測試。"""

from __future__ import annotations

import sqlite3

from app.models import AttachmentStatus
from app.storage.manifest import SCHEMA_VERSION, ManifestStore

SHA_A = "a" * 64
SHA_B = "b" * 64

OLD_ATTACHMENTS_DDL = """
CREATE TABLE attachments (
    asset_id      TEXT PRIMARY KEY,
    snapshot_id   TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    role          TEXT NOT NULL,
    status        TEXT NOT NULL,
    path          TEXT,
    size          INTEGER,
    sha256        TEXT,
    etag          TEXT,
    last_modified TEXT,
    question_type TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    error_code    TEXT,
    error_message TEXT,
    updated_at    TEXT NOT NULL
);
"""


def _complete(store: ManifestStore, asset_id: str, *, path: str, sha: str) -> None:
    store.mark_attachment_started(
        asset_id=asset_id,
        snapshot_id="snap-1",
        record_id="r1",
        url="https://wwwq.moex.gov.tw/exam/x.ashx",
        role="試題",
    )
    store.complete_attachment(
        asset_id=asset_id, path=path, sha256=sha, size=100, version=1
    )


def test_v1_database_gets_dedupe_column(tmp_path) -> None:
    """既有（v1）資料庫開啟時要自動補上 dedupe_of，不動既有資料。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_ATTACHMENTS_DDL)
    conn.execute(
        "INSERT INTO attachments (asset_id, snapshot_id, record_id, url, role, status,"
        " path, size, sha256, version, updated_at)"
        " VALUES ('old1', 'snap-1', 'r1', 'https://x/y.pdf', '試題', '已完成',"
        " '科目/舊_試題.pdf', 10, ?, 1, '2026-11-01T00:00:00+08:00')",
        (SHA_A,),
    )
    conn.commit()
    conn.close()

    store = ManifestStore(path)

    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(attachments)")}
    assert "dedupe_of" in columns
    row = store.get_attachment("old1")
    assert row is not None and row.path == "科目/舊_試題.pdf" and row.dedupe_of is None
    meta = sqlite3.connect(path).execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert meta[0] == str(SCHEMA_VERSION)


def test_find_content_duplicate_returns_completed_row(tmp_path) -> None:
    store = ManifestStore(tmp_path / "manifest.db")
    _complete(store, "a1", path="科目A/檔_試題.pdf", sha=SHA_A)
    _complete(store, "a2", path="科目B/檔_試題.pdf", sha=SHA_B)

    found = store.find_content_duplicate(sha256=SHA_A, exclude_asset_id="a9")

    assert found is not None
    assert found.asset_id == "a1"
    assert found.path == "科目A/檔_試題.pdf"


def test_find_content_duplicate_excludes_self_and_unknown(tmp_path) -> None:
    store = ManifestStore(tmp_path / "manifest.db")
    _complete(store, "a1", path="科目A/檔_試題.pdf", sha=SHA_A)

    assert store.find_content_duplicate(sha256=SHA_A, exclude_asset_id="a1") is None
    assert store.find_content_duplicate(sha256=SHA_B, exclude_asset_id="a1") is None


def test_find_content_duplicate_ignores_failed_rows(tmp_path) -> None:
    store = ManifestStore(tmp_path / "manifest.db")
    store.mark_attachment_started(
        asset_id="a1",
        snapshot_id="snap-1",
        record_id="r1",
        url="https://wwwq.moex.gov.tw/exam/x.ashx",
        role="試題",
    )
    store.fail_attachment(asset_id="a1", error_code="invalid-pdf", error_message="壞檔")

    assert store.find_content_duplicate(sha256=SHA_A, exclude_asset_id="a2") is None
    assert store.get_attachment("a1").status is AttachmentStatus.FAILED
