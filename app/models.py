"""共用資料模型、列舉、穩定 ID 與文字正規化（不做任何 I/O）。

本模組只定義契約與純函式；網路、檔案、資料庫存取一律在其他模組實作。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

#: 官方來源頁（考畢試題查詢平臺）
SOURCE_PAGE = "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"

#: 官方附件端點（相對查詢頁）
ASSET_ENDPOINT = "https://wwwq.moex.gov.tw/exam/wHandExamQandA_File.ashx"

#: 穩定 ID 策略版本；官方識別碼缺失時的雜湊策略改變時必須遞增
ID_STRATEGY_VERSION = "moex-v1"

#: 試卷群組鍵規則版本
PAPER_GROUP_RULE_VERSION = "paper-v1"


class QuestionType(str, Enum):
    """題型。預設為 UNKNOWN，不得憑答案連結有無推論純題型。"""

    ESSAY = "申論題"
    CHOICE = "測驗題"
    MIXED = "混合題"
    UNKNOWN = "未確認"


class AssetRole(str, Enum):
    """附件角色。未知角色不得自動當作答案下載。"""

    QUESTION = "試題"
    CHOICE_ANSWER = "測驗答案"
    OTHER = "其他"


class AssetScope(str, Enum):
    """附件適用範圍：單一考科或整場考試。"""

    SUBJECT = "考科"
    EXAM = "考試"


class ParseStatus(str, Enum):
    """單次查詢頁面的解析結果分類。

    OK_WITH_DIFF 代表官方統計文字與實際渲染列數不一致，但差異只出現在統計，
    沒有解析失敗、上限或錯誤頁；依 SPEC 允許下載並標示差異。
    其餘非 OK 狀態都必須保留診斷資訊，且不得在下游被當成「零結果」。
    """

    OK = "完整"
    OK_WITH_DIFF = "完整（統計差異）"
    NO_RESULT = "無結果"
    LIMIT_EXCEEDED = "結果過大"
    INCOMPLETE = "結果未完整"
    ERROR_PAGE = "來源錯誤頁"
    STRUCTURE_CHANGED = "結構變更"


@dataclass(frozen=True)
class TypeEvidence:
    """題型判定證據；可追溯來源、原因與規則版本。"""

    question_type: QuestionType
    source: str
    reason: str
    rule_version: str
    conflicted: bool = False


class AttachmentStatus(str, Enum):
    """附件下載狀態（SPEC §7）。"""

    PENDING = "待下載"
    DOWNLOADING = "下載中"
    VERIFYING = "驗證中"
    COMPLETED = "已完成"
    SKIPPED = "已略過"
    FAILED = "失敗"
    CANCELLED = "已取消"


class TaskStatus(str, Enum):
    """任務狀態（SPEC §7）。"""

    PENDING = "待開始"
    QUERYING = "查詢中"
    SELECTING = "待勾選"
    DOWNLOADING = "下載中"
    CANCELLING = "取消中"
    CANCELLED = "已取消"
    COMPLETED = "完成"
    PARTIAL = "部分完成"
    FAILED = "失敗"

    @property
    def is_active(self) -> bool:
        return self in (
            TaskStatus.PENDING,
            TaskStatus.QUERYING,
            TaskStatus.SELECTING,
            TaskStatus.DOWNLOADING,
            TaskStatus.CANCELLING,
        )


class PdfStatus(str, Enum):
    """PDF 檢查結果分類。

    INVALID 與 SNIFF_FAILED 意義不同：前者檔案本身不是可用的 PDF，
    後者是檔案可能有效但無法在時限內完成檢查，不得當成無效檔。
    """

    VALID = "有效"
    INVALID = "無效"
    SNIFF_FAILED = "嗅探失敗"


@dataclass(frozen=True)
class PdfInspection:
    """單一 PDF 的檢查結果（只讀首頁）。"""

    status: PdfStatus
    reason_code: str
    size_bytes: int
    sha256: str
    page_count: int | None
    has_text: bool
    text_length: int
    text_preview: str
    evidence: TypeEvidence
    elapsed_ms: float


@dataclass(frozen=True)
class ExamAsset:
    """單一附件。

    asset_id 為內部穩定 ID；official_id 為由來源參數組成的官方識別碼（若可取得）。
    """

    asset_id: str
    official_id: str | None
    role: AssetRole
    original_label: str
    url: str
    observed_at: str
    source_version: str | None
    evidence: TypeEvidence
    scope: AssetScope = AssetScope.SUBJECT
    content_hint: str | None = None


@dataclass(frozen=True)
class ExamRecord:
    """一筆來源考科紀錄，對應零至多個附件。"""

    record_id: str
    official_id: str | None
    year: int
    exam_name: str
    exam_short_name: str | None
    category: str
    subject: str
    level: str | None
    source_page: str
    assets: tuple[ExamAsset, ...]
    exam_name_key: str = ""
    category_key: str = ""
    subject_key: str = ""
    observed_at: str = ""
    id_strategy: str = ID_STRATEGY_VERSION

    def assets_by_role(self, role: AssetRole) -> tuple[ExamAsset, ...]:
        """取指定角色的附件（保持來源順序）。"""
        return tuple(asset for asset in self.assets if asset.role is role)


@dataclass(frozen=True)
class OfficialCounts:
    """頁面統計文字所宣稱的筆數。"""

    exams: int
    categories: int
    subjects: int


@dataclass(frozen=True)
class ParsedCounts:
    """實際解析出的筆數。"""

    exams: int
    categories: int
    subjects: int
    duplicate_rows: int


@dataclass(frozen=True)
class ParsedPage:
    """一次查詢頁面的解析結果。

    status 與 warnings 供上層判斷完整性；records 只包含真實存在的考科。
    """

    status: ParseStatus
    records: tuple[ExamRecord, ...]
    exam_assets: tuple[ExamAsset, ...]
    official_counts: OfficialCounts | None
    parsed_counts: ParsedCounts
    stats_text: str
    source_url: str
    observed_at: str
    warnings: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """是否可作為完整快照使用（統計差異仍算完整，但會保留警告）。"""
        return self.status in (ParseStatus.OK, ParseStatus.OK_WITH_DIFF)

    @property
    def message(self) -> str:
        """給使用者看的狀態說明。"""
        if self.status is ParseStatus.OK:
            return "查詢結果完整"
        if self.status is ParseStatus.OK_WITH_DIFF:
            return "查詢結果完整，但官方統計與實際列數有差異（已保留兩組數字）"
        if self.status is ParseStatus.NO_RESULT:
            return "查無符合條件的資料"
        if self.status is ParseStatus.LIMIT_EXCEEDED:
            return "查詢結果超過官方上限，請縮小查詢條件"
        if self.status is ParseStatus.INCOMPLETE:
            return "查詢結果未完整，統計筆數與實際列數不一致"
        if self.status is ParseStatus.ERROR_PAGE:
            return "來源回應為錯誤頁，查詢未成功"
        return "來源頁面結構與預期不同，無法完成解析"


@dataclass(frozen=True)
class SelectOption:
    """下拉選單選項。value 為送出值，text 為顯示文字。"""

    value: str
    text: str
    selected: bool = False


@dataclass(frozen=True)
class SearchFormPage:
    """查詢表單頁面狀態，供 P1-3 的查詢流程使用。"""

    action: str
    hidden_fields: dict[str, str] = field(default_factory=dict)
    year_options: tuple[SelectOption, ...] = ()
    exam_options: tuple[SelectOption, ...] = ()
    control_names: tuple[str, ...] = ()
    postback_targets: tuple[str, ...] = ()
    text_fields: tuple[str, ...] = ()
    select_values: dict[str, str] = field(default_factory=dict)


_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class QuerySpec:
    """查詢條件；年度為民國年，范圍包含起迄。"""

    start_year: int
    end_year: int
    exam_code: str = ""
    exam_name_keyword: str = ""
    category_keyword: str = ""
    subject_keyword: str = ""

    def __post_init__(self) -> None:
        if self.start_year > self.end_year:
            raise ValueError("起年不得大於迄年")
        if self.start_year <= 0:
            raise ValueError("年度必須為正的民國年")

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(range(self.start_year, self.end_year + 1))

    def cache_payload(self) -> str:
        """快取鍵內容：條件、來源與解析器版本由呼叫端一併加入。"""
        return "|".join(
            [
                str(self.start_year),
                str(self.end_year),
                self.exam_code,
                compact_key(self.exam_name_keyword),
                compact_key(self.category_keyword),
                compact_key(self.subject_keyword),
            ]
        )


@dataclass(frozen=True)
class YearDiagnostics:
    """單一年度批次的解析診斷。"""

    year: int
    status: ParseStatus
    official: OfficialCounts | None
    parsed: ParsedCounts
    stats_text: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuerySnapshot:
    """一次查詢的凍結快照。"""

    snapshot_id: str
    cache_key: str
    spec: QuerySpec
    records: tuple[ExamRecord, ...]
    status: ParseStatus
    years: tuple[YearDiagnostics, ...]
    acquired_at: str
    parser_version: str
    source_url: str
    warnings: tuple[str, ...] = ()
    cached: bool = False

    @property
    def is_complete(self) -> bool:
        """未完整快照不得開放批量下載（統計差異仍允許，但需顯示警告）。"""
        return self.status in (ParseStatus.OK, ParseStatus.OK_WITH_DIFF)

    @property
    def message(self) -> str:
        """給使用者看的完整性說明。"""
        if self.status is ParseStatus.OK:
            return "查詢結果完整"
        if self.status is ParseStatus.OK_WITH_DIFF:
            return "查詢結果完整，但官方統計與實際列數有差異（已保留兩組數字）"
        if self.status is ParseStatus.NO_RESULT:
            return "查無符合條件的資料"
        if self.status is ParseStatus.LIMIT_EXCEEDED:
            return "查詢結果超過官方上限，請縮小查詢條件"
        if self.status is ParseStatus.INCOMPLETE:
            return "查詢結果未完整，統計筆數與實際列數不一致"
        if self.status is ParseStatus.ERROR_PAGE:
            return "來源回應為錯誤頁，查詢未成功"
        return "來源頁面結構與預期不同，無法完成解析"

    @property
    def total_subjects(self) -> int:
        return len(self.records)

    def assets_by_url(self) -> dict[str, tuple[ExamAsset, ...]]:
        """依 URL 分組，供傳輸去重估算。"""
        grouped: dict[str, list[ExamAsset]] = {}
        for record in self.records:
            for asset in record.assets:
                grouped.setdefault(asset.url, []).append(asset)
        return {url: tuple(items) for url, items in grouped.items()}


def normalize_text(text: str) -> str:
    """正規化官方文字：NFKC、去除首尾空白、合併連續空白。

    保留原始字元，不刪除語意字元；原始值仍須另存。
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    return _WHITESPACE.sub(" ", normalized).strip()


def compact_key(text: str) -> str:
    """比對用鍵：在 normalize_text 後移除所有空白並轉小寫。

    來源在名稱中插入排版空格（例如「一般 行政」、「臺 灣」），
    因此比對時以無空白版本為準；顯示仍使用原始文字。
    """
    return _WHITESPACE.sub("", normalize_text(text)).casefold()


def stable_hash(payload: str, length: int = 16) -> str:
    """產生穩定短雜湊（SHA-256 前 N 個十六進位字元）。"""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def build_record_id(
    exam_code: str | None,
    category_code: str | None,
    subject_code: str | None,
    exam_name: str,
    category: str,
    subject: str,
) -> tuple[str, str | None, str]:
    """產生考科紀錄的穩定 ID。

    優先使用官方識別碼；缺少時改用來源上下文雜湊，並回傳策略版本。
    回傳 (record_id, official_id, id_strategy)。
    """
    if exam_code and category_code and subject_code:
        official_id = f"moex:{exam_code}:{category_code}:{subject_code}"
        return official_id, official_id, ID_STRATEGY_VERSION

    payload = "|".join(
        (compact_key(exam_name), compact_key(category), compact_key(subject))
    )
    return f"h1:{stable_hash(payload)}", None, ID_STRATEGY_VERSION


def build_asset_id(record_id: str, role: str, ordinal: int) -> str:
    """產生附件穩定 ID；同角色多份附件以序號區分（拆卷）。"""
    return f"{record_id}#{role.lower()}{ordinal}"


def build_exam_asset_id(exam_code: str | None, role: str, ordinal: int = 1) -> str:
    """產生考試層級附件的穩定 ID。"""
    key = exam_code or stable_hash(role + str(ordinal), 12)
    return f"exam:{key}#{role.lower()}{ordinal}"


def paper_group_key(asset: ExamAsset) -> str | None:
    """同一份試卷的分組鍵；以官方附件 ID 的考試／科目／類型／卷次為準。"""
    if asset.scope is not AssetScope.SUBJECT or not asset.official_id:
        return None
    parts = asset.official_id.split(":")
    if len(parts) != 5 or parts[0] != "moex":
        return None
    _, exam_code, _category_code, subject_code, type_and_seq = parts
    if not exam_code or not subject_code or not type_and_seq:
        return None
    if type_and_seq[0] in ("A", "B"):
        return None
    return f"paper:{exam_code}:{subject_code}:{type_and_seq}"

