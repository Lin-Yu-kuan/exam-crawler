"""歸檔路徑計算、模板驗證、清理、碰撞處理與根目錄邊界檢查。

邊界：只做純路徑計算與安全檢查，**不下載、不建立檔案**。
模板以明確的變數替換實作，不使用 eval 或任何程式求值。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.models import AssetRole, ExamAsset, ExamRecord, QuestionType

#: A／B／C 三種預設模板（SPEC §5）
DEFAULT_TEMPLATES: dict[str, str] = {
    "A": "{科目}/{年度}_{考試名稱}_{類科}_{題型}.pdf",
    "B": "{年度}/{考試名稱}_{類科}/{科目}_{題型}.pdf",
    "C": "{年度}_{考試名稱}_{類科}_{科目}_{題型}.pdf",
}

#: 模板允許的變數（其餘一律拒絕）
ALLOWED_VARIABLES = frozenset(
    {
        "年度",
        "年度標籤",
        "考試名稱",
        "考試簡稱",
        "類科",
        "科目",
        "等別",
        "題型類別",
        "題型",
    }
)

FILENAME_TYPE_TOKENS: dict[QuestionType, str] = {
    QuestionType.ESSAY: "申論試題",
    QuestionType.CHOICE: "測驗試題",
    QuestionType.MIXED: "混合試題",
    QuestionType.UNKNOWN: "試題",
}
ANSWER_TOKEN = "測驗答案"
MISSING_LEVEL = "等別未確認"

ILLEGAL_CHARACTERS = set('<>:"/\\|?*')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
}
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
CONTROL_CATEGORY = "C"
WHITESPACE = re.compile(r"\s+")

DEFAULT_MAX_SEGMENT_LENGTH = 100
DEFAULT_MAX_SEGMENT_BYTES = 200
DEFAULT_MAX_TOTAL_LENGTH = 240


class PathBuildError(Exception):
    """路徑無法安全建立；呼叫端必須顯示原因，不可默默略過。"""


class TemplateError(PathBuildError):
    """模板格式不合法。"""


class PathEscapeError(PathBuildError):
    """最終路徑超出下載根目錄。"""


class PathMode(str, Enum):
    """四種歸檔模式。"""

    A = "A 依科目"
    B = "B 依年度考試"
    C = "C 平鋪"
    D = "D 自訂"


@dataclass(frozen=True)
class PlannedFile:
    """單一附件的預估歸檔位置。"""

    record_id: str
    asset_id: str
    role: AssetRole
    question_type: QuestionType
    relative_path: str
    is_provisional: bool
    disambiguated: bool = False


@dataclass(frozen=True)
class PathPlan:
    """整批預覽結果；沒有實際建立任何檔案。"""

    root: str
    template: str
    files: tuple[PlannedFile, ...]
    warnings: tuple[str, ...] = ()

    def paths(self) -> tuple[str, ...]:
        return tuple(item.relative_path for item in self.files)


def _hash_token(value: str, length: int = 6) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def validate_template(template: str) -> str:
    """驗證模板；不合法時拋出 TemplateError。"""
    if not template or not template.strip():
        raise TemplateError("模板不可為空")
    if "\\" in template:
        raise TemplateError("模板請使用「/」作為目錄分隔符，不可使用反斜線")
    if re.match(r"^[A-Za-z]:", template):
        raise TemplateError("模板不可使用磁碟機代號（絕對路徑）")
    if "\x00" in template:
        raise TemplateError("模板含有控制字元")

    if not template.lower().endswith(".pdf"):
        raise TemplateError("模板必須以 .pdf 結尾")

    segments = template.split("/")
    if any(segment == "" for segment in segments):
        raise TemplateError("模板含有空目錄段（連續斜線或開頭／結尾斜線）")
    if any(segment in (".", "..") for segment in segments):
        raise TemplateError("模板不可包含「.」或「..」目錄段")

    filename = segments[-1]
    if not re.search(r"\{題型\}\.pdf$", filename, re.IGNORECASE):
        raise TemplateError("模板檔名必須以「{題型}.pdf」作結尾")

    sanitized = PLACEHOLDER.sub("X", template)
    if "{" in sanitized or "}" in sanitized:
        raise TemplateError("模板含未配對的大括號")

    for name in PLACEHOLDER.findall(template):
        if name not in ALLOWED_VARIABLES:
            raise TemplateError(f"模板含未知或不允許的變數：{{{name}}}")
    return template


def _strip_control(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch)[0] != CONTROL_CATEGORY)


def _truncate_body(body: str, keep_chars: int, keep_bytes: int) -> str:
    """依字元數與 UTF-8 位元組數截斷主體，保留開頭且不切斷多位元組字元。"""
    if keep_chars <= 0 or keep_bytes <= 0:
        return ""
    kept: list[str] = []
    used_bytes = 0
    for index, char in enumerate(body):
        if index >= keep_chars:
            break
        size = len(char.encode("utf-8"))
        if used_bytes + size > keep_bytes:
            break
        kept.append(char)
        used_bytes += size
    return "".join(kept)


def sanitize_segment(
    value: str,
    *,
    max_length: int = DEFAULT_MAX_SEGMENT_LENGTH,
    max_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
    context: str = "",
) -> str:
    """清理單一路徑段：NFKC、去控制字元、換掉非法字元、處理保留名稱。

    除字元數上限外，另以 UTF-8 位元組數截斷：中文字每字 3 位元組，
    純字元截斷仍可能超過檔案系統 255 位元組的段上限（ENAMETOOLONG）。
    """
    text = unicodedata.normalize("NFKC", value or "")
    text = _strip_control(text)
    text = "".join("_" if ch in ILLEGAL_CHARACTERS else ch for ch in text)
    text = WHITESPACE.sub(" ", text).strip()
    text = text.strip(" .")

    if not text:
        raise PathBuildError(f"清理後名稱為空：{context or value!r}")

    stem = text.split(".")[0].upper()
    if stem in RESERVED_NAMES:
        text = f"_{text}"

    if len(text) > max_length or len(text.encode("utf-8")) > max_bytes:
        token = _hash_token(value)
        suffix = ".pdf" if text.lower().endswith(".pdf") else ""
        body = text[: len(text) - len(suffix)] if suffix else text
        keep_chars = max(1, max_length - len(token) - 1 - len(suffix))
        keep_bytes = max_bytes - len(token.encode("utf-8")) - 1 - len(suffix.encode("utf-8"))
        if keep_bytes < 1:
            raise PathBuildError(
                f"名稱過長且位元組上限過小，無法安全縮短：{context or value!r}"
            )
        text = f"{_truncate_body(body, keep_chars, keep_bytes)}_{token}{suffix}"

    if not text:
        raise PathBuildError(f"清理後名稱為空：{context or value!r}")
    return text


def _variable_map(
    record: ExamRecord,
    asset: ExamAsset,
    *,
    asset_type: QuestionType,
    record_type: QuestionType,
) -> dict[str, str]:
    """建立模板變數；缺資料時使用 SPEC 指定的替代文字。"""
    is_answer = asset.role is AssetRole.CHOICE_ANSWER
    type_token = ANSWER_TOKEN if is_answer else FILENAME_TYPE_TOKENS[asset_type]
    return {
        "年度": str(record.year) if record.year else "",
        "年度標籤": f"{record.year}年" if record.year else "",
        "考試名稱": record.exam_name,
        "考試簡稱": record.exam_short_name or record.exam_name,
        "類科": record.category,
        "科目": record.subject,
        "等別": record.level or MISSING_LEVEL,
        "題型類別": (record_type if is_answer else asset_type).value,
        "題型": type_token,
    }


def _render_segments(
    template: str, variables: dict[str, str], *, max_segment: int
) -> list[str]:
    """先以模板結構分段，再替換變數；資料值中的斜線只當成待清理字元。"""
    segments: list[str] = []
    for index, raw_segment in enumerate(template.split("/")):
        substituted = PLACEHOLDER.sub(lambda match: variables[match.group(1)], raw_segment)
        name = sanitize_segment(
            substituted, max_length=max_segment, context=f"第 {index + 1} 段：{raw_segment}"
        )
        segments.append(name)
    return segments


def _with_short_id(segments: list[str], short_id: str, type_token: str) -> list[str]:
    """在角色後綴（{題型}）前插入穩定短 ID。"""
    result = list(segments)
    filename = result[-1]
    stem, suffix = filename[:-4], filename[-4:]
    token_index = stem.rfind(type_token)
    if token_index >= 0:
        result[-1] = f"{stem[:token_index]}{short_id}_{stem[token_index:]}{suffix}"
    else:  # 理論上不會發生（模板已驗證），保守處理
        result[-1] = f"{stem}_{short_id}{suffix}"
    return result


def _collision_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


def ensure_within_root(root: Path, target: Path) -> Path:
    """確認最終位置仍在根目錄內（含符號連結／junction 解析）。"""
    resolved_root = Path(root).resolve()
    resolved_target = Path(target).resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PathEscapeError(
            f"歸檔路徑超出下載根目錄：{resolved_target} 不在 {resolved_root} 內"
        ) from exc
    return resolved_target


def is_within_root(root: Path, target: Path) -> bool:
    """不拋例外的邊界檢查。"""
    try:
        ensure_within_root(root, target)
    except PathEscapeError:
        return False
    return True


def _limit_total_length(
    root: Path, segments: list[str], limit: int, seed: str
) -> list[str]:
    """總長度超限時，以穩定 ID 縮短檔名主體（單步計算預算，避免逐步拉锯）。"""
    if limit <= 0:
        return segments

    root_length = len(str(Path(root).resolve())) + 1
    separators = len(segments) - 1
    budget = limit - root_length - separators
    filename = segments[-1]
    if len(filename) <= budget:
        return segments

    stem, suffix = filename[:-4], filename[-4:]
    token = _hash_token(seed, 8)
    keep = budget - len(token) - 1 - len(suffix)
    if keep < 8:
        raise PathBuildError(
            f"路徑過長且無法安全縮短（可用 {budget} 字元，根目錄過深）：{'/'.join(segments)}"
        )
    result = list(segments)
    result[-1] = f"{stem[:keep]}_{token}{suffix}"
    return result


def build_plan(
    root: str | Path,
    records: tuple[ExamRecord, ...],
    *,
    mode: PathMode = PathMode.A,
    template: str | None = None,
    asset_types: dict[str, QuestionType] | None = None,
    record_types: dict[str, QuestionType] | None = None,
    max_segment_length: int = DEFAULT_MAX_SEGMENT_LENGTH,
    max_total_length: int = DEFAULT_MAX_TOTAL_LENGTH,
) -> PathPlan:
    """計算整批附件的歸檔位置（僅預覽，不建立檔案）。

    asset_types：附件 ID → 題型；缺少時視為未確認。
    record_types：紀錄 ID → 考科整體題型（供 {題型類別} 對答案使用）。
    """
    if mode is PathMode.D:
        if template is None:
            raise TemplateError("模式 D 必須提供自訂模板")
    else:
        template = DEFAULT_TEMPLATES[mode.name]
    template = validate_template(template)

    asset_types = asset_types or {}
    record_types = record_types or {}
    root_path = Path(root)
    warnings: list[str] = []
    files: list[PlannedFile] = []
    used: dict[str, str] = {}

    for record in records:
        question_assets = record.assets_by_role(AssetRole.QUESTION)
        answer_assets = record.assets_by_role(AssetRole.CHOICE_ANSWER)
        record_type = record_types.get(
            record.record_id,
            _infer_record_type(record, asset_types),
        )

        for asset in record.assets:
            asset_type = asset_types.get(asset.asset_id, QuestionType.UNKNOWN)
            variables = _variable_map(
                record, asset, asset_type=asset_type, record_type=record_type
            )
            segments = _render_segments(
                template, variables, max_segment=max_segment_length
            )

            multiple = (
                len(question_assets) > 1
                if asset.role is AssetRole.QUESTION
                else len(answer_assets) > 1
            )
            short_id = ""
            if multiple:
                short_id = "_" + asset.asset_id.rsplit("#", 1)[-1]
                segments = _with_short_id(segments, short_id, variables["題型"])

            relative = "/".join(segments)
            key = _collision_key(relative)
            if key in used and used[key] != asset.asset_id:
                token = _hash_token(asset.asset_id, 8)
                segments = _with_short_id(segments, f"_{token}", variables["題型"])
                relative = "/".join(segments)
                key = _collision_key(relative)
                disambiguated = True
                warnings.append(
                    f"路徑碰撞已以穩定 ID 消歧：{relative}"
                )
            else:
                disambiguated = bool(multiple)

            segments = _limit_total_length(root_path, segments, max_total_length, asset.asset_id)
            relative = "/".join(segments)
            key = _collision_key(relative)
            used[key] = asset.asset_id

            target = root_path.joinpath(*segments)
            ensure_within_root(root_path, target)

            files.append(
                PlannedFile(
                    record_id=record.record_id,
                    asset_id=asset.asset_id,
                    role=asset.role,
                    question_type=asset_type,
                    relative_path=relative,
                    is_provisional=(
                        asset.role is AssetRole.QUESTION and asset_type is QuestionType.UNKNOWN
                    ),
                    disambiguated=disambiguated,
                )
            )

    return PathPlan(root=str(root_path), template=template, files=tuple(files), warnings=tuple(warnings))


def _infer_record_type(
    record: ExamRecord, asset_types: dict[str, QuestionType]
) -> QuestionType:
    """以已知的試題附件題型推得考科整體題型；不一致時視為未確認。"""
    known = {
        asset_types.get(asset.asset_id, QuestionType.UNKNOWN)
        for asset in record.assets_by_role(AssetRole.QUESTION)
    }
    known.discard(QuestionType.UNKNOWN)
    if len(known) == 1:
        return known.pop()
    return QuestionType.UNKNOWN


def preview_lines(plan: PathPlan) -> tuple[str, ...]:
    """給 UI 的預覽文字；暫定路徑會加註說明。"""
    lines: list[str] = []
    for item in plan.files:
        suffix = "（暫定，題型未確認）" if item.is_provisional else ""
        lines.append(f"{item.relative_path}{suffix}")
    return tuple(lines)
