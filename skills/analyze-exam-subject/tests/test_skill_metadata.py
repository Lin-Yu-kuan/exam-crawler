"""技能中繼資料與相對連結的靜態檢查，不依賴第三方套件。"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)]+)\)")


class SkillMetadataTests(unittest.TestCase):
    def test_frontmatter_name_and_description(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        end = text.find("\n---\n", 4)
        self.assertGreater(end, 0)
        frontmatter = text[4:end]
        self.assertRegex(frontmatter, r"(?m)^name: analyze-exam-subject$")
        description = next((line.split(":", 1)[1].strip() for line in frontmatter.splitlines()
                            if line.startswith("description:")), "")
        self.assertTrue(description)
        self.assertLessEqual(len(description), 1024)

    def test_relative_markdown_links_exist(self) -> None:
        for markdown in [ROOT / "SKILL.md", *sorted((ROOT / "references").glob("*.md")),
                         *sorted((ROOT / "tests").glob("*.md"))]:
            text = markdown.read_text(encoding="utf-8")
            for target in LINK.findall(text):
                target = target.split("#", 1)[0].strip()
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                path = (markdown.parent / target).resolve()
                self.assertTrue(path.exists(), f"{markdown.name} 的連結不存在：{target}")
                self.assertTrue(path.is_relative_to(ROOT.resolve()), f"連結逃出技能資料夾：{target}")

    def test_python_sources_compile(self) -> None:
        for source in [*(ROOT / "scripts").glob("*.py"), *(ROOT / "tests").glob("*.py")]:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")


if __name__ == "__main__":
    unittest.main()
