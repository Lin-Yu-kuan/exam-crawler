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


if __name__ == "__main__":
    unittest.main()
