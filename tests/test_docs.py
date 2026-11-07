"""The docs make claims about the code. These tests keep the checkable ones true.

Run from the pilot root:  python3 -I -m unittest discover -s tests -v
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farex_pilot import runner, stage3  # noqa: E402

PAGES = sorted([*ROOT.glob("docs/*.md"), ROOT / "README.md", *[p for p in map(ROOT.joinpath, ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")) if p.exists()]])


def source(name):
    return (ROOT / "farex_pilot" / name).read_text()


class TestLinks(unittest.TestCase):
    def test_relative_links_resolve(self):
        for page in PAGES:
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", page.read_text()):
                if not target.startswith(("http://", "https://", "mailto:")):
                    with self.subTest(page=page.name, target=target):
                        self.assertTrue((page.parent / target).exists())

    def test_repo_paths_in_backticks_exist(self):
        for page in PAGES:
            for path in re.findall(r"`((?:farex_pilot|tests|tools|stage3|prompts|tasks|freeze|docs)/[\w./-]+\.(?:py|md|json|txt))`", page.read_text()):
                with self.subTest(page=page.name, path=path):
                    self.assertTrue((ROOT / path).exists())


class TestCommands(unittest.TestCase):
    def flags(self, module):
        return set(re.findall(r'add_argument\("(--[a-z0-9-]+)"', source(module)))

    def test_documented_flags_exist(self):
        for page in PAGES:
            for module, args in re.findall(r"python3 -m farex_pilot\.(stage3|runner) ([^\n`]*)", page.read_text()):
                for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", args.split("#")[0]):
                    with self.subTest(page=page.name, flag=flag):
                        self.assertIn(flag, self.flags(f"{module}.py"))


class TestFacts(unittest.TestCase):
    def test_every_row_field_is_documented(self):
        text = (ROOT / "docs" / "outputs.md").read_text()
        row = stage3.base_row("r", "stage3", stage3.load_tasks()[0], "reference", "math", 1)
        missing = [f for f in row if f"`{f}`" not in text]
        self.assertEqual(missing, [])

    def test_every_status_is_documented(self):
        text = (ROOT / "docs" / "outputs.md").read_text()
        self.assertEqual([s for s in sorted(runner.STATUSES) if f"`{s}`" not in text], [])

    def test_call_count_matches_the_code(self):
        self.assertIn(f"**{stage3.EXPECTED_CHAT_CALLS}**", (ROOT / "docs" / "concepts.md").read_text())

    def test_quoted_refusals_exist_in_the_code(self):
        code = "".join(source(n) for n in ("stage3.py", "stage3_report.py"))
        for quoted in re.findall(r"^\| `(refusing[^`]*)`", (ROOT / "docs" / "troubleshooting.md").read_text(), re.M):
            for piece in quoted.split("..."):
                piece = piece.strip()
                if piece.startswith("refusing:"):
                    piece = piece[len("refusing:"):].strip()
                elif piece.startswith("refusing to freeze:"):
                    piece = piece[len("refusing to freeze:"):].strip()
                with self.subTest(quoted=quoted):
                    self.assertIn(piece, code)


class TestVoice(unittest.TestCase):
    """branding/kit/VOICE.md: words Farex never uses about itself."""

    def test_no_banned_claims(self):
        banned = re.compile(r"(?<![-\w])(?:platform|benchmark|production-ready|enterprise|FAREX|FareX)(?![-\w])")
        for page in PAGES:
            text = re.sub(r"`[^`]*`", "", page.read_text())
            with self.subTest(page=page.name):
                self.assertEqual(banned.findall(text), [])

    def test_no_emojis(self):
        for page in PAGES:
            with self.subTest(page=page.name):
                self.assertIsNone(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", page.read_text()))


if __name__ == "__main__":
    unittest.main()
