"""下載器測試：串流、大小限制、驗證、略過、版本與啟動核對（離線）。"""

from __future__ import annotations

import hashlib
import pathlib

import httpx
import pytest

from app.engine.downloader import DownloadResult, DownloadTarget, Downloader
from app.models import (
    AssetRole,
    AttachmentStatus,
    ExamAsset,
    ExamRecord,
    QuestionType,
    TypeEvidence,
)
from app.storage.manifest import ManifestStore, ReconcileReport

URL = "https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx?t=Q&code=113010&c=501&s=0101&q=1"


class ChunkStream(httpx.SyncByteStream):
    """可控的串流回應：可模擬中斷與缺少 Content-Length。"""

    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    def __iter__(self):
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise httpx.ReadError("模擬連線中斷")
            yield chunk


class Server:
    """依序回傳預設回應，並記錄所有請求。"""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def pdf_response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=body, headers={"content-type": "application/pdf"}
    )


def make_target(asset_id: str = "a1", relative: str = "科目/試題_試題.pdf") -> DownloadTarget:
    evidence = TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="test",
        reason="測試",
        rule_version="test-v1",
    )
    record = ExamRecord(
        record_id="r1",
        official_id=None,
        year=113,
        exam_name="113年測試考試",
        exam_short_name=None,
        category="測試_一般行政",
        subject="測試科目",
        level=None,
        source_page="test",
        assets=(),
    )
    asset = ExamAsset(
        asset_id=asset_id,
        official_id=None,
        role=AssetRole.QUESTION,
        original_label="試題",
        url=URL,
        observed_at="2026-11-01T00:00:00+08:00",
        source_version=None,
        evidence=evidence,
    )
    return DownloadTarget(record=record, asset=asset, relative_path=relative)


def make_downloader(tmp_path: pathlib.Path, store: ManifestStore, server: Server, **kwargs) -> Downloader:
    return Downloader(
        root=tmp_path / "downloads",
        store=store,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(server.handler)),
        sleep=lambda seconds: None,
        max_attempts=kwargs.pop("max_attempts", 3),
        **kwargs,
    )


@pytest.fixture()
def store(tmp_path) -> ManifestStore:
    return ManifestStore(tmp_path / "manifest.db")


@pytest.fixture()
def sample_pdf(pdf_dir: pathlib.Path) -> bytes:
    return (pdf_dir / "112090_301_0305_Q.pdf").read_bytes()


@pytest.fixture()
def other_pdf(pdf_dir: pathlib.Path) -> bytes:
    return (pdf_dir / "112190_001_0301_Q.pdf").read_bytes()


def test_successful_download_archives_and_records(tmp_path, store, sample_pdf) -> None:
    server = Server([pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.COMPLETED
    assert result.sha256 == hashlib.sha256(sample_pdf).hexdigest()
    assert result.version == 1
    final = tmp_path / "downloads" / "科目" / "試題_試題.pdf"
    assert final.is_file() and final.read_bytes() == sample_pdf
    assert not list((tmp_path / "downloads" / ".parts").glob("*.part"))

    row = store.get_attachment("a1")
    assert row is not None
    assert row.status is AttachmentStatus.COMPLETED
    assert row.path == "科目/試題_試題.pdf"
    assert row.size == len(sample_pdf)
    assert row.dedupe_of is None
    assert store.attachment_versions("a1")[0]["version"] == 1


def test_second_download_skips_without_request(tmp_path, store, sample_pdf) -> None:
    server = Server([pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    downloader.download(make_target())
    again = downloader.download(make_target())

    assert again.skipped is True
    assert again.status is AttachmentStatus.SKIPPED
    assert len(server.requests) == 1


def test_identical_content_shares_one_file(tmp_path, store, sample_pdf) -> None:
    """不同網址但內容相同：只寫一份實體檔，第二筆紀錄指向同一路徑。"""
    server = Server([pdf_response(sample_pdf), pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    first = downloader.download(make_target("a1", "科目A/檔_試題.pdf"))
    second = downloader.download(make_target("a2", "科目B/檔_試題.pdf"))

    assert first.dedupe_of is None and first.shared is False
    assert second.shared is True
    assert second.dedupe_of == "a1"
    assert second.relative_path == first.relative_path == "科目A/檔_試題.pdf"

    pdfs = sorted((tmp_path / "downloads").rglob("*.pdf"))
    assert len(pdfs) == 1, "相同內容不應重複寫入"

    row = store.get_attachment("a2")
    assert row is not None
    assert row.status is AttachmentStatus.COMPLETED
    assert row.path == "科目A/檔_試題.pdf"
    assert row.dedupe_of == "a1"
    assert row.sha256 == hashlib.sha256(sample_pdf).hexdigest()
    assert not list((tmp_path / "downloads" / ".parts").glob("*.part"))


def test_shared_file_missing_falls_back_to_own_path(tmp_path, store, sample_pdf) -> None:
    """共用的檔案被刪除後，下一次下載要自己寫一份，不能指向不存在的路徑。"""
    server = Server([pdf_response(sample_pdf), pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    first = downloader.download(make_target("a1", "科目A/檔_試題.pdf"))
    assert first.relative_path is not None
    (tmp_path / "downloads" / "科目A" / "檔_試題.pdf").unlink()

    second = downloader.download(make_target("a2", "科目B/檔_試題.pdf"))

    assert second.shared is False
    assert second.relative_path == "科目B/檔_試題.pdf"
    assert (tmp_path / "downloads" / "科目B" / "檔_試題.pdf").is_file()


def test_tampered_file_is_downloaded_again(tmp_path, store, sample_pdf) -> None:
    server = Server([pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)
    downloader.download(make_target())

    final = tmp_path / "downloads" / "科目" / "試題_試題.pdf"
    final.write_bytes(b"corrupted")

    again = downloader.download(make_target())

    assert again.skipped is False
    assert again.status is AttachmentStatus.COMPLETED
    assert len(server.requests) == 2
    assert final.read_bytes() == sample_pdf


def test_size_limit_aborts_download(tmp_path, store, sample_pdf) -> None:
    server = Server([pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server, max_bytes=1024)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.FAILED
    assert result.error_code == "size-limit"
    assert not (tmp_path / "downloads" / "科目" / "試題_試題.pdf").exists()
    assert not list((tmp_path / "downloads" / ".parts").glob("*.part"))


def test_content_length_mismatch_fails(tmp_path, store, sample_pdf) -> None:
    response = httpx.Response(
        200,
        stream=ChunkStream([sample_pdf]),
        headers={
            "content-type": "application/pdf",
            "content-length": str(len(sample_pdf) + 100),
        },
    )
    server = Server([response])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.FAILED
    assert result.error_code == "length-mismatch"


def test_missing_content_length_still_validated(tmp_path, store, sample_pdf) -> None:
    chunks = [sample_pdf[:1000], sample_pdf[1000:]]
    response = httpx.Response(
        200, stream=ChunkStream(chunks), headers={"content-type": "application/pdf"}
    )
    server = Server([response])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.COMPLETED
    assert "content-length" not in response.headers


def test_interrupted_stream_retries_then_fails(tmp_path, store, sample_pdf) -> None:
    def broken() -> httpx.Response:
        return httpx.Response(
            200,
            stream=ChunkStream([sample_pdf[:300], sample_pdf[300:600]], fail_after=1),
            headers={"content-type": "application/pdf"},
        )

    server = Server([broken(), broken()])
    downloader = make_downloader(tmp_path, store, server, max_attempts=2)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.FAILED
    assert result.error_code == "transport-error"
    assert len(server.requests) == 2
    assert not list((tmp_path / "downloads" / ".parts").glob("*.part"))


def test_404_is_not_retried(tmp_path, store) -> None:
    server = Server([httpx.Response(404)])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.FAILED
    assert result.error_code == "not-found"
    assert len(server.requests) == 1


def test_403_is_not_retried(tmp_path, store) -> None:
    server = Server([httpx.Response(403)])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.error_code == "access-denied"
    assert len(server.requests) == 1


def test_500_then_success(tmp_path, store, sample_pdf) -> None:
    server = Server([httpx.Response(500), pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.COMPLETED
    assert len(server.requests) == 2


def test_blocked_host_is_rejected(tmp_path, store) -> None:
    server = Server([pdf_response(b"x")])
    downloader = make_downloader(tmp_path, store, server)
    target = make_target()
    target = DownloadTarget(
        record=target.record,
        asset=ExamAsset(
            asset_id="a1",
            official_id=None,
            role=AssetRole.QUESTION,
            original_label="試題",
            url="https://evil.example.com/file.pdf",
            observed_at="2026-11-01T00:00:00+08:00",
            source_version=None,
            evidence=target.asset.evidence,
        ),
        relative_path=target.relative_path,
    )

    result = downloader.download(target)

    assert result.error_code == "blocked-host"
    assert server.requests == []


def test_cancel_stops_stream_and_keeps_no_file(tmp_path, store, sample_pdf) -> None:
    chunks = [sample_pdf[:1000], sample_pdf[1000:2000], sample_pdf[2000:]]
    response = httpx.Response(
        200, stream=ChunkStream(chunks), headers={"content-type": "application/pdf"}
    )
    server = Server([response])
    downloader = make_downloader(tmp_path, store, server)
    calls = {"count": 0}

    def should_cancel() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    result = downloader.download(make_target(), should_cancel=should_cancel)

    assert result.status is AttachmentStatus.CANCELLED
    assert store.get_attachment("a1").status is AttachmentStatus.CANCELLED
    assert not (tmp_path / "downloads" / "科目" / "試題_試題.pdf").exists()


def test_html_disguised_as_pdf_is_invalid(tmp_path, store) -> None:
    server = Server([pdf_response("<html>請先登入</html>".encode())])
    downloader = make_downloader(tmp_path, store, server)

    result = downloader.download(make_target())

    assert result.status is AttachmentStatus.FAILED
    assert result.error_code == "invalid-pdf"
    assert not (tmp_path / "downloads" / "科目" / "試題_試題.pdf").exists()


def test_version_update_keeps_previous_file(tmp_path, store, sample_pdf, other_pdf) -> None:
    server = Server([pdf_response(sample_pdf), pdf_response(other_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    first = downloader.download(make_target())
    # 內容有更新時須以「檢查官方更新」強制重新下載比對雜湊
    second = downloader.download(make_target(), force=True)

    assert first.version == 1
    assert second.version == 2
    assert second.relative_path != first.relative_path
    assert second.relative_path.endswith("_試題.pdf")
    assert hashlib.sha256(other_pdf).hexdigest()[:8] in second.relative_path

    first_path = tmp_path / "downloads" / first.relative_path
    second_path = tmp_path / "downloads" / second.relative_path
    assert first_path.read_bytes() == sample_pdf
    assert second_path.read_bytes() == other_pdf
    assert len(store.attachment_versions("a1")) == 2


def test_reconcile_adopts_archived_file(tmp_path, store, sample_pdf) -> None:
    """歸檔成功但狀態未提交：重啟核對後應補記完成。"""
    root = tmp_path / "downloads"
    final = root / "科目" / "試題_試題.pdf"
    final.parent.mkdir(parents=True)
    final.write_bytes(sample_pdf)
    digest = hashlib.sha256(sample_pdf).hexdigest()

    store.mark_attachment_started(
        asset_id="a1", snapshot_id="s1", record_id="r1", url=URL, role="試題"
    )
    store.begin_attachment_verify(
        asset_id="a1", path="科目/試題_試題.pdf", sha256=digest, size=len(sample_pdf), version=1
    )

    report: ReconcileReport = store.reconcile(root)

    assert report.adopted == ("a1",)
    row = store.get_attachment("a1")
    assert row.status is AttachmentStatus.COMPLETED
    assert row.sha256 == digest


def test_reconcile_marks_interrupted_and_clears_parts(tmp_path, store) -> None:
    root = tmp_path / "downloads"
    (root / ".parts").mkdir(parents=True)
    (root / ".parts" / "dl_deadbeef.part").write_bytes(b"partial")
    store.mark_attachment_started(
        asset_id="a2", snapshot_id="s1", record_id="r1", url=URL, role="試題"
    )

    report = store.reconcile(root)

    assert report.interrupted == ("a2",)
    assert report.removed_parts == ("dl_deadbeef.part",)
    assert store.get_attachment("a2").status is AttachmentStatus.FAILED


def test_reconcile_reports_untracked_pdf(tmp_path, store) -> None:
    root = tmp_path / "downloads"
    (root / "手動放的").mkdir(parents=True)
    (root / "手動放的" / "陌生檔案.pdf").write_bytes(b"%PDF-1.4 fake")

    report = store.reconcile(root)

    assert report.untracked_pdfs == ("手動放的/陌生檔案.pdf",)


def seed_completed(
    store: ManifestStore,
    root: pathlib.Path,
    *,
    asset_id: str,
    relative: str,
    content: bytes,
    version: int = 1,
    dedupe_of: str | None = None,
) -> str:
    """建立已完成附件與實體檔，供搬移測試使用。"""
    digest = hashlib.sha256(content).hexdigest()
    store.mark_attachment_started(
        asset_id=asset_id, snapshot_id="s1", record_id="r1", url=URL, role="試題"
    )
    store.begin_attachment_verify(
        asset_id=asset_id, path=relative, sha256=digest, size=len(content), version=version
    )
    store.complete_attachment(
        asset_id=asset_id,
        path=relative,
        sha256=digest,
        size=len(content),
        version=version,
        question_type="測驗題",
        dedupe_of=dedupe_of,
    )
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return digest


def relocate_target(asset_id: str, desired: str) -> DownloadTarget:
    base = make_target(asset_id, desired)
    return DownloadTarget(
        record=base.record,
        asset=base.asset,
        relative_path=desired,
        resolve_path=lambda inspection: desired,
    )


def test_relocate_moves_file_and_removes_empty_old_dirs(tmp_path, store, sample_pdf) -> None:
    root = tmp_path / "downloads"
    old_rel = "科目/檔_試題.pdf"
    seed_completed(store, root, asset_id="a1", relative=old_rel, content=sample_pdf)
    server = Server([pdf_response(sample_pdf)])
    downloader = make_downloader(tmp_path, store, server)

    new_rel = "113年/考試_類科/檔_試題.pdf"
    result = downloader.download(relocate_target("a1", new_rel))

    assert result.relocated is True
    assert result.skipped is True
    assert result.relative_path == new_rel
    assert (root / new_rel).read_bytes() == sample_pdf
    assert not (root / old_rel).exists()
    assert not (root / "科目").exists()
    assert store.get_attachment("a1").path == new_rel
    assert store.attachment_versions("a1")[-1]["path"] == new_rel
    assert server.requests == []


def test_relocate_collision_does_not_overwrite_different_content(
    tmp_path, store, sample_pdf, other_pdf
) -> None:
    root = tmp_path / "downloads"
    old_rel = "A/檔_試題.pdf"
    new_rel = "B/檔_試題.pdf"
    seed_completed(store, root, asset_id="a1", relative=old_rel, content=sample_pdf)
    (root / new_rel).parent.mkdir(parents=True)
    (root / new_rel).write_bytes(other_pdf)
    downloader = make_downloader(tmp_path, store, Server([pdf_response(sample_pdf)]))

    result = downloader.download(relocate_target("a1", new_rel))

    assert result.relocated is True
    assert result.relative_path != new_rel
    assert hashlib.sha256(sample_pdf).hexdigest()[:8] in result.relative_path
    assert (root / new_rel).read_bytes() == other_pdf
    assert not (root / old_rel).exists()
    assert store.get_attachment("a1").path == result.relative_path


def test_relocate_preserves_version_hash(tmp_path, store, sample_pdf) -> None:
    root = tmp_path / "downloads"
    old_rel = "A/檔_試題.pdf"
    seed_completed(
        store, root, asset_id="a1", relative=old_rel, content=sample_pdf, version=2
    )
    downloader = make_downloader(tmp_path, store, Server([pdf_response(sample_pdf)]))

    new_rel = "B/檔_試題.pdf"
    result = downloader.download(relocate_target("a1", new_rel))

    assert result.relocated is True
    assert hashlib.sha256(sample_pdf).hexdigest()[:8] in result.relative_path
    assert not (root / old_rel).exists()


def test_relocate_only_owner_moves_shared_file(tmp_path, store, sample_pdf) -> None:
    root = tmp_path / "downloads"
    old_rel = "A/shared.pdf"
    digest = seed_completed(
        store, root, asset_id="a1", relative=old_rel, content=sample_pdf
    )
    seed_completed(
        store,
        root,
        asset_id="a2",
        relative=old_rel,
        content=sample_pdf,
        dedupe_of="a1",
    )
    downloader = make_downloader(tmp_path, store, Server([pdf_response(sample_pdf)]))

    non_owner = downloader.download(relocate_target("a2", "B/a2.pdf"))
    assert non_owner.skipped is True
    assert non_owner.relocated is False
    assert (root / old_rel).exists()

    owner = downloader.download(relocate_target("a1", "C/a1.pdf"))
    assert owner.relocated is True
    assert not (root / old_rel).exists()
    assert (root / "C/a1.pdf").exists()
    assert store.get_attachment("a1").path == "C/a1.pdf"
    assert store.get_attachment("a2").path == "C/a1.pdf"
    assert store.get_attachment("a2").sha256 == digest


def test_cleanup_untracked_duplicates_removes_only_identical(
    tmp_path, store, sample_pdf, other_pdf
) -> None:
    root = tmp_path / "downloads"
    seed_completed(store, root, asset_id="a1", relative="A/keep.pdf", content=sample_pdf)
    duplicate = root / "Dup" / "dup.pdf"
    different = root / "Other" / "other.pdf"
    duplicate.parent.mkdir(parents=True)
    different.parent.mkdir(parents=True)
    duplicate.write_bytes(sample_pdf)
    different.write_bytes(other_pdf)
    downloader = make_downloader(tmp_path, store, Server([pdf_response(sample_pdf)]))

    untracked = store.list_untracked_pdfs(root)
    removed = downloader.cleanup_untracked_duplicates(untracked)

    assert removed == ("Dup/dup.pdf",)
    assert not duplicate.exists()
    assert not duplicate.parent.exists()
    assert different.is_file()
    assert store.list_untracked_pdfs(root) == ("Other/other.pdf",)


def test_cleanup_keeps_duplicate_when_tracked_copy_is_missing(
    tmp_path, store, sample_pdf
) -> None:
    root = tmp_path / "downloads"
    seed_completed(store, root, asset_id="a1", relative="A/lost.pdf", content=sample_pdf)
    (root / "A/lost.pdf").unlink()
    duplicate = root / "Dup" / "dup.pdf"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(sample_pdf)
    downloader = make_downloader(tmp_path, store, Server([pdf_response(sample_pdf)]))

    removed = downloader.cleanup_untracked_duplicates(
        store.list_untracked_pdfs(root)
    )

    assert removed == ()
    assert duplicate.is_file()


def test_share_existing_writes_dedupe_without_network_request(
    tmp_path, store, sample_pdf
) -> None:
    root = tmp_path / "downloads"
    digest = seed_completed(
        store, root, asset_id="primary-q1", relative="A/115_試題.pdf", content=sample_pdf
    )
    server = Server([])
    downloader = make_downloader(tmp_path, store, server)

    follower = make_target("follower-q1", "B/115_另一類科_試題.pdf")
    result = downloader.share_existing(follower, source_asset_id="primary-q1")

    assert result.status is AttachmentStatus.COMPLETED
    assert result.shared_by_paper is True
    assert result.dedupe_of == "primary-q1"
    assert result.relative_path == "A/115_試題.pdf"
    assert result.sha256 == digest
    assert server.requests == []

    row = store.get_attachment("follower-q1")
    assert row is not None
    assert row.status is AttachmentStatus.COMPLETED
    assert row.path == "A/115_試題.pdf"
    assert row.sha256 == digest
    assert row.dedupe_of == "primary-q1"


def test_share_existing_fails_when_source_file_is_missing_or_corrupted(
    tmp_path, store, sample_pdf
) -> None:
    root = tmp_path / "downloads"
    seed_completed(
        store, root, asset_id="primary-q1", relative="A/115_試題.pdf", content=sample_pdf
    )
    (root / "A/115_試題.pdf").unlink()
    server = Server([])
    downloader = make_downloader(tmp_path, store, server)

    follower = make_target("follower-q1", "B/115_另一類科_試題.pdf")
    result = downloader.share_existing(follower, source_asset_id="primary-q1")

    assert result.status is AttachmentStatus.FAILED
    assert result.error_code == "shared-source-missing"
    row = store.get_attachment("follower-q1")
    assert row is not None


def test_share_existing_fast_path_with_source_result(
    tmp_path, store, sample_pdf
) -> None:
    """傳入 source_result 時走快速路徑：不需重新從磁碟讀取校驗 SHA-256。"""
    server = Server([])
    downloader = make_downloader(tmp_path, store, server)

    source_result = DownloadResult(
        asset_id="primary-q1",
        status=AttachmentStatus.COMPLETED,
        relative_path="A/115_試題.pdf",
        size_bytes=len(sample_pdf),
        sha256=hashlib.sha256(sample_pdf).hexdigest(),
        version=1,
    )

    follower = make_target("follower-q1", "B/115_另一類科_試題.pdf")
    result = downloader.share_existing(
        follower, source_asset_id="primary-q1", source_result=source_result
    )

    assert result.status is AttachmentStatus.COMPLETED
    assert result.shared_by_paper is True
    assert result.dedupe_of == "primary-q1"
    assert result.relative_path == "A/115_試題.pdf"
    assert result.sha256 == source_result.sha256

    row = store.get_attachment("follower-q1")
    assert row is not None
    assert row.status is AttachmentStatus.COMPLETED
    assert row.path == "A/115_試題.pdf"
    assert row.dedupe_of == "primary-q1"
