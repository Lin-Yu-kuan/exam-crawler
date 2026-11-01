"""考選部 WebForms 查詢客戶端：Session、表單事件、重試與網域驗證。

邊界：只負責 HTTP 與表單事件，**不寫檔、不解析成模型、不處理 UI**。
同一 Client 的查詢事件一律串行（可變表單狀態不可並行使用）。
"""

from __future__ import annotations

import email.utils
import random
import time
import urllib.parse
from datetime import datetime, timezone

import httpx

from app.crawler.parser import EXAM_CODE_CONTROL, YEAR_END_CONTROL, YEAR_START_CONTROL, parse_search_form
from app.crawler.rate_limiter import RateLimiter
from app.models import SOURCE_PAGE, SearchFormPage

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) kaoshi-practice-tool/0.1"
DEFAULT_ALLOWED_HOSTS = ("wwwq.moex.gov.tw", "www.moex.gov.tw")
MAX_REDIRECTS = 5
ERROR_PAGE_MARKER = "htmNotFoundPage"
ROC_OFFSET = 1911


class SourceError(Exception):
    """來源存取失敗的基礎例外；message 為可顯示的繁中說明。"""

    def __init__(self, message: str, *, category: str = "source-error") -> None:
        super().__init__(message)
        self.category = category


class BlockedHost(SourceError):
    """網域或協定不在白名單，拒絕連線。"""

    def __init__(self, url: str) -> None:
        super().__init__(f"網址不在允許的官方來源內，已拒絕連線：{url}", category="blocked-host")


class AccessDenied(SourceError):
    """403：可能是權限或存取條件不符，不得當成限流重試。"""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"來源拒絕存取（HTTP 403）：{url}。已停止對此來源的新請求，請稍後再試或確認使用規範",
            category="access-denied",
        )


class NotFound(SourceError):
    """404：永久錯誤，不重試。"""

    def __init__(self, url: str) -> None:
        super().__init__(f"找不到資源（HTTP 404）：{url}", category="not-found")


class RateLimited(SourceError):
    """持續限流，需暫停任務。"""

    def __init__(self, url: str, attempts: int) -> None:
        super().__init__(
            f"來源持續限流（已嘗試 {attempts} 次）：{url}，請稍後再試",
            category="rate-limited",
        )


class SourceErrorPage(SourceError):
    """來源回傳錯誤頁（可能仍是 HTTP 200）。"""

    def __init__(self, url: str) -> None:
        super().__init__(f"來源回應為錯誤頁：{url}", category="error-page")


class ViewStateExpired(SourceError):
    """表單狀態（ViewState）失效，需要重建 Session。"""

    def __init__(self) -> None:
        super().__init__("表單狀態已失效，已重建連線", category="viewstate-expired")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """解析 Retry-After（秒數或 HTTP 日期）。"""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


class MoexClient:
    """考選部考畢試題查詢客戶端。

    - 每次請求都經過共用限速器（含在途上限與全域啟動間隔）。
    - 重新導向逐跳驗證網域。
    - 429／503 依 Retry-After 退避；403 立即停止；404 不重試。
    - 查詢前依最新回應重建表單狀態；年度（迄）以 postback 設定。
    """

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
        allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
        timeout: httpx.Timeout | float = httpx.Timeout(10.0, read=30.0),
        max_attempts: int = 4,
        start_url: str = SOURCE_PAGE,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._limiter = rate_limiter or RateLimiter()
        self._allowed_hosts = tuple(host.lower() for host in allowed_hosts)
        self._max_attempts = max(1, max_attempts)
        self._start_url = start_url
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._http = httpx.Client(
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
            timeout=timeout,
        )
        self._form: SearchFormPage | None = None
        self._values: dict[str, str] = {}
        self.request_count = 0
        self.postback_count = 0

    # --- 生命週期 -----------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MoexClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def reset(self) -> None:
        """重建 Session：清除 Cookie 與表單狀態。"""
        self._http.cookies.clear()
        self._form = None
        self._values = {}

    # --- 網域與請求 ---------------------------------------------------
    def _validate_url(self, url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise BlockedHost(url)
        host = (parts.hostname or "").lower()
        if host not in self._allowed_hosts:
            raise BlockedHost(url)

    def _send_once(self, method: str, url: str, data: dict[str, str] | None) -> httpx.Response:
        """單一請求（含逐跳重新導向驗證）。"""
        current_url = url
        current_method = method
        current_data = data
        for _ in range(MAX_REDIRECTS + 1):
            self._validate_url(current_url)
            with self._limiter.slot():
                response = self._http.request(current_method, current_url, data=current_data)
                self.request_count += 1
            if response.is_redirect and response.headers.get("location"):
                current_url = urllib.parse.urljoin(current_url, response.headers["location"])
                if response.status_code in (301, 302, 303) and current_method == "POST":
                    current_method, current_data = "GET", None
                continue
            return response
        raise SourceError(f"重新導向次數過多：{url}", category="too-many-redirects")

    def _request(
        self, method: str, url: str, data: dict[str, str] | None = None
    ) -> httpx.Response:
        """帶重試政策的請求。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send_once(method, url, data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_attempts:
                    raise SourceError(
                        f"連線失敗（{type(exc).__name__}），已重試 {attempt} 次：{url}",
                        category="transport-error",
                    ) from exc
                self._limiter.wait_backoff(attempt)
                continue

            status = response.status_code
            if status in (429, 503):
                retry_after = _retry_after_seconds(response)
                if retry_after:
                    self._limiter.apply_retry_after(retry_after)
                elif attempt < self._max_attempts:
                    self._limiter.wait_backoff(attempt)
                if attempt >= self._max_attempts:
                    raise RateLimited(url, attempt)
                continue
            if status == 403:
                raise AccessDenied(url)
            if status == 404:
                raise NotFound(url)
            if status >= 500:
                if attempt >= self._max_attempts:
                    raise SourceError(
                        f"來源暫時無法使用（HTTP {status}）：{url}", category="server-error"
                    )
                self._limiter.wait_backoff(attempt)
                continue
            if status >= 400:
                raise SourceError(f"來源回應 HTTP {status}：{url}", category="http-error")
            if ERROR_PAGE_MARKER in str(response.url):
                raise SourceErrorPage(str(response.url))
            return response

    # --- 表單狀態 -----------------------------------------------------
    def _remember_form(self, html: str) -> SearchFormPage:
        form = parse_search_form(html, base_url=self._start_url)
        self._form = form
        self._values = dict(form.select_values)
        return form

    def _form_page(self, *, force: bool = False) -> SearchFormPage:
        if self._form is None or force:
            response = self._request("GET", self._start_url)
            return self._remember_form(response.text)
        return self._form

    def _payload(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        data = dict(self._form.hidden_fields if self._form else {})
        data.update(self._values)
        if extra:
            data.update(extra)
        return data

    def _postback(self, control: str, value: str) -> None:
        """送出 select 變更事件：必須以 __EVENTTARGET 觸發才生效。"""
        form = self._form_page()
        self._values[control] = value
        response = self._request(
            "POST",
            form.action,
            self._payload({"__EVENTTARGET": control, "__EVENTARGUMENT": ""}),
        )
        self.postback_count += 1
        self._remember_form(response.text)

    # --- 對外查詢 -----------------------------------------------------
    def fetch_form(self, *, force: bool = False) -> SearchFormPage:
        """取得（或重新取得）查詢表單狀態。"""
        return self._form_page(force=force)

    def set_end_year(self, year_roc: int) -> None:
        """以 postback 設定年度（迄）。"""
        target = str(year_roc + ROC_OFFSET)
        form = self._form_page()
        if self._values.get(YEAR_END_CONTROL) == target:
            return
        self._postback(YEAR_END_CONTROL, target)

    def search(
        self,
        *,
        year_roc: int,
        exam_code: str = "",
        extra_fields: dict[str, str] | None = None,
    ) -> str:
        """查詢單一年度，回傳結果頁 HTML。

        WebForms 狀態失效時重建 Session 並重播查詢步驟，最多 1 次。
        """
        last_error: SourceError | None = None
        for attempt in (1, 2):
            try:
                if attempt > 1:
                    self.reset()
                form = self._form_page()
                self.set_end_year(year_roc)
                form = self._form_page()
                extra = {
                    YEAR_START_CONTROL: str(year_roc + ROC_OFFSET),
                    EXAM_CODE_CONTROL: exam_code,
                    "ctl00$holderContent$btnSearch": "查詢",
                    "__EVENTTARGET": "",
                    "__EVENTARGUMENT": "",
                }
                if extra_fields:
                    extra.update(extra_fields)
                self._values[EXAM_CODE_CONTROL] = exam_code
                self._values[YEAR_START_CONTROL] = str(year_roc + ROC_OFFSET)
                response = self._request("POST", form.action, self._payload(extra))
                if ERROR_PAGE_MARKER in str(response.url):
                    raise ViewStateExpired()
                return response.text
            except (SourceErrorPage, ViewStateExpired) as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise SourceError("查詢失敗", category="query-failed")
