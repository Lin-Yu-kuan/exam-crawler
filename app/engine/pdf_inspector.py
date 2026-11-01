"""PDF 有效性檢查與首頁題型證據。

邊界與限制（SPEC §4.1）：
- 只讀第一頁，且只保留前 300 個字元作為證據；不掃描後續頁面、不做 OCR。
- 文字抽取順序不代表頁面視覺位置。
- 純題型必須有排他證據；單獨出現「單一選擇題」不足以判定。
- 解析在獨立且可強制終止的程序中執行，預設 5 秒逾時；不用無法中斷的執行緒等待。

「無效 PDF」（不是 PDF／損壞）與「嗅探失敗」（逾時或程序異常）是不同的結果；
有效但無法辨識題型時回傳 UNKNOWN，仍可歸檔。
"""

from __future__ import annotations

import hashlib
import multiprocessing
import queue as queue_module
import re
import time
from pathlib import Path

from app.models import PdfInspection, PdfStatus, QuestionType, TypeEvidence

RULE_VERSION = "pdf-v1"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_PREVIEW_CHARS = 300
MIN_TEXT_CHARS = 10
PDF_MAGIC_WINDOW = 1024

_MIXED_PATTERNS = (
    re.compile(r"(試題|科目)[^。\n]{0,12}(包括|包含|分為)[^。\n]{0,10}(作文|申論)[^。\n]{0,8}(及|與|和)[^。\n]{0,8}測驗"),
    re.compile(r"(作文|申論)[^。\n]{0,8}(及|與|和)[^。\n]{0,8}測驗[^。\n]{0,8}(兩部分|二部分|两部分)"),
    re.compile(r"測驗[^。\n]{0,8}(及|與|和)[^。\n]{0,8}(作文|申論)[^。\n]{0,8}(兩部分|二部分)"),
)
_EXCLUSIVE_CHOICE_PATTERNS = (
    re.compile(r"全部(均)?為(測驗題|選擇題)"),
    re.compile(r"本(試題|科目)全部為(測驗題|選擇題)"),
)
_EXCLUSIVE_ESSAY_PATTERNS = (
    re.compile(r"全部(均)?為申論(題|試題)"),
    re.compile(r"本(試題|科目)全部為申論(題|試題)"),
)
_COUNT_TOTAL = re.compile(r"共\s*(\d+)\s*題")
_COUNT_SINGLE = re.compile(r"單選題\s*(\d+)\s*題")
_COUNT_MULTI = re.compile(r"(?:複選題|多選題)\s*(\d+)\s*題")
_WEAK_HINTS = ("單選題", "選擇題", "申論", "不必抄題", "作答")


def _evidence(
    question_type: QuestionType,
    source: str,
    reason: str,
    *,
    conflicted: bool = False,
) -> TypeEvidence:
    return TypeEvidence(
        question_type=question_type,
        source=source,
        reason=reason,
        rule_version=RULE_VERSION,
        conflicted=conflicted,
    )


def _has_text_layer(text: str) -> bool:
    return len(text.strip()) >= MIN_TEXT_CHARS


def classify_first_page_text(text: str) -> TypeEvidence:
    """依首頁前段文字判定題型（純函式，可單獨測試）。

    優先序：無文字層 → 明確混合 → 排他證據 → 題數加總 → 證據不足。
    """
    if not _has_text_layer(text):
        return _evidence(
            QuestionType.UNKNOWN,
            "pdf:no-text-layer",
            "首頁沒有可用文字層（可能為掃描檔），無法判定題型，保留未確認",
        )

    mixed_hits = [pattern.search(text) for pattern in _MIXED_PATTERNS]
    mixed = any(mixed_hits)
    exclusive_choice = any(pattern.search(text) for pattern in _EXCLUSIVE_CHOICE_PATTERNS)
    exclusive_essay = any(pattern.search(text) for pattern in _EXCLUSIVE_ESSAY_PATTERNS)

    if mixed:
        conflicted = exclusive_choice or exclusive_essay
        return _evidence(
            QuestionType.MIXED,
            "pdf:explicit-mixed",
            "首頁明確指出同時包含申論／作文與測驗部分"
            + ("；另有排他性敘述與此衝突，標示待確認" if conflicted else ""),
            conflicted=conflicted,
        )

    if exclusive_choice and exclusive_essay:
        return _evidence(
            QuestionType.UNKNOWN,
            "pdf:conflicting-exclusive",
            "同時出現純測驗與純申論的排他敘述，證據互相衝突，保留未確認",
            conflicted=True,
        )

    if exclusive_choice:
        return _evidence(
            QuestionType.CHOICE,
            "pdf:exclusive-choice",
            "首頁出現排他性敘述，指出全部題目皆為測驗題",
        )

    if exclusive_essay:
        return _evidence(
            QuestionType.ESSAY,
            "pdf:exclusive-essay",
            "首頁出現排他性敘述，指出全部題目皆為申論題",
        )

    total_match = _COUNT_TOTAL.search(text)
    single_match = _COUNT_SINGLE.search(text)
    multi_match = _COUNT_MULTI.search(text)
    if total_match and single_match:
        total = int(total_match.group(1))
        single = int(single_match.group(1))
        multi = int(multi_match.group(1)) if multi_match else 0
        if total > 0 and single + multi == total:
            return _evidence(
                QuestionType.CHOICE,
                "pdf:choice-count",
                f"首頁載明共 {total} 題，其中單選 {single} 題、複選／多選 {multi} 題，"
                "加總等於總題數，具排他性",
            )
        if total > 0 and single + multi != total:
            return _evidence(
                QuestionType.UNKNOWN,
                "pdf:insufficient-evidence",
                f"首頁題數為 {total}，但只列出 {single + multi} 題選擇題，"
                "可能含有其他題型，證據不足",
            )

    weak = [hint for hint in _WEAK_HINTS if hint in text]
    hint_text = f"（僅見跡象：{'、'.join(weak)}）" if weak else ""
    return _evidence(
        QuestionType.UNKNOWN,
        "pdf:insufficient-evidence",
        "首頁沒有可據以判定的排他證據，維持未確認" + hint_text,
    )


def is_pdf_magic(header: bytes) -> bool:
    """檢查檔頭是否含 PDF 特徵（允許前 1KB 內出現，符合 PDF 規格）。"""
    return b"%PDF-" in header[:PDF_MAGIC_WINDOW]


def _worker(path: str, max_chars: int, result_queue) -> None:
    """子程序：抽取第一頁文字；例外一律回報，不以 pass 吞掉問題。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        page_count = len(reader.pages)
        if page_count == 0:
            result_queue.put(("empty", 0, ""))
            return
        text = reader.pages[0].extract_text() or ""
        result_queue.put(("ok", page_count, text[:max_chars]))
    except BaseException as exc:  # noqa: BLE001 - 邊界程序需回報所有失敗型別
        result_queue.put(("error", None, f"{type(exc).__name__}: {exc}"))


def sniff_first_page_text(
    path: str | Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, max_chars: int = MAX_PREVIEW_CHARS
) -> tuple[str, int | None, str]:
    """在可終止的子程序中抽取首頁文字。

    回傳 (狀態, 頁數, 文字或錯誤訊息)；狀態為 ok／empty／error／timeout。
    """
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_worker, args=(str(path), max_chars, result_queue), daemon=True
    )
    process.start()
    try:
        process.join(timeout)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():  # 極端情況再強制結束
                process.kill()
                process.join(1.0)
            return "timeout", None, f"嗅探逾時（{timeout} 秒）"
        try:
            status, page_count, payload = result_queue.get(timeout=1.0)
        except queue_module.Empty:
            return "error", None, "子程序未回傳結果"
        return status, page_count, payload
    finally:
        result_queue.close()
        result_queue.join_thread()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(
    path: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_chars: int = MAX_PREVIEW_CHARS,
) -> PdfInspection:
    """檢查單一 PDF 檔案：有效性、首頁文字與題型證據。"""
    started = time.perf_counter()
    target = Path(path)

    if not target.is_file():
        return _result(
            PdfStatus.INVALID,
            "file-not-found",
            0,
            "",
            None,
            "",
            _evidence(QuestionType.UNKNOWN, "pdf:not-found", "檔案不存在，無法檢查"),
            started,
        )

    size = target.stat().st_size
    with target.open("rb") as handle:
        header = handle.read(PDF_MAGIC_WINDOW)
    digest = _sha256_of(target)

    if not is_pdf_magic(header):
        preview = header[:64].decode("utf-8", errors="replace")
        return _result(
            PdfStatus.INVALID,
            "not-pdf",
            size,
            digest,
            None,
            preview,
            _evidence(
                QuestionType.UNKNOWN,
                "pdf:not-pdf",
                "檔案沒有 PDF 特徵（可能是 HTML 錯誤頁或登入頁），不得視為有效附件",
            ),
            started,
        )

    status, page_count, payload = sniff_first_page_text(
        target, timeout=timeout, max_chars=max_chars
    )
    if status == "timeout":
        return _result(
            PdfStatus.SNIFF_FAILED,
            "timeout",
            size,
            digest,
            None,
            "",
            _evidence(
                QuestionType.UNKNOWN,
                "pdf:timeout",
                f"首頁嗅探未在 {timeout} 秒內完成，題型保留未確認",
            ),
            started,
        )
    if status == "error":
        return _result(
            PdfStatus.INVALID,
            "corrupt-pdf",
            size,
            digest,
            None,
            "",
            _evidence(
                QuestionType.UNKNOWN,
                "pdf:corrupt",
                f"PDF 無法解析：{payload}",
            ),
            started,
        )
    if status == "empty":
        return _result(
            PdfStatus.INVALID,
            "no-pages",
            size,
            digest,
            0,
            "",
            _evidence(QuestionType.UNKNOWN, "pdf:no-pages", "PDF 沒有任何頁面"),
            started,
        )

    text = payload
    return _result(
        PdfStatus.VALID,
        "ok" if _has_text_layer(text) else "no-text-layer",
        size,
        digest,
        page_count,
        text,
        classify_first_page_text(text),
        started,
    )


def _result(
    status: PdfStatus,
    reason_code: str,
    size: int,
    digest: str,
    page_count: int | None,
    text: str,
    evidence: TypeEvidence,
    started: float,
) -> PdfInspection:
    return PdfInspection(
        status=status,
        reason_code=reason_code,
        size_bytes=size,
        sha256=digest,
        page_count=page_count,
        has_text=_has_text_layer(text),
        text_length=len(text.strip()),
        text_preview=text[:MAX_PREVIEW_CHARS],
        evidence=evidence,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
