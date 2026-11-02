"""實際 PDFium 渲染、逐頁恢復與視覺待確認狀態驗證。"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:  # pragma: no cover - 只影響沒有 Pillow 的環境
    Image = None
    ImageDraw = None
    HAS_PIL = False

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import exam_files as tool
import pdf_render as renderer

HAS_PDFIUM = importlib.util.find_spec("pypdfium2") is not None


def mixed_fixture(path: Path) -> None:
    """六頁：文字、掃描、圖文、向量、公式、旋轉；不是正式考題。"""
    writer = PdfWriter()
    raster = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(raster)
    draw.text((30, 20), "SCANNED: A -> B -> C", fill="black", font_size=26)
    draw.rectangle((30, 100, 180, 200), outline="black", width=4)
    draw.line((180, 150, 400, 150), fill="black", width=4)
    draw.ellipse((400, 100, 500, 200), outline="black", width=4)
    scan = DecodedStreamObject()
    scan.set_data(raster.tobytes())
    scan.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                 NameObject("/Width"): NumberObject(600), NameObject("/Height"): NumberObject(300),
                 NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    raster.close()
    scan_ref = writer._add_object(scan)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    streams = [
        b"BT /F1 16 Tf 20 200 Td (Q1: Explain a binary tree.) Tj ET",
        b"q 280 0 0 140 10 40 cm /Im1 Do Q",
        b"BT /F1 16 Tf 20 220 Td (Q3: Interpret this diagram.) Tj ET q 280 0 0 140 10 40 cm /Im1 Do Q",
        b"BT /F1 16 Tf 20 230 Td (Q4: Trace the vector tree.) Tj ET 2 w 150 180 m 60 80 l S 150 180 m 240 80 l S 135 180 30 25 re S 45 55 30 25 re S 225 55 30 25 re S",
        b"BT /F1 16 Tf 20 200 Td (Q5: y = x) Tj /F1 10 Tf 5 Ts (2) Tj 0 Ts /F1 16 Tf ( + 1) Tj ET 20 185 m 170 185 l S",
        b"BT /F1 16 Tf 20 200 Td (Q6: Rotated page) Tj ET",
    ]
    for index, commands in enumerate(streams):
        page = writer.add_blank_page(width=300, height=250)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
                                                          NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): scan_ref})})
        content = DecodedStreamObject()
        content.set_data(commands)
        page[NameObject("/Contents")] = writer._add_object(content)
        if index == 5:
            page.rotate(90)
    writer.write(path)


@unittest.skipUnless(HAS_PDFIUM and HAS_PIL, "缺少 pypdfium2 或 Pillow，未執行實際渲染測試")
class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "中文題庫"
        self.root.mkdir()
        mixed_fixture(self.root / "試題.pdf")
        self.data = tool.inventory(self.root)
        self.selection = {"schema_version": 1, "study_subject": "資料結構", "selected_sources": self.data["files"]}
        self.output = self.root / "_analysis" / "本次" / "render-index.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_render(self, **kwargs: object) -> dict:
        return renderer.render_pdf(self.data, self.selection, None, self.output, **kwargs)

    def test_all_page_types_and_no_vision_fallback(self) -> None:
        result = self.run_render()
        text = tool.extract(self.data, self.selection, None)
        rows = result["documents"][0]["pages"]
        self.assertEqual(len(rows), 6)
        self.assertEqual(result["status"], "rendered")
        self.assertTrue(text["documents"][0]["pages"][0]["text"])
        self.assertEqual(text["documents"][0]["pages"][1]["text"], "")
        self.assertTrue(all(row["visual_status"] == "not_checked" for row in rows))
        for row in rows:
            with Image.open(row["image_path"]) as image:
                self.assertGreater(image.width, 0)
                self.assertLessEqual(image.width * image.height, renderer.MAX_PIXELS)
                self.assertLess(image.convert("L").getextrema()[0], 100)
        self.assertGreater(rows[0]["width"], rows[0]["height"])
        self.assertLess(rows[5]["width"], rows[5]["height"])
        # 無視覺宿主只能取得待檢視圖片；渲染不能偽造 checked。
        pending = [row for row in rows if row["visual_status"] == "not_checked"]
        self.assertEqual(len(pending), 6)

    def test_selection_boundaries_and_corrupt_pdf(self) -> None:
        with self.assertRaises(ValueError):
            renderer.render_pdf(self.data, None, None, self.output)
        preview = renderer.render_pdf(self.data, None, [1], self.output)
        self.assertEqual(len(preview["documents"][0]["pages"]), 1)
        (self.root / "bad.pdf").write_bytes(b"bad")
        data = tool.inventory(self.root)
        selected = {**self.selection, "selected_sources": data["files"]}
        result = renderer.render_pdf(data, selected, None, self.output.parent / "bad-index.json")
        self.assertEqual(result["status"], "partial")
        self.assertEqual({doc["status"] for doc in result["documents"]}, {"rendered", "failed"})

    def test_pixel_cap_real_render(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=4000, height=3000)
        writer.write(self.root / "大頁.pdf")
        data = tool.inventory(self.root)
        selected = {**self.selection, "selected_sources": [item for item in data["files"] if item["path"].endswith("大頁.pdf")]}
        result = renderer.render_pdf(data, selected, None, self.output, dpi=300)
        row = result["documents"][0]["pages"][0]
        self.assertEqual(row["status"], "rendered")
        self.assertLessEqual(row["width"] * row["height"], 16_000_000)
        self.assertLess(row["actual_dpi"], 300)

    def test_duplicate_cache_and_corrupt_image_repair(self) -> None:
        (self.root / "共用卷.pdf").write_bytes((self.root / "試題.pdf").read_bytes())
        self.data = tool.inventory(self.root)
        self.selection["selected_sources"] = self.data["files"]
        first = self.run_render()
        self.assertEqual(len(first["documents"]), 1)
        self.assertEqual(len(first["documents"][0]["paths"]), 2)
        Path(first["documents"][0]["pages"][1]["image_path"]).write_bytes(b"corrupt")
        second = self.run_render(resume_path=self.output)
        rows = second["documents"][0]["pages"]
        self.assertTrue(rows[0]["reused"])
        self.assertFalse(rows[1]["reused"])
        self.assertEqual(rows[1]["status"], "rendered")
        third = renderer.render_pdf(self.data, self.selection, [1], self.output.parent / "300dpi.json", dpi=300, resume_path=self.output)
        self.assertFalse(third["documents"][0]["pages"][0]["reused"])

    def test_interrupt_and_resume_completed_page(self) -> None:
        original = renderer.render_page
        calls = []
        def interrupt(pdf: object, number: int, *args: object) -> dict:
            calls.append(number)
            if number == 2:
                raise KeyboardInterrupt()
            return original(pdf, number, *args)
        with patch.object(renderer, "render_page", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_render()
        partial = tool.read_json(self.output)
        self.assertEqual(partial["status"], "in_progress")
        self.assertEqual(len(partial["documents"][0]["pages"]), 1)
        resumed = self.run_render(resume_path=self.output)
        self.assertTrue(resumed["documents"][0]["pages"][0]["reused"])
        self.assertEqual(len(resumed["documents"][0]["pages"]), 6)

    def test_source_changed_does_not_reuse(self) -> None:
        self.run_render()
        with (self.root / "試題.pdf").open("ab") as stream:
            stream.write(b"\nchanged")
        resumed = self.run_render(resume_path=self.output)
        self.assertEqual(resumed["documents"][0]["status"], "failed")
        self.assertEqual(resumed["documents"][0]["pages"], [])

    def test_unselected_file_excluded_and_failed_page_retried(self) -> None:
        (self.root / "未選科目.pdf").write_bytes(b"broken unselected pdf")
        self.data = tool.inventory(self.root)
        original = renderer.render_page
        def fail_page(pdf: object, number: int, *args: object) -> dict:
            if number == 2:
                raise ValueError("模擬單頁失敗")
            return original(pdf, number, *args)
        with patch.object(renderer, "render_page", side_effect=fail_page):
            partial = self.run_render()
        self.assertEqual(len(partial["documents"]), 1)
        self.assertEqual(partial["status"], "partial")
        resumed = self.run_render(resume_path=self.output)
        rows = resumed["documents"][0]["pages"]
        self.assertTrue(rows[0]["reused"])
        self.assertFalse(rows[1]["reused"])
        self.assertEqual(resumed["status"], "rendered")

    def test_resume_preserves_unvisited_pages_and_reuses_images(self) -> None:
        first = self.run_render()
        images = sorted((self.output.parent / "render-images").glob("*.png"))
        Path(first["documents"][0]["pages"][1]["image_path"]).write_bytes(b"corrupt")
        original = renderer.render_page

        def interrupt(pdf: object, number: int, *args: object) -> dict:
            if number == 2:
                raise KeyboardInterrupt()
            return original(pdf, number, *args)

        with patch.object(renderer, "render_page", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_render(resume_path=self.output)
        partial = tool.read_json(self.output)
        self.assertEqual(partial["status"], "in_progress")
        self.assertEqual([row["page"] for row in partial["documents"][0]["pages"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(sorted((self.output.parent / "render-images").glob("*.png"))), len(images))
        resumed = self.run_render(resume_path=self.output)
        rows = resumed["documents"][0]["pages"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0]["reused"])
        self.assertFalse(rows[1]["reused"])
        self.assertTrue(all(row["reused"] for row in rows[2:]))

    def test_journal_replay_recovers_completed_page(self) -> None:
        first = self.run_render()
        document = first["documents"][0]
        page_one = document["pages"][0]
        output = self.output.parent / "journal-index.json"
        tool.write_new(output, {"schema_version": 1, "kind": "exam-render-index", "config": first["config"],
                                "page_scope": None, "status": "in_progress", "documents": [
                                    {"sha256": document["sha256"], "paths": document["paths"],
                                     "status": "in_progress", "total_pages": 6, "pages": []}]})
        journal = output.with_name(output.name + ".journal.jsonl")
        journal.write_text(json.dumps({"sha256": document["sha256"], "row": page_one}, ensure_ascii=False) + "\n",
                           encoding="utf-8")
        resumed = renderer.render_pdf(self.data, self.selection, None, output, resume_path=output)
        rows = resumed["documents"][0]["pages"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0]["reused"])
        self.assertFalse(journal.exists())

    def test_same_output_rejects_scope_or_config_change(self) -> None:
        self.run_render()
        with self.assertRaises(ValueError):
            renderer.render_pdf(self.data, self.selection, [1], self.output, resume_path=self.output)
        with self.assertRaises(ValueError):
            renderer.render_pdf(self.data, self.selection, None, self.output, dpi=300, resume_path=self.output)

    def test_explicit_150_dpi_reuses_default_cache(self) -> None:
        self.run_render()
        again = renderer.render_pdf(self.data, self.selection, None, self.output, dpi=150, resume_path=self.output)
        self.assertTrue(all(row["reused"] for row in again["documents"][0]["pages"]))

    def test_cli_and_unsupported_resume(self) -> None:
        import subprocess
        inventory = self.base / "inventory.json"
        tool.write_new(inventory, self.data)
        run = subprocess.run([sys.executable, str(Path(tool.__file__)), "render", "--inventory", str(inventory),
                              "--pages", "1", "--output", str(self.output)], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        invalid = self.base / "invalid.json"
        tool.write_new(invalid, {"schema_version": 1})
        with self.assertRaises(ValueError):
            renderer.render_pdf(self.data, self.selection, None, invalid, resume_path=invalid)
        self.assertEqual(tool.read_json(invalid), {"schema_version": 1})


if __name__ == "__main__":
    unittest.main()
