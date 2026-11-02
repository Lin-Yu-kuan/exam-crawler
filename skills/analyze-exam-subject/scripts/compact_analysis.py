"""精簡 AI 工作包、來源補齊、分析快取與確定性統計；不呼叫模型。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any

from exam_files import digest, read_json, select_sources, verify_source, write_new

PROTOCOL = 1
RELEVANCE = {"r": "relevant", "c": "cross_domain", "i": "irrelevant", "u": "uncertain"}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare(inventory: dict, selection: dict, renders: dict, topics: list[dict],
            prior: dict | None = None, batch_size: int = 5) -> dict:
    """本機來源登錄與模型工作包分離；快取以整份內容為單位。"""
    if not 1 <= batch_size <= 20:
        raise ValueError("每批頁數必須介於 1～20")
    topic_ids = [topic["topic_id"] for topic in topics]
    if not topics or len(set(topic_ids)) != len(topic_ids) or any(not isinstance(t, str) or not t for t in topic_ids):
        raise ValueError("topics 必須提供不重複的非空 topic_id")
    if not selection.get("classification_version"):
        raise ValueError("請提供 classification_version")
    validate_target(selection.get("target_exam"))
    root, chosen = select_sources(inventory, selection, None)
    chosen_paths = {str(Path(item["path"]).resolve()) for item in chosen}
    for item in chosen:
        verify_source(Path(item["path"]), root, item["sha256"])
    if renders.get("kind") != "exam-render-index":
        raise ValueError("需要 render-index")
    config = {"protocol": PROTOCOL, "study_subject": selection["study_subject"],
              "target_exam": selection.get("target_exam"), "classification_version": selection["classification_version"],
              "topics": topics, "mode": "compact"}
    signature = fingerprint(config)
    sources: dict[str, dict] = {}
    for item in selection["selected_sources"]:
        if str(Path(item["path"]).resolve()) not in chosen_paths:
            raise ValueError("來源不在選取內")
        sha = item["sha256"]
        sid = "S" + sha[:16]
        if sid in sources and sources[sid]["sha256"] != sha:
            raise ValueError("來源短 ID 碰撞")
        source = sources.setdefault(sid, {"sha256": sha, "refs": [], "pages": {}, "total_pages": None})
        if item not in source["refs"]:
            source["refs"].append(item)
    for doc in renders["documents"]:
        sid = "S" + doc["sha256"][:16]
        if sid not in sources or sources[sid]["sha256"] != doc["sha256"]:
            continue
        source = sources[sid]
        source["total_pages"] = doc.get("total_pages")
        for page in doc["pages"]:
            if page["status"] != "rendered":
                continue
            image = Path(page["image_path"])
            if digest(image) != page["image_sha256"]:
                raise ValueError("圖片雜湊不符，請先恢復渲染")
            if str(page["page"]) in source["pages"]:
                raise ValueError("渲染索引同一來源頁碼重複，請先選定使用的圖片版本")
            source["pages"][str(page["page"])] = {"image": str(image), "sha256": page["image_sha256"]}
    reusable = prior.get("cache", {}) if prior and prior.get("config_hash") == signature else {}
    cached: dict[str, dict] = {}
    batches: list[dict] = []
    for sid, source in sources.items():
        # metadata 也影響目標考試分層；路徑改變不影響判讀。
        metadata = [{k: v for k, v in ref.items() if k not in {"path", "metadata_evidence"}} for ref in source["refs"]]
        source["cache_key"] = fingerprint([signature, source["sha256"], source["total_pages"],
                                           {p: row["sha256"] for p, row in source["pages"].items()},
                                           sorted(metadata, key=fingerprint)])
        old = reusable.get(source["cache_key"])
        if old and old.get("complete") is True:
            cached[sid] = old
            continue
        numbers = sorted(map(int, source["pages"]))
        for start in range(0, len(numbers), batch_size):
            batches.append({"batch_id": f"{sid}-{start // batch_size + 1}", "source": sid,
                            "pages": [{"p": p, "image": source["pages"][str(p)]["image"]}
                                      for p in numbers[start:start + batch_size]]})
    return {"schema_version": 1, "kind": "compact-work", "config": config, "config_hash": signature,
            "sources": sources, "batches": batches, "cached": cached}


def packet(work: dict, batch_id: str) -> dict:
    """只輸出當批圖片與分類表，完整來源登錄留在本機。"""
    batch = next((b for b in work["batches"] if b["batch_id"] == batch_id), None)
    if batch is None:
        raise ValueError("找不到工作批次")
    return {"study_subject": work["config"]["study_subject"], "topics": work["config"]["topics"],
            "target_exam": work["config"]["target_exam"], **batch,
            "source_roles": sorted({r.get("role", "unknown") for r in work["sources"][batch["source"]]["refs"]}),
            "reply": "results:[{source,reviewed_pages,questions:[{p,q,r,t}]}]；例外加 why，跨頁加 pages。"}


def validate_target(target: Any) -> None:
    if target is None or isinstance(target, str) and target.strip():
        return
    if isinstance(target, dict) and target and set(target) <= {"exam_name", "category", "year"}:
        if all((type(v) is int if k == "year" else isinstance(v, str) and bool(v.strip())) for k, v in target.items()):
            return
    raise ValueError("target_exam 應為 null、完整考試名稱或 exam_name/category/year 的精確條件物件")


def matches_target(ref: dict, target: Any) -> bool:
    if target is None:
        return True
    constraints = {"exam_name": target} if isinstance(target, str) else target
    return all(ref.get(key) == value for key, value in constraints.items())


def merge(work: dict, results: list[dict]) -> dict:
    """合併精簡判斷並補回來源。只接受已知題型標籤及已選頁碼。"""
    if work.get("kind") != "compact-work":
        raise ValueError("不是 compact-work")
    topic_ids = {t["topic_id"] for t in work["config"]["topics"]}
    questions: dict[str, dict] = {}
    reviewed: dict[str, set[int]] = defaultdict(set)
    entries = [{"source": sid, **entry["result"]} for sid, entry in work.get("cached", {}).items()] + results
    compact_rows: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        sid = entry["source"]
        if sid not in work["sources"]:
            raise ValueError("未知來源 ID")
        source = work["sources"][sid]
        valid_pages = set(map(int, source["pages"]))
        seen = entry.get("reviewed_pages", [])
        if any(type(p) is not int or p not in valid_pages for p in seen):
            raise ValueError("視覺核對頁碼不在來源內")
        reviewed[sid].update(seen)
        for row in entry.get("questions", []):
            if all(ref.get("role") in {"official_answer", "corrected_answer"} for ref in source["refs"]):
                raise ValueError("答案附件不得計為新題目")
            p, number, rel = row["p"], row["q"], row["r"]
            pages = row.get("pages", [p])
            if (type(p) is not int or p not in valid_pages or not isinstance(number, str) or not number.strip()
                    or not pages or min(pages) != p or any(type(v) is not int or v not in valid_pages for v in pages)):
                raise ValueError("題號／跨頁引用無效")
            if rel not in RELEVANCE:
                raise ValueError("未知相關性代碼")
            tags = row.get("t", [])
            if not isinstance(tags, list) or any(tag not in topic_ids for tag in tags):
                raise ValueError("未知主題 ID")
            if rel in {"r", "c"} and not tags:
                raise ValueError("相關題必須有主題")
            if rel in {"c", "u"} and not row.get("why"):
                raise ValueError("交叉／待確認題需提供簡短理由")
            if "count" in row and type(row["count"]) is not bool:
                raise ValueError("count 必須為布林值")
            qid = f"{sid}-P{p}-Q{number}"
            normalized = {"p": p, "q": number, "r": rel, "t": sorted(set(tags)),
                          "pages": sorted(set(pages)), "count": row.get("count", True),
                          "why": row.get("why", ""), "sample": row.get("sample", "")}
            if qid in questions:
                if questions[qid]["_compact"] != normalized:
                    raise ValueError(f"同一題有衝突判斷：{qid}")
                continue
            compact_rows[sid].append(normalized)
            questions[qid] = {"question_id": qid, "content_sha256": source["sha256"], "pages": normalized["pages"],
                              "question_number": number, "countable": normalized["count"], "relevance": RELEVANCE[rel],
                              "topic_ids": normalized["t"], "reason": normalized["why"], "summary": normalized["sample"],
                              "source_refs": source["refs"], "image_evidence": [
                                  {"source_sha256": source["sha256"], "page": n, "image_path": source["pages"][str(n)]["image"],
                                   "image_sha256": source["pages"][str(n)]["sha256"]} for n in normalized["pages"]],
                              "_source": sid, "_compact": normalized}
    cache = {}
    source_status = {}
    for sid, source in work["sources"].items():
        # 即使工作包來自上次，也核對 PDF 與圖片內容，不盲信快取。
        for ref in source["refs"]:
            if digest(Path(ref["path"])) != source["sha256"]:
                raise ValueError("來源已變更，請重新 prepare")
        for image in source["pages"].values():
            if digest(Path(image["image"])) != image["sha256"]:
                raise ValueError("圖片已變更，請重新 render／prepare")
        complete = (type(source["total_pages"]) is int and source["total_pages"] > 0
                    and reviewed[sid] == set(range(1, source["total_pages"] + 1))
                    and all(row["r"] != "u" for row in compact_rows[sid]))
        source_status[sid] = "complete" if complete else "partial"
        cache[source["cache_key"]] = {"complete": complete, "result": {
            "reviewed_pages": sorted(reviewed[sid]), "questions": compact_rows[sid]}}
    for question in questions.values():
        sid = question.pop("_source")
        question.pop("_compact")
        checked = set(question["pages"]).issubset(reviewed[sid])
        question["visual_status"] = "checked" if checked else "not_checked"
        question["interpretation_method"] = "text_and_vision" if checked else "text_only"
        question["analysis_status"] = "completed" if checked and question["relevance"] != "uncertain" else "needs_review"
    total = None if any(s["total_pages"] is None for s in work["sources"].values()) else sum(s["total_pages"] for s in work["sources"].values())
    result = {"schema_version": 2, **work["config"], "config_hash": work["config_hash"],
              "status": "complete" if source_status and all(s == "complete" for s in source_status.values()) else "partial",
              "source_status": source_status, "questions": list(questions.values()), "cache": cache,
              "coverage": {"selected_pages": total, "rendered_pages": sum(len(s["pages"]) for s in work["sources"].values()),
                           "visually_checked_pages": sum(len(p) for p in reviewed.values())}}
    return result


def summarize(index: dict, examples: int = 2) -> dict:
    """計唯一題、主題及年度考試分層，不推測獨立命題事件。"""
    if examples < 0 or examples > 5:
        raise ValueError("每主題代表題數需介於 0～5")
    ids = [q["question_id"] for q in index["questions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("索引包含重複 question_id")
    validate_target(index.get("target_exam"))
    all_related = [q for q in index["questions"] if q.get("countable", True)
               and q.get("analysis_status") == "completed" and q["relevance"] in {"relevant", "cross_domain"}]
    related = [q for q in all_related if any(matches_target(ref, index.get("target_exam")) for ref in q["source_refs"])]
    groups: dict[tuple, set[str]] = defaultdict(set)
    hits = Counter()
    samples: dict[str, list[dict]] = defaultdict(list)
    for q in related:
        for tag in set(q["topic_ids"]):
            hits[tag] += 1
            if len(samples[tag]) < examples:
                samples[tag].append({"id": q["question_id"], "pages": q["pages"],
                                     "summary": q.get("summary", ""), "sources": [r for r in q["source_refs"] if matches_target(r, index.get("target_exam"))]})
        for ref in q["source_refs"]:
            if matches_target(ref, index.get("target_exam")):
                groups[(ref.get("year"), ref.get("exam_name"))].add(q["question_id"])
    return {"schema_version": 1, "study_subject": index["study_subject"], "target_exam": index.get("target_exam"),
            "status": index["status"], "coverage": index["coverage"], "unique_related_questions": len(related),
            "supplemental_related_questions": len(all_related) - len(related),
            "pending_questions": sum(q.get("analysis_status") != "completed" for q in index["questions"]),
            "topics": [{"topic_id": t["topic_id"], "label": t.get("label", t["topic_id"]),
                        "count": hits[t["topic_id"]], "examples": samples[t["topic_id"]]} for t in index["topics"]],
            "by_year_exam": [{"year": key[0], "exam_name": key[1], "unique_questions": len(value)} for key, value in groups.items()],
            "notes": ["主題可多標籤，合計可能超過相關題數。", "年度考試分層不是獨立命題事件計數；未知年份或考試保留 null。"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for name in ("inventory", "selection", "render-index", "topics"):
        prep.add_argument("--" + name, required=True, type=Path)
    prep.add_argument("--prior", type=Path)
    prep.add_argument("--batch-size", type=int, default=5)
    prep.add_argument("--output", required=True, type=Path)
    pack = sub.add_parser("packet")
    pack.add_argument("--work", required=True, type=Path)
    pack.add_argument("--batch-id", required=True)
    pack.add_argument("--output", required=True, type=Path)
    combine = sub.add_parser("merge")
    combine.add_argument("--work", required=True, type=Path)
    combine.add_argument("--results", required=True, nargs="+", type=Path)
    combine.add_argument("--output", required=True, type=Path)
    summary = sub.add_parser("summary")
    summary.add_argument("--index", required=True, type=Path)
    summary.add_argument("--examples", type=int, default=2)
    summary.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("輸出已存在，請使用新檔案")
        if args.command == "prepare":
            result = prepare(read_json(args.inventory), read_json(args.selection), read_json(args.render_index),
                             read_json(args.topics)["topics"], read_json(args.prior) if args.prior else None, args.batch_size)
        elif args.command == "merge":
            result = merge(read_json(args.work), [entry for path in args.results for entry in read_json(path)["results"]])
        elif args.command == "packet":
            result = packet(read_json(args.work), args.batch_id)
        else:
            result = summarize(read_json(args.index), args.examples)
        write_new(args.output, result)
        print(f"已輸出：{args.output}")
        if args.command == "prepare":
            print(json.dumps({"pending_batches": [b["batch_id"] for b in result["batches"]],
                              "reused_sources": len(result["cached"])}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"無法完成：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
