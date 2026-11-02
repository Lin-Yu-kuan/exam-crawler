"""端到端 CLI：盤點、渲染、精簡分析、合併、統計。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

HAS_PIL = importlib.util.find_spec("PIL") is not None
HAS_PDFIUM = importlib.util.find_spec("pypdfium2") is not None
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
EXAM = SCRIPTS / "exam_files.py"
COMPACT = SCRIPTS / "compact_analysis.py"


def make_pdf(path: Path) -> None:
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in ("Q1: binary tree", "Q2: TCP handshake"):
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)


@unittest.skipUnless(HAS_PIL and HAS_PDFIUM, "缺少 Pillow 或 pypdfium2，未執行 CLI 端到端測試")
class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "題庫"
        self.root.mkdir()
        self.pdf = self.root / "試題.pdf"
        make_pdf(self.pdf)
        self.skill = SCRIPTS.parent
        self.skill = SCRIPTS.parent

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: object, expect: int = 0) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONUTF8": "1"}
        result = subprocess.run([sys.executable, *(str(arg) for arg in args)], cwd=self.skill, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def write_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_full_cli_pipeline(self) -> None:
        run = Path(self.run_cli(EXAM, "run-dir", self.root, "--subject", "資料/結構").stdout.strip())
        self.assertTrue(run.is_dir())
        self.assertEqual(run.parent.parent, self.root / "_analysis")
        inventory = run / "inventory.json"
        self.run_cli(EXAM, "inventory", self.root, "--output", inventory)
        selection = run / "selection.json"
        self.write_json(selection, {
            "schema_version": 1, "study_subject": "資料結構", "classification_version": "1",
            "target_exam": "高考", "selected_sources": [
                {"path": str(self.pdf), "sha256": hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
                 "official_subject": "資料結構", "exam_name": "高考", "year": 114,
                 "category": "資訊處理", "role": "question"}]})
        render = run / "render-index.json"
        self.run_cli(EXAM, "render", "--inventory", inventory, "--selection", selection, "--output", render)
        topics = run / "topics.json"
        self.write_json(topics, {"topics": [{"topic_id": "TREE", "label": "樹"}]})
        work = run / "work.json"
        prepared = json.loads(self.run_cli(COMPACT, "prepare", "--inventory", inventory, "--selection", selection,
                                           "--render-index", render, "--topics", topics,
                                           "--output", work).stdout.splitlines()[-1])
        self.assertEqual(len(prepared["pending_batches"]), 1)
        status = json.loads(self.run_cli(COMPACT, "status", "--work", work).stdout)
        self.assertEqual(status["batch_count"], 1)
        self.assertEqual(status["page_count"], 2)
        packet_path = run / "packet.json"
        self.run_cli(COMPACT, "packet", "--work", work, "--batch-id", prepared["pending_batches"][0],
                     "--output", packet_path)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertTrue(packet["rules"])
        self.assertNotIn("target_exam", packet)
        source = packet["source"]
        self.assertEqual(len(packet["pages"]), 2)

        rendered = json.loads(render.read_text(encoding="utf-8"))
        rows = rendered["documents"][0]["pages"]
        visual = run / "visual-review.json"
        self.write_json(visual, {
            "schema_version": 1,
            "host_vision": {"available": True, "tool": "test", "probe_image": rows[0]["image_path"],
                            "probe_result": "ok"},
            "pages": [{"source_sha256": row["source_sha256"], "page": row["page"],
                       "image_path": row["image_path"], "image_sha256": row["image_sha256"],
                       "visual_status": "checked", "observations": "synthetic", "reason": ""} for row in rows]})
        results = run / "results.json"
        self.write_json(results, {"results": [{"source": source, "reviewed_pages": [1, 2], "questions": [
            {"p": 1, "q": "1", "r": "r", "t": ["TREE"], "sample": "樹插入"},
            {"p": 2, "q": "2", "r": "i", "t": []}]}]})
        index = run / "question-index.json"
        self.run_cli(COMPACT, "merge", "--work", work, "--results", results, "--visual-review", visual,
                     "--output", index)
        data = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["coverage"]["visually_checked_pages"], 2)
        self.assertEqual(data["source_status"][0]["status"], "processed")
        self.assertEqual(data["questions"][0]["analysis_status"], "completed")
        summary = run / "summary.json"
        self.run_cli(COMPACT, "summary", "--index", index, "--target", "高考", "--output", summary)
        stats = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(stats["unique_related_questions"], 1)
        self.assertEqual(stats["topics"][0]["count"], 1)
        self.assertIsNone(stats["event_count"])


if __name__ == "__main__":
    unittest.main()
