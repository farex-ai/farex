"""Scoring for generic_v2 (stage3/SPEC.md section 4).

A thin layer over the frozen generic_v1 scorers in scoring.py, which stay unchanged so the v1 freeze
remains valid. It depends only on the task and the response, never on the route. It adds:

  components["contract_violation"]   None, "code" or "no_code". A label for why a cell failed.
  evidence QA answered with code     verdict_missing becomes a scored fail instead of review_needed.
                                     correct = 0 and compliant = 0: text inside code is not a citation.
  evidence QA pass that contains code  pass becomes review_needed with reason answer_inside_code, so a
                                     person reads it. The verdict or quote may sit inside the code.

The tag never turns a pass into a fail, and it never changes any other outcome.
"""

from __future__ import annotations

import re

from . import scoring

SCORER_VERSION = "generic_v2-scoring-2.1.0"
NO_CODE_MAX_CHARS = 200

_CODE_RE = re.compile(r"(?m)^\s*(?:```|def\s|import\s|print\()")
_DEF_RE = re.compile(r"(?m)^\s*(?:async\s+)?def\s")


def contract_violation(task: dict, response: str, base: dict) -> str | None:
    """The answer came back in another specialist's format. `base` is the v1 scorer result."""
    if base["score_status"] in ("response_oversized", "response_malformed"):
        return None                     # never parsed
    if task["type"] == "coding":
        return "no_code" if len(response) < NO_CODE_MAX_CHARS and not _DEF_RE.search(response) else None
    if base["pass"] != 1 and base["components"].get("correct") != 1 and _CODE_RE.search(response):
        return "code"
    return None


def score(task: dict, response: str, backend=None) -> dict:
    base = scoring.score(task, response, backend)
    tag = contract_violation(task, response, base)
    if task["type"] == "evidence_qa" and base["reason"] == "verdict_missing" and tag == "code":
        base = dict(base, scored=True, score_status="scored", components=dict(base["components"], correct=0, compliant=0))
        base["pass"] = 0
    if task["type"] == "evidence_qa" and base["pass"] == 1 and _CODE_RE.search(response):
        base = dict(base, scored=False, score_status="review_needed", reason="answer_inside_code")
        base["pass"] = None
    return dict(base, components=dict(base["components"], contract_violation=tag))
