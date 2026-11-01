"""SQLite manifest：查詢快照的儲存、讀取與新鮮度查詢。

邊界：只負責持久化與版本紀錄，不做網路或 UI；寫入一律使用短交易，
且不得在持有交易期間等待網路。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.models import (
    AssetRole,
    AssetScope,
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

SCHEMA_VERSION = 1

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
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

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
