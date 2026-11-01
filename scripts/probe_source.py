"""官方來源探測腳本（P1-1 用）。

用途：以少量、限速請求確認考選部試題查詢頁的表單結構與回應特徵，
並把去敏感化的 HTML 存成 fixture 供後續離線解析測試使用。

限制：
- 只發送少量請求，請求間隔至少 1.5 秒。
- 不保存 Cookie、Session 標頭或表單 token 以外的敏感資訊（token 會遮罩）。
- 不支援 async；一次只跑一個流程。

使用方式（PowerShell）：
    python scripts/probe_source.py --out tests/fixtures
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import re
import time

import httpx

ENTRY_URL = "https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) study-tool-source-validation/0.1"

_last_request_at = 0.0


def throttle(low: float = 1.5, high: float = 2.5) -> None:
    """全域請求間隔，避免對官方站點造成負擔。"""
    global _last_request_at
    wait = low + (high - low) * random.random() - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch(url: str) -> tuple[int, str, str, bytes]:
    """發送單次 GET，回傳 (狀態碼, 最終網址, Content-Type, 內容位元組)。"""
    throttle()
    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        resp = client.get(url, timeout=30)
        return resp.status_code, str(resp.url), resp.headers.get("Content-Type", ""), resp.content


def decode(body: bytes, content_type: str) -> str:
    """依標頭／頁面宣告解碼；解不出來就記錄而非靜默忽略。"""
    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    else:
        head = body[:2048].decode("ascii", errors="ignore")
        m = re.search(r'charset=["\']?([\w\-]+)', head, re.I)
        if m:
            charset = m.group(1)
    try:
        return body.decode(charset)
    except UnicodeDecodeError:
        return f"<DECODE-ERROR charset={charset} bytes={len(body)}>"


def mask_tokens(html: str) -> str:
    """遮罩 ViewState 等表單 token 值，只留下欄位名稱與長度。

    不依賴屬性順序（原始頁面與 BeautifulSoup 重新序列化後的順序不同）。
    """

    def mask_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        name_match = re.search(r'name="(__[^"]+)"', tag, re.I)
        if not name_match:
            return tag

        def mask_value(value_match: re.Match[str]) -> str:
            value = value_match.group(1)
            digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:12]
            return f'value="<MASKED len={len(value)} sha256_12={digest}>"'

        return re.sub(r'value="([^"]*)"', mask_value, tag, count=1)

    return re.sub(r"<input\b[^>]*>", mask_tag, html, flags=re.I)


def summarize_form(html: str) -> dict:
    """抽出表單、控制項與 hidden fields 摘要。"""
    forms = re.findall(r"<form\b[^>]*>", html, re.I)
    inputs = re.findall(r"<(input|select)\b([^>]*)>", html, re.I)
    controls = []
    for tag, attrs in inputs:
        def attr(name: str) -> str | None:
            m = re.search(rf'{name}="([^"]*)"', attrs, re.I)
            return m.group(1) if m else None

        controls.append(
            {
                "tag": tag.lower(),
                "type": attr("type"),
                "name": attr("name"),
                "id": attr("id"),
                "value_len": len(attr("value") or ""),
            }
        )
    return {"forms": forms, "controls": controls}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tests/fixtures")
    parser.add_argument("--url", default=ENTRY_URL)
    parser.add_argument("--name", default="entry")
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out) / "source"
    out_dir.mkdir(parents=True, exist_ok=True)

    status, final_url, content_type, body = fetch(args.url)
    html = decode(body, content_type)
    report = {
        "entry_url": args.url,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": (re.search(r"<title>(.*?)</title>", html, re.I | re.S).group(1).strip()
                  if re.search(r"<title>(.*?)</title>", html, re.I | re.S) else None),
        "form_summary": summarize_form(html),
    }

    (out_dir / f"{args.name}.html").write_text(html, encoding="utf-8")
    (out_dir / f"{args.name}.masked.html").write_text(mask_tokens(html), encoding="utf-8")
    (out_dir / f"{args.name}.meta.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "form_summary"}, ensure_ascii=False, indent=2))
    print("controls:", len(report["form_summary"]["controls"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
