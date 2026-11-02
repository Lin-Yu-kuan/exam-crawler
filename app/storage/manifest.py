"""SQLite manifest：查詢快照的儲存、讀取與新鮮度查詢。

邊界：只負責持久化與版本紀錄，不做網路或 UI；寫入一律使用短交易，
且不得在持有交易期間等待網路。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.models import (
    AssetRole,
    AssetScope,
    AttachmentStatus,
    ExamAsset,
    ExamRecord,
    OfficialCounts,
    ParseStatus,
    ParsedCounts,
    QuerySnapshot,
    QuerySpec,
    TypeEvidence,
    QuestionType,
    YearDiagnostics,
)

SCHEMA_VERSION = 2

DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id    TEXT PRIMARY KEY,
    cache_key      TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    spec_json      TEXT NOT NULL,
    status         TEXT NOT NULL,
    official_json  TEXT,
    parsed_json    TEXT,
    warnings_json  TEXT NOT NULL,
    acquired_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_cache
    ON snapshots (cache_key, acquired_at DESC);

CREATE TABLE IF NOT EXISTS snapshot_records (
    snapshot_id  TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    year         INTEGER NOT NULL,
    exam_name    TEXT NOT NULL,
    category     TEXT NOT NULL,
    subject      TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, record_id)
);

CREATE TABLE IF NOT EXISTS snapshot_years (
    snapshot_id   TEXT NOT NULL,
    year          INTEGER NOT NULL,
    status        TEXT NOT NULL,
    official_json TEXT,
    parsed_json   TEXT,
    stats_text    TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, year)
);

CREATE TABLE IF NOT EXISTS attachments (
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
    dedupe_of     TEXT,
    error_code    TEXT,
    error_message TEXT,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_record ON attachments (record_id);
CREATE INDEX IF NOT EXISTS idx_attachments_snapshot ON attachments (snapshot_id);

CREATE TABLE IF NOT EXISTS attachment_versions (
    asset_id    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, version)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _record_to_json(record: ExamRecord) -> str:
    return json.dumps(asdict(record), ensure_ascii=False)


def _record_from_json(payload: str) -> ExamRecord:
    data = json.loads(payload)
    assets = tuple(
        ExamAsset(
            asset_id=item["asset_id"],
            official_id=item["official_id"],
            role=AssetRole(item["role"]),
            original_label=item["original_label"],
            url=item["url"],
            observed_at=item["observed_at"],
            source_version=item["source_version"],
            evidence=TypeEvidence(
                question_type=QuestionType(item["evidence"]["question_type"]),
                source=item["evidence"]["source"],
                reason=item["evidence"]["reason"],
                rule_version=item["evidence"]["rule_version"],
                conflicted=item["evidence"]["conflicted"],
            ),
            scope=AssetScope(item.get("scope", AssetScope.SUBJECT.value)),
            content_hint=item.get("content_hint"),
        )
        for item in data["assets"]
    )
    return ExamRecord(
        record_id=data["record_id"],
        official_id=data["official_id"],
        year=data["year"],
        exam_name=data["exam_name"],
        exam_short_name=data["exam_short_name"],
        category=data["category"],
        subject=data["subject"],
        level=data["level"],
        source_page=data["source_page"],
        assets=assets,
        exam_name_key=data.get("exam_name_key", ""),
        category_key=data.get("category_key", ""),
        subject_key=data.get("subject_key", ""),
        observed_at=data.get("observed_at", ""),
        id_strategy=data.get("id_strategy", ""),
    )


class ManifestStore:
    """SQLite 快照儲存。

    每個操作各自開關連線，避免長交易與跨執行緒共用連線。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """每次操作各自開關連線（自動提交模式，交易由呼叫端明訂）。"""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(DDL)
            self._migrate(conn)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把既有資料庫補上新增欄位（只新增可空欄位，不動既有資料）。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(attachments)")}
        if "dedupe_of" not in columns:
            conn.execute("ALTER TABLE attachments ADD COLUMN dedupe_of TEXT")

    # --- 寫入 ---------------------------------------------------------
    def save_snapshot(self, snapshot: QuerySnapshot) -> None:
        """以單一短交易寫入快照、紀錄與年度診斷。"""
        totals = _aggregate_counts(snapshot)
        with self._connection() as conn:
            conn.execute("BEGIN")
            try:
                self._write_snapshot(conn, snapshot, totals)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _write_snapshot(conn: sqlite3.Connection, snapshot: QuerySnapshot, totals: dict) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots ("
            " snapshot_id, cache_key, source_url, parser_version, spec_json,"
            " status, official_json, parsed_json, warnings_json, acquired_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                snapshot.cache_key,
                snapshot.source_url,
                snapshot.parser_version,
                json.dumps(asdict(snapshot.spec), ensure_ascii=False),
                snapshot.status.value,
                json.dumps(asdict(totals["official"]), ensure_ascii=False)
                if totals["official"]
                else None,
                json.dumps(asdict(totals["parsed"]), ensure_ascii=False),
                json.dumps(list(snapshot.warnings), ensure_ascii=False),
                snapshot.acquired_at,
            ),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO snapshot_records ("
            " snapshot_id, record_id, year, exam_name, category, subject, payload_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    snapshot.snapshot_id,
                    record.record_id,
                    record.year,
                    record.exam_name,
                    record.category,
                    record.subject,
                    _record_to_json(record),
                )
                for record in snapshot.records
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO snapshot_years ("
            " snapshot_id, year, status, official_json, parsed_json, stats_text, warnings_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    snapshot.snapshot_id,
                    diag.year,
                    diag.status.value,
                    json.dumps(asdict(diag.official), ensure_ascii=False)
                    if diag.official
                    else None,
                    json.dumps(asdict(diag.parsed), ensure_ascii=False),
                    diag.stats_text,
                    json.dumps(list(diag.warnings), ensure_ascii=False),
                )
                for diag in snapshot.years
            ],
        )

    # --- 讀取 ---------------------------------------------------------
    def load_snapshot(self, snapshot_id: str) -> QuerySnapshot | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                return None
            records = tuple(
                _record_from_json(item["payload_json"])
                for item in conn.execute(
                    "SELECT payload_json FROM snapshot_records WHERE snapshot_id = ? "
                    "ORDER BY year, exam_name, category, subject",
                    (snapshot_id,),
                )
            )
            years = tuple(
                YearDiagnostics(
                    year=item["year"],
                    status=ParseStatus(item["status"]),
                    official=OfficialCounts(**json.loads(item["official_json"]))
                    if item["official_json"]
                    else None,
                    parsed=ParsedCounts(**json.loads(item["parsed_json"])),
                    stats_text=item["stats_text"],
                    warnings=tuple(json.loads(item["warnings_json"])),
                )
                for item in conn.execute(
                    "SELECT * FROM snapshot_years WHERE snapshot_id = ? ORDER BY year",
                    (snapshot_id,),
                )
            )
        return QuerySnapshot(
            snapshot_id=row["snapshot_id"],
            cache_key=row["cache_key"],
            spec=QuerySpec(**json.loads(row["spec_json"])),
            records=records,
            status=ParseStatus(row["status"]),
            years=years,
            acquired_at=row["acquired_at"],
            parser_version=row["parser_version"],
            source_url=row["source_url"],
            warnings=tuple(json.loads(row["warnings_json"])),
            cached=False,
        )

    def find_fresh(
        self, cache_key: str, *, ttl_seconds: float, now: datetime | None = None
    ) -> QuerySnapshot | None:
        """取出仍在 TTL 內的最新快照；找不到時回傳 None。"""
        current = now or datetime.now(timezone.utc)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT snapshot_id, acquired_at FROM snapshots "
                "WHERE cache_key = ? ORDER BY acquired_at DESC",
                (cache_key,),
            ).fetchall()
        threshold = current - timedelta(seconds=ttl_seconds)
        for row in rows:
            acquired = datetime.fromisoformat(row["acquired_at"])
            if acquired >= threshold:
                snapshot = self.load_snapshot(row["snapshot_id"])
                if snapshot is not None:
                    return replace(snapshot, cached=True)
        return None

    def count_snapshots(self) -> int:
        with self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    def list_recent_specs(self, limit: int = 15) -> tuple[QuerySpec, ...]:
        """取出最近查詢過的歷史條件（去重，最新在前）。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT spec_json FROM snapshots ORDER BY acquired_at DESC"
            ).fetchall()
        seen: set[tuple] = set()
        result: list[QuerySpec] = []
        for row in rows:
            try:
                spec_dict = json.loads(row["spec_json"])
                key = (
                    spec_dict.get("start_year"),
                    spec_dict.get("end_year"),
                    spec_dict.get("exam_name_keyword", ""),
                    spec_dict.get("category_keyword", ""),
                    spec_dict.get("subject_keyword", ""),
                )
                if key not in seen:
                    seen.add(key)
                    result.append(QuerySpec(**spec_dict))
                    if len(result) >= limit:
                        break
            except Exception:
                continue
        return tuple(result)

    # --- 附件狀態 -----------------------------------------------------
    def get_attachment(self, asset_id: str) -> AttachmentRow | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        return _attachment_from_row(row) if row else None

    def list_attachments(
        self, *, snapshot_id: str | None = None, status: AttachmentStatus | None = None
    ) -> tuple[AttachmentRow, ...]:
        query = "SELECT * FROM attachments"
        clauses: list[str] = []
        params: list[str] = []
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY record_id, asset_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(_attachment_from_row(row) for row in rows)

    def find_content_duplicate(
        self, *, sha256: str, exclude_asset_id: str
    ) -> AttachmentRow | None:
        """找出同內容且已完成的既有附件（供內容去重共用檔案）。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM attachments"
                " WHERE sha256 = ? AND asset_id <> ? AND path IS NOT NULL"
                " AND status IN (?, ?)"
                " ORDER BY rowid LIMIT 1",
                (
                    sha256,
                    exclude_asset_id,
                    AttachmentStatus.COMPLETED.value,
                    AttachmentStatus.SKIPPED.value,
                ),
            ).fetchone()
        return _attachment_from_row(row) if row else None

    def mark_attachment_started(
        self, *, asset_id: str, snapshot_id: str, record_id: str, url: str, role: str
    ) -> None:
        """記錄下載開始（短交易）。"""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO attachments ("
                " asset_id, snapshot_id, record_id, url, role, status, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET"
                " snapshot_id = excluded.snapshot_id,"
                " record_id = excluded.record_id,"
                " url = excluded.url,"
                " role = excluded.role,"
                " status = excluded.status,"
                " error_code = NULL,"
                " error_message = NULL,"
                " updated_at = excluded.updated_at",
                (
                    asset_id,
                    snapshot_id,
                    record_id,
                    url,
                    role,
                    AttachmentStatus.DOWNLOADING.value,
                    _now_iso(),
                ),
            )

    def begin_attachment_verify(
        self, *, asset_id: str, path: str, sha256: str, size: int, version: int
    ) -> None:
        """歸檔前的意圖記錄：檔名、雜湊與版本，供啟動核對修復中斷。"""
        with self._connection() as conn:
            conn.execute(
                "UPDATE attachments SET status = ?, path = ?, sha256 = ?, size = ?,"
                " version = ?, updated_at = ? WHERE asset_id = ?",
                (
                    AttachmentStatus.VERIFYING.value,
                    path,
                    sha256,
                    size,
                    version,
                    _now_iso(),
                    asset_id,
                ),
            )

    def complete_attachment(
        self,
        *,
        asset_id: str,
        path: str,
        sha256: str,
        size: int,
        version: int,
        etag: str | None = None,
        last_modified: str | None = None,
        question_type: str | None = None,
        status: AttachmentStatus = AttachmentStatus.COMPLETED,
        dedupe_of: str | None = None,
    ) -> None:
        """標記完成（含已略過），並寫入版本紀錄。

        dedupe_of 記錄「內容與哪個 asset 相同、共用同一實體檔」，便於後續稽核。
        """
        timestamp = _now_iso()
        with self._connection() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE attachments SET status = ?, path = ?, sha256 = ?, size = ?,"
                    " version = ?, etag = ?, last_modified = ?, question_type = ?,"
                    " dedupe_of = ?, error_code = NULL, error_message = NULL,"
                    " updated_at = ? WHERE asset_id = ?",
                    (
                        status.value,
                        path,
                        sha256,
                        size,
                        version,
                        etag,
                        last_modified,
                        question_type,
                        dedupe_of,
                        timestamp,
                        asset_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO attachment_versions ("
                    " asset_id, version, path, sha256, size, archived_at"
                    ") VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(asset_id, version) DO UPDATE SET"
                    " path = excluded.path, sha256 = excluded.sha256,"
                    " size = excluded.size, archived_at = excluded.archived_at",
                    (asset_id, version, path, sha256, size, timestamp),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fail_attachment(
        self, *, asset_id: str, error_code: str, error_message: str,
        status: AttachmentStatus = AttachmentStatus.FAILED,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE attachments SET status = ?, error_code = ?, error_message = ?,"
                " updated_at = ? WHERE asset_id = ?",
                (status.value, error_code, error_message, _now_iso(), asset_id),
            )

    def attachment_versions(self, asset_id: str) -> tuple[dict[str, object], ...]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM attachment_versions WHERE asset_id = ? ORDER BY version",
                (asset_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def reconcile(self, root: str | Path) -> ReconcileReport:
        """啟動核對：修復歸檔成功但狀態未提交、清理殘留暫存檔。"""
        base = Path(root)
        adopted: list[str] = []
        interrupted: list[str] = []
        removed_parts: list[str] = []

        pending = tuple(
            row
            for row in self.list_attachments()
            if row.status in (AttachmentStatus.DOWNLOADING, AttachmentStatus.VERIFYING)
        )
        for row in pending:
            target = base / row.path if row.path else None
            if target is not None and target.is_file() and row.sha256:
                if _file_sha256(target) == row.sha256 and target.stat().st_size == (row.size or 0):
                    self.complete_attachment(
                        asset_id=row.asset_id,
                        path=row.path or "",
                        sha256=row.sha256,
                        size=row.size or 0,
                        version=row.version,
                    )
                    adopted.append(row.asset_id)
                    continue
            self.fail_attachment(
                asset_id=row.asset_id,
                error_code="interrupted",
                error_message="下載或歸檔中斷，需重新下載",
            )
            interrupted.append(row.asset_id)

        parts_dir = base / PARTS_DIRNAME
        if parts_dir.is_dir():
            for part in parts_dir.glob("*.part"):
                try:
                    part.unlink()
                    removed_parts.append(part.name)
                except OSError:
                    pass

        tracked = {
            str((base / row.path).resolve()).casefold()
            for row in self.list_attachments()
            if row.path
        }
        untracked: list[str] = []
        if base.is_dir():
            for candidate in base.rglob("*.pdf"):
                if str(candidate.resolve()).casefold() not in tracked:
                    untracked.append(str(candidate.relative_to(base)).replace("\\", "/"))

        return ReconcileReport(
            adopted=tuple(adopted),
            interrupted=tuple(interrupted),
            removed_parts=tuple(removed_parts),
            untracked_pdfs=tuple(sorted(untracked)),
        )


PARTS_DIRNAME = ".parts"


@dataclass(frozen=True)
class AttachmentRow:
    """manifest 中的附件狀態。"""

    asset_id: str
    snapshot_id: str
    record_id: str
    url: str
    role: str
    status: AttachmentStatus
    path: str | None
    size: int | None
    sha256: str | None
    etag: str | None
    last_modified: str | None
    version: int
    error_code: str | None
    error_message: str | None
    updated_at: str
    question_type: str | None = None
    dedupe_of: str | None = None


@dataclass(frozen=True)
class ReconcileReport:
    """啟動核對結果。"""

    adopted: tuple[str, ...] = ()
    interrupted: tuple[str, ...] = ()
    removed_parts: tuple[str, ...] = ()
    untracked_pdfs: tuple[str, ...] = ()


def _attachment_from_row(row) -> AttachmentRow:
    return AttachmentRow(
        asset_id=row["asset_id"],
        snapshot_id=row["snapshot_id"],
        record_id=row["record_id"],
        url=row["url"],
        role=row["role"],
        status=AttachmentStatus(row["status"]),
        path=row["path"],
        size=row["size"],
        sha256=row["sha256"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        version=int(row["version"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        updated_at=row["updated_at"],
        question_type=row["question_type"],
        dedupe_of=row["dedupe_of"],
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aggregate_counts(snapshot: QuerySnapshot) -> dict[str, OfficialCounts | ParsedCounts | None]:
    """把年度診斷彙總為整份快照的筆數。"""
    official_exams = official_categories = official_subjects = 0
    has_official = False
    parsed_exams = parsed_categories = parsed_subjects = duplicate_rows = 0
    for diag in snapshot.years:
        if diag.official is not None:
            has_official = True
            official_exams += diag.official.exams
            official_categories += diag.official.categories
            official_subjects += diag.official.subjects
        parsed_exams += diag.parsed.exams
        parsed_categories += diag.parsed.categories
        parsed_subjects += diag.parsed.subjects
        duplicate_rows += diag.parsed.duplicate_rows
    return {
        "official": OfficialCounts(official_exams, official_categories, official_subjects)
        if has_official
        else None,
        "parsed": ParsedCounts(parsed_exams, parsed_categories, parsed_subjects, duplicate_rows),
    }
