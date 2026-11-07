"""tools/publish_check.py on made-up inputs, plus the live repo.

Run from the pilot root:  python3 -I -m unittest discover -s tests -v
"""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("publish_check", ROOT / "tools" / "publish_check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


class TestPatterns(unittest.TestCase):
    def test_machine_paths(self):
        for line in ("see /Users/someone/x", "/home/someone/y", r"C:\Users\someone"):
            self.assertEqual(pc.check_text("f", line), ["f:1: machine path"])

    def test_the_rule_text_itself_is_not_a_path(self):
        self.assertEqual(pc.check_text("f", "check for `/Users/` paths and secrets"), [])

    def test_secrets(self):
        cases = {"private key block": "-----BEGIN RSA PRIVATE KEY-----", "OpenAI-style key": "sk-" + "a" * 30,
                 "GitHub token": "ghp_" + "a" * 36, "AWS access key": "AKIA" + "A" * 16, "Slack token": "xoxb-" + "1" * 12,
                 "assigned secret": 'api_key = "' + "a" * 24 + '"'}
        for name, line in cases.items():
            with self.subTest(name=name):
                self.assertIn(f"f:1: {name}", pc.check_text("f", line))

    def test_ordinary_text_passes(self):
        for line in ("the token count comes from the provider", "sha256:cd89c30eda26ee911a4611b18680086e", 'token = "short"'):
            self.assertEqual(pc.check_text("f", line), [])

    def test_line_numbers(self):
        self.assertEqual(pc.check_text("f", "ok\n/Users/someone"), ["f:2: machine path"])

    def test_never_tracked_paths(self):
        for path in (".env", ".env.local", "sub/.env", "internal/note.md", "artifacts/private/key.json",
                     "artifacts/stage2_prep/execution_order.json", "artifacts/stage3_prep/execution_order.json", "artifacts/runs/a.jsonl"):
            with self.subTest(path=path):
                self.assertTrue(pc.check_path(path))

    def test_ordinary_paths_pass(self):
        for path in ("environment.md", "farex_pilot/runner.py", "artifacts/review/x.csv"):
            self.assertEqual(pc.check_path(path), [])


class TestRepo(unittest.TestCase):
    def test_a_planted_file_is_found_in_a_real_git_repo(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.txt").write_text("/Users/someone/x\n")
            (root / "bin").write_bytes(b"\xff\xfe\x00")
            (root / "PLAN.md").write_text("plan\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            blockers, notes = pc.run(root)
            self.assertEqual(blockers, ["a.txt:1: machine path"])
            self.assertEqual(len(notes), 1)

    def test_this_repo_has_no_blockers(self):
        self.assertEqual(pc.run()[0], [])


if __name__ == "__main__":
    unittest.main()
