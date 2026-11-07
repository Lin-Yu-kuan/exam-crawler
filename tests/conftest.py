"""共用的 fixture 路徑與 HTML 片段建構工具。

合成 HTML 只用在真實樣本無法涵蓋的邊界情境（拆卷、共用 URL、結構異常），
並在測試名稱中標明「合成」。
"""

from __future__ import annotations

import pathlib

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
HTML_DIR = FIXTURES / "html"
PDF_DIR = FIXTURES / "pdf"
EXPECTED_DIR = FIXTURES / "expected"


@pytest.fixture(scope="session")
def html_dir() -> pathlib.Path:
    return HTML_DIR


@pytest.fixture(scope="session")
def pdf_dir() -> pathlib.Path:
    return PDF_DIR


@pytest.fixture(scope="session")
def expected_json() -> dict:
    import json

    return json.loads((EXPECTED_DIR / "result_sample.expected.json").read_text("utf-8"))


def exam_row(name: str, code: str, with_zip: bool = True) -> str:
    """考試層級列（依 2026-11-01 觀察到的真實標記簡化）。"""
    zip_link = (
        f'<td><a href="wHandExamQandA_File.ashx?t=B&code={code}" '
        f'title="另開視窗，開啓{name}盲用電腦專用試題(zip檔)">盲用電腦<br/>專用試題</a></td>'
        if with_zip
        else ""
    )
    return (
        '<tr><td class="level1"><table border="0"><tr>'
        '<td class="link_1Ua"></td>'
        f'<td style="vertical-align:text-top;">{name}</td>'
        f"{zip_link}"
        f'<td><a href="wHandExamQandA_File.ashx?t=A&code={code}" '
        f'title="另開視窗，開啓{name}所有測驗題標準答案(Pdf檔)">本考試所有測<br/>驗題標準答案</a></td>'
        "</tr></table></td></tr>"
    )


def category_row(exam_code: str, category_code: str, text: str, with_checkbox: bool = True) -> str:
    """類科列；with_checkbox=False 模擬伺服端篩選後的 span 形態。"""
    control = f"ctl00$holderContent$chk_{exam_code}_{category_code}"
    element_id = f"ctl00_holderContent_chk_{exam_code}_{category_code}"
    if not with_checkbox:
        return (
            '<tr><td class="level2"><table border="0"><tr>'
            '<td style="width:20px;"></td><td class="link_2Ua"></td>'
            f"<td><span>{text}</span></td></tr></table></td></tr>"
        )
    return (
        '<tr><td class="level2"><table border="0"><tr>'
        '<td style="width:20px;"></td><td class="link_2Ua"></td>'
        f'<td><input id="{element_id}" name="{control}" type="checkbox"/></td>'
        f'<td><label for="{element_id}">{text}</label></td>'
        "</tr></table></td></tr>"
    )


def subject_row(
    exam_code: str,
    category_code: str,
    subject_code: str,
    name: str,
    question_queries: tuple[str, ...] = ("1",),
    with_answer: bool = True,
) -> str:
    """科目列。question_queries 可給多個卷次（q）以模擬拆卷。"""
    control = f"ctl00$holderContent$chk_{exam_code}_{category_code}_{subject_code}"
    element_id = f"ctl00_holderContent_chk_{exam_code}_{category_code}_{subject_code}"
    links = "".join(
        '<td><a class="exam-question-ans" '
        f'href="wHandExamQandA_File.ashx?t=Q&code={exam_code}&c={category_code}'
        f'&s={subject_code}&q={q}" title="另開視窗，開啓{name}試題(Pdf檔)">試題</a></td>'
        for q in question_queries
    )
    answer = (
        '<td><a class="exam-question-ans" '
        f'href="wHandExamQandA_File.ashx?t=S&code={exam_code}&c={category_code}'
        f'&s={subject_code}&q=1" title="另開視窗，開啓{name}答案(Pdf檔)">答案</a></td>'
        if with_answer
        else "<td><a></a></td>"
    )
    return (
        '<tr><td><table border="0"><tr>'
        '<td style="width:35px;"></td><td class="link_3Ua"></td>'
        f'<td><input id="{element_id}" name="{control}" type="checkbox"/></td>'
        f'<td><label class="exam-title" for="{element_id}">{name}</label></td>'
        f"{links}{answer}"
        "</tr></table></td></tr>"
    )


def result_page(rows: str, stats: str) -> str:
    """組出最小可解析的結果頁（無 tbody，與真實頁面同層級）。"""
    return (
        "<html><head><title>考畢試題查詢平臺-考選部</title></head><body>"
        '<form id="aspnetForm" method="post" action="wFrmExamQandASearch.aspx"></form>'
        f'<span id="ctl00_holderContent_lblStatistics">{stats}</span>'
        f'<table id="ctl00_holderContent_tblExamQand">{rows}</table>'
        "</body></html>"
    )


def error_page() -> str:
    """觀察到的錯誤頁（HTTP 200）。"""
    return (
        "<html><head><title>您所輸入的網址並不存在！</title></head>"
        "<body><div>您所輸入的網址並不存在！</div></body></html>"
    )


class FakeMoexServer:
    """模擬考選部查詢站點的 httpx.MockTransport 處理器。

    行為對齊 2026-11-01 的實測：GET 取得表單、select 變更需 postback、
    POST btnSearch 回傳指定年度的結果頁。
    """

    def __init__(
        self,
        template_html: str,
        results: dict[int, str] | None = None,
        *,
        error_first_search: bool = False,
        force_status: int | None = None,
    ) -> None:
        from app.crawler.parser import EXAM_CODE_CONTROL, YEAR_END_CONTROL, YEAR_START_CONTROL

        self.template = template_html
        self.results = results or {}
        self.requests: list[httpx.Request] = []
        self.viewstate = 0
        self.error_first_search = error_first_search
        self.force_status = force_status
        self._errored = False
        self.advanced = False
        self.last_query_fields: dict[str, str] = {}
        self.selected = {
            YEAR_START_CONTROL: "2026",
            YEAR_END_CONTROL: "2026",
            EXAM_CODE_CONTROL: "",
        }
        self.get_script: list[dict] = []

    # --- 內部工具 -----------------------------------------------------
    def _form_html(self) -> str:
        from bs4 import BeautifulSoup as _BS

        soup = _BS(self.template, "lxml")
        self.viewstate += 1
        hidden = soup.find("input", attrs={"name": "__VIEWSTATE"})
        if hidden is not None:
            hidden["value"] = f"VS{self.viewstate}"
        for control, value in self.selected.items():
            select = soup.find("select", attrs={"name": control})
            if select is None:
                continue
            for option in select.find_all("option"):
                if option.has_attr("selected"):
                    del option["selected"]
            match = select.find("option", attrs={"value": value})
            if match is not None:
                match["selected"] = "selected"
        return str(soup)

    def _response_from_script(self, spec: dict) -> httpx.Response:
        return httpx.Response(
            spec.get("status", 200),
            headers=spec.get("headers"),
            text=spec.get("text", ""),
        )

    # --- 處理器 -------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        import urllib.parse as _urlparse

        from app.crawler.moex_client import BTN_QUERY, BTN_SEARCH, IBTN_FULL
        from app.crawler.parser import YEAR_END_CONTROL, YEAR_START_CONTROL

        self.requests.append(request)

        if request.url.path.endswith("htmNotFoundPage.htm"):
            return httpx.Response(200, text=error_page())
        if self.force_status is not None:
            return httpx.Response(self.force_status, text="forced")

        if request.method == "GET":
            if self.get_script:
                return self._response_from_script(self.get_script.pop(0))
            # 全新 GET 等同新 Session：年度選項回到預設值
            from app.crawler.parser import EXAM_CODE_CONTROL

            self.selected = {
                YEAR_START_CONTROL: "2026",
                YEAR_END_CONTROL: "2026",
                EXAM_CODE_CONTROL: "",
            }
            self.advanced = False
            return httpx.Response(200, text=self._form_html())

        data = _urlparse.parse_qs(request.content.decode("utf-8"))
        target = data.get("__EVENTTARGET", [""])[0]
        if IBTN_FULL in data:
            self.advanced = True
            return httpx.Response(200, text=self._form_html())
        if target in (YEAR_START_CONTROL, YEAR_END_CONTROL):
            self.selected[target] = data.get(target, [""])[0]
            return httpx.Response(200, text=self._form_html())

        if BTN_SEARCH in data or BTN_QUERY in data:
            self.last_query_fields = {key: values[0] for key, values in data.items()}
            if self.error_first_search and not self._errored:
                self._errored = True
                return httpx.Response(302, headers={"location": "/htmNotFoundPage.htm"})
            year = int(data.get(YEAR_START_CONTROL, ["0"])[0]) - 1911
            html = self.results.get(year, result_page("", "查無結果"))
            return httpx.Response(200, text=html)

        return httpx.Response(200, text=self._form_html())

    def posts(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]

    def gets(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "GET"]


def synthetic_year_page(year_roc: int, *, subjects: int = 1) -> str:
    """建立指定年度的最小結果頁（用於多年份合併測試）。"""
    exam_code = f"{year_roc}999"
    rows = exam_row(f"{year_roc}年測試考試", exam_code, with_zip=False)
    for index in range(subjects):
        rows += category_row(exam_code, f"{500 + index}", f"測試_類科{index}")
        rows += subject_row(
            exam_code,
            f"{500 + index}",
            f"01{index + 1:02d}",
            f"測試科目{index}",
            with_answer=True,
        )
    return result_page(rows, f"共 1 考試 {subjects} 類科 {subjects} 科目")
