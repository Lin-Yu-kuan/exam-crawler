"""可攜式考題盤點與文字擷取；不做 AI 分類、不修改來源。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

TOOL_VERSION = "1"


def digest(path: Path) -> str:
    """串流計算內容識別。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest_stream(stream)


def _digest_stream(stream: Any) -> str:
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def linked(path: Path) -> bool:
    """Windows junction 與符號連結都不追蹤。"""
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def manifest_metadata(path: Path) -> dict[str, list[dict[str, Any]]]:
    """只讀已知 schema，依內容雜湊連結來源，不讀取資料庫指向的檔案。"""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or str(version[0]) not in {"1", "2"}:
            raise ValueError("不支援的 manifest 版本")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT a.sha256, a.asset_id, a.record_id, a.snapshot_id, a.role,
                   r.year, r.exam_name, r.category, r.subject
            FROM attachments a JOIN snapshot_records r
            ON a.record_id=r.record_id AND a.snapshot_id=r.snapshot_id
            WHERE a.sha256 IS NOT NULL
        """)
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            data = dict(row)
            sha = data.pop("sha256")
            result.setdefault(sha, []).append(data)
        return result
    finally:
        conn.close()


def inventory(root: Path, manifest: Path | None = None) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("來源必須是資料夾")
    warnings: list[str] = []
    metadata: dict[str, list[dict[str, Any]]] = {}
    if manifest:
        try:
            metadata = manifest_metadata(manifest)
        except (sqlite3.Error, OSError, ValueError) as exc:
            warnings.append(f"manifest 未採用：{exc}")
    files: list[dict[str, Any]] = []
    contents: dict[str, list[str]] = {}
    for current, dirs, names in os.walk(root, followlinks=False, onerror=lambda exc: warnings.append(str(exc))):
        parent = Path(current)
        kept = []
        for name in sorted(dirs):
            try:
                if name.casefold() != "_analysis" and not linked(parent / name):
                    kept.append(name)
            except OSError as exc:
                warnings.append(str(exc))
        dirs[:] = kept
        for name in sorted(names):
            path = parent / name
            if path.suffix.casefold() != ".pdf":
                continue
            try:
                if linked(path):
                    warnings.append(f"略過連結：{path}")
                    continue
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise ValueError("檔案超出來源根目錄")
                sha = digest(resolved)
                files.append({"path": str(resolved), "relative_path": str(path.relative_to(root)),
                              "sha256": sha, "size": resolved.stat().st_size, "status": "inventoried",
                              "metadata": metadata.get(sha, [])})
                contents.setdefault(sha, []).append(str(resolved))
            except (OSError, ValueError) as exc:
                files.append({"path": str(path), "status": "failed", "error": str(exc)})
    return {"schema_version": 1, "root": str(root), "files": files,
            "contents": [{"sha256": sha, "paths": paths} for sha, paths in contents.items()],
            "warnings": warnings}


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def extract(data: dict[str, Any], selection: dict[str, Any] | None,
            pages: list[int] | None, resume: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        import pypdf
    except ImportError as exc:
        raise ValueError("缺少 pypdf；請在選定環境執行 python -m pip install pypdf") from exc
    if not selection and pages != [1]:
        raise ValueError("全面擷取前須提供 selection；無選取時只允許 --pages 1")
    if pages is not None and (not pages or any(page < 1 for page in pages)):
        raise ValueError("頁碼必須是正整數")
    root = Path(data["root"]).resolve(strict=True)
    available = {str(Path(item["path"]).resolve()): item for item in data["files"] if item["status"] == "inventoried"}
    chosen = list(available.values())
    if selection is not None:
        if selection.get("schema_version") != 1 or not selection.get("study_subject"):
            raise ValueError("selection 缺少支援的版本或備考科目")
        chosen = []
        for source in selection.get("selected_sources", []):
            key = str(Path(source["path"]).resolve())
            item = available.get(key)
            if not item or item["sha256"] != source.get("sha256"):
                raise ValueError("selection 與盤點來源／雜湊不一致，請重新盤點")
            if item not in chosen:
                chosen.append(item)
        if not chosen:
            raise ValueError("尚未選取來源")
    config = {"tool_version": TOOL_VERSION, "pypdf_version": pypdf.__version__, "page_scope": pages}
    cache = {}
    if resume and all(resume.get(key) == value for key, value in config.items()):
        cache = {doc["sha256"]: doc for doc in resume.get("documents", []) if doc.get("status") == "extracted"}
    grouped: dict[str, list[str]] = {}
    documents: list[dict[str, Any]] = []
    for item in chosen:
        path = Path(item["path"])
        try:
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError("來源已移出根目錄")
            if any(linked(part) for part in [path, *path.parents] if part != root and part.is_relative_to(root)):
                raise ValueError("來源已變成連結")
            if digest(path) != item["sha256"]:
                raise ValueError("來源內容已變更，請重新盤點")
            grouped.setdefault(item["sha256"], []).append(str(path))
        except (OSError, ValueError) as exc:
            documents.append({"sha256": item["sha256"], "paths": [str(path)], "status": "failed", "pages": [], "error": str(exc)})
    for sha, paths in grouped.items():
        if sha in cache:
            documents.append({**cache[sha], "paths": paths, "reused": True})
            continue
        doc: dict[str, Any] = {"sha256": sha, "paths": paths, "status": "extracted", "pages": []}
        try:
            with Path(paths[0]).open("rb") as stream:
                reader = pypdf.PdfReader(stream)
                if reader.is_encrypted and not reader.decrypt(""):
                    raise ValueError("PDF 已加密，無法讀取")
                count = len(reader.pages)
                if not count:
                    raise ValueError("PDF 沒有頁面")
                doc["total_pages"] = count
                for number in pages if pages is not None else range(1, count + 1):
                    try:
                        if number > count:
                            raise ValueError("頁碼超出 PDF 範圍")
                        text = reader.pages[number - 1].extract_text() or ""
                        doc["pages"].append({"page": number, "text": text,
                                             "status": "text_extracted" if text.strip() else "needs_visual_review"})
                    except Exception as exc:
                        doc["pages"].append({"page": number, "text": "", "status": "failed", "error": str(exc)})
                        doc["status"] = "partial"
        except Exception as exc:
            doc.update(status="failed", error=str(exc))
        documents.append(doc)
    return {"schema_version": 1, **config, "documents": documents}


def write_new(path: Path, data: dict[str, Any]) -> None:
    """排他建立輸出，不覆寫既有資料。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory", help="盤點 PDF，不讀取全文")
    inv.add_argument("root", type=Path)
    inv.add_argument("--manifest", type=Path)
    inv.add_argument("--output", required=True, type=Path)
    ext = commands.add_parser("extract", help="擷取已選 PDF 文字")
    ext.add_argument("--inventory", required=True, type=Path)
    ext.add_argument("--selection", type=Path)
    ext.add_argument("--pages", help="一基頁碼，逗號分隔")
    ext.add_argument("--resume", type=Path)
    ext.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("輸出已存在，請選擇新的執行檔案")
        if args.command == "inventory":
            result = inventory(args.root, args.manifest)
        else:
            pages = sorted(set(int(value) for value in args.pages.split(","))) if args.pages else None
            result = extract(read_json(args.inventory), read_json(args.selection) if args.selection else None,
                             pages, read_json(args.resume) if args.resume else None)
        write_new(args.output, result)
        print(f"已輸出：{args.output}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"無法完成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
