"""tools/clean_export.py on made-up repos, plus the live repo's HEAD.

Run from the pilot root:  python3 -I -m unittest discover -s tools -v

This lives in tools/, not tests/, on purpose. The generic_v2 freeze hashes every tests/test_*.py by glob, so a new file
there would change the hashes and block the run until a re-freeze. Move it to tests/ when the suite is next re-frozen.
"""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("clean_export", ROOT / "tools" / "clean_export.py")
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)
IS_SOURCE = (ROOT / ".git").exists() and (ROOT / "CLAUDE.md").exists()    # an export never has CLAUDE.md, even with --init


def make_repo(root: Path, files: dict[str, str]) -> None:
    """A real git repo whose HEAD holds `files`, with a tag, and a copy of publish_check for the export to load."""
    ident = ["-c", "user.name=t", "-c", "user.email=t@invalid", "-c", "commit.gpgsign=false"]
    files = {**files, "tools/publish_check.py": (ROOT / "tools" / "publish_check.py").read_text()}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *ident, "commit", "-q", "-m", "one"], cwd=root, check=True)
    subprocess.run(["git", *ident, "tag", "v1"], cwd=root, check=True)


class TestRefusals(unittest.TestCase):
    def test_existing_destination(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(SystemExit) as cm:
                ce.check_dest(Path(t))
            self.assertIn("already exists", str(cm.exception))

    def test_destination_inside_the_repo(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(SystemExit) as cm:
                ce.check_dest(Path(t) / "sub" / "out", root=Path(t).resolve())
            self.assertIn("inside the repo", str(cm.exception))

    def test_missing_parent(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(SystemExit):
                ce.check_dest(Path(t) / "no" / "out")

    def test_new_destination_is_accepted(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(ce.check_dest(Path(t) / "out"), (Path(t) / "out").resolve())


class TestExclude(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(ce.excluded("CLAUDE.md"))
        self.assertTrue(ce.excluded("PLAN.md"))
        self.assertTrue(ce.excluded("artifacts/review/a.csv"))
        for path in ("docs/PLAN.md", "NOTPLAN.md", "artifacts/reviewed.csv", "README.md"):
            with self.subTest(path=path):
                self.assertIsNone(ce.excluded(path))

    def test_a_new_publish_check_note_is_excluded_too(self):
        class Fake:
            NEEDS_DECISION = ((__import__("re").compile(r"^drafts/"), "unpublished drafts"),)
        self.assertEqual(ce.excluded("drafts/a.md", Fake), "publish_check note: unpublished drafts")
        self.assertIsNone(ce.excluded("docs/a.md", Fake))

    def test_every_publish_check_note_is_on_the_explicit_list(self):
        spec2 = importlib.util.spec_from_file_location("pc", ROOT / "tools" / "publish_check.py")
        pc = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(pc)
        for path in ("PLAN.md", "CLAUDE.md", "artifacts/review/x.csv"):
            with self.subTest(path=path):
                self.assertTrue(any(rx.search(path) for rx, _ in pc.NEEDS_DECISION))
                self.assertFalse(ce.excluded(path, pc).startswith("publish_check note"))


class TestStubAndReferences(unittest.TestCase):
    """The PLAN.md stub, and how references to excluded paths are classified."""

    def export(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src, out = Path(tmp.name) / "src", Path(tmp.name) / "out"
        src.mkdir()
        make_repo(src, {"PLAN.md": "secret plan\n", **files})
        ce.export(out, src)
        return out

    def test_stub_is_written_when_design_notes_exist(self):
        out = self.export({"docs/design.md": "d\n", "README.md": "see PLAN.md\n"})
        text = (out / "PLAN.md").read_text()
        self.assertEqual(text, ce.load_publish_check(ROOT).STUB_PLAN)
        self.assertIn("not published", text)
        self.assertIn("docs/design.md", text)
        self.assertLessEqual(len(text.splitlines()), 4 + 1)
        self.assertNotIn("secret", text)

    def test_no_stub_without_design_notes_so_the_reference_dangles(self):
        out = self.export({"README.md": "see PLAN.md\n"})
        self.assertFalse((out / "PLAN.md").exists())
        self.assertEqual(ce.dangling(out), ["README.md:1: names PLAN.md"])

    def test_references_to_plan_resolve_once_the_stub_exists(self):
        out = self.export({"docs/design.md": "d\n", "README.md": "see PLAN.md\n"})
        refs = ce.references(out)
        self.assertEqual(refs["resolved"], ["README.md:1: names PLAN.md"])
        self.assertEqual((refs["output"], refs["fixture"], refs["dangling"]), ([], [], []))

    def test_fixture_name_is_only_exempt_in_its_own_file_and_alone_on_the_line(self):
        out = self.export({"docs/design.md": "d\n", "tests/test_publish_check.py": 'p = "artifacts/review/x.csv"\nq = "artifacts/review/y.csv"\nr = "artifacts/review/x.csv artifacts/review/"\n',
                           "docs/a.md": "artifacts/review/x.csv\n"})
        refs = ce.references(out)
        self.assertEqual(refs["fixture"], ["tests/test_publish_check.py:1: names artifacts/review/"])
        self.assertEqual(refs["output"], ["tests/test_publish_check.py:3: names artifacts/review/"])     # the bare directory is still judged on its own
        self.assertEqual(refs["dangling"], ["docs/a.md:1: names artifacts/review/", "tests/test_publish_check.py:2: names artifacts/review/"])

    def test_the_stub_is_not_a_publish_check_note_but_other_plans_are(self):
        out = self.export({"docs/design.md": "d\n"})
        self.assertEqual(ce.blockers_in(out), ([], []))
        (out / "PLAN.md").write_text(ce.load_publish_check(ROOT).STUB_PLAN + "an added line\n")
        self.assertEqual(len(ce.blockers_in(out)[1]), 1)

    def test_output_location_needs_the_listed_file_and_no_concrete_name(self):
        out = self.export({"docs/design.md": "d\n",
                           "docs/review.md": "Sheets go to `artifacts/review/` or artifacts/review/<sheet>.csv\nSee artifacts/review/real.csv\n",
                           "docs/other.md": "Sheets go to `artifacts/review/`\n"})
        refs = ce.references(out)
        self.assertEqual(refs["output"], ["docs/review.md:1: names artifacts/review/"])
        self.assertEqual(refs["dangling"], ["docs/other.md:1: names artifacts/review/", "docs/review.md:2: names artifacts/review/"])


class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir()
        make_repo(self.src, {"README.md": "See PLAN.md and artifacts/review/ for more.\n", "PLAN.md": "plan\n", "CLAUDE.md": "notes\n",
                             "artifacts/review/s.csv": "x\n", "artifacts/ok.txt": "ok\n", "code.py": "x = 1\n"})
        self.out = Path(self.tmp.name) / "out"

    def test_export_excludes_and_has_no_history(self):
        skipped = ce.export(self.out, self.src)
        self.assertEqual(sorted(p for p, _ in skipped), ["CLAUDE.md", "PLAN.md", "artifacts/review/s.csv"])
        self.assertEqual(ce.files(self.out), ["README.md", "artifacts/ok.txt", "code.py", "tools/publish_check.py"])
        self.assertFalse((self.out / ".git").exists())
        self.assertFalse((self.out / "artifacts" / "review").exists())

    def test_export_reads_head_not_the_working_tree(self):
        (self.src / "code.py").write_text("x = 2\n")
        (self.src / "untracked.txt").write_text("u\n")
        ce.export(self.out, self.src)
        self.assertEqual((self.out / "code.py").read_text(), "x = 1\n")
        self.assertFalse((self.out / "untracked.txt").exists())

    def test_dirty_only_counts_tracked_files(self):
        (self.src / "untracked.txt").write_text("u\n")
        self.assertEqual(ce.dirty(self.src), [])
        (self.src / "code.py").write_text("x = 2\n")
        self.assertEqual(len(ce.dirty(self.src)), 1)

    def test_dangling_references(self):
        ce.export(self.out, self.src)
        self.assertEqual([r for r in ce.dangling(self.out) if r.startswith("README")], ["README.md:1: names PLAN.md", "README.md:1: names artifacts/review/"])

    def test_lookalike_names_do_not_dangle(self):
        ce.export(self.out, self.src)
        (self.out / "a.md").write_text("MYPLAN.md and my-PLAN.md and artifacts/reviewed\n")
        self.assertEqual([r for r in ce.dangling(self.out) if r.startswith("a.md")], ["a.md:1: names artifacts/review/"])

    def test_blockers_in_the_export_are_found(self):
        ce.export(self.out, self.src)
        (self.out / "leak.txt").write_text("/" + "Users/someone/x\n")
        blockers, notes = ce.blockers_in(self.out)
        self.assertEqual((blockers, notes), (["leak.txt:1: machine path"], []))

    def test_init_makes_one_commit_and_no_tags_or_remote(self):
        ce.export(self.out, self.src)
        ce.init_repo(self.out)
        self.assertEqual(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=self.out, capture_output=True, text=True).stdout.strip(), "1")
        for what in ("tag", "remote"):
            self.assertEqual(subprocess.run(["git", what], cwd=self.out, capture_output=True, text=True).stdout, "")


@unittest.skipUnless(IS_SOURCE, "needs the source repo, which holds the excluded files")
class TestLiveRepo(unittest.TestCase):
    def test_head_exports_clean(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "out"
            skipped = ce.export(out)
            names = {p for p, _ in skipped}
            self.assertTrue({"CLAUDE.md", "PLAN.md"} <= names)
            self.assertFalse(any(p.startswith("artifacts/review/") for p in ce.files(out)))
            self.assertEqual(ce.blockers_in(out), ([], []))
            self.assertFalse((out / ".git").exists())

    def test_head_has_no_dangling_references(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "out"
            ce.export(out)
            self.assertEqual(ce.dangling(out), [])
            self.assertTrue((out / "PLAN.md").is_file())
            self.assertIn("not published", (out / "PLAN.md").read_text())


if __name__ == "__main__":
    unittest.main()
