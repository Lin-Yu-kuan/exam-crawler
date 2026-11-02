"""精簡分析的來源還原、分層、快取失效與衝突驗證。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compact_analysis as compact
import exam_files as tool


class CompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pdf = self.root / "來源.pdf"
        self.pdf.write_bytes(b"source fixture; rendering is tested separately")
        self.image = self.root / "page.png"
        self.image.write_bytes(b"image fixture; integrity only")
        self.inventory = tool.inventory(self.root)
        self.sha = tool.digest(self.pdf)
        self.selection = {"schema_version": 1, "study_subject": "資料結構", "classification_version": "1", "target_exam": None,
                          "selected_sources": [{"path": str(self.pdf), "sha256": self.sha, "official_subject": "資料結構與資料庫",
                                                "exam_name": "高考", "year": 114, "category": "資訊處理", "role": "question"}]}
        self.renders = {"kind": "exam-render-index", "documents": [{"sha256": self.sha, "total_pages": 2,
                        "pages": [{"page": p, "status": "rendered", "image_path": str(self.image),
                                   "image_sha256": tool.digest(self.image)} for p in (1, 2)]}]}
        self.topics = [{"topic_id": "TREE", "label": "樹"}, {"topic_id": "GRAPH", "label": "圖"}]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def work(self, prior: dict | None = None) -> dict:
        return compact.prepare(self.inventory, self.selection, self.renders, self.topics, prior, batch_size=1)

    def results(self, work: dict) -> list[dict]:
        return [{"source": next(iter(work["sources"])), "reviewed_pages": [1, 2], "questions": [
            {"p": 1, "q": "1", "r": "r", "t": ["TREE", "TREE", "GRAPH"]},
            {"p": 2, "q": "2", "r": "i", "t": []}]}]

    def test_hydration_and_unique_topic_statistics(self) -> None:
        work = self.work()
        result = compact.merge(work, self.results(work))
        self.assertEqual(result["status"], "complete")
        q = result["questions"][0]
        self.assertEqual(q["source_refs"][0]["official_subject"], "資料結構與資料庫")
        self.assertEqual(q["image_evidence"][0]["image_sha256"], tool.digest(self.image))
        stats = compact.summarize(result)
        self.assertEqual(stats["unique_related_questions"], 1)
        self.assertEqual([topic["count"] for topic in stats["topics"]], [1, 1])

    def test_packet_excludes_registry_and_full_text(self) -> None:
        work = self.work()
        packet = compact.packet(work, work["batches"][0]["batch_id"])
        self.assertEqual(len(packet["pages"]), 1)
        self.assertNotIn("sources", packet)
        self.assertNotIn("refs", packet)
        self.assertNotIn("text", packet["pages"][0])

    def test_repeated_result_deduplicated_conflict_rejected(self) -> None:
        work = self.work()
        results = self.results(work)
        index = compact.merge(work, results + results)
        self.assertEqual(len(index["questions"]), 2)
        conflicting = deepcopy(results)
        conflicting[0]["questions"][0]["t"] = ["GRAPH"]
        with self.assertRaises(ValueError):
            compact.merge(work, results + conflicting)

    def test_unchecked_and_unknown_not_in_confirmed_counts(self) -> None:
        work = self.work()
        results = self.results(work)
        results[0]["reviewed_pages"] = []
        index = compact.merge(work, results)
        self.assertEqual(index["status"], "partial")
        self.assertTrue(any("visual-review" in warning for warning in index["warnings"]))
        self.assertEqual(compact.summarize(index)["unique_related_questions"], 0)
        results[0]["reviewed_pages"] = [1, 2]
        results[0]["questions"][0].update(r="u", t=[], why="圖形模糊")
        index = compact.merge(work, results)
        self.assertEqual(index["status"], "partial")
        self.assertEqual(compact.summarize(index)["pending_questions"], 1)

    def test_whole_document_cache_and_classification_invalidation(self) -> None:
        work = self.work()
        index = compact.merge(work, self.results(work))
        again = self.work(index)
        self.assertEqual(again["batches"], [])
        self.assertEqual(len(compact.merge(again, [])["questions"]), 2)
        self.topics[0]["label"] = "樹與索引"
        self.assertEqual(len(self.work(index)["batches"]), 2)

    def test_image_change_invalidates_prepare_and_merge(self) -> None:
        work = self.work()
        self.image.write_bytes(b"new image")
        with self.assertRaises(ValueError):
            self.work()
        with self.assertRaises(ValueError):
            compact.merge(work, self.results(work))

    def test_target_exam_statistics_and_parent_not_counted(self) -> None:
        self.selection["target_exam"] = {"exam_name": "普考"}
        work = self.work()
        index = compact.merge(work, self.results(work))
        stats = compact.summarize(index)
        self.assertEqual(stats["unique_related_questions"], 0)
        self.assertEqual(stats["supplemental_related_questions"], 1)
        self.selection["target_exam"] = "高考"
        work = self.work()
        results = self.results(work)
        results[0]["questions"][0]["count"] = False
        self.assertEqual(compact.summarize(compact.merge(work, results))["unique_related_questions"], 0)

    def test_unknown_topic_and_out_of_scope_page_rejected(self) -> None:
        work = self.work()
        results = self.results(work)
        results[0]["questions"][0]["t"] = ["UNKNOWN"]
        with self.assertRaises(ValueError):
            compact.merge(work, results)
        results = self.results(work)
        results[0]["reviewed_pages"].append(3)
        with self.assertRaises(ValueError):
            compact.merge(work, results)

    def test_added_document_only_schedules_new_work(self) -> None:
        work = self.work()
        prior = compact.merge(work, self.results(work))
        other = self.root / "new.pdf"
        other.write_bytes(b"different content")
        sha = tool.digest(other)
        self.inventory = tool.inventory(self.root)
        self.selection["selected_sources"].append({**self.selection["selected_sources"][0], "path": str(other), "sha256": sha})
        doc = deepcopy(self.renders["documents"][0])
        doc["sha256"] = sha
        self.renders["documents"].append(doc)
        next_work = self.work(prior)
        self.assertEqual(len(next_work["cached"]), 1)
        self.assertEqual(len(next_work["batches"]), 2)

    def test_compact_response_is_smaller_without_losing_references(self) -> None:
        work = self.work()
        response = self.results(work)
        index = compact.merge(work, response)
        compact_size = len(json.dumps(response, ensure_ascii=False).encode())
        expanded_size = len(json.dumps(index["questions"], ensure_ascii=False).encode())
        self.assertLess(compact_size, expanded_size)
        self.assertEqual(len(index["questions"][0]["source_refs"]), 1)

    def visual_review(self, statuses: dict[int, str] | None = None, available: bool = True) -> dict:
        statuses = statuses or {1: "checked", 2: "checked"}
        return {"schema_version": 1,
                "host_vision": {"available": available, "tool": "test", "probe_image": str(self.image),
                                "probe_result": "ok" if available else "unavailable"},
                "pages": [{"source_sha256": self.sha, "page": page, "image_path": str(self.image),
                           "image_sha256": tool.digest(self.image), "visual_status": status,
                           "observations": "synthetic", "reason": ""}
                          for page, status in statuses.items()]}

    def test_contract_fields_and_visual_review(self) -> None:
        work = self.work()
        index = compact.merge(work, self.results(work), self.visual_review())
        self.assertEqual(index["schema_version"], 3)
        self.assertEqual(index["status"], "complete")
        self.assertEqual(index["source_status"][0]["status"], "processed")
        self.assertEqual(index["coverage"]["selected_files"], 1)
        self.assertEqual(index["coverage"]["processed_files"], 1)
        self.assertEqual(index["coverage"]["selected_pages"], 2)
        self.assertEqual(index["coverage"]["rendered_pages"], 2)
        self.assertEqual(index["coverage"]["visually_checked_pages"], 2)
        self.assertEqual(index["coverage"]["pending_visual_pages"], 0)
        self.assertEqual(index["coverage"]["unrendered_pages"], 0)
        self.assertIsNone(index["coverage"]["text_extracted_pages"])
        question = index["questions"][0]
        self.assertEqual(question["visual_status"], "checked")
        self.assertEqual(question["analysis_status"], "completed")
        self.assertEqual(question["image_evidence"][0]["image_sha256"], tool.digest(self.image))
        self.assertEqual(question["answer_refs"], [])
        self.assertEqual(question["duplicate_candidates"], [])
        stats = compact.summarize(index)
        self.assertEqual(stats["coverage"]["processed_files"], 1)
        self.assertEqual(stats["pending_questions"], 0)

    def test_visual_review_unreadable_and_unavailable(self) -> None:
        work = self.work()
        index = compact.merge(work, self.results(work), self.visual_review({1: "checked", 2: "unreadable"}))
        self.assertEqual(index["status"], "partial")
        self.assertEqual(index["coverage"]["unreadable_pages"], 1)
        self.assertEqual(index["coverage"]["visually_checked_pages"], 1)
        self.assertEqual(index["source_status"][0]["status"], "needs_review")
        second = next(q for q in index["questions"] if q["pages"] == [2])
        self.assertEqual(second["visual_status"], "unreadable")
        self.assertEqual(second["analysis_status"], "unreadable")
        unavailable = compact.merge(work, [{"source": next(iter(work["sources"])), "reviewed_pages": [],
                                            "questions": []}], self.visual_review({1: "not_checked", 2: "not_checked"},
                                                                                  available=False))
        self.assertEqual(unavailable["status"], "partial")
        self.assertEqual(unavailable["coverage"]["visually_checked_pages"], 0)
        with self.assertRaises(ValueError):
            compact.merge(work, self.results(work), self.visual_review(available=False))

    def test_per_entry_review_does_not_complete_cross_page_question(self) -> None:
        work = self.work()
        sid = next(iter(work["sources"]))
        first = {"source": sid, "reviewed_pages": [1],
                 "questions": [{"p": 1, "q": "1", "r": "r", "t": ["TREE"], "pages": [1, 2]}]}
        second = {"source": sid, "reviewed_pages": [2], "questions": []}
        index = compact.merge(work, [first, second])
        self.assertEqual(index["status"], "partial")
        self.assertEqual(index["questions"][0]["visual_status"], "not_checked")
        complete = {"source": sid, "reviewed_pages": [1, 2],
                    "questions": [{"p": 1, "q": "1", "r": "r", "t": ["TREE"], "pages": [1, 2]}]}
        index = compact.merge(work, [first, complete])
        self.assertEqual(index["status"], "complete")
        self.assertEqual(index["questions"][0]["visual_status"], "checked")

    def test_target_and_metadata_changes_reuse_analysis_cache(self) -> None:
        work = self.work()
        index = compact.merge(work, self.results(work))
        self.selection["target_exam"] = "普考"
        target_changed = self.work(index)
        self.assertEqual(target_changed["batches"], [])
        self.assertEqual(len(target_changed["cached"]), 1)
        self.selection["selected_sources"][0]["year"] = 115
        metadata_changed = self.work(index)
        self.assertEqual(metadata_changed["batches"], [])
        self.assertEqual(len(metadata_changed["cached"]), 1)

    def test_missing_render_pages_are_reported_not_silent(self) -> None:
        self.renders["documents"][0]["pages"].pop()
        work = self.work()
        self.assertTrue(work["warnings"])
        sid = next(iter(work["sources"]))
        self.assertEqual(work["sources"][sid]["missing_pages"], [2])
        self.assertEqual(len(work["batches"]), 1)
        results = [{"source": sid, "reviewed_pages": [1], "questions": [
            {"p": 1, "q": "1", "r": "r", "t": ["TREE"]}]}]
        index = compact.merge(work, results)
        self.assertEqual(index["status"], "partial")
        self.assertEqual(index["coverage"]["selected_pages"], 2)
        self.assertEqual(index["coverage"]["rendered_pages"], 1)
        self.assertEqual(index["coverage"]["pending_visual_pages"], 1)
        self.assertEqual(index["coverage"]["unrendered_pages"], 1)
        self.assertEqual(index["coverage"]["pending_files"], 1)

    def test_packet_is_self_contained_and_has_no_target_bias(self) -> None:
        self.selection["target_exam"] = "高考"
        work = self.work()
        packet = compact.packet(work, work["batches"][0]["batch_id"])
        self.assertTrue(packet["rules"])
        self.assertNotIn("target_exam", packet)
        self.assertEqual(packet["source_roles"], ["question"])
        self.assertGreater(packet["max_sample_chars"], 0)

    def test_answers_duplicates_and_summary_override(self) -> None:
        answer = self.root / "答案.pdf"
        answer.write_bytes(b"answer fixture")
        answer_sha = tool.digest(answer)
        self.inventory = tool.inventory(self.root)
        self.selection["selected_sources"].append(
            {"path": str(answer), "sha256": answer_sha, "official_subject": "資料結構與資料庫",
             "exam_name": "高考", "year": 114, "category": "資訊處理", "role": "official_answer"})
        self.renders["documents"].append(
            {"sha256": answer_sha, "total_pages": 1,
             "pages": [{"page": 1, "status": "rendered", "image_path": str(self.image),
                        "image_sha256": tool.digest(self.image)}]})
        work = self.work()
        sid = next(iter(work["sources"]))
        answer_sid = "S" + answer_sha[:16]
        results = self.results(work)
        results[0]["questions"][0]["answers"] = [
            {"source": answer_sid, "p": 1, "q": "1", "role": "official_answer", "basis": "答案卷"}]
        results[0]["questions"][1]["duplicate_of"] = [f"{sid}-P1-Q1"]
        index = compact.merge(work, results)
        first = next(q for q in index["questions"] if q["question_number"] == "1")
        second = next(q for q in index["questions"] if q["question_number"] == "2")
        self.assertEqual(first["answer_refs"][0]["page"], 1)
        self.assertEqual(first["answer_refs"][0]["role"], "official_answer")
        self.assertEqual(second["duplicate_candidates"][0]["other_question_id"], f"{sid}-P1-Q1")
        stats = compact.summarize(index, target="普考")
        self.assertEqual(stats["unique_related_questions"], 0)
        self.assertEqual(stats["supplemental_related_questions"], 1)
        self.assertIsNone(stats["event_count"])
        self.assertEqual(stats["schema_version"], 2)
        self.assertEqual(stats["answer_pair_count"], 1)
        self.assertEqual(stats["duplicate_candidate_count"], 1)
        reused = compact.merge(self.work(index), [])
        self.assertEqual(len(reused["questions"]), 2)
        self.assertEqual(reused["questions"][0]["answer_refs"][0]["question_number"], "1")


if __name__ == "__main__":
    unittest.main()
