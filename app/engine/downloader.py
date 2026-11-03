"""附件下載：串流暫存、驗證、原子歸檔、雜湊與版本紀錄。

邊界（SPEC §6）：
- 先寫入根目錄同檔案系統內的 `.parts/<id>.part`，成功驗證後才嗅探與原子更名。
- 只有 manifest 已完成且本機大小、SHA-256 相符才算「已略過」。
- 既有但未追蹤的檔案不盲目採用；內容不同時保留舊版，新檔名帶內容雜湊。
- 內容更新或檔案損壞時重新下載；失敗保留其他已完成的附件。
"""

from __future__ import annotations

import hashlib
import os
import time
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from app.crawler.rate_limiter import RateLimiter
from app.engine.path_builder import FILENAME_TYPE_TOKENS, ANSWER_TOKEN, ensure_within_root
from app.engine.pdf_inspector import DEFAULT_TIMEOUT_SECONDS, inspect_pdf
from app.models import AttachmentStatus, ExamAsset, ExamRecord, PdfInspection, PdfStatus
from app.storage.manifest import PARTS_DIRNAME, ManifestStore

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_ALLOWED_HOSTS = ("wwwq.moex.gov.tw", "www.moex.gov.tw")
TYPE_TOKENS = tuple(FILENAME_TYPE_TOKENS.values()) + (ANSWER_TOKEN,)


class DownloadError(Exception):
    """下載失敗；code 為穩定錯誤碼，message 為繁中說明。"""

    code = "download-error"
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SizeLimitExceeded(DownloadError):
    code = "size-limit"


class LengthMismatch(DownloadError):
    code = "length-mismatch"


class InvalidPdf(DownloadError):
    code = "invalid-pdf"


class NotFound(DownloadError):
    code = "not-found"


class AccessDenied(DownloadError):
    code = "access-denied"


class BlockedHost(DownloadError):
    code = "blocked-host"


class Cancelled(DownloadError):
    code = "cancelled"


class TransientError(DownloadError):
    code = "transport-error"
    retryable = True


class ServerError(DownloadError):
    code = "server-error"
    retryable = True


@dataclass(frozen=True)
class DownloadTarget:
    """單一附件的下載目標。

    relative_path 為預估路徑；若提供 resolve_path，會在嗅探後以實際題型重算檔名。
    """

    record: ExamRecord
    asset: ExamAsset
    relative_path: str
    resolve_path: Callable[[PdfInspection], str] | None = None


@dataclass(frozen=True)
class DownloadResult:
    """單一下載結果。

    skipped 代表依 manifest 判定為已完成而省略；dedupe_of 代表內容與另一個附件
    完全相同，因此共用同一個實體檔（沒有另外寫入）。
    """

    asset_id: str
    status: AttachmentStatus
    relative_path: str | None = None
    size_bytes: int = 0
    sha256: str | None = None
    version: int = 1
    skipped: bool = False
    relocated: bool = False
    dedupe_of: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status in (AttachmentStatus.COMPLETED, AttachmentStatus.SKIPPED)

    @property
    def shared(self) -> bool:
        return self.dedupe_of is not None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path.replace("\\", "/")).casefold()


class Downloader:
    """單一附件下載器；可被多執行緒呼叫，不共用可變的 HTTP 狀態。"""

    def __init__(
        self,
        *,
        root: str | Path,
        store: ManifestStore,
        client_factory: Callable[[], httpx.Client] | None = None,
        snapshot_id: str = "",
        allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        inspector: Callable[..., PdfInspection] = inspect_pdf,
        sniff_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout: httpx.Timeout | float = httpx.Timeout(10.0, read=30.0),
        sleep: Callable[[float], None] = time.sleep,
        path_resolver: Callable[[DownloadTarget, PdfInspection], str] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._parts_dir = self._root / PARTS_DIRNAME
        self._parts_dir.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._client_factory = client_factory or (
            lambda: httpx.Client(follow_redirects=False, timeout=timeout)
        )
        self._snapshot_id = snapshot_id
        self._allowed_hosts = tuple(host.lower() for host in allowed_hosts)
        self._max_bytes = max_bytes
        self._inspector = inspector
        self._sniff_timeout = sniff_timeout
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._path_resolver = path_resolver
        self._rate_limiter = rate_limiter

    # --- 對外 API -----------------------------------------------------
    def download(
        self,
        target: DownloadTarget,
        *,
        should_cancel: Callable[[], bool] | None = None,
        force: bool = False,
    ) -> DownloadResult:
        """下載單一附件；失敗時回傳結果並記錄原因，不拋例外。

        force=True 供「檢查官方更新」使用：即使 manifest 已完成也會重新下載比對雜湊。
        """
        asset = target.asset
        existing = self._store.get_attachment(asset.asset_id)
        if not force and existing is not None and existing.status in (
            AttachmentStatus.COMPLETED,
            AttachmentStatus.SKIPPED,
        ):
            if self._is_intact(existing):
                try:
                    return self._relocate_existing(target, existing)
                except OSError as exc:
                    message = (
                        f"搬移既有檔案失敗：{exc}。"
                        "檔案可能被其他程式占用，請關閉後重新整理或重新下載"
                    )
                    return DownloadResult(
                        asset_id=asset.asset_id,
                        status=AttachmentStatus.FAILED,
                        error_code="io-error",
                        error_message=message,
                    )

        if should_cancel is not None and should_cancel():
            return self._cancelled(target, "已取消，未開始下載")

        self._store.mark_attachment_started(
            asset_id=asset.asset_id,
            snapshot_id=self._snapshot_id,
            record_id=target.record.record_id,
            url=asset.url,
            role=asset.role.value,
        )

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._attempt(target, should_cancel)
            except Cancelled as exc:
                return self._cancelled(target, exc.message)
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                error: DownloadError = TransientError(f"連線中斷：{type(exc).__name__}")
                if attempt < self._max_attempts:
                    self._sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                self._store.fail_attachment(
                    asset_id=asset.asset_id,
                    error_code=error.code,
                    error_message=error.message,
                )
                return DownloadResult(
                    asset_id=asset.asset_id,
                    status=AttachmentStatus.FAILED,
                    error_code=error.code,
                    error_message=error.message,
                )
            except DownloadError as exc:
                if exc.retryable and attempt < self._max_attempts:
                    self._sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                self._store.fail_attachment(
                    asset_id=asset.asset_id,
                    error_code=exc.code,
                    error_message=exc.message,
                )
                return DownloadResult(
                    asset_id=asset.asset_id,
                    status=AttachmentStatus.FAILED,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            except OSError as exc:  # 磁碟或權限問題：不當成可重試網路錯誤
                message = f"寫入檔案失敗：{exc}"
                self._store.fail_attachment(
                    asset_id=asset.asset_id, error_code="io-error", error_message=message
                )
                return DownloadResult(
                    asset_id=asset.asset_id,
                    status=AttachmentStatus.FAILED,
                    error_code="io-error",
                    error_message=message,
                )

    # --- 既有檔案搬移 -------------------------------------------------
    def relocate(
        self, target: DownloadTarget, *, desired_relative: str | None = None
    ) -> DownloadResult:
        """只搬移已完成的檔案，不發出網路請求。"""
        asset = target.asset
        existing = self._store.get_attachment(asset.asset_id)
        if (
            existing is None
            or existing.status not in (AttachmentStatus.COMPLETED, AttachmentStatus.SKIPPED)
            or not self._is_intact(existing)
        ):
            return DownloadResult(
                asset_id=asset.asset_id,
                status=AttachmentStatus.FAILED,
                error_code="not-relocatable",
                error_message="附件尚未完成或檔案已遺失，需重新下載",
            )
        try:
            return self._relocate_existing(
                target, existing, desired_relative=desired_relative
            )
        except OSError as exc:
            return DownloadResult(
                asset_id=asset.asset_id,
                status=AttachmentStatus.FAILED,
                error_code="io-error",
                error_message=(
                    f"搬移既有檔案失敗：{exc}。"
                    "檔案可能被其他程式占用，請關閉後重新整理或重新下載"
                ),
            )

    def cleanup_untracked_duplicates(
        self, relative_paths: tuple[str, ...] | list[str]
    ) -> tuple[str, ...]:
        """刪除內容與已追蹤檔案完全相同、但已不再被追蹤的舊 PDF。"""
        tracked = {
            row.sha256
            for row in self._store.list_attachments()
            if row.sha256 and self._is_intact(row)
        }
        removed: list[str] = []
        for relative in relative_paths:
            path = self._root.joinpath(*relative.split("/"))
            try:
                ensure_within_root(self._root, path)
            except Exception:
                continue
            if not path.is_file():
                continue
            try:
                digest = _sha256_file(path)
            except OSError:
                continue
            if digest not in tracked:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            self._remove_empty_parents(path.parent)
            removed.append(relative)
        return tuple(removed)

    def _relocate_existing(
        self,
        target: DownloadTarget,
        existing,
        *,
        desired_relative: str | None = None,
    ) -> DownloadResult:
        """依目前模式搬移已完成的檔案；不重新下載。

        模式變更後若只複製到新路徑，舊檔與舊資料夾會永久殘留；因此這裡採
        「同檔案系統原子搬移→更新 manifest→清除空目錄」。
        """
        asset = target.asset
        old_relative = existing.path
        if not old_relative:
            raise OSError("manifest 缺少既有檔案路徑")
        old_file = self._root.joinpath(*old_relative.split("/"))
        if desired_relative is None:
            inspection = (
                self._inspector(old_file) if target.resolve_path is not None else None
            )
            desired_relative = (
                target.resolve_path(inspection)
                if target.resolve_path is not None and inspection is not None
                else target.relative_path
            )

        if _path_key(desired_relative) == _path_key(old_relative):
            return DownloadResult(
                asset_id=asset.asset_id,
                status=AttachmentStatus.SKIPPED,
                relative_path=old_relative,
                size_bytes=existing.size or 0,
                sha256=existing.sha256,
                version=existing.version,
                skipped=True,
            )

        # 共用檔只由 dedupe 擁有者搬移；其他附件仍指向同一個實體檔。
        sharers = self._store.list_attachments_at_path(
            old_relative, exclude_asset_id=asset.asset_id
        )
        if sharers and (
            existing.dedupe_of is not None
            or any(row.asset_id < asset.asset_id for row in sharers)
        ):
            return DownloadResult(
                asset_id=asset.asset_id,
                status=AttachmentStatus.SKIPPED,
                relative_path=old_relative,
                size_bytes=existing.size or 0,
                sha256=existing.sha256,
                version=existing.version,
                skipped=True,
            )

        if existing.version > 1 and existing.sha256:
            desired_relative = _versioned_relative(desired_relative, existing.sha256)
        desired_relative = self._unique_relocate_target(
            desired_relative, existing.sha256 or ""
        )
        new_file = self._root.joinpath(*desired_relative.split("/"))
        ensure_within_root(self._root, new_file)
        new_file.parent.mkdir(parents=True, exist_ok=True)

        self._replace_with_retry(old_file, new_file)
        self._remove_empty_parents(old_file.parent)
        self._store.relocate_attachments(
            old_path=old_relative,
            new_path=desired_relative,
            sha256=existing.sha256,
        )

        return DownloadResult(
            asset_id=asset.asset_id,
            status=AttachmentStatus.SKIPPED,
            relative_path=desired_relative,
            size_bytes=existing.size or 0,
            sha256=existing.sha256,
            version=existing.version,
            skipped=True,
            relocated=True,
        )

    def _unique_relocate_target(self, desired_relative: str, sha256: str) -> str:
        """避免搬移時覆寫不同內容；相同內容則直接沿用。"""
        candidate = desired_relative
        for index in range(0, 100):
            path = self._root.joinpath(*candidate.split("/"))
            if not path.exists():
                return candidate
            if sha256 and _sha256_file(path) == sha256:
                return candidate
            if index == 0:
                candidate = _versioned_relative(desired_relative, sha256)
            else:
                stem, suffix = candidate[:-4], candidate[-4:]
                marker = f"_{index + 1}"
                if marker not in stem[-8:]:
                    candidate = f"{stem}{marker}{suffix}"
        raise OSError("無法為既有檔案找到不衝突的目標檔名")

    def _replace_with_retry(self, old_file: Path, new_file: Path) -> None:
        """Windows 上檔案可能被檢視器或同步程式短暫鎖住。"""
        last_error: OSError | None = None
        for attempt in range(3):
            try:
                os.replace(old_file, new_file)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt < 2:
                    self._sleep(0.2)
        if last_error is not None:
            raise last_error

    def _remove_empty_parents(self, start: Path) -> None:
        """清除搬移後變空的舊資料夾，不越過下載根目錄。"""
        current = start
        while current != self._root and self._root in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    # --- 單次嘗試 -----------------------------------------------------
    def _attempt(
        self, target: DownloadTarget, should_cancel: Callable[[], bool] | None
    ) -> DownloadResult:
        asset = target.asset
        self._validate_url(asset.url)
        part_path = self._parts_dir / f"{_part_name(asset.asset_id)}.part"
        digest = hashlib.sha256()
        written = 0

        try:
            limiter_slot = self._rate_limiter.slot() if self._rate_limiter else nullcontext()
            with limiter_slot:
                with self._client_factory() as client:
                    with client.stream("GET", asset.url) as response:
                        self._raise_for_status(response)
                        declared = _declared_length(response)
                        if declared is not None and declared > self._max_bytes:
                            raise SizeLimitExceeded(
                                f"附件超過大小上限（{declared} > {self._max_bytes} bytes）"
                            )

                        with part_path.open("wb") as handle:
                            for chunk in response.iter_bytes(CHUNK_SIZE):
                                if should_cancel is not None and should_cancel():
                                    raise Cancelled("下載中取消")
                                written += len(chunk)
                                if written > self._max_bytes:
                                    raise SizeLimitExceeded(
                                        f"附件超過大小上限（已接收 {written} bytes，上限 {self._max_bytes}）"
                                    )
                                digest.update(chunk)
                                handle.write(chunk)

                        if declared is not None and written != declared:
                            raise LengthMismatch(
                                f"傳輸長度不符（宣告 {declared} bytes，實際 {written} bytes）"
                            )
                        etag = response.headers.get("etag")
                        last_modified = response.headers.get("last-modified")

            if written == 0:
                raise InvalidPdf("下載內容為空")

            inspection = self._inspector(part_path, timeout=self._sniff_timeout)
            if inspection.status is PdfStatus.INVALID:
                raise InvalidPdf(
                    f"附件不是有效的 PDF（{inspection.reason_code}）"
                )

            sha256 = digest.hexdigest()

            shared = self._find_shared(sha256, asset.asset_id)
            if shared is not None:
                share_path, share_asset_id = shared
                self._store.begin_attachment_verify(
                    asset_id=asset.asset_id,
                    path=share_path,
                    sha256=sha256,
                    size=written,
                    version=1,
                )
                self._store.complete_attachment(
                    asset_id=asset.asset_id,
                    path=share_path,
                    sha256=sha256,
                    size=written,
                    version=1,
                    etag=etag,
                    last_modified=last_modified,
                    question_type=inspection.evidence.question_type.value,
                    dedupe_of=share_asset_id,
                )
                return DownloadResult(
                    asset_id=asset.asset_id,
                    status=AttachmentStatus.COMPLETED,
                    relative_path=share_path,
                    size_bytes=written,
                    sha256=sha256,
                    version=1,
                    dedupe_of=share_asset_id,
                )

            if target.resolve_path is not None:
                base_relative = target.resolve_path(inspection)
            elif self._path_resolver is not None:
                base_relative = self._path_resolver(target, inspection)
            else:
                base_relative = target.relative_path
            relative, version, adopted = self._resolve_destination(
                target, sha256, base_relative
            )
            final_path = self._root.joinpath(*relative.split("/"))
            ensure_within_root(self._root, final_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)

            self._store.begin_attachment_verify(
                asset_id=asset.asset_id,
                path=relative,
                sha256=sha256,
                size=written,
                version=version,
            )
            os.replace(part_path, final_path)
            self._store.complete_attachment(
                asset_id=asset.asset_id,
                path=relative,
                sha256=sha256,
                size=written,
                version=version,
                etag=etag,
                last_modified=last_modified,
                question_type=inspection.evidence.question_type.value,
                status=AttachmentStatus.SKIPPED if adopted else AttachmentStatus.COMPLETED,
            )
            return DownloadResult(
                asset_id=asset.asset_id,
                status=AttachmentStatus.SKIPPED if adopted else AttachmentStatus.COMPLETED,
                relative_path=relative,
                size_bytes=written,
                sha256=sha256,
                version=version,
                skipped=adopted,
            )
        finally:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass

    # --- 輔助 ---------------------------------------------------------
    def _find_shared(self, sha256: str, asset_id: str) -> tuple[str, str] | None:
        """若相同內容已歸檔且檔案完好，回傳 (相對路徑, 來源 asset_id)。"""
        row = self._store.find_content_duplicate(sha256=sha256, exclude_asset_id=asset_id)
        if row is None or not row.path:
            return None
        if not self._is_intact(row):
            return None
        return row.path, row.asset_id

    def _resolve_destination(
        self, target: DownloadTarget, sha256: str, base_relative: str | None = None
    ) -> tuple[str, int, bool]:
        """決定最終相對路徑與版本。

        - 檔案內容與新內容相同：視為已略過（adopt）。
        - manifest 記錄的檔案損壞（雜湊不符）：修復並覆寫原路徑。
        - 既有完好舊版且內容不同：保留舊版，新檔名帶內容雜湊並遞增版本。
        """
        relative = (base_relative or target.relative_path).replace("\\", "/")
        existing_row = self._store.get_attachment(target.asset.asset_id)
        current_version = existing_row.version if existing_row else 1
        final_path = self._root.joinpath(*relative.split("/"))

        if not final_path.exists():
            return relative, current_version, False

        file_hash = _sha256_file(final_path)
        if file_hash == sha256:
            return relative, current_version, True

        if (
            existing_row is not None
            and existing_row.path == relative
            and existing_row.sha256
            and file_hash != existing_row.sha256
        ):
            # 我們自己追蹤的檔案損壞，重新下載後覆寫原路徑
            return relative, current_version, False

        version = current_version + 1
        return _versioned_relative(relative, sha256), version, False

    def _is_intact(self, row) -> bool:
        if not row.path or row.size is None or not row.sha256:
            return False
        path = self._root.joinpath(*row.path.split("/"))
        if not path.is_file():
            return False
        if path.stat().st_size != row.size:
            return False
        return _sha256_file(path) == row.sha256

    def _validate_url(self, url: str) -> None:
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https"):
            raise BlockedHost(f"不允許的協定：{url}")
        host = (parsed.host or "").lower()
        if host not in self._allowed_hosts:
            raise BlockedHost(f"下載網域不在白名單：{url}")

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 404:
            raise NotFound(f"找不到附件（HTTP 404）：{response.url}")
        if status == 403:
            raise AccessDenied(f"來源拒絕存取（HTTP 403）：{response.url}")
        if status in (429, 503):
            raise ServerError(f"來源限流或不可用（HTTP {status}）：{response.url}")
        if status >= 500:
            raise ServerError(f"來源伺服器錯誤（HTTP {status}）：{response.url}")
        if status >= 400:
            raise DownloadError(f"下載失敗（HTTP {status}）：{response.url}")

    def _cancelled(self, target: DownloadTarget, message: str) -> DownloadResult:
        self._store.fail_attachment(
            asset_id=target.asset.asset_id,
            error_code=Cancelled.code,
            error_message=message,
            status=AttachmentStatus.CANCELLED,
        )
        return DownloadResult(
            asset_id=target.asset.asset_id,
            status=AttachmentStatus.CANCELLED,
            error_code=Cancelled.code,
            error_message=message,
        )


def _declared_length(response: httpx.Response) -> int | None:
    header = response.headers.get("content-length")
    if not header:
        return None
    try:
        return int(header)
    except ValueError:
        return None


def _part_name(asset_id: str) -> str:
    token = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:16]
    return f"dl_{token}"


def _versioned_relative(relative: str, sha256: str) -> str:
    """在角色後綴（題型）前插入內容雜湊，保留舊版檔案。"""
    stem, suffix = relative[:-4], relative[-4:]
    token_hash = sha256[:8]
    for token in TYPE_TOKENS:
        index = stem.rfind(token)
        if index >= 0:
            return f"{stem[:index]}{token_hash}_{stem[index:]}{suffix}"
    return f"{stem}_{token_hash}{suffix}"
