"""查詢服務：年度批次查詢、合併去重、完整性判定、快取與取消。

邊界：協調 client（HTTP）與 manifest（持久化），不處理 UI。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Protocol

from app.crawler.moex_client import SourceError
from app.crawler.parser import RULE_VERSION, parse_result_page
from app.engine.filter import records_matching_keywords
from app.models import (
    SOURCE_PAGE,
    ExamRecord,
    OfficialCounts,
    ParseStatus,
    ParsedCounts,
    QuerySnapshot,
    QuerySpec,
    YearDiagnostics,
    stable_hash,
)

#: 單次查詢允許的最大年度數（安全上限，避免無界限抓取）
MAX_YEARS = 20
#: 快取 TTL 預設 24 小時
DEFAULT_CACHE_TTL_SECONDS = 24 * 3600


class QueryCancelled(Exception):
    """使用者取消查詢；呼叫端不應把部分結果當成完整快照。"""

    def __init__(self, message: str = "查詢已取消") -> None:
        super().__init__(message)


class SearchClient(Protocol):
    """QueryService 需要的客戶端介面（便於以模擬物件測試）。"""

    def search(  # pragma: no cover - 介面
        self,
        *,
        year_roc: int,
        exam_code: str = "",
        keywords: tuple[str, str, str] | None = None,
    ) -> str:
        ...


class SnapshotStore(Protocol):
    """QueryService 需要的儲存介面。"""

    def find_fresh(  # pragma: no cover - 介面
        self, cache_key: str, *, ttl_seconds: float, now: datetime | None = None
    ) -> QuerySnapshot | None:
        ...

    def save_snapshot(self, snapshot: QuerySnapshot) -> None:  # pragma: no cover - 介面
        ...


ProgressCallback = Callable[[int, int, YearDiagnostics], None]
CancelCheck = Callable[[], bool]


def _now(clock: Callable[[], datetime] | None) -> datetime:
    if clock is not None:
        return clock()
    return datetime.now(timezone.utc)


class QueryService:
    """以年度為批次單位執行查詢並產生可驗證的凍結快照。"""

    def __init__(
        self,
        client: SearchClient,
        store: SnapshotStore,
        *,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        parser_version: str = RULE_VERSION,
        source_url: str = SOURCE_PAGE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._ttl = cache_ttl_seconds
        self._parser_version = parser_version
        self._source_url = source_url
        self._clock = clock

    def cache_key(self, spec: QuerySpec) -> str:
        """快取鍵包含條件、來源與解析器版本（SPEC §3.2）。"""
        payload = "|".join([spec.cache_payload(), self._source_url, self._parser_version])
        return stable_hash(payload, 32)

    def query(
        self,
        spec: QuerySpec,
        *,
        force_refresh: bool = False,
        on_progress: ProgressCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> QuerySnapshot:
        """執行查詢；完整與未完整快照會明確區分。"""
        years = spec.years
        if len(years) > MAX_YEARS:
            raise ValueError(f"年度範圍過大（{len(years)} 年），單次最多 {MAX_YEARS} 年")

        key = self.cache_key(spec)
        if not force_refresh:
            cached = self._store.find_fresh(key, ttl_seconds=self._ttl, now=_now(self._clock))
            if cached is not None:
                if on_progress is not None:
                    for index, diag in enumerate(cached.years, start=1):
                        on_progress(index, len(cached.years), diag)
                return cached

        records: dict[str, ExamRecord] = {}
        diagnostics: list[YearDiagnostics] = []
        warnings: list[str] = []
        expected_records = 0

        keywords = (
            spec.exam_name_keyword.strip(),
            spec.category_keyword.strip(),
            spec.subject_keyword.strip(),
        )
        use_keywords = any(keywords)
        advanced = use_keywords

        for index, year in enumerate(years, start=1):
            if should_cancel is not None and should_cancel():
                raise QueryCancelled()

            try:
                html = self._client.search(
                    year_roc=year,
                    exam_code=spec.exam_code,
                    keywords=keywords if advanced else None,
                )
            except SourceError as exc:
                # 進階查詢在第一批就失敗時，整批改用簡易查詢＋本機過濾，
                # 避免同一份快照混用兩種查詢範圍。
                if advanced and index == 1:
                    advanced = False
                    reset = getattr(self._client, "reset", None)
                    if callable(reset):
                        reset()
                    warnings.append(
                        f"伺服端關鍵字查詢失敗（{exc}），已改用完整查詢並在本機過濾"
                    )
                    html = self._client.search(year_roc=year, exam_code=spec.exam_code)
                else:
                    raise

            page = parse_result_page(html, source_url=self._source_url)

            diagnostics.append(
                YearDiagnostics(
                    year=year,
                    status=page.status,
                    official=page.official_counts,
                    parsed=page.parsed_counts,
                    stats_text=page.stats_text,
                    warnings=page.warnings,
                )
            )
            warnings.extend(f"{year} 年：{warning}" for warning in page.warnings)

            page_records = page.records
            if use_keywords:
                filtered = records_matching_keywords(
                    page_records,
                    exam_name=keywords[0],
                    category=keywords[1],
                    subject=keywords[2],
                )
                if len(filtered) != len(page_records):
                    warnings.append(
                        f"{year} 年：本機關鍵字過濾 {len(page_records)} → {len(filtered)} 筆"
                    )
                page_records = filtered

            for record in page_records:
                existing = records.get(record.record_id)
                if existing is None:
                    records[record.record_id] = record
                    continue
                # 跨年度或重複批次出現同一紀錄時合併附件，不重複計數
                known = {asset.url for asset in existing.assets}
                merged = existing.assets + tuple(
                    asset for asset in record.assets if asset.url not in known
                )
                records[record.record_id] = replace(existing, assets=merged)

            expected_records += len(page_records)

            if on_progress is not None:
                on_progress(index, len(years), diagnostics[-1])

            if should_cancel is not None and should_cancel():
                raise QueryCancelled()

        status, totals_official, totals_parsed, extra_warnings = self._evaluate(
            diagnostics, records, expected_records
        )
        warnings.extend(extra_warnings)

        acquired_at = _now(self._clock).isoformat(timespec="seconds")
        snapshot = QuerySnapshot(
            snapshot_id=f"snap-{stable_hash(key + acquired_at + str(len(records)), 20)}",
            cache_key=key,
            spec=spec,
            records=tuple(records.values()),
            status=status,
            years=tuple(diagnostics),
            acquired_at=acquired_at,
            parser_version=self._parser_version,
            source_url=self._source_url,
            warnings=tuple(warnings),
            cached=False,
        )
        self._store.save_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _evaluate(
        diagnostics: list[YearDiagnostics],
        records: dict[str, ExamRecord],
        expected_records: int,
    ) -> tuple[ParseStatus, OfficialCounts | None, ParsedCounts, list[str]]:
        """彙總各年度狀態，決定整份快照是否完整。

        兩層檢查：
        1. 官方統計 vs 各年度解析結果（來源層級，差異屬「完整（統計差異）」）。
        2. 合併後紀錄數 vs 各批次總和（工具層級，不符代表重複批次，屬「未完整」）。
        """
        messages: list[str] = []
        official_exams = official_categories = official_subjects = 0
        parsed_exams = parsed_categories = parsed_subjects = 0
        has_official = False
        duplicate_rows = 0

        for diag in diagnostics:
            if diag.official is not None:
                has_official = True
                official_exams += diag.official.exams
                official_categories += diag.official.categories
                official_subjects += diag.official.subjects
            parsed_exams += diag.parsed.exams
            parsed_categories += diag.parsed.categories
            parsed_subjects += diag.parsed.subjects
            duplicate_rows += diag.parsed.duplicate_rows

        totals_official = (
            OfficialCounts(official_exams, official_categories, official_subjects)
            if has_official
            else None
        )
        # 來源統計核對用「解析結果」而非「合併後紀錄」；兩者是不同層級的檢查
        totals_parsed = ParsedCounts(
            exams=parsed_exams,
            categories=parsed_categories,
            subjects=parsed_subjects,
            duplicate_rows=duplicate_rows,
        )

        statuses = {diag.status for diag in diagnostics}
        if ParseStatus.LIMIT_EXCEEDED in statuses:
            status = ParseStatus.LIMIT_EXCEEDED
            messages.append("部分年度超過官方 6,000 筆上限，請縮小年度範圍或條件")
        elif statuses & {ParseStatus.ERROR_PAGE, ParseStatus.STRUCTURE_CHANGED, ParseStatus.INCOMPLETE}:
            status = ParseStatus.INCOMPLETE
            failed = sorted(diag.year for diag in diagnostics if diag.status is not ParseStatus.OK)
            messages.append(f"部分年度未完整：{failed}")
        elif statuses == {ParseStatus.NO_RESULT}:
            status = ParseStatus.NO_RESULT
        elif ParseStatus.OK_WITH_DIFF in statuses:
            status = ParseStatus.OK_WITH_DIFF
            messages.append("部分年度的官方統計與實際列數不一致，已保留兩組數字")
        else:
            status = ParseStatus.OK

        if status in (ParseStatus.OK, ParseStatus.OK_WITH_DIFF) and totals_official is not None:
            if (
                totals_official.exams != totals_parsed.exams
                or totals_official.categories != totals_parsed.categories
                or totals_official.subjects != totals_parsed.subjects
            ):
                status = ParseStatus.OK_WITH_DIFF
                messages.append(
                    "合計統計差異：官方 "
                    f"{totals_official.exams}/{totals_official.categories}/{totals_official.subjects}"
                    "；解析 "
                    f"{totals_parsed.exams}/{totals_parsed.categories}/{totals_parsed.subjects}"
                )

        if status in (ParseStatus.OK, ParseStatus.OK_WITH_DIFF) and len(records) != expected_records:
            # 合併後筆數與各批次總和不同：代表跨批次去重或重複批次，屬工具端異常
            status = ParseStatus.INCOMPLETE
            messages.append(
                f"合併後紀錄數不符（可能有重複批次）：預期 {expected_records}，實際 {len(records)}"
            )

        return status, totals_official, totals_parsed, messages
