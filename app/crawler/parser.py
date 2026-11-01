"""HTML／表單解析為模型。

邊界：本模組**不做網路請求、不寫檔、不讀取 PDF**，只接受已取得的 HTML 字串或位元組。
結構異常一律回報明確狀態，不得當成零結果。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.models import (
    ASSET_ENDPOINT,
    SOURCE_PAGE,
    AssetRole,
    AssetScope,
    ExamAsset,
    ExamRecord,
    OfficialCounts,
    ParseStatus,
    ParsedCounts,
    ParsedPage,
    QuestionType,
    SearchFormPage,
    SelectOption,
    TypeEvidence,
    build_asset_id,
    build_exam_asset_id,
    build_record_id,
    compact_key,
    normalize_text,
)

TABLE_ID = "ctl00_holderContent_tblExamQand"
STATS_ID = "ctl00_holderContent_lblStatistics"
YEAR_START_CONTROL = "ctl00$holderContent$wUctlExamYearStart$ddlExamYear"
YEAR_END_CONTROL = "ctl00$holderContent$wUctlExamYearEnd$ddlExamYear"
EXAM_CODE_CONTROL = "ctl00$holderContent$ddlExamCode"

RULE_VERSION = "html-v1"

STATS_PATTERN = re.compile(r"共\s*(\d+)\s*考試\s*(\d+)\s*類科\s*(\d+)\s*科目")
LIMIT_PATTERN = re.compile(r"大於\s*6000\s*筆")
NO_RESULT_TEXT = "查無結果"
ERROR_TITLES = ("您所輸入的網址並不存在", "404")
CONTROL_PATTERN = re.compile(
    r"^ctl00\$holderContent\$chk_(\d{6})_(\d{3})(?:_([0-9A-Za-z]+))?$"
)
HREF_FORMAT_PATTERN = re.compile(r"\((Pdf|zip)檔\)", re.IGNORECASE)

# 附件參數 t 的語意（由官方連結標籤與副檔名說明確認）
ROLE_BY_TYPE = {
    "Q": (AssetRole.QUESTION, AssetScope.SUBJECT),
    "S": (AssetRole.CHOICE_ANSWER, AssetScope.SUBJECT),
    "M": (AssetRole.CHOICE_ANSWER, AssetScope.SUBJECT),  # 更正答案
    "A": (AssetRole.CHOICE_ANSWER, AssetScope.EXAM),
    "B": (AssetRole.OTHER, AssetScope.EXAM),
}


def decode_html(body: bytes, content_type: str = "") -> tuple[str, str]:
    """依 Content-Type 與頁面宣告解碼；回傳 (文字, 使用的編碼)。

    不以忽略錯誤的方式解碼，避免悄悄破壞官方名稱。
    """
    charset = ""
    match = re.search(r"charset=[\"']?([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    else:
        head = body[:2048].decode("latin-1", errors="ignore")
        match = re.search(r"charset=[\"']?([\w\-]+)", head, re.IGNORECASE)
        if match:
            charset = match.group(1)

    for candidate in [c for c in (charset, "utf-8", "big5", "cp950") if c]:
        try:
            return body.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    # 全部失敗時仍以取代字元解碼，但呼叫端可從回傳的編碼判斷需要告警
    return body.decode("utf-8", errors="replace"), "utf-8(replace)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _is_error_page(soup: BeautifulSoup) -> bool:
    title = normalize_text(soup.title.string) if soup.title and soup.title.string else ""
    return any(marker in title for marker in ERROR_TITLES)


def _asset_params(href: str) -> dict[str, str]:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    return {key: values[0] for key, values in query.items() if values}


def _content_hint(anchor: Tag, role: AssetRole) -> str | None:
    title = anchor.get("title") or ""
    match = HREF_FORMAT_PATTERN.search(title)
    if match:
        return match.group(1).lower()
    if role is AssetRole.OTHER:
        href = anchor.get("href") or ""
        if href.lower().endswith(".zip"):
            return "zip"
    return None


def _evidence_for_question(has_answer: bool) -> TypeEvidence:
    """HTML 層級的題型證據：答案連結只能證明含測驗部分。"""
    if has_answer:
        return TypeEvidence(
            question_type=QuestionType.UNKNOWN,
            source="html:answer-link",
            reason="同考科存在測驗答案連結，僅代表含測驗部分，無法區分純測驗或混合；須待 PDF 嗅探",
            rule_version=RULE_VERSION,
        )
    return TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="html:no-answer-link",
        reason="同考科無答案連結，不能推論為純申論；須待 PDF 嗅探",
        rule_version=RULE_VERSION,
    )


def _evidence_for_answer() -> TypeEvidence:
    """答案附件的證據：角色由官方「答案」標籤判定，題型仍未確認。"""
    return TypeEvidence(
        question_type=QuestionType.UNKNOWN,
        source="html:official-label",
        reason="附件角色依官方標籤「答案」判定；不得暗示包含申論解答",
        rule_version=RULE_VERSION,
    )


def _exam_name_from_level1(level1: Tag) -> str:
    """取出考試名稱：level1 內第一個不含連結的文字儲存格。"""
    for cell in level1.find_all("td"):
        if cell.find("a"):
            continue
        text = normalize_text(cell.get_text(" ", strip=True))
        if text:
            return text
    return ""


def _category_text(level2: Tag) -> tuple[str, str | None]:
    """取出類科文字與 checkbox 控制項名稱（篩選結果可能沒有 checkbox）。"""
    node = level2.find("label") or level2.find("span")
    text = normalize_text(node.get_text(" ", strip=True)) if node else ""
    if not text:
        text = normalize_text(level2.get_text(" ", strip=True))
    box = level2.find("input", attrs={"type": "checkbox"})
    return text, (box.get("name") if box else None)


def _year_from_exam_name(exam_name: str) -> int | None:
    match = re.match(r"^\s*(\d{2,3})\s*年", exam_name)
    return int(match.group(1)) if match else None


def _official_codes(
    control_name: str | None, params: dict[str, str]
) -> tuple[str | None, str | None, str | None]:
    """取得 (考試代碼, 類科代碼, 科目代碼)；控制項優先，其次連結參數。"""
    if control_name:
        match = CONTROL_PATTERN.match(control_name)
        if match:
            return match.group(1), match.group(2), match.group(3)
    return params.get("code"), params.get("c"), params.get("s")


def parse_result_page(
    html: str, *, source_url: str = SOURCE_PAGE, observed_at: str | None = None
) -> ParsedPage:
    """解析查詢結果頁。

    回傳的 ParsedPage.status 會明確區分無結果、結果過大、統計不符、
    錯誤頁與結構變更；只有 OK 才算完整快照。
    """
    observed = observed_at or _now_iso()
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []

    table = soup.find(id=TABLE_ID)
    stats_el = soup.find(id=STATS_ID)
    stats_text = normalize_text(stats_el.get_text(" ", strip=True)) if stats_el else ""

    if table is None:
        status = ParseStatus.ERROR_PAGE if _is_error_page(soup) else ParseStatus.STRUCTURE_CHANGED
        if stats_text:
            warnings.append(f"缺少結果表格但仍有統計文字：{stats_text}")
        return ParsedPage(
            status=status,
            records=(),
            exam_assets=(),
            official_counts=None,
            parsed_counts=ParsedCounts(0, 0, 0, 0),
            stats_text=stats_text,
            source_url=source_url,
            observed_at=observed,
            warnings=tuple(warnings),
        )

    official_counts: OfficialCounts | None = None
    base_status = ParseStatus.OK
    if LIMIT_PATTERN.search(stats_text):
        base_status = ParseStatus.LIMIT_EXCEEDED
        warnings.append("官方回覆結果超過 6,000 筆上限，需縮小年度或條件")
    elif NO_RESULT_TEXT in stats_text:
        base_status = ParseStatus.NO_RESULT
    else:
        match = STATS_PATTERN.search(stats_text)
        if match:
            official_counts = OfficialCounts(*(int(value) for value in match.groups()))
        else:
            base_status = ParseStatus.STRUCTURE_CHANGED
            warnings.append(f"統計文字格式無法辨識：{stats_text or '（空白）'}")

    records: list[ExamRecord] = []
    exam_assets: list[ExamAsset] = []
    exam_names: set[str] = set()
    categories: set[tuple[str, str]] = set()
    duplicate_rows = 0

    record_index: dict[str, int] = {}
    seen_exam_assets: set[tuple[str, str, str]] = set()
    current_exam = ""
    current_exam_code: str | None = None
    current_category = ""
    current_category_code: str | None = None

    for row in table.find_all("tr", recursive=False):
        level1 = row.find("td", class_="level1")
        level2 = row.find("td", class_="level2")
        title = row.find("label", class_="exam-title")

        # 考試層級列
        if level1 is not None:
            current_exam = _exam_name_from_level1(level1)
            exam_names.add(current_exam)
            current_exam_code = None
            for anchor in level1.find_all("a", href=True):
                params = _asset_params(anchor.get("href", ""))
                code = params.get("code")
                if current_exam_code is None and code:
                    current_exam_code = code
                role_info = ROLE_BY_TYPE.get(params.get("t", ""))
                if role_info is None or not code:
                    continue
                role, scope = role_info
                key = (code, params.get("t", ""), params.get("q", ""))
                if key in seen_exam_assets:
                    continue
                seen_exam_assets.add(key)
                exam_assets.append(
                    ExamAsset(
                        asset_id=build_exam_asset_id(code, params.get("t", ""), len(exam_assets) + 1),
                        official_id=f"moex:{code}:{params.get('t', '')}",
                        role=role,
                        original_label=normalize_text(anchor.get_text(" ", strip=True)),
                        url=urllib.parse.urljoin(ASSET_ENDPOINT, anchor.get("href", "")),
                        observed_at=observed,
                        source_version=None,
                        evidence=_evidence_for_answer()
                        if role is AssetRole.CHOICE_ANSWER
                        else TypeEvidence(
                            question_type=QuestionType.UNKNOWN,
                            source="html:official-label",
                            reason="考試層級附件（非 PDF 或不屬單一考科），不作為題型證據",
                            rule_version=RULE_VERSION,
                        ),
                        scope=scope,
                        content_hint=_content_hint(anchor, role),
                    )
                )
            continue

        # 類科列（可能有 checkbox，也可能是無 checkbox 的 span）
        if level2 is not None:
            current_category, category_control = _category_text(level2)
            current_category_code = _official_codes(category_control, {})[1]
            categories.add((current_exam, current_category))
            continue

        # 科目列
        if title is None:
            continue

        subject = normalize_text(title.get_text(" ", strip=True))
        box = row.find("input", attrs={"type": "checkbox"})
        control = box.get("name") if box else None

        anchors = [
            anchor
            for anchor in row.find_all("a", href=True)
            if "wHandExamQandA_File.ashx" in (anchor.get("href") or "")
        ]
        parsed_links: list[tuple[str, dict[str, str], Tag]] = [
            (anchor.get("href", ""), _asset_params(anchor.get("href", "")), anchor)
            for anchor in anchors
        ]
        first_params = parsed_links[0][1] if parsed_links else {}
        exam_code, category_code, subject_code = _official_codes(control, first_params)
        exam_code = exam_code or current_exam_code
        category_code = category_code or current_category_code

        has_answer = any(params.get("t") == "S" for _, params, _ in parsed_links)
        record_id, official_id, strategy = build_record_id(
            exam_code, category_code, subject_code, current_exam, current_category, subject
        )

        assets: list[ExamAsset] = []
        seen_urls: set[str] = set()
        ordinal_by_type: dict[str, int] = {}
        for href, params, anchor in parsed_links:
            type_code = params.get("t", "X")
            role_info = ROLE_BY_TYPE.get(type_code)
            if role_info is None:
                warnings.append(f"未知附件型別 t={type_code!r}（{subject}）")
                role, scope = AssetRole.OTHER, AssetScope.SUBJECT
            else:
                role, scope = role_info
            if scope is not AssetScope.SUBJECT:
                continue
            url = urllib.parse.urljoin(ASSET_ENDPOINT, href)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            ordinal_by_type[type_code] = ordinal_by_type.get(type_code, 0) + 1
            label = normalize_text(anchor.get_text(" ", strip=True)) or (
                "試題" if role is AssetRole.QUESTION else "答案"
            )
            assets.append(
                ExamAsset(
                    asset_id=build_asset_id(record_id, type_code, ordinal_by_type[type_code]),
                    official_id=(
                        f"moex:{exam_code}:{category_code}:{subject_code}:"
                        f"{params.get('t', '')}{params.get('q', '')}"
                        if exam_code and category_code and subject_code
                        else None
                    ),
                    role=role,
                    original_label=label,
                    url=url,
                    observed_at=observed,
                    source_version=None,
                    evidence=(
                        _evidence_for_question(has_answer)
                        if role is AssetRole.QUESTION
                        else _evidence_for_answer()
                    ),
                    scope=scope,
                    content_hint=_content_hint(anchor, role),
                )
            )

        year = _year_from_exam_name(current_exam)
        if year is None:
            warnings.append(f"考試名稱無法取得民國年度：{current_exam or '（空白）'}")
            if base_status is ParseStatus.OK:
                base_status = ParseStatus.STRUCTURE_CHANGED
            year = 0

        existing_index = record_index.get(record_id)
        if existing_index is not None:
            duplicate_rows += 1
            existing = records[existing_index]
            known = {asset.url for asset in existing.assets}
            merged = existing.assets + tuple(a for a in assets if a.url not in known)
            records[existing_index] = replace(existing, assets=merged)
            continue

        record = ExamRecord(
            record_id=record_id,
            official_id=official_id,
            year=year,
            exam_name=current_exam,
            exam_short_name=None,
            category=current_category,
            subject=subject,
            level=None,
            source_page=source_url,
            assets=tuple(assets),
            exam_name_key=compact_key(current_exam),
            category_key=compact_key(current_category),
            subject_key=compact_key(subject),
            observed_at=observed,
            id_strategy=strategy,
        )
        record_index[record_id] = len(records)
        records.append(record)

    parsed_counts = ParsedCounts(
        exams=len(exam_names),
        categories=len(categories),
        subjects=len(records),
        duplicate_rows=duplicate_rows,
    )

    if base_status is ParseStatus.OK and parsed_counts.subjects == 0 and (
        official_counts is None
        or official_counts.subjects > 0
        or official_counts.categories > 0
        or official_counts.exams > 0
    ):
        base_status = ParseStatus.STRUCTURE_CHANGED
        warnings.append("結果表格存在但沒有任何考科列")
    elif base_status is ParseStatus.OK and official_counts is not None:
        if (
            parsed_counts.exams != official_counts.exams
            or parsed_counts.categories != official_counts.categories
            or parsed_counts.subjects != official_counts.subjects
        ):
            # 只有統計文字與列數不一致，沒有結構或上限問題：仍屬可用快照。
            # 但若合併過重複列才造成短少，代表欄位結構異常，維持「未完整」。
            if parsed_counts.duplicate_rows > 0:
                base_status = ParseStatus.INCOMPLETE
                warnings.append(
                    "重複列合併後筆數與統計不符：官方 "
                    f"{official_counts.exams}/{official_counts.categories}/{official_counts.subjects}"
                    "；實際解析 "
                    f"{parsed_counts.exams}/{parsed_counts.categories}/{parsed_counts.subjects}"
                )
            else:
                base_status = ParseStatus.OK_WITH_DIFF
                warnings.append(
                    "統計不符：官方 "
                    f"{official_counts.exams}/{official_counts.categories}/{official_counts.subjects}"
                    "；實際解析 "
                    f"{parsed_counts.exams}/{parsed_counts.categories}/{parsed_counts.subjects}"
                )
    if base_status is ParseStatus.NO_RESULT and parsed_counts.subjects > 0:
        base_status = ParseStatus.STRUCTURE_CHANGED
        warnings.append("統計顯示查無結果，但表格仍有考科列")

    return ParsedPage(
        status=base_status,
        records=tuple(records),
        exam_assets=tuple(exam_assets),
        official_counts=official_counts,
        parsed_counts=parsed_counts,
        stats_text=stats_text,
        source_url=source_url,
        observed_at=observed,
        warnings=tuple(warnings),
    )


def parse_search_form(html: str, *, base_url: str = SOURCE_PAGE) -> SearchFormPage:
    """解析查詢表單頁，取得 hidden fields、下拉選項與 postback 目標。"""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", id="aspnetForm") or soup.find("form")

    hidden_fields: dict[str, str] = {}
    control_names: list[str] = []
    postback_targets: list[str] = []
    text_fields: list[str] = []
    select_values: dict[str, str] = {}

    for element in soup.select("input, select"):
        name = element.get("name")
        if not name:
            continue
        control_names.append(name)
        if element.name == "input":
            kind = (element.get("type") or "").lower()
            if kind == "hidden":
                hidden_fields[name] = element.get("value", "")
            elif kind == "text":
                text_fields.append(name)
        else:
            chosen = element.find("option", selected=True) or element.find("option")
            select_values[name] = chosen.get("value", "") if chosen else ""
        onchange = element.get("onchange") or ""
        # ASP.NET 產生的屬性值會用 \' 跳脫引號，先還原再取出 postback 目標
        for target in re.findall(r"__doPostBack\('([^']+)'", onchange.replace("\\", "")):
            postback_targets.append(target)

    def options_of(control: str) -> tuple[SelectOption, ...]:
        element = soup.find("select", attrs={"name": control})
        if element is None:
            return ()
        return tuple(
            SelectOption(
                value=option.get("value", ""),
                text=normalize_text(option.get_text(" ", strip=True)),
                selected=option.has_attr("selected"),
            )
            for option in element.find_all("option")
        )

    action = base_url
    if form is not None and form.get("action"):
        action = urllib.parse.urljoin(base_url, form.get("action"))

    return SearchFormPage(
        action=action,
        hidden_fields=hidden_fields,
        year_options=options_of(YEAR_START_CONTROL),
        exam_options=options_of(EXAM_CODE_CONTROL),
        control_names=tuple(control_names),
        postback_targets=tuple(dict.fromkeys(postback_targets)),
        text_fields=tuple(text_fields),
        select_values=select_values,
    )
