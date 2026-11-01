"""抓取 robots.txt 與錯誤頁樣本（各 1 次請求）。"""

from __future__ import annotations

import pathlib
import random
import time

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) study-tool-source-validation/0.1"
TARGETS = [
    ("robots_wwwq.txt", "https://wwwq.moex.gov.tw/robots.txt"),
    ("error_notfound.html", "https://wwwq.moex.gov.tw/exam/wFrmNotExistPage.aspx"),
]


def main() -> int:
    out = pathlib.Path("tests/fixtures/source")
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for name, url in TARGETS:
            time.sleep(1.5 + random.random())
            resp = client.get(url, timeout=30)
            print(f"[GET] {url} -> {resp.status_code} {len(resp.content)}B {resp.url}")
            (out / name).write_bytes(resp.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
