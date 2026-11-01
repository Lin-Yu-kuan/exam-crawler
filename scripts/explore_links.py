"""探索官方網站結構：抓首頁並列出與「試題／Exam」相關的連結。

每次請求間隔至少 1.5 秒，僅抓少量頁面。
"""

from __future__ import annotations

import random
import re
import time
import urllib.parse

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) study-tool-source-validation/0.1"
CANDIDATES = [
    "https://wwwc.moex.gov.tw/main/home/wfrmHomeContent.aspx",
    "https://www.moex.gov.tw/",
]


def polite_get(client: httpx.Client, url: str) -> httpx.Response | None:
    time.sleep(1.5 + random.random())
    try:
        resp = client.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"[ERR] {url} -> {type(exc).__name__}: {exc}")
        return None
    print(f"[GET] {url} -> {resp.status_code} {len(resp.content)}B {resp.url}")
    return resp


def main() -> int:
    with httpx.Client(headers={"User-Agent": UA}) as client:
        for url in CANDIDATES:
            resp = polite_get(client, url)
            if resp is None or resp.status_code != 200:
                continue
            html = resp.text
            links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S)
            print(f"  total links: {len(links)}")
            for href, text in links:
                label = re.sub(r"<[^>]+>", "", text).strip()
                if any(k in label for k in ("試題", "答案", "Exam")) or re.search(r"exam", href, re.I):
                    print("   ", urllib.parse.urljoin(str(resp.url), href), "|", label[:40])
            title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            print("  title:", title.group(1).strip() if title else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
