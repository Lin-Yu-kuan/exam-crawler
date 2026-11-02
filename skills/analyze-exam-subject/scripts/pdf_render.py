"""逐頁渲染與可驗證快取；不呼叫視覺模型、不執行 OCR。"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
from shutil import copyfile
from typing import Any
import uuid

from exam_files import digest, linked, read_json, select_sources, verify_source, write_new

MAX_PIXELS = 16_000_000
RENDER_VERSION = "1"


def bounded_scale(width: float, height: float, dpi: float) -> float:
    """以 ceil 後的實際邊長限制像素總數，保留頁面比例。"""
    if not all(math.isfinite(value) and value > 0 for value in (width, height, dpi)):
        raise ValueError("頁面尺寸與 DPI 必須是有限正數")
    scale = dpi / 72
    if math.ceil(width * scale) * math.ceil(height * scale) <= MAX_PIXELS:
        return scale
    low, high = 0.0, scale
    for _ in range(80):
        middle = (low + high) / 2
        if math.ceil(width * middle) * math.ceil(height * middle) <= MAX_PIXELS:
            low = middle
        else:
            high = middle
    if low <= 0:
        raise ValueError("無法安全縮放頁面")
    return low


def checkpoint(output: Path, data: dict[str, Any]) -> None:
    """只替換本次已建立的索引，以原子更名保存逐頁進度。"""
    temp = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)


def cached_page(row: dict[str, Any], image_root: Path, image_module: Any) -> bool:
    """快取只信任本地圖片雜湊與實際像素，不信任先前的成功標籤。"""
    try:
        path = Path(row["image_path"])
        if (row.get("status") != "rendered" or not path.resolve(strict=True).is_relative_to(image_root.resolve())
                or any(linked(part) for part in [path, *path.parents] if part.is_relative_to(image_root))):
            return False
        if digest(path) != row["image_sha256"]:
            return False
        with image_module.open(path) as image:
            if image.format != "PNG" or list(image.size) != [row["width"], row["height"]]:
                return False
            if image.width * image.height > MAX_PIXELS:
                return False
            image.verify()
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def render_page(pdf: Any, number: int, sha: str, image_root: Path,
                config: dict[str, Any]) -> dict[str, Any]:
    """一次僅持有一頁 bitmap；所有資源均於成功或失敗後關閉。"""
    key = hashlib.sha256(json.dumps([sha, number, config], sort_keys=True).encode()).hexdigest()
    final = image_root / (key + "-" + uuid.uuid4().hex + ".png")
    temp = final.with_suffix(".part")
    try:
        with ExitStack() as stack:
            page = pdf[number - 1]
            stack.callback(page.close)
            width, height = page.get_size()
            scale = bounded_scale(width, height, config["requested_dpi"])
            bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
            stack.callback(bitmap.close)
            image = bitmap.to_pil()
            stack.callback(image.close)
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("渲染器輸出超出像素上限")
            image.save(temp, format="PNG")
            row = {"page": number, "source_sha256": sha, "status": "rendered",
                   "visual_status": "not_checked", "cache_key": key,
                   "image_path": str(final), "image_sha256": digest(temp),
                   "width": image.width, "height": image.height,
                   "actual_dpi": scale * 72, "requested_dpi": config["requested_dpi"],
                   "render_version": config["render_version"], "reused": False}
            os.replace(temp, final)
            return row
    finally:
        temp.unlink(missing_ok=True)


def _normalized_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """舊索引的 DPI 可能是整數；比較前統一成浮點數。"""
    result = dict(config or {})
    if "requested_dpi" in result:
        result["requested_dpi"] = float(result["requested_dpi"])
    return result


def _put_page(doc: dict[str, Any], row: dict[str, Any]) -> None:
    """同頁重試時取代舊資料，不讓索引重複累積。"""
    pages = doc.setdefault("pages", [])
    for index, existing in enumerate(pages):
        if existing.get("page") == row.get("page"):
            pages[index] = row
            return
    pages.append(row)


def render_pdf(data: dict[str, Any], selection: dict[str, Any] | None,
               pages: list[int] | None, output: Path, *, dpi: float = 150.0,
               resume_path: Path | None = None) -> dict[str, Any]:
    """來源與選取重新驗證；每頁落盤，可從相同或新索引恢復。"""
    try:
        import pypdfium2 as pdfium
        from PIL import Image, __version__ as pillow_version
    except ImportError as exc:
        raise ValueError("缺少圖片依賴；請在選定環境執行 python -m pip install pypdfium2 Pillow") from exc
    dpi = float(dpi)
    if not math.isfinite(dpi) or dpi <= 0 or dpi > 1200:
        raise ValueError("DPI 必須介於 0（不含）與 1200 之間")
    root, chosen = select_sources(data, selection, pages)
    # 不可覆寫來源／任意 JSON；相同索引恢復必須先辨識格式。
    output = output.absolute()
    if output.is_symlink() or (output.exists() and linked(output)):
        raise ValueError("索引不可為連結")
    output = output.resolve()
    previous = read_json(resume_path) if resume_path else None
    if previous is not None and (previous.get("kind") != "exam-render-index" or previous.get("schema_version") != 1):
        raise ValueError("resume 不是支援的渲染索引")
    same_output = resume_path is not None and resume_path.resolve() == output
    if output.exists() and not same_output:
        raise ValueError("輸出已存在，請使用新的索引或以 --resume 恢復同一索引")
    config = {"render_version": RENDER_VERSION, "pypdfium_version": str(pdfium.PYPDFIUM_INFO),
              "pdfium_version": str(pdfium.PDFIUM_INFO), "pillow_version": pillow_version,
              "requested_dpi": dpi, "max_pixels": MAX_PIXELS}
    if same_output:
        # 同一個索引只允許相同選取、相同頁碼範圍與相同渲染設定；否則使用新輸出。
        if previous is None:
            raise ValueError("找不到可恢復的渲染索引")
        if _normalized_config(previous.get("config")) != config:
            raise ValueError("同一索引不可變更渲染設定，請使用新輸出")
        if previous.get("page_scope") != pages:
            raise ValueError("同一索引不可變更頁碼範圍，請使用新輸出")
        selected_shas = {item["sha256"] for item in chosen}
        previous_shas = {doc.get("sha256") for doc in previous.get("documents", [])}
        if previous_shas != selected_shas:
            raise ValueError("同一索引僅允許相同選取恢復，請使用新輸出")
    image_root = output.parent / "render-images"
    if image_root.exists() and linked(image_root):
        raise ValueError("圖片目錄不可為連結")
    image_root.mkdir(parents=True, exist_ok=True)
    resume_images = resume_path.resolve().parent / "render-images" if previous is not None else None
    cache: dict[tuple[str, int], dict[str, Any]] = {}
    if previous and _normalized_config(previous.get("config")) == config:
        for doc in previous.get("documents", []):
            for row in doc.get("pages", []):
                if row.get("status") == "rendered":
                    cache[(doc["sha256"], row["page"])] = row
    if same_output:
        # 從舊索引開始，保留尚未走到的頁；後續 checkpoint 不會把進度覆蓋成空白。
        result = previous
        result.update({"schema_version": 1, "kind": "exam-render-index", "config": config,
                       "page_scope": pages, "status": "in_progress"})
        docs_by_sha = {doc["sha256"]: doc for doc in result.get("documents", [])}
        for doc in docs_by_sha.values():
            doc["status"] = "in_progress"
            doc.pop("error", None)
        checkpoint(output, result)
    else:
        result = {"schema_version": 1, "kind": "exam-render-index", "config": config,
                  "page_scope": pages, "status": "in_progress", "documents": []}
        docs_by_sha = {}
        write_new(output, result)
    grouped: dict[str, list[str]] = {}
    for item in chosen:
        path = Path(item["path"])
        try:
            verify_source(path, root, item["sha256"])
            grouped.setdefault(item["sha256"], []).append(str(path))
        except (OSError, ValueError) as exc:
            doc = docs_by_sha.get(item["sha256"])
            if doc is None:
                doc = {"sha256": item["sha256"], "paths": [str(path)], "status": "failed", "pages": []}
                result["documents"].append(doc)
                docs_by_sha[item["sha256"]] = doc
            doc.update({"paths": [str(path)], "status": "failed", "pages": [], "error": str(exc)})
            doc.pop("total_pages", None)
            checkpoint(output, result)
    for sha, paths in grouped.items():
        doc = docs_by_sha.get(sha)
        if doc is None:
            doc = {"sha256": sha, "paths": paths, "status": "in_progress", "pages": []}
            result["documents"].append(doc)
            docs_by_sha[sha] = doc
        else:
            doc["paths"] = paths
            doc["status"] = "in_progress"
            doc.pop("error", None)
        checkpoint(output, result)
        try:
            with pdfium.PdfDocument(paths[0]) as pdf:
                doc["total_pages"] = len(pdf)
                if not len(pdf):
                    raise ValueError("PDF 沒有頁面")
                for number in pages if pages is not None else range(1, len(pdf) + 1):
                    try:
                        if number > len(pdf):
                            raise ValueError("頁碼超出 PDF 範圍")
                        old = cache.get((sha, number))
                        if old and resume_images is not None and cached_page(old, resume_images, Image):
                            old_path = Path(old["image_path"]).resolve(strict=True)
                            if old_path.is_relative_to(image_root.resolve()):
                                # 已在本次目錄內：直接沿用，避免每次恢復都複製一份。
                                row = {**old, "image_path": str(old_path), "reused": True,
                                       "visual_status": "not_checked"}
                            else:
                                # 複製到本次輸出，以便後續僅攜帶本次目錄也能再次恢復。
                                target = image_root / (old["cache_key"] + "-" + uuid.uuid4().hex + ".png")
                                copyfile(old_path, target)
                                row = {**old, "image_path": str(target), "reused": True,
                                       "visual_status": "not_checked"}
                        else:
                            row = render_page(pdf, number, sha, image_root, config)
                    except Exception as exc:
                        row = {"page": number, "source_sha256": sha, "status": "failed",
                               "visual_status": "not_checked", "error": str(exc)}
                    _put_page(doc, row)
                    checkpoint(output, result)
                doc["pages"].sort(key=lambda row: row.get("page", 0))
                seen_pages: set[Any] = set()
                unique_pages = []
                for row in doc["pages"]:
                    if row.get("page") in seen_pages:
                        continue
                    seen_pages.add(row.get("page"))
                    unique_pages.append(row)
                doc["pages"] = unique_pages
                expected = len(pdf) if pages is None else len(pages)
                doc["status"] = "rendered" if len(doc["pages"]) == expected and all(
                    row["status"] == "rendered" for row in doc["pages"]) else "partial"
        except Exception as exc:
            doc.update(status="failed", error=str(exc))
        checkpoint(output, result)
    result["status"] = "rendered" if all(doc["status"] == "rendered" for doc in result["documents"]) else "partial"
    checkpoint(output, result)
    return result
