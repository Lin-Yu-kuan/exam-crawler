"""使用合成 PDF 驗證可攜式工具，不讀取使用者試卷。"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "exam_files.py"
SPEC = importlib.util.spec_from_file_location("exam_files", SCRIPT)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def pdf(path: Path, text: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 10 100 Td (Binary tree question) Tj ET")
        page[NameObject("/Contents")] = content
    writer.add_blank_page(width=300, height=300)
    writer.write(path)


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "中文題庫"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def selection(self, data: dict) -> dict:
        return {"schema_version": 1, "study_subject": "資料結構", "selected_sources": [
            {"path": item["path"], "sha256": item["sha256"]} for item in data["files"]]}

    def test_nested_duplicates_and_output_exclusion(self) -> None:
        first = self.root / "資料結構" / "114.pdf"
        pdf(first)
        second = self.root / "合科.PDF"
        second.write_bytes(first.read_bytes())
        pdf(self.root / "_analysis" / "舊輸出.pdf")
        data = tool.inventory(self.root)
        self.assertEqual(len(data["files"]), 2)
        self.assertEqual(len(data["contents"]), 1)
        result = tool.extract(data, self.selection(data), None)
        self.assertEqual(len(result["documents"]), 1)
        doc = result["documents"][0]
        self.assertEqual(len(doc["paths"]), 2)
        self.assertIn("Binary tree", doc["pages"][0]["text"])
        self.assertEqual(doc["pages"][1]["status"], "needs_visual_review")

    def test_corrupt_and_blank_pdf_do_not_stop_other_documents(self) -> None:
        pdf(self.root / "空白.pdf", text=False)
        (self.root / "損壞.pdf").write_bytes(b"not a pdf")
        data = tool.inventory(self.root)
        docs = tool.extract(data, self.selection(data), None)["documents"]
        self.assertEqual({doc["status"] for doc in docs}, {"extracted", "failed"})
        blank = next(doc for doc in docs if doc["status"] == "extracted")
        self.assertTrue(all(page["status"] == "needs_visual_review" for page in blank["pages"]))

    def test_selection_required_and_hash_change_rejected(self) -> None:
        path = self.root / "試題.pdf"
        pdf(path)
        data = tool.inventory(self.root)
        with self.assertRaises(ValueError):
            tool.extract(data, None, None)
        headers = tool.extract(data, None, [1])
        self.assertEqual(len(headers["documents"][0]["pages"]), 1)
        selection = self.selection(data)
        with self.assertRaises(ValueError):
            tool.extract(data, {**selection, "selected_sources": []}, None)
        path.write_bytes(b"changed")
        result = tool.extract(data, selection, None)
        self.assertEqual(result["documents"][0]["status"], "failed")

    def test_resume_requires_identical_scope_and_current_content(self) -> None:
        pdf(self.root / "試題.pdf")
        data = tool.inventory(self.root)
        selected = self.selection(data)
        previous = tool.extract(data, selected, [1])
        same = tool.extract(data, selected, [1], previous)
        self.assertTrue(same["documents"][0]["reused"])
        full = tool.extract(data, selected, None, previous)
        self.assertNotIn("reused", full["documents"][0])
        self.assertEqual(len(full["documents"][0]["pages"]), 2)
        invalid = tool.extract(data, selected, [3])
        self.assertEqual(invalid["documents"][0]["status"], "partial")

    def test_known_manifest_preserves_multiple_events_read_only(self) -> None:
        pdf(self.root / "試題.pdf")
        sha = tool.digest(self.root / "試題.pdf")
        db = Path(self.temp.name) / "manifest.sqlite3"
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE meta(key TEXT, value TEXT);
                INSERT INTO meta VALUES('schema_version', '2');
                CREATE TABLE attachments(sha256 TEXT, asset_id TEXT, record_id TEXT, snapshot_id TEXT, role TEXT);
                CREATE TABLE snapshot_records(record_id TEXT, snapshot_id TEXT, year INTEGER, exam_name TEXT, category TEXT, subject TEXT);
            """)
            for index in range(2):
                conn.execute("INSERT INTO attachments VALUES(?,?,?,?,?)", (sha, f"a{index}", f"r{index}", "s", "試題"))
                conn.execute("INSERT INTO snapshot_records VALUES(?,?,?,?,?,?)", (f"r{index}", "s", 113 + index, "考試", "類科", "資料結構"))
        conn.close()
        before = db.read_bytes()
        data = tool.inventory(self.root, db)
        self.assertEqual(len(data["files"][0]["metadata"]), 2)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(data["warnings"], [])

    def test_unknown_manifest_falls_back(self) -> None:
        pdf(self.root / "試題.pdf")
        db = Path(self.temp.name) / "unknown.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE unknown(value TEXT)")
        conn.close()
        data = tool.inventory(self.root, db)
        self.assertTrue(data["warnings"])
        self.assertEqual(data["files"][0]["metadata"], [])

    def test_no_source_overwrite_and_cli(self) -> None:
        pdf(self.root / "試題.pdf")
        output = self.root / "_analysis" / "inventory.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "inventory", str(self.root), "--output", str(output)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            tool.write_new(output, {})
        self.assertEqual(output.read_bytes(), before)
        data = json.loads(before)
        self.assertEqual(len(data["files"]), 1)

    def test_symlink_outside_root_is_not_scanned(self) -> None:
        outside = Path(self.temp.name) / "outside.pdf"
        pdf(outside)
        try:
            (self.root / "link.pdf").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"環境不允許建立符號連結：{exc}")
        self.assertEqual(tool.inventory(self.root)["files"], [])


if __name__ == "__main__":
    unittest.main()
