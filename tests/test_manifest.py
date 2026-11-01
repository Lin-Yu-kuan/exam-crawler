"""ManifestStore 測試：快照寫入、讀取與快取新鮮度。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

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
    QuestionType,
    TypeEvidence,
    YearDiagnostics,
)
from app.storage.manifest import ManifestStore


def make_record(record_id: str, *, year: int = 113, with_answer: bool = True) -> ExamRecord:
    evidence = TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="html:answer-link",
        reason="測試用",
        rule_version="html-v1",
    )
    assets = [
        ExamAsset(
            asset_id=f"{record_id}#q1",
            official_id=f"{record_id}:Q1",
            role=AssetRole.QUESTION,
            original_label="試題",
            url="https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx?t=Q&code=1&c=1&s=1&q=1",
            observed_at="2026-11-01T12:00:00+08:00",
            source_version=None,
            evidence=evidence,
            scope=AssetScope.SUBJECT,
            content_hint="pdf",
        )
    ]
    if with_answer:
        assets.append(
            ExamAsset(
                asset_id=f"{record_id}#s1",
                official_id=f"{record_id}:S1",
                role=AssetRole.CHOICE_ANSWER,
                original_label="更正答案",
                url="https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx?t=M&code=1&c=1&s=1&q=1",
                observed_at="2026-11-01T12:00:00+08:00",
                source_version=None,
                evidence=evidence,
                scope=AssetScope.SUBJECT,
                content_hint="pdf",
            )
        )
    return ExamRecord(
        record_id=record_id,
        official_id=record_id,
        year=year,
        exam_name="113年測試考試",
        exam_short_name=None,
        category="測試_一般行政",
        subject="測試科目",
        level=None,
        source_page="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
        assets=tuple(assets),
        exam_name_key="113年測試考試",
        category_key="測試_一般行政",
        subject_key="測試科目",
        observed_at="2026-11-01T12:00:00+08:00",
        id_strategy="moex-v1",
    )


def make_snapshot(snapshot_id: str = "snap-1", cache_key: str = "key-1") -> QuerySnapshot:
    record = make_record("moex:113010:501:0101")
    return QuerySnapshot(
        snapshot_id=snapshot_id,
        cache_key=cache_key,
        spec=QuerySpec(113, 113),
        records=(record,),
        status=ParseStatus.OK,
        years=(
            YearDiagnostics(
                year=113,
                status=ParseStatus.OK,
                official=OfficialCounts(1, 1, 1),
                parsed=ParsedCounts(1, 1, 1, 0),
                stats_text="共 1 考試 1 類科 1 科目",
                warnings=(),
            ),
        ),
        acquired_at="2026-11-01T12:00:00+00:00",
        parser_version="html-v1",
        source_url="https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx",
        warnings=(),
        cached=False,
    )


@pytest.fixture()
def store(tmp_path) -> ManifestStore:
    return ManifestStore(tmp_path / "nested" / "manifest.db")


def test_snapshot_roundtrip(store: ManifestStore) -> None:
    """寫入後能完整讀回紀錄、附件與年度診斷。"""
    snapshot = make_snapshot()
    store.save_snapshot(snapshot)

    loaded = store.load_snapshot("snap-1")

    assert loaded is not None
    assert loaded.spec == snapshot.spec
    assert loaded.status is ParseStatus.OK
    assert len(loaded.records) == 1
    record = loaded.records[0]
    assert record.record_id == snapshot.records[0].record_id
    assert record.subject_key == "測試科目"
    assert len(record.assets) == 2
    assert record.assets[1].original_label == "更正答案"
    assert loaded.years[0].official == OfficialCounts(1, 1, 1)


def test_save_is_idempotent_for_same_snapshot(store: ManifestStore) -> None:
    store.save_snapshot(make_snapshot())
    store.save_snapshot(make_snapshot())
    assert store.count_snapshots() == 1


def test_find_fresh_respects_ttl(store: ManifestStore) -> None:
    snapshot = make_snapshot()
    store.save_snapshot(snapshot)
    now = datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)

    fresh = store.find_fresh("key-1", ttl_seconds=3600, now=now)
    expired = store.find_fresh("key-1", ttl_seconds=60, now=now + timedelta(hours=2))

    assert fresh is not None and fresh.cached is True
    assert expired is None


def test_find_fresh_uses_cache_key(store: ManifestStore) -> None:
    store.save_snapshot(make_snapshot())
    assert store.find_fresh("other-key", ttl_seconds=86400) is None


def test_newest_snapshot_wins(store: ManifestStore) -> None:
    store.save_snapshot(make_snapshot("snap-1"))
    newer = replace(make_snapshot("snap-2"), acquired_at="2026-11-01T15:00:00+00:00")
    store.save_snapshot(newer)

    found = store.find_fresh(
        "key-1", ttl_seconds=86400, now=datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    )

    assert found is not None
    assert found.snapshot_id == "snap-2"


def test_unknown_snapshot_returns_none(store: ManifestStore) -> None:
    assert store.load_snapshot("missing") is None
