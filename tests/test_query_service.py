"""QueryService 測試：年度批次、合併去重、完整性、快取與取消（全離線）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import synthetic_year_page

from app.crawler.moex_client import SourceError
from app.engine.query_service import MAX_YEARS, QueryCancelled, QueryService
from app.models import ParseStatus, QuerySpec
from app.storage.manifest import ManifestStore


class StubClient:
    """以固定 HTML 回應模擬查詢客戶端，並記錄呼叫。"""

    def __init__(
        self,
        pages: dict[int, str],
        *,
        fail_keywords: Exception | None = None,
    ) -> None:
        self.pages = pages
        self.fail_keywords = fail_keywords
        self.calls: list[tuple[int, str]] = []
        self.keyword_calls: list[tuple[str, str, str] | None] = []
        self.resets = 0

    def search(
        self,
        *,
        year_roc: int,
        exam_code: str = "",
        keywords: tuple[str, str, str] | None = None,
    ) -> str:
        self.calls.append((year_roc, exam_code))
        self.keyword_calls.append(keywords)
        if keywords and self.fail_keywords is not None:
            raise self.fail_keywords
        return self.pages[year_roc]

    def reset(self) -> None:
        self.resets += 1


class StubClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture()
def store(tmp_path) -> ManifestStore:
    return ManifestStore(tmp_path / "manifest.db")


@pytest.fixture()
def sample_html(html_dir) -> str:
    return (html_dir / "result_sample.html").read_text(encoding="utf-8")


def test_complete_single_year_snapshot(store, sample_html) -> None:
    """單一年度完整結果會存成可驗證的快照。"""
    client = StubClient({113: sample_html})
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(113, 113))

    assert snapshot.status is ParseStatus.OK
    assert snapshot.is_complete is True
    assert len(snapshot.records) == 371
    assert snapshot.cached is False
    assert client.calls == [(113, "")]
    assert snapshot.years[0].official.subjects == 371
    assert store.count_snapshots() == 1
    assert store.load_snapshot(snapshot.snapshot_id) is not None


def test_multi_year_batches_are_merged(store) -> None:
    """多年度以年度為批次查詢後合併，紀錄不重複。"""
    client = StubClient({113: synthetic_year_page(113), 112: synthetic_year_page(112)})
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(112, 113))

    assert client.calls == [(112, ""), (113, "")]
    assert len(snapshot.records) == 2
    assert {record.year for record in snapshot.records} == {112, 113}
    assert snapshot.status is ParseStatus.OK


def test_cross_batch_duplicates_are_deduped(store, sample_html) -> None:
    """同一紀錄重複出現時只保留一份，並因統計不符標示未完整。"""
    client = StubClient({113: sample_html, 114: sample_html})
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(113, 114))

    assert len(snapshot.records) == 371
    assert snapshot.status is ParseStatus.INCOMPLETE
    assert any("合併後紀錄數不符" in warning for warning in snapshot.warnings)


def test_limit_exceeded_snapshot_is_incomplete(store, html_dir) -> None:
    """官方 6,000 筆上限不得形成完整快照。"""
    client = StubClient({113: (html_dir / "result_limit_exceeded.html").read_text(encoding="utf-8")})
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(113, 113))

    assert snapshot.status is ParseStatus.LIMIT_EXCEEDED
    assert snapshot.is_complete is False
    assert snapshot.records == ()


def test_stats_mismatch_snapshot_is_complete_with_warning(store, html_dir) -> None:
    """統計差異不阻擋下載，但狀態與警告必須保留。"""
    client = StubClient({113: (html_dir / "result_stats_mismatch.html").read_text(encoding="utf-8")})
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(113, 113))

    assert snapshot.status is ParseStatus.OK_WITH_DIFF
    assert snapshot.is_complete is True
    assert any("統計不符" in warning or "差異" in warning for warning in snapshot.warnings)


def test_all_years_no_result(store, html_dir) -> None:
    page = (html_dir / "result_noresult.html").read_text(encoding="utf-8")
    service = QueryService(StubClient({113: page, 114: page}), store)

    snapshot = service.query(QuerySpec(113, 114))

    assert snapshot.status is ParseStatus.NO_RESULT
    assert snapshot.records == ()


def test_cache_hit_avoids_new_requests(store, sample_html) -> None:
    """TTL 內重複查詢使用快照，不再派發請求。"""
    client = StubClient({113: sample_html})
    clock = StubClock()
    service = QueryService(client, store, clock=clock)

    first = service.query(QuerySpec(113, 113))
    second = service.query(QuerySpec(113, 113))

    assert first.cached is False
    assert second.cached is True
    assert second.snapshot_id == first.snapshot_id
    assert len(client.calls) == 1


def test_cache_expires_after_ttl(store, sample_html) -> None:
    client = StubClient({113: sample_html})
    clock = StubClock()
    service = QueryService(client, store, cache_ttl_seconds=3600, clock=clock)

    service.query(QuerySpec(113, 113))
    clock.advance(hours=2)
    again = service.query(QuerySpec(113, 113))

    assert again.cached is False
    assert len(client.calls) == 2


def test_force_refresh_ignores_cache(store, sample_html) -> None:
    client = StubClient({113: sample_html})
    service = QueryService(client, store)

    service.query(QuerySpec(113, 113))
    refreshed = service.query(QuerySpec(113, 113), force_refresh=True)

    assert refreshed.cached is False
    assert len(client.calls) == 2


def test_different_conditions_use_different_cache_keys(store, sample_html) -> None:
    client = StubClient({113: sample_html})
    service = QueryService(client, store)

    first = service.query(QuerySpec(113, 113))
    second = service.query(QuerySpec(113, 113, exam_code="113080"))

    assert first.cache_key != second.cache_key
    assert len(client.calls) == 2


def test_cancel_stops_dispatch(store) -> None:
    """取消後不再派發請求，且不產生快照。"""
    client = StubClient({112: synthetic_year_page(112), 113: synthetic_year_page(113)})
    service = QueryService(client, store)
    state = {"cancelled": False}

    def should_cancel() -> bool:
        return state["cancelled"]

    def on_progress(index, total, diag) -> None:
        state["cancelled"] = True

    with pytest.raises(QueryCancelled):
        service.query(QuerySpec(112, 113), on_progress=on_progress, should_cancel=should_cancel)

    assert len(client.calls) == 1
    assert store.count_snapshots() == 0


def test_progress_reports_each_year(store) -> None:
    client = StubClient({112: synthetic_year_page(112), 113: synthetic_year_page(113)})
    service = QueryService(client, store)
    seen: list[tuple[int, int, int]] = []

    service.query(QuerySpec(112, 113), on_progress=lambda i, t, d: seen.append((i, t, d.year)))

    assert seen == [(1, 2, 112), (2, 2, 113)]


def test_year_range_too_large_is_rejected(store) -> None:
    service = QueryService(StubClient({}), store)
    with pytest.raises(ValueError):
        service.query(QuerySpec(90, 90 + MAX_YEARS))


def test_invalid_year_order_is_rejected() -> None:
    with pytest.raises(ValueError):
        QuerySpec(114, 113)


# --- 第一階段關鍵字（類科／科目） --------------------------------------


def test_keywords_use_advanced_search_and_local_filter(store, sample_html) -> None:
    client = StubClient({113: sample_html})
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(113, 113, subject_keyword="國文"))

    assert client.keyword_calls == [("", "", "國文")]
    assert snapshot.records
    assert all("國文" in record.subject for record in snapshot.records)
    assert snapshot.years[0].parsed.subjects == 371, "診斷仍應記錄完整解析結果"
    assert any("本機關鍵字過濾" in warning for warning in snapshot.warnings)
    assert snapshot.status is ParseStatus.OK


def test_advanced_failure_falls_back_to_simple_query(store, sample_html) -> None:
    client = StubClient({113: sample_html}, fail_keywords=SourceError("模擬伺服器拒絕"))
    service = QueryService(client, store)

    snapshot = service.query(QuerySpec(113, 113, category_keyword="一般行政"))

    assert client.keyword_calls == [("", "一般行政", ""), None]
    assert client.resets == 1
    assert any("改用完整查詢" in warning for warning in snapshot.warnings)
    assert snapshot.records
    assert all("一般行政" in record.category for record in snapshot.records)
    assert snapshot.status is ParseStatus.OK


def test_cache_key_includes_keywords(store) -> None:
    service = QueryService(StubClient({}), store)

    assert service.cache_key(QuerySpec(113, 113, subject_keyword="國文")) != service.cache_key(
        QuerySpec(113, 113)
    )
    assert service.cache_key(
        QuerySpec(113, 113, category_keyword="一般行政")
    ) != service.cache_key(QuerySpec(113, 113, subject_keyword="國文"))
