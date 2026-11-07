#!/usr/bin/env python3
"""Export the committed files at HEAD into a new directory, with no git history, tags or working-tree files.

    python3 tools/clean_export.py DEST [--allow-dirty] [--init] [--strict]

DEST must not exist and must not be inside this repo. The export is read from the commit (git archive), so ignored and
untracked files can't leak into it. The source repo is never modified.

Files on EXCLUDE are left out, because they hold results that aren't published yet or internal notes. Anything that
tools/publish_check.py flags as a NOTE is left out too, so a new note can't slip through. The summary lists what was kept
and excluded, then runs the publish_check rules over the export. Blockers exit 1.

It also checks references: lines in exported text files that name an excluded path, such as a docstring or a doc page that
points at PLAN.md. They are reported, not rewritten, because several of those files are hashed by the freeze. Each is
classified, and only the last class counts against --strict:
  RESOLVED  the named file exists in the export. PLAN.md is replaced by a short stub (publish_check.STUB_PLAN) that says the
            plan is not published and points to docs/design.md. The stub is written only if docs/design.md is in the export.
            Resolved means the file exists, not that a section the line cites does.
  OUTPUT    the line names artifacts/review/ as the place the user's own run writes review sheets (a directory or a <placeholder>
            path, never a concrete file), in a file listed in OUTPUT_LOCATION. The sheets in the source repo's copy are excluded;
            the directory itself is not published content.
  FIXTURE   the line names the made-up file artifacts/review/x.csv in tests/test_publish_check.py (see FIXTURE_NAMES) and
            nothing else that is excluded.
  DANGLING  anything else, including a concrete excluded file named in any other file.
This file, its test and tools/publish_check.py name the excluded paths on purpose and aren't scanned.

Refuses if tracked files have uncommitted changes, unless --allow-dirty (the export is still HEAD, not those changes).
--init runs git init in DEST and makes one commit, so a check that needs a git repo can run there. It is for checking only.

Nothing here pushes, adds a remote or publishes. A clean result is not a security review.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A path ending in "/" is a directory prefix. Each entry says why, and the summary prints it.
EXCLUDE = (
    ("CLAUDE.md", "internal project notes, with Stage 2 figures"),
    ("PLAN.md", "internal plan, with Stage 2 figures and open decisions"),
    ("artifacts/review/", "review sheets hold model output and reviewer marks"),
)
# Files that name artifacts/review/ as where a user's own output goes. A line there counts as OUTPUT only if it names the
# directory or a <placeholder> path (see OUTPUT_SHAPE); a concrete file name stays DANGLING.
OUTPUT_LOCATION = {
    "artifacts/review/": ("docs/outputs.md", "docs/review.md", "docs/stage3-run.md", "analysis/human_review_join.py",
                          "tests/test_publish_check.py"),
}
# Made-up file names in a hashed test, used to check that review sheets are not blocked. They are not references.
FIXTURE_NAMES = {"tests/test_publish_check.py": ("artifacts/review/x.csv",)}
OUTPUT_SHAPE = re.compile(r"artifacts/review/(?![\w.-]*\w)")
SKIP_REFS = {"tools/clean_export.py", "tools/test_clean_export.py", "tools/publish_check.py"}      # they hold the exclude lists


def git(*args: str, root: Path = ROOT) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout


def dirty(root: Path = ROOT) -> list[str]:
    """Tracked files that differ from HEAD. Untracked files don't count: the export never reads them."""
    return [line for line in git("status", "--porcelain", "--untracked-files=no", root=root).splitlines() if line]


def check_dest(dest: Path, root: Path = ROOT) -> Path:
    dest = dest.expanduser().resolve()
    if dest.exists() or dest.is_symlink():
        raise SystemExit(f"refusing: {dest} already exists. Give a new path.")
    if dest == root or root in dest.parents:
        raise SystemExit(f"refusing: {dest} is inside the repo.")
    if not dest.parent.is_dir():
        raise SystemExit(f"refusing: the parent of {dest} does not exist.")
    return dest


def load_publish_check(root: Path):
    spec = importlib.util.spec_from_file_location("publish_check", root / "tools" / "publish_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def excluded(path: str, pc=None) -> str | None:
    """The reason a tracked path is left out, or None. pc is the publish_check module, for its NOTE rules."""
    for rule, why in EXCLUDE:
        if path == rule or (rule.endswith("/") and path.startswith(rule)):
            return why
    if pc is not None:
        for rx, why in pc.NEEDS_DECISION:
            if rx.search(path):
                return f"publish_check note: {why}"
    return None


def export(dest: Path, root: Path = ROOT) -> list[tuple[str, str]]:
    """Unpack `git archive HEAD` into dest, skipping excluded paths. Returns the excluded (path, reason) pairs.
    The "data" filter refuses links and paths that leave dest."""
    pc = load_publish_check(root) if (root / "tools" / "publish_check.py").exists() else None
    dest.mkdir()
    skipped = []
    with subprocess.Popen(["git", "archive", "--format=tar", "HEAD"], cwd=root,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                why = excluded(member.name.rstrip("/") + ("/" if member.isdir() else ""), pc)
                if why:
                    if member.isfile():
                        skipped.append((member.name, why))
                    continue
                tar.extract(member, dest, filter="data")
        err = proc.stderr.read().decode()
    if proc.returncode:
        raise SystemExit(f"git archive failed: {err.strip()}")
    write_stub(dest, pc, [p for p, _ in skipped])
    return sorted(skipped)


def write_stub(dest: Path, pc, skipped: list[str]) -> bool:
    """Put the short PLAN.md stub where the excluded plan was, so references to it point at something true. Only if the plan
    was excluded, nothing is in its place, and the docs/design.md the stub points to is in the export."""
    if pc is None or "PLAN.md" not in skipped or (dest / "PLAN.md").exists() or not (dest / "docs" / "design.md").is_file():
        return False
    (dest / "PLAN.md").write_text(pc.STUB_PLAN, encoding="utf-8")
    return True


def files(dest: Path) -> list[str]:
    return sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())


def blockers_in(dest: Path) -> tuple[list[str], list[str]]:
    """The export's own publish_check rules, run over its files (it may not be a git repo, so not publish_check.run)."""
    pc = load_publish_check(dest)
    blockers, notes = [], []
    for path in files(dest):
        blockers += pc.check_path(path)
        try:
            text = (dest / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = None
        notes += pc.check_decision(path, text)
        if path in pc.SKIP_CONTENT or text is None:
            continue
        blockers += pc.check_text(path, text)
    return blockers, notes


def _without(line: str, names) -> str:
    for name in names:
        line = line.replace(name, "")
    return line


def references(dest: Path) -> dict[str, list[str]]:
    """Lines in the export that name an excluded path, sorted into resolved, output, fixture and dangling (see the module docstring).
    A file is matched by name, a directory by its path."""
    found = {"resolved": [], "output": [], "fixture": [], "dangling": []}
    for rule, _ in EXCLUDE:
        rx = re.compile(r"(?<![\w-])" + re.escape(rule.rstrip("/")))
        for path in files(dest):
            if path in SKIP_REFS:
                continue
            try:
                text = (dest / path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if not rx.search(line):
                    continue
                msg = f"{path}:{n}: names {rule}"
                if not rule.endswith("/") and (dest / rule).is_file():
                    found["resolved"].append(msg)
                elif not rx.search(_without(line, FIXTURE_NAMES.get(path, ()))):
                    found["fixture"].append(msg)
                elif path in OUTPUT_LOCATION.get(rule, ()) and OUTPUT_SHAPE.search(line):
                    found["output"].append(msg)
                else:
                    found["dangling"].append(msg)
    return {k: sorted(v) for k, v in found.items()}


def dangling(dest: Path) -> list[str]:
    """The references that are neither resolved, an output location nor a fixture. Only these fail --strict."""
    return references(dest)["dangling"]


def init_repo(dest: Path) -> None:
    """One commit with a made-up identity, so it works without a git config and never reads the owner's."""
    ident = ["-c", "user.name=clean-export", "-c", "user.email=clean-export@invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", *ident, "commit", "-q", "-m", "Clean export (for checking only)"], cwd=dest, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Export the committed files at HEAD into a new directory, without history.")
    ap.add_argument("dest", type=Path, help="new directory to create")
    ap.add_argument("--allow-dirty", action="store_true", help="export HEAD even if tracked files have uncommitted changes")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any exported file names an excluded path that is not resolved, an output location or a fixture")
    ap.add_argument("--init", action="store_true", help="git init DEST and make one commit, for checking only")
    a = ap.parse_args()
    changed = dirty()
    if changed and not a.allow_dirty:
        raise SystemExit("refusing: tracked files have uncommitted changes (the export would not match them):\n  "
                         + "\n  ".join(changed) + "\nCommit them, or pass --allow-dirty to export HEAD anyway.")
    dest = check_dest(a.dest)
    skipped = export(dest)
    kept = files(dest)
    blockers, notes = blockers_in(dest)
    refs_by_class = references(dest)
    refs = refs_by_class["dangling"]
    if a.init:
        init_repo(dest)
    print(f"Exported {git('rev-parse', '--short', 'HEAD').strip()} to {dest}")
    print(f"{len(kept)} files kept, {len(skipped)} excluded.")
    for path, why in skipped:
        print(f"EXCLUDED  {path}: {why}")
    for r in refs_by_class["resolved"]:
        print(f"RESOLVED  {r}")
    for r in refs_by_class["output"]:
        print(f"OUTPUT    {r}")
    for r in refs_by_class["fixture"]:
        print(f"FIXTURE   {r}")
    for r in refs:
        print(f"DANGLING  {r}")
    for b in blockers:
        print(f"BLOCK  {b}")
    for n in notes:
        print(f"NOTE   {n}")
    print(f"{len(blockers)} blockers, {len(notes)} notes, {len(refs)} dangling references in the export ({len(refs_by_class['resolved'])} resolved by the stub, {len(refs_by_class['output'])} output locations, {len(refs_by_class['fixture'])} test fixtures)." + (" One local commit made with --init; there is no remote." if a.init else ""))
    return 1 if blockers or notes or (a.strict and refs) else 0


if __name__ == "__main__":
    sys.exit(main())
