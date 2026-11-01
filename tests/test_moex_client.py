"""MoexClient 測試：表單事件、重試政策、網域驗證與 Session 重建（全離線）。"""

from __future__ import annotations

import random
import urllib.parse

import httpx
import pytest
from conftest import FakeMoexServer

from app.crawler.moex_client import (
    AccessDenied,
    BlockedHost,
    MoexClient,
    NotFound,
    RateLimited,
    SourceError,
)
from app.crawler.parser import EXAM_CODE_CONTROL, YEAR_END_CONTROL, YEAR_START_CONTROL
from app.crawler.rate_limiter import RateLimitConfig, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(server: FakeMoexServer, clock: FakeClock | None = None, **kwargs) -> MoexClient:
    clock = clock or FakeClock()
    limiter = RateLimiter(
        RateLimitConfig(max_in_flight=2, min_interval=0.0, max_interval=0.0),
        clock=clock.monotonic,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    return MoexClient(
        transport=httpx.MockTransport(server.handler),
        rate_limiter=limiter,
        sleep=clock.sleep,
        max_attempts=kwargs.pop("max_attempts", 4),
        start_url=kwargs.pop("start_url", "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"),
        **kwargs,
    )


@pytest.fixture()
def server(html_dir, expected_json) -> FakeMoexServer:
    template = (html_dir / "search_form.html").read_text(encoding="utf-8")
    return FakeMoexServer(template, results={113: (html_dir / "result_sample.html").read_text(encoding="utf-8")})


def form_data(request: httpx.Request) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(request.content.decode("utf-8"))


def search_page_gets(server) -> list[httpx.Request]:
    """只計算查詢頁本身的 GET（排除重新導向到錯誤頁的 GET）。"""
    return [request for request in server.gets() if request.url.path.endswith(".aspx")]


def test_search_sets_end_year_with_postback_and_uses_fresh_viewstate(server) -> None:
    """年度（迄）以 postback 設定，查詢使用最新 ViewState。"""
    with make_client(server) as client:
        html = client.search(year_roc=113)

    assert "ctl00_holderContent_tblExamQand" in html
    posts = server.posts()
    assert len(posts) == 2, "應為 1 次年度 postback + 1 次查詢"

    postback = form_data(posts[0])
    assert postback["__EVENTTARGET"] == [YEAR_END_CONTROL]
    assert postback[YEAR_END_CONTROL] == ["2024"]

    search = form_data(posts[1])
    assert search[YEAR_START_CONTROL] == ["2024"]
    # 空字串的 select 值不會被送出，等同官方預設「所有考試簡稱」
    assert search.get(EXAM_CODE_CONTROL, [""]) == [""]
    assert "ctl00$holderContent$btnSearch" in search
    # postback 回應帶回的 ViewState 必須用於後續查詢（不可重送過期值）
    assert search["__VIEWSTATE"] == ["VS2"]
    assert server.viewstate == 2


def test_second_year_reuses_session(server) -> None:
    """同一年度範圍內共用同一表單狀態，只重新設定迄年。"""
    with make_client(server) as client:
        client.search(year_roc=113)
        client.search(year_roc=114)

    assert len(search_page_gets(server)) == 1
    assert len(server.posts()) == 4  # (postback+search) x2
    first_of_second = form_data(server.posts()[2])
    assert first_of_second[YEAR_END_CONTROL] == ["2025"]


def test_error_page_triggers_session_rebuild(server) -> None:
    """第一次查詢遇到錯誤頁時重建 Session 並重播，最多一次。"""
    server.error_first_search = True
    with make_client(server) as client:
        html = client.search(year_roc=113)

    assert "ctl00_holderContent_tblExamQand" in html
    assert len(search_page_gets(server)) == 2, "重建 Session 需重新取得表單"
    assert len(server.posts()) == 4  # 第一次 postback+search，重建後再一次


def test_403_is_not_retried(server) -> None:
    """403 代表權限或存取條件不符，不當成限流重試。"""
    server.force_status = 403
    with make_client(server) as client:
        with pytest.raises(AccessDenied):
            client.fetch_form()
    assert len(server.requests) == 1


def test_404_is_not_retried(server) -> None:
    server.force_status = 404
    with make_client(server) as client:
        with pytest.raises(NotFound):
            client.fetch_form()
    assert len(server.requests) == 1


def test_429_honours_retry_after(server) -> None:
    """429 依 Retry-After 暫停後重試。"""
    clock = FakeClock()
    server.get_script = [{"status": 429, "headers": {"retry-after": "3"}}]
    with make_client(server, clock) as client:
        client.fetch_form()

    assert len(server.gets()) == 2
    assert clock.now >= 3.0 - 1e-9


def test_429_exhausts_attempts(server) -> None:
    clock = FakeClock()
    server.get_script = [{"status": 429, "headers": {"retry-after": "0"}}] * 4
    with make_client(server, clock, max_attempts=4) as client:
        with pytest.raises(RateLimited):
            client.fetch_form()
    assert len(server.gets()) == 4


def test_5xx_retries_then_succeeds(server) -> None:
    clock = FakeClock()
    server.get_script = [{"status": 500}, {"status": 503}, {"status": 500}]
    with make_client(server, clock, max_attempts=4) as client:
        client.fetch_form()
    assert len(server.gets()) == 4
    assert clock.now > 0, "應有退避等待"


def test_5xx_persistent_raises(server) -> None:
    clock = FakeClock()
    server.get_script = [{"status": 500}] * 4
    with make_client(server, clock, max_attempts=4) as client:
        with pytest.raises(SourceError) as excinfo:
            client.fetch_form()
    assert excinfo.value.category == "server-error"
    assert len(server.gets()) == 4


def test_redirect_to_foreign_host_is_blocked(server) -> None:
    server.get_script = [{"status": 302, "headers": {"location": "https://evil.example.com/x"}}]
    with make_client(server) as client:
        with pytest.raises(BlockedHost):
            client.fetch_form()
    assert len(server.requests) == 1


def test_non_whitelisted_start_url_is_blocked(server) -> None:
    with make_client(server, start_url="https://example.com/page") as client:
        with pytest.raises(BlockedHost):
            client.fetch_form()
    assert server.requests == []
