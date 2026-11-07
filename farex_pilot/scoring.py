"""Scorers for the generic_v1 routing pilot (PLAN.md section C).

Scoring depends only on the task (its type and requirements), never on which
destination prompt answered. Every scorer returns a dict:

    {"scored": bool, "pass": 0/1/None, "score_status": str, "components": {...}, "reason": str}

status values:
  scored            pass is 0 or 1 (a definite verdict under the rules below)
  review_needed     the response is readable but the rules cannot pick one answer
                    (conflicting or no clear candidate). pass is None; NOT counted as wrong
  response_oversized  response longer than MAX_RESPONSE_CHARS; never parsed; pass None
  response_malformed  response is not a string; never parsed; pass None
  sandbox_unavailable coding response that could not be run (no verified backend, or a sandbox
                    infrastructure error); pass None

Extraction and evidence QA also report components["correct"] (right entity / verdict) and
components["compliant"] (the exclusion or citation part of the requirement) separately;
pass is the full requirement (both).

Coding responses are never executed in this process. `score_coding` extracts
the code, applies a static AST policy screen (a filter, NOT the isolation
boundary), and hands the code to a sandbox backend. If no verified backend is
available the result is `scored=False, status="sandbox_unavailable"`; there is
no host-execution fallback.

Standard library only.
"""

from __future__ import annotations

import ast
import difflib
import re
import unicodedata

SCORER_VERSION = "generic_v1-scoring-1.2.0"

# Bounded response handling. The size cap is checked BEFORE any parsing, regex
# over the full text, AST work, compilation or execution. 4096 characters is
# well below the input size at which CPython 3.14's parser raised MemoryError
# ("Parser stack overflowed") on invalid input in our tests (~9,000 characters),
# leaving about 2x margin. Stage 2 caps generation at 512 tokens (see runner).
MAX_RESPONSE_CHARS = 4096
MAX_BRACKET_DEPTH = 64          # checked before ast.parse; deeply nested source is "review", not parsed

# ---------------------------------------------------------------- helpers

_NUM_RE = re.compile(
    r"(?:^|(?<=[\s(=:$*]))-(?=\$?\d)"       # sign (only after start/space/(/=/:)
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"        # 1,250.00
    r"|\d+(?:\.\d+)?"                        # 391 / 2.5
)


def _num_tokens(text: str):
    """Yield (start_index, value) for every number in text, in order."""
    sign, sign_pos = 1, None
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        if tok == "-":
            sign, sign_pos = -1, m.start()
            continue
        start = sign_pos if sign == -1 else m.start()
        yield start, sign * float(tok.replace(",", ""))
        sign = 1


def parse_numbers(text: str) -> list[float]:
    """All numbers in `text`, in order. Thousands separators and `$` dropped.

    A leading '-' counts as a sign only when it follows start-of-text,
    whitespace, '(', '=', ':', '$' or '*' and is directly followed by a digit,
    so "17 - 23" and "10-5" never yield a negative number.
    """
    return [v for _, v in _num_tokens(text or "")]


def normalize(text: str) -> str:
    """Casefold, NFKC, punctuation -> space, collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _phrase_spans(norm_text: str, phrase: str):
    """Start indices of a normalized phrase in a normalized text (word-bounded)."""
    p = normalize(phrase)
    if not p:
        return []
    return [m.start() for m in re.finditer(r"(?<!\w)" + re.escape(p) + r"(?!\w)", norm_text)]


def _contains_phrase(norm_text: str, phrase: str) -> bool:
    return bool(_phrase_spans(norm_text, phrase))


def _result(scored, passed, reason, status=None, **components):
    if status is None:
        status = "scored" if scored else "review_needed"
    return {"scored": scored, "pass": passed, "score_status": status, "components": components, "reason": reason}


def _review(reason, **components):
    return _result(False, None, reason, status="review_needed", **components)


def _fail(reason, **components):
    return _result(True, 0, reason, **components)


def _pass_or_fail(ok, reason_ok, reason_bad, **components):
    return _result(True, int(ok), reason_ok if ok else reason_bad, **components)


def _bound_response(response) -> str | None:
    """Pre-parse size/type gate. Returns a status string, or None if the response may be parsed."""
    if not isinstance(response, str):
        return "response_malformed"
    if len(response) > MAX_RESPONSE_CHARS:
        return "response_oversized"
    return None


def _bounded_source(src: str) -> str | None:
    """Pre-AST gate for source text (extracted code or fenced blocks)."""
    if len(src) > MAX_RESPONSE_CHARS:
        return "source_oversized"
    depth = 0
    for ch in src:
        if ch in "([{":
            depth += 1
            if depth > MAX_BRACKET_DEPTH:
                return "source_too_complex"
        elif ch in ")]}":
            depth = max(0, depth - 1)
    return None


# ---------------------------------------------------------------- math
#
# Rules (no "last number wins"). Principle: when the parser cannot tell which number
# is the answer, the result is review_needed, never a wrong answer.
#  1. Remove code-fence lines and markdown emphasis.
#  2. Explicit markers: "answer is N", "answer: N", "final answer N", "result: N", "total: N".
#     A marker line with further different numbers after the marker value ("Answer: 391 or 381")
#     is a conflict.
#  3. Each line may yield one candidate value:
#     a. the line is a number, optionally followed by text with NO other number ("391", "$6.00",
#        "6 dollars") -> that number;
#     b. a list-item marker ("1. Multiply ...", "2) ...") is never an answer -> no candidate;
#     c. a leading number followed by other numbers ("144 divided by 12 is 12", "25 pencils cost $6.00",
#        "391 (that is 17 * 23)") -> no candidate (cannot tell operand from answer);
#     d. an equation ("17 * 23 = 391", "5 x $1.20 = $6.00"): every non-expression right-hand side
#        must agree on ONE value and a leading plain number (if any) must equal it -> that value;
#        otherwise no candidate.
#  4. The answer must be announced first: the first line or a marker must give a candidate.
#     Otherwise review (answer not first / unclear).
#  5. Every candidate from every line and every marker must agree. Any disagreement
#     (intermediate results in a multi-step answer, marker vs. line) -> review (conflicting_answers).
#  6. No numbers at all -> review (no_number). Empty / fence-only -> fail (empty).
# Only a single, unambiguous value that differs from the expected one is scored as wrong.

_MARKER_RE = re.compile(
    r"(?i)\b(?:final\s+answer|answer|result|total)\b\s*(?:is\b)?\s*[:=]?\s*\$?\s*(-?\d[\d,]*(?:\.\d+)?)"
)
_LEAD_NUM_RE = re.compile(r"^\s*\$?\s*(-?\d[\d,]*(?:\.\d+)?)")
_LEAD_EXPR_RE = re.compile(r"^\s*\$?\s*-?\d[\d,]*(?:\.\d+)?\s*[-+*/×x÷]\s*\$?\s*\d")
_LIST_ITEM_RE = re.compile(r"^\s*\d{1,2}[.)]\s+\S")


def _clean_lines(response: str) -> list[str]:
    lines = [re.sub(r"\*\*|__|`", "", l).strip() for l in response.splitlines()]  # keep single * (multiplication)
    return [l for l in lines if l and not re.fullmatch(r"`{3}.*", l)]


def _num_value(s: str) -> float:
    return float(s.replace(",", ""))


def _line_answer(line: str):
    """(value, kind) for one line. value is None when the line does not decide one answer."""
    if _LIST_ITEM_RE.match(line):
        return None, "list_item"
    lead = None
    m = _LEAD_NUM_RE.match(line)
    if m and not _LEAD_EXPR_RE.match(line):
        lead = _num_value(m.group(1))
    if "=" in line:
        rhs_vals = set()
        for seg in line.split("=")[1:]:
            if _LEAD_EXPR_RE.match(seg):      # "= 17 * 23" restates an expression; not a result
                continue
            m2 = _LEAD_NUM_RE.match(seg)
            if m2:
                rhs_vals.add(_num_value(m2.group(1)))
        if len(rhs_vals) == 1 and (lead is None or lead in rhs_vals):
            return rhs_vals.pop(), "equation"
        if rhs_vals or lead is not None:
            return None, "conflicting_candidates"
        return None, "no_clear_answer"
    if _LEAD_EXPR_RE.match(line):
        return None, "expression_without_result"
    if lead is not None:
        others = {v for v in parse_numbers(line) if abs(v - lead) > 1e-9}
        if others:
            return None, "leading_number_with_other_numbers"
        return lead, "leading_number"
    return None, "no_clear_answer"


def _markers(lines: list[str]):
    """(values, conflict). conflict is True if a marker line names other numbers after the marker value."""
    vals, conflict = set(), False
    for l in lines:
        for m in _MARKER_RE.finditer(l):
            v = _num_value(m.group(1))
            vals.add(v)
            if any(abs(x - v) > 1e-9 for x in parse_numbers(l[m.end():])):
                conflict = True
    return vals, conflict


def score_math(task: dict, response: str) -> dict:
    lines = _clean_lines(response)
    if not lines:
        return _fail("empty", candidates=[])
    markers, marker_conflict = _markers(lines)
    per_line = [_line_answer(l) for l in lines]
    first_val, first_kind = per_line[0]
    exp = float(task["expected"])
    candidates = sorted(markers | {v for v, _ in per_line if v is not None})

    if marker_conflict or len(markers) > 1:
        return _review("conflicting_answers", candidates=candidates, kind="marker")
    if first_val is None and not markers:
        if not parse_numbers("\n".join(lines)):
            return _review("no_number", candidates=[])
        return _review(first_kind, candidates=candidates[:5])
    if len(candidates) > 1:
        return _review("conflicting_answers", candidates=candidates[:5],
                       kind="marker_vs_line" if markers else "multiple_line_results")
    value = candidates[0]
    kind = "marker" if markers else first_kind
    ok = abs(value - exp) <= 1e-9
    return _pass_or_fail(ok, "ok", "wrong_final_number", last_number=value, kind=kind)


# ---------------------------------------------------------------- extraction
#
# Every extraction requirement has two parts, scored separately:
#   correct    the response SELECTS the accepted entity (components["correct"])
#   compliant  the response does not include the excluded items the requirement names
#              ("does not include the location", "none of the other numbers", "not the old address")
#              (components["compliant"])
# pass = the full requirement = correct AND compliant. The requirement is not loosened.
#
# Rules:
#  * Passage echo (>= 60% of the passage's words as one contiguous run): fail, passage_echo
#    (instruction violation: not an extraction). correct=None, compliant=0.
#  * Candidates are accepted-answer hits and distractor hits, with their positions.
#    A candidate is NEGATED (a rejected alternative) when a negation/contrast word
#    ("not", "no", "instead", "than", "without", "never", "rather", "old") is among the 3 words
#    before it AND inside the same clause (no , ; : . ! ? ( ) or dash between them).
#    "There is no ambiguity: $1,250.00" is therefore NOT negated.
#  * Selected = not negated.
#      no accepted selected, distractor selected      -> fail wrong_entity            (correct 0)
#      no accepted selected, accepted only negated     -> fail accepted_negated        (correct 0)
#      nothing selected                                -> fail accepted_missing        (correct 0)
#      accepted first, a distractor also selected      -> fail distractor_included     (correct 1, compliant 0)
#      distractor first, accepted also selected        -> fail distractor_included     (correct None: conflicting
#                                                         selection; compliant 0). The requirement fails either way.
#      accepted selected, distractor only as negated   -> review negated_alternative_mentioned (correct 1,
#                                                         compliant None: whether naming a rejected item
#                                                         "includes" it is a human judgement)
#      accepted selected, no distractor anywhere       -> pass ok                      (correct 1, compliant 1)

NEG_WORDS = {"not", "no", "instead", "than", "without", "never", "rather", "old"}
ECHO_FRACTION = 0.6
_CLAUSE_BREAK_RE = re.compile(r"[,;:.!?()\[\]—–]|\s-\s")


def _echoes_passage(passage: str, norm_response: str) -> bool:
    pw = normalize(passage).split()
    rw = norm_response.split()
    if not pw or not rw:
        return False
    run = difflib.SequenceMatcher(None, pw, rw, autojunk=False).find_longest_match(0, len(pw), 0, len(rw)).size
    return run >= ECHO_FRACTION * len(pw)


def _negated(raw_prefix: str) -> bool:
    """raw_prefix: the text before a candidate, punctuation intact. Only the current clause counts."""
    clause = _CLAUSE_BREAK_RE.split(raw_prefix[-200:])[-1]
    return any(w in NEG_WORDS for w in normalize(clause).split()[-3:])


def _raw_phrase_spans(text: str, phrase: str):
    """Start indices of `phrase` in `text`, case-insensitive, words separated by any non-word run."""
    words = normalize(phrase).split()
    if not words:
        return []
    pat = r"(?<!\w)" + r"[\W_]+".join(re.escape(w) for w in words) + r"(?!\w)"
    return [m.start() for m in re.finditer(pat, text, re.I)]


def _candidates(task: dict, response: str):
    """List of dicts: pos, accepted (bool), negated (bool), label. Positions index the NFKC text."""
    text = unicodedata.normalize("NFKC", response)
    out = []
    if "accepted_numbers" in task:
        acc = task["accepted_numbers"]
        dis = task.get("distractor_numbers", [])
        for pos, v in _num_tokens(text):
            if any(abs(v - a) <= 1e-9 for a in acc):
                kind = True
            elif any(abs(v - d) <= 1e-9 for d in dis):
                kind = False
            else:
                continue
            out.append({"pos": pos, "accepted": kind, "negated": _negated(text[:pos]), "label": v})
    else:
        seen = set()
        for is_acc, phrases in ((True, task["accepted"]), (False, task.get("distractors", []))):
            for ph in phrases:
                for pos in _raw_phrase_spans(text, ph):
                    if (pos, is_acc) in seen:      # "December 25" inside "December 25th": count once
                        continue
                    seen.add((pos, is_acc))
                    out.append({"pos": pos, "accepted": is_acc, "negated": _negated(text[:pos]), "label": ph})
    return sorted(out, key=lambda c: c["pos"])


def score_extraction(task: dict, response: str) -> dict:
    norm = normalize(response)
    if not norm:
        return _fail("empty", correct=0, compliant=0, accepted_found=False, distractor_found=False)
    if _echoes_passage(task["passage"], norm):
        return _fail("passage_echo", correct=None, compliant=0, accepted_found=None, distractor_found=None)
    cands = _candidates(task, response)
    selected = [c for c in cands if not c["negated"]]
    acc = [c for c in selected if c["accepted"]]
    dis = [c for c in selected if not c["accepted"]]
    neg_acc = [c for c in cands if c["negated"] and c["accepted"]]
    neg_dis = [c for c in cands if c["negated"] and not c["accepted"]]
    comp = dict(accepted_found=bool(acc), distractor_found=bool(dis),
                distractors_hit=sorted({c["label"] for c in dis}, key=str),
                rejected_alternatives=sorted({c["label"] for c in cands if c["negated"]}, key=str))
    if not acc:
        if dis:
            return _fail("wrong_entity", correct=0, compliant=0, **comp)
        if neg_acc:
            return _fail("accepted_negated", correct=0, compliant=None if neg_dis else 1, **comp)
        return _fail("accepted_missing", correct=0, compliant=None if neg_dis else 1, **comp)
    if dis:
        first_ok = selected[0]["accepted"]
        return _fail("distractor_included", correct=1 if first_ok else None, compliant=0,
                     selection="accepted_first" if first_ok else "conflicting", **comp)
    if neg_dis:
        return _review("negated_alternative_mentioned", correct=1, compliant=None, **comp)
    return _pass_or_fail(True, "ok", "", correct=1, compliant=1, **comp)


# ---------------------------------------------------------------- evidence QA
#
# Verdict: exactly one distinct yes/no token outside quoted material (both -> ambiguous,
# none -> missing). Quote: the task's support span appears as a contiguous, normalized
# phrase in the response (quoted or not). Shared words or n-grams do not count.
# The suite only has yes/no items; every reference verdict must follow from the passage.

_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|(?<!\w)'[^']*'(?!\w)")


def score_evidence_qa(task: dict, response: str) -> dict:
    unquoted = normalize(_QUOTED_RE.sub(" ", response or ""))
    verdict_words = {w for w in unquoted.split() if w in ("yes", "no")}
    if len(verdict_words) == 1:
        verdict = verdict_words.pop()
        verdict_status = "parsed"
    else:
        verdict = None
        verdict_status = "ambiguous" if verdict_words else "missing"
    verdict_ok = verdict == task["verdict"]

    norm = normalize(response)
    quote_ok = any(_contains_phrase(norm, s) for s in task["support_spans"])

    if verdict is None:
        if not (response or "").strip():
            return _fail("empty", correct=0, compliant=0, verdict=None, verdict_status="empty",
                         verdict_ok=False, quote_ok=False)
        return _review(f"verdict_{verdict_status}", correct=None, compliant=int(quote_ok), verdict=None,
                       verdict_status=verdict_status, verdict_ok=False, quote_ok=quote_ok)
    if not verdict_ok:
        reason = "wrong_verdict"
    elif not quote_ok:
        reason = "no_supporting_quote"
    else:
        reason = "ok"
    # correct = right verdict; compliant = cites the support phrase the task asks for.
    return _pass_or_fail(verdict_ok and quote_ok, "ok", reason, correct=int(verdict_ok), compliant=int(quote_ok),
                         verdict=verdict, verdict_status=verdict_status, verdict_ok=verdict_ok, quote_ok=quote_ok)


# ---------------------------------------------------------------- coding: extraction

_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)


def _defines(src: str, name: str):
    """True / False, or "too_complex" when the source was not parsed because of the bounds."""
    if _bounded_source(src):
        return "too_complex"
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return False
    except (MemoryError, RecursionError):
        return "too_complex"
    return any(isinstance(n, ast.FunctionDef) and n.name == name for n in tree.body)


def extract_code(response: str, function_name: str) -> tuple[str | None, str]:
    """Return (code, method). PLAN.md C.1 order: fenced block defining the
    function -> whole response -> from first def/import/from line, dropping
    trailing lines until it parses. Callers must bound `response` first."""
    text = response or ""
    too_complex = False

    def defines(src):
        nonlocal too_complex
        r = _defines(src, function_name)
        too_complex |= r == "too_complex"
        return r is True

    for block in _FENCE_RE.findall(text):
        if defines(block):
            return block, "fenced"
    if defines(text):
        return text, "whole_response"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r"\s*(def|import|from)\s", l)), None)
    if start is not None:
        chunk = lines[start:]
        while chunk:
            src = "\n".join(chunk)
            if defines(src):
                return src, "from_first_def"
            chunk = chunk[:-1]
    # A parse limit is not evidence that the function is missing.
    return None, "source_too_complex" if too_complex else "not_found"


# ---------------------------------------------------------------- coding: static policy

ALLOWED_IMPORTS = {"math", "re", "string", "collections", "itertools", "functools", "typing"}
BANNED_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "globals", "locals",
                "vars", "getattr", "setattr", "delattr", "breakpoint", "help", "memoryview", "__builtins__"}


def policy_check(src: str) -> tuple[bool, str]:
    """Static pre-screen. Second layer only; the sandbox is the boundary."""
    bound = _bounded_source(src)
    if bound:
        return False, bound
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as e:
        return False, f"syntax_error: {e.__class__.__name__}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    return False, f"import_not_allowed: {a.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS or node.level:
                return False, f"import_not_allowed: {node.module}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            return False, f"call_not_allowed: {node.func.id}"
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            return False, f"name_not_allowed: {node.id}"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return False, f"dunder_attribute: {node.attr}"
    return True, "ok"


# ---------------------------------------------------------------- coding: scoring

class SandboxUnavailable:
    """Default backend: refuses to execute anything."""
    name = "unavailable"
    verified = False

    def run(self, code, function_name, tests):  # pragma: no cover - never executes
        raise RuntimeError("no sandbox backend; candidate code is never run on the host")


def score_coding(task: dict, response: str, backend=None) -> dict:
    fn = task["function_name"]
    code, method = extract_code(response, fn)
    if code is None:
        if method == "source_too_complex":
            return _review("source_too_complex", extraction=method, policy_rejected=False)
        return _fail("function_not_found", extraction=method, policy_rejected=False)
    ok, why = policy_check(code)
    if not ok:
        if why in ("source_oversized", "source_too_complex"):
            return _review(why, extraction=method, policy_rejected=False)
        # Policy rejection is a scored failure but reported separately (PLAN.md F.7).
        return _fail("sandbox_policy_rejected", extraction=method, policy_rejected=True, policy_reason=why)
    if backend is None or not getattr(backend, "verified", False):
        return _result(False, None, "sandbox_unavailable", status="sandbox_unavailable", extraction=method,
                       policy_rejected=False, backend=getattr(backend, "name", None))
    out = backend.run(code, fn, task["tests"])
    # out (sandbox.DockerBackend.run): passed/total are computed ON THE HOST from the returned values;
    # outcome is one of "completed", "timeout", "output_limit", "no_harness_result", "infrastructure_error".
    outcome = out.get("outcome")
    common = dict(extraction=method, policy_rejected=False, **_sb(out))
    if outcome == "infrastructure_error" or out.get("error"):
        return _result(False, None, f"sandbox_error: {out.get('error') or outcome}", status="sandbox_unavailable", **common)
    if outcome == "timeout" or out.get("timed_out"):
        return _fail("timeout", **common)
    if outcome == "output_limit":
        return _fail("output_limit_exceeded", **common)
    if outcome == "no_harness_result":
        # The candidate ended or corrupted the harness before it reported (e.g. os._exit, OOM kill).
        return _fail("no_harness_result", **common)
    all_ok = out.get("total", 0) > 0 and out.get("passed") == out.get("total")
    return _pass_or_fail(all_ok, "ok", "tests_failed", **common)


def _sb(out):
    return {"tests_passed": out.get("passed"), "tests_total": out.get("total"),
            "exit_code": out.get("exit_code"), "timed_out": out.get("timed_out", False),
            "sandbox_outcome": out.get("outcome"), "output_limited": out.get("output_limited", False),
            "container_left_behind": out.get("container_left_behind"),
            "stderr_tail": (out.get("stderr_tail") or "")[-500:]}


# ---------------------------------------------------------------- dispatch

SCORERS = {"math": score_math, "extraction": score_extraction, "evidence_qa": score_evidence_qa}


def score(task: dict, response: str, backend=None) -> dict:
    gate = _bound_response(response)
    if gate:
        return _result(False, None, gate, status=gate, length=len(response) if isinstance(response, str) else None)
    if task["type"] == "coding":
        return score_coding(task, response, backend)
    if task["type"] == "extraction" and not response.strip():
        return _fail("empty", accepted_found=False, distractor_found=False)
    return SCORERS[task["type"]](task, response)
