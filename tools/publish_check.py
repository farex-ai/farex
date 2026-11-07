#!/usr/bin/env python3
"""Check what git would publish, before any push. Reads tracked files only and writes nothing.

    python3 tools/publish_check.py

Blockers exit 1: machine paths, credential-shaped strings, and files that must never be tracked.
Notes don't fail the check. They list tracked files that need an owner decision first, such as files
holding results that aren't published yet (the internal plan's artifact publication policy).

This is a filter for the mistakes it knows about. A clean result is not a security review.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MACHINE_PATH = re.compile(r"(?:/Users/[A-Za-z0-9._-]+|/home/[a-z][a-z0-9._-]*|[A-Z]:\\Users\\)")
SECRET_PATTERNS = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "assigned secret": re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[=:]\s*['\"][A-Za-z0-9/+_-]{16,}['\"]"),
}
NEVER_TRACKED = (
    (re.compile(r"(^|/)\.env(\.|$)"), "env file"),
    (re.compile(r"^internal/"), "internal notes"),
    (re.compile(r"^artifacts/private/"), "blinding key"),
    (re.compile(r"^artifacts/stage\d_prep/"), "execution order, reveals conditions"),
    (re.compile(r"^artifacts/runs/"), "raw run output, released only as a reviewed bundle"),
)
NEEDS_DECISION = (
    (re.compile(r"^artifacts/review/"), "review sheets hold model output and reviewer marks"),
    (re.compile(r"^(PLAN|CLAUDE)\.md$"), "holds Stage 2 figures that are not published yet"),
)
# What tools/clean_export.py writes in place of the excluded plan. It holds no results, so it is not a note.
STUB_PLAN = ("# Plan\n\nThe working plan for this project is not published.\n"
             "The goals, method, limits and roadmap are in docs/design.md.\n")
SKIP_CONTENT = {"tools/publish_check.py", "tests/test_publish_check.py"}     # they contain the patterns on purpose


def tracked(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def check_path(path: str) -> list[str]:
    return [f"{path}: tracked {why}" for rx, why in NEVER_TRACKED if rx.search(path)]


def check_text(path: str, text: str) -> list[str]:
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        if MACHINE_PATH.search(line):
            found.append(f"{path}:{n}: machine path")
        found += [f"{path}:{n}: {name}" for name, rx in SECRET_PATTERNS.items() if rx.search(line)]
    return found


def check_decision(path: str, text: str | None) -> list[str]:
    """Notes for a path. A PLAN.md that is exactly the export stub is not a note, whatever else its name matches."""
    if path == "PLAN.md" and text == STUB_PLAN:
        return []
    return [f"{path}: {why}" for rx, why in NEEDS_DECISION if rx.search(path)]


def run(root: Path = ROOT) -> tuple[list[str], list[str]]:
    blockers, notes = [], []
    for path in tracked(root):
        blockers += check_path(path)
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            text = None
        notes += check_decision(path, text)
        if path in SKIP_CONTENT or text is None:
            continue
        blockers += check_text(path, text)
    return blockers, notes


def main() -> int:
    blockers, notes = run()
    for b in blockers:
        print(f"BLOCK  {b}")
    for n in notes:
        print(f"NOTE   {n}")
    print(f"{len(blockers)} blockers, {len(notes)} notes. Scope: tracked files, known patterns only.")
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
