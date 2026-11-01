"""檢查 fixture 是否已遮罩敏感 token（離線）。"""

from __future__ import annotations

import pathlib
import re

for name in [
    "search_form.html",
    "full_form.html",
    "result_sample.html",
    "result_stats_mismatch.html",
    "result_noresult.html",
    "result_limit_exceeded.html",
    "error_notfound.html",
]:
    path = pathlib.Path("tests/fixtures/html") / name
    html = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'name="__VIEWSTATE"[^>]{0,100}', html)
    print(
        f"{name}: size={len(html)} masked={html.count('MASKED')} "
        f"viewstate={html.count('__VIEWSTATE')}"
    )
    if match:
        print("   ", match.group(0)[:110])
