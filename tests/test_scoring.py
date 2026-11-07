"""Hand-written scorer, task-data, sandbox-config and dry-run tests (stdlib unittest).

Run from the pilot root:  python3 -I -m unittest discover -s tests -v

Passing these tests shows only that the rules behave as written on the cases
below. Scorer validity is checked against human review of real outputs (Stage 2).
No test here executes model-written code or opens a network connection.
"""

import csv
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from farex_pilot import runner, sandbox, scoring  # noqa: E402

TASKS = {t["id"]: t for t in runner.load_tasks()}
LONG = "lorem ipsum " * 10000


def P(task_id, response, backend=None):
    return scoring.score(TASKS[task_id], response, backend)


class Base(unittest.TestCase):
    def assertPass(self, tid, resp, backend=None):
        r = P(tid, resp, backend)
        self.assertEqual(r["pass"], 1, f"{tid}: {resp[:80]!r} -> {r}")

    def assertReview(self, tid, resp, reason=None):
        r = P(tid, resp)
        self.assertIsNone(r["pass"], f"{tid}: {resp[:80]!r} -> {r}")
        self.assertFalse(r["scored"])
        self.assertEqual(r["score_status"], "review_needed", r)
        if reason:
            self.assertEqual(r["reason"], reason, r)

    def assertStatus(self, tid, resp, status):
        r = P(tid, resp)
        self.assertIsNone(r["pass"]); self.assertFalse(r["scored"])
        self.assertEqual(r["score_status"], status, r)

    def assertFail(self, tid, resp, backend=None, reason=None):
        r = P(tid, resp, backend)
        self.assertEqual(r["pass"], 0, f"{tid}: {resp[:80]!r} -> {r}")
        self.assertTrue(r["scored"])
        if reason:
            self.assertEqual(r["reason"], reason, r)


# ---------------------------------------------------------------- math

class TestMath(Base):
    def test_correct_canonical(self):
        self.assertPass("math_1", "391")
        self.assertPass("math_4", "6")

    def test_correct_other_formats(self):
        self.assertPass("math_1", "The answer is 391.")
        self.assertPass("math_1", "17 * 23 = 391")
        self.assertPass("math_1", "**391**")
        self.assertPass("math_4", "$6.00")
        self.assertPass("math_4", "6 dollars")
        self.assertPass("math_4", "5 × $1.20 = $6.00")
        self.assertPass("math_1", "Answer: 391\n17 * 23 = 391")
        self.assertPass("math_1", "391\n17 * 23 = 391")
        self.assertPass("math_2", "144 / 12 = 12.0")
        self.assertPass("math_2", "144 divided by 12 is 12.\nAnswer: 12")       # unclear first line, explicit marker
        self.assertReview("math_4", "6. (25 pencils = 5 packs, 5 × $1.20)", reason="list_item")

    def test_incorrect(self):
        self.assertFail("math_1", "381", reason="wrong_final_number")
        self.assertFail("math_4", "$10.00", reason="wrong_final_number")   # per-pencil misreading of the old wording
        self.assertFail("math_3", "-200")                                  # sign is respected
        self.assertFail("math_1", "17 * 23 = 381", reason="wrong_final_number")
        self.assertFail("math_1", "The answer is 390.", reason="wrong_final_number")
        self.assertFail("math_4", "Answer: $5.00", reason="wrong_final_number")
        self.assertFail("math_2", "12.5", reason="wrong_final_number")

    def test_audit_regressions_operand_or_index_is_not_an_answer(self):
        # B2 audit fixtures: the parser must not pick an operand or a list index and call it wrong.
        self.assertReview("math_2", "144 divided by 12 is 12.", reason="leading_number_with_other_numbers")
        self.assertReview("math_4", "25 pencils cost $6.00.", reason="leading_number_with_other_numbers")
        self.assertReview("math_1", "1. Multiply 17 by 23 to get 391.", reason="list_item")
        self.assertReview("math_1", "1. Multiply 17 by 23.\n2. 17 * 23 = 391", reason="list_item")
        # The multi-step pencils calculation (operands restated before the answer).
        self.assertReview("math_4", "25 pencils = 5 packs, so 5 × $1.20 = $6.00", reason="conflicting_candidates")
        self.assertReview("math_4", "25 / 5 = 5 packs\n5 * 1.20 = 6\nThe answer is $6.", reason="conflicting_answers")
        self.assertReview("math_4", "25 / 5 = 5 packs\n5 * 1.20 = 6", reason="conflicting_answers")
        self.assertReview("math_4", "To find the cost:\n25 / 5 = 5 packs\n5 x $1.20 = $6.00", reason="no_clear_answer")
        for resp in ("144 divided by 12 is 12.", "25 pencils cost $6.00.", "1. Multiply 17 by 23 to get 391."):
            for tid in ("math_1", "math_2", "math_4"):
                self.assertNotEqual(P(tid, resp)["pass"], 0, (tid, resp))

    def test_answer_first_rule(self):
        # Other numbers on the answer line make the answer line ambiguous -> review, not a guess.
        self.assertReview("math_1", "391 (that is 17 * 23)", reason="leading_number_with_other_numbers")
        self.assertReview("math_1", "391 = 17 * 23", reason="conflicting_candidates")
        self.assertReview("math_1", "391 (check: 17 * 20 = 340, 17 * 3 = 51)", reason="conflicting_candidates")
        # The answer must come first (first line or explicit marker).
        self.assertReview("math_1", "Let me think.\n391", reason="no_clear_answer")

    def test_conflicts_and_unclear_go_to_review(self):
        self.assertReview("math_1", "17 * 23 = 391\nAnswer: 390", reason="conflicting_answers")
        self.assertReview("math_1", "17 * 23 = 391 (check: 17 * 20 = 340, 17 * 3 = 51)", reason="conflicting_candidates")
        self.assertReview("math_3", "99 - 101", reason="expression_without_result")
        self.assertReview("math_1", "Answer: 391 or 381", reason="conflicting_answers")
        self.assertReview("math_1", "Answer: 391\nFinal answer: 381", reason="conflicting_answers")
        self.assertReview("math_1", "The answer is not 390; it is 391.", reason="no_clear_answer")

    def test_minus_is_not_a_sign_between_numbers(self):
        self.assertEqual(scoring.parse_numbers("17 - 23"), [17, 23])
        self.assertEqual(scoring.parse_numbers("10-5"), [10, 5])
        self.assertEqual(scoring.parse_numbers("$1,250.00"), [1250.0])

    def test_malformed(self):
        self.assertFail("math_1", "", reason="empty")                    # no answer given = incorrect
        self.assertFail("math_1", "   \n\t ", reason="empty")
        self.assertFail("math_1", "```\n```", reason="empty")
        self.assertStatus("math_1", LONG, "response_oversized")          # never parsed
        self.assertReview("math_1", "three hundred ninety-one", reason="no_number")  # number words: review, not wrong

    def test_wrong_specialist_style(self):
        # coding-style answer to a math task: function, no final number
        self.assertReview("math_1", "def multiply():\n    return 17 * 23", reason="no_clear_answer")
        # evidence-style answer
        self.assertReview("math_1", "Yes. \"17 * 23\"", reason="no_clear_answer")
        # extraction-style answer that happens to be right should pass (scored on requirement only)
        self.assertPass("math_1", "391")


# ---------------------------------------------------------------- extraction

class TestExtraction(Base):
    def comp(self, tid, resp):
        c = P(tid, resp)["components"]
        return c.get("correct"), c.get("compliant")

    def test_correct_canonical(self):
        self.assertPass("extraction_1", "December 25th")
        self.assertPass("extraction_2", "Stanford University")

    def test_correct_other_formats(self):
        self.assertPass("extraction_1", "The date is Dec. 25.")
        self.assertPass("extraction_2", "The organization is **Stanford**.")
        self.assertPass("extraction_3", "Total: $1,250.00")
        self.assertPass("extraction_3", "1250")
        self.assertPass("extraction_4", "Send it to maria.lopez@example.org")
        self.assertEqual(self.comp("extraction_1", "December 25th"), (1, 1))

    def test_incorrect(self):
        self.assertFail("extraction_1", "New York", reason="wrong_entity")  # listed distractor only
        self.assertFail("extraction_3", "$4,471", reason="wrong_entity")
        self.assertFail("extraction_4", "m.lopez@example.com", reason="wrong_entity")
        self.assertEqual(self.comp("extraction_3", "$4,471"), (0, 0))

    def test_echo(self):
        # Passage echo is rejected (instruction violation: not extraction), even though the answer is inside it.
        self.assertFail("extraction_2", "Dr. Smith works at Stanford University", reason="passage_echo")
        self.assertEqual(self.comp("extraction_2", "Dr. Smith works at Stanford University"), (None, 0))

    def test_requirement_exclusions_are_compliance_failures(self):
        # Each requirement names what must NOT be included. Including it fails the requirement
        # (compliant=0) even when the right entity is selected (correct=1). Not loosened.
        for tid, resp in (("extraction_3", "$1,250.00, payable within 30 days"),
                          ("extraction_3", "1250 (invoice 4471 was issued on 3 March 2026)"),
                          ("extraction_1", "Date: December 25th; location: New York"),
                          ("extraction_2", "Stanford University (Dr. Smith's employer)")):
            self.assertFail(tid, resp, reason="distractor_included")
            self.assertEqual(self.comp(tid, resp), (1, 0), resp)

    def test_listing_every_candidate_is_not_a_pass(self):
        r = P("extraction_3", "The numbers are 1,250.00, 4471, 2026 and 30.")
        self.assertEqual((r["pass"], r["reason"], r["components"]["compliant"]), (0, "distractor_included", 0))

    def test_conflicting_selection(self):
        # Distractor first and accepted later: the selection is unclear (correct=None), but the
        # exclusion requirement is violated either way, so the requirement verdict is definite.
        r = P("extraction_3", "4471, total $1,250.00")
        self.assertEqual((r["pass"], r["reason"]), (0, "distractor_included"))
        self.assertEqual((r["components"]["correct"], r["components"]["compliant"]), (None, 0))
        self.assertFail("extraction_2", "Smith", reason="wrong_entity")
        self.assertFail("extraction_4", "Send it to m.lopez@example.com", reason="wrong_entity")

    def test_negation(self):
        # B3 audit fixture: "no" in a different clause does not negate the answer.
        self.assertPass("extraction_3", "There is no ambiguity: $1,250.00")
        self.assertPass("extraction_3", "No doubt, the total is $1,250.00")
        # A negated distractor is a rejected alternative: the selection is right (correct=1), but whether
        # naming it violates "not the old address" is a human judgement -> review, compliance unknown.
        for resp in ("maria.lopez@example.org, not m.lopez@example.com",
                     "maria.lopez@example.org (not the old m.lopez@example.com address)"):
            self.assertReview("extraction_4", resp, reason="negated_alternative_mentioned")
            self.assertEqual(self.comp("extraction_4", resp), (1, None))
        # maria.lopez is not mistaken for the distractor m.lopez
        self.assertPass("extraction_4", "maria.lopez@example.org (the new address)")
        # Accepted entity only mentioned as rejected -> not selected -> wrong.
        self.assertFail("extraction_3", "Not $1,250.00", reason="accepted_negated")
        self.assertFail("extraction_3", "The total is not $1,250.00 but $30", reason="wrong_entity")

    def test_malformed(self):
        self.assertFail("extraction_1", "", reason="empty")
        self.assertFail("extraction_1", "  \n ", reason="empty")
        self.assertFail("extraction_1", "```\n```", reason="empty")
        self.assertStatus("extraction_1", (TASKS["extraction_1"]["passage"] + " ") * 2000, "response_oversized")

    def test_wrong_specialist_style(self):
        self.assertFail("extraction_1", "25")                                   # math-style bare number
        self.assertFail("extraction_2", "def extract(s):\n    return s.split('at ')[1]")  # code
        # evidence-style answer that contains the right span and no distractor passes (requirement met)
        self.assertPass("extraction_1", "Yes. \"The meeting is on December 25th\"")  # 7 of 12 passage words: under the 60% echo threshold


# ---------------------------------------------------------------- evidence QA

class TestEvidenceQA(Base):
    def test_correct_canonical(self):
        self.assertPass("evidence_qa_1", "No. \"Sunday service is suspended for maintenance\"")
        self.assertPass("evidence_qa_2", "Yes. \"The Greenvale reservoir holds 3.2 million litres\"")

    def test_correct_other_formats(self):
        self.assertPass("evidence_qa_3", "yes — 'the bridge opened to traffic in 1937'")
        self.assertPass("evidence_qa_4", "No. The text says the museum is open Tuesday through Sunday from 10am to 5pm.")
        self.assertPass("evidence_qa_2", "**Yes**\n\n> The Greenvale reservoir holds 3.2 million litres")
        self.assertPass("evidence_qa_1", "No, the claim is wrong: “Sunday service is suspended for maintenance until spring.”")

    def test_incorrect(self):
        self.assertFail("evidence_qa_2", "No. \"The Greenvale reservoir holds 3.2 million litres\"", reason="wrong_verdict")
        self.assertFail("evidence_qa_2", "Yes. \"holds 3.2 million\"", reason="no_supporting_quote")  # partial span, not the support phrase
        self.assertFail("evidence_qa_4", "No. \"closed on public holidays\"", reason="no_supporting_quote")
        c = P("evidence_qa_4", "No. \"closed on public holidays\"")["components"]
        self.assertEqual((c["correct"], c["compliant"]), (1, 0))      # right verdict, citation requirement not met

    def test_outside_knowledge_without_passage_fails(self):
        # Correct verdict from world knowledge but no passage-based support -> fail.
        self.assertFail("evidence_qa_1", "No. Trains do not run on Sundays in most cities.", reason="no_supporting_quote")
        self.assertFail("evidence_qa_2", "Yes. The reservoir is large and holds a lot of water.", reason="no_supporting_quote")

    def test_verdict_parsing(self):
        self.assertReview("evidence_qa_1", "Yes and no. \"Sunday service is suspended for maintenance\"", reason="verdict_ambiguous")
        # yes/no inside a quotation is ignored
        self.assertPass("evidence_qa_4", "No — \"open Tuesday through Sunday from 10am to 5pm\" (so 'no' for Mondays)")

    def test_malformed(self):
        self.assertFail("evidence_qa_1", "", reason="empty")
        self.assertFail("evidence_qa_1", "\n\n", reason="empty")
        self.assertReview("evidence_qa_1", "I cannot tell from the text.", reason="verdict_missing")
        self.assertStatus("evidence_qa_1", LONG, "response_oversized")
        self.assertFail("evidence_qa_2", "```\nyes\n", reason="no_supporting_quote")

    def test_wrong_specialist_style(self):
        self.assertReview("evidence_qa_2", "3.2", reason="verdict_missing")                       # math-style
        self.assertReview("evidence_qa_2", "The Greenvale reservoir holds 3.2 million litres", reason="verdict_missing")  # extraction-style
        self.assertReview("evidence_qa_3", "def check():\n    return 1937 < 1950", reason="verdict_missing")  # code


# ---------------------------------------------------------------- coding (no execution)

class FakeBackend:
    """Returns canned outcomes; never executes the code it is given."""
    name, verified = "fake", True

    def __init__(self, out):
        self.out, self.seen = out, []

    def run(self, code, fn, tests):
        self.seen.append(code)
        return dict(self.out)


ALL_PASS = {"passed": 4, "total": 4, "exit_code": 0, "timed_out": False, "stderr_tail": "", "outcome": "completed"}


class TestCodingExtraction(Base):
    fn = "reverse_string"

    def ex(self, resp):
        return scoring.extract_code(resp, self.fn)

    def test_canonical(self):
        self.assertEqual(self.ex("def reverse_string(s):\n    return s[::-1]")[1], "whole_response")
        self.assertEqual(self.ex("```python\ndef reverse_string(s):\n    return s[::-1]\n```")[1], "fenced")

    def test_other_formats(self):
        code, m = self.ex("Here you go:\n```python\ndef reverse_string(text):\n    return ''.join(reversed(text))\n```\nHope it helps.")
        self.assertEqual(m, "fenced")
        code, m = self.ex("Sure.\ndef reverse_string(s):\n    return s[::-1]\nThis slices backwards.")
        self.assertEqual(m, "from_first_def")
        self.assertNotIn("slices backwards", code)
        code, m = self.ex("```\nprint('x')\n```\n```py\ndef reverse_string(s):\n    return s[::-1]\n```")
        self.assertEqual(m, "fenced")
        self.assertIn("def reverse_string", code)

    def test_incorrect_name_or_syntax(self):
        self.assertFail("coding_1", "def rev(s):\n    return s[::-1]", reason="function_not_found")
        self.assertFail("coding_1", "def reverse_string(s)\n    return s[::-1]", reason="function_not_found")

    def test_incorrect_logic_reported_by_backend(self):
        fb = FakeBackend({**ALL_PASS, "passed": 2})
        self.assertFail("coding_1", "def reverse_string(s):\n    return s", backend=fb, reason="tests_failed")

    def test_timeout_reported_by_backend(self):
        fb = FakeBackend({**ALL_PASS, "passed": 0, "timed_out": True, "exit_code": None, "outcome": "timeout"})
        self.assertFail("coding_1", "def reverse_string(s):\n    while True: pass", backend=fb, reason="timeout")

    def test_sandbox_outcomes_reported_by_backend(self):
        code = "def reverse_string(s):\n    return s[::-1]"
        self.assertFail("coding_1", code, FakeBackend({**ALL_PASS, "passed": 0, "outcome": "output_limit"}),
                        reason="output_limit_exceeded")
        self.assertFail("coding_1", code, FakeBackend({**ALL_PASS, "passed": 0, "outcome": "no_harness_result"}),
                        reason="no_harness_result")
        r = P("coding_1", code, FakeBackend({**ALL_PASS, "passed": 0, "outcome": "infrastructure_error",
                                             "error": "docker_run_exit_125"}))
        self.assertEqual((r["pass"], r["score_status"]), (None, "sandbox_unavailable"))

    def test_pass_reported_by_backend(self):
        fb = FakeBackend(ALL_PASS)
        self.assertPass("coding_1", "def reverse_string(s):\n    return s[::-1]", backend=fb)
        self.assertEqual(len(fb.seen), 1)

    def test_malformed(self):
        for resp in ("", "   ", "```\n```"):
            self.assertFail("coding_1", resp, reason="function_not_found")
        self.assertStatus("coding_1", LONG, "response_oversized")

    def test_wrong_specialist_style(self):
        self.assertFail("coding_1", "cba", reason="function_not_found")             # extraction/math-style
        self.assertFail("coding_1", "Yes. \"reverse the string\"", reason="function_not_found")  # evidence-style


class TestCodingPolicyAndIsolation(Base):
    def test_policy_rejections(self):
        bad = {
            "import os\ndef reverse_string(s):\n    return s[::-1]": "import_not_allowed",
            "from subprocess import run\ndef reverse_string(s):\n    return s": "import_not_allowed",
            "def reverse_string(s):\n    open('/etc/passwd')\n    return s": "call_not_allowed",
            "def reverse_string(s):\n    return eval('s[::-1]')": "call_not_allowed",
            "def reverse_string(s):\n    return ().__class__.__bases__": "dunder_attribute",
            "def reverse_string(s):\n    f = getattr\n    return s": "name_not_allowed",
            "def reverse_string(s):\n    return __builtins__['open']": "name_not_allowed",
        }
        for src, why in bad.items():
            r = P("coding_1", src, FakeBackend(ALL_PASS))
            self.assertEqual((r["pass"], r["reason"]), (0, "sandbox_policy_rejected"), src)
            self.assertTrue(r["components"]["policy_rejected"])
            self.assertTrue(r["components"]["policy_reason"].startswith(why), r)

    def test_allowed_imports_pass_policy(self):
        self.assertTrue(scoring.policy_check("import math\nfrom collections import Counter\n")[0])

    def test_deep_nesting_is_review_not_parse(self):
        deep = "def reverse_string(s):\n    return " + "(" * 200 + "s" + ")" * 200 + "\n"
        self.assertLess(len(deep), scoring.MAX_RESPONSE_CHARS)     # under the size gate: the depth gate decides
        fb = FakeBackend(ALL_PASS)
        r = P("coding_1", deep, fb)
        self.assertEqual(fb.seen, [])                                 # never sent to the sandbox
        self.assertEqual((r["pass"], r["score_status"], r["reason"]), (None, "review_needed", "source_too_complex"))
        src = "def reverse_string(s):\n    return " + "(" * 100 + "s" + ")" * 100 + "\n"
        self.assertEqual(scoring.policy_check(src), (False, "source_too_complex"))

    def test_no_backend_means_unscored_not_host_execution(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run:
            r = P("coding_1", "def reverse_string(s):\n    return s[::-1]", backend=None)
            popen.assert_not_called()
            run.assert_not_called()
        self.assertEqual((r["scored"], r["pass"], r["reason"]), (False, None, "sandbox_unavailable"))

    def test_unverified_docker_backend_refuses(self):
        b = sandbox.DockerBackend("python@sha256:" + "0" * 64)
        r = P("coding_1", "def reverse_string(s):\n    return s[::-1]", backend=b)
        self.assertEqual(r["reason"], "sandbox_unavailable")
        with self.assertRaises(RuntimeError):
            b.run("x=1", "f", [])

    def test_docker_args_have_every_control(self):
        a = sandbox.docker_run_args("python@sha256:" + "0" * 64, "farex-sbx-test", "/tmp/w")
        s = " ".join(a)
        for flag in ("--network none", "--user 65534:65534", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "--read-only", "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m", "--memory 256m",
                     "--memory-swap 256m", "--cpus 1", "--pids-limit 32", "--ulimit nofile=64:64",
                     "--ulimit fsize=1048576:1048576", "--pull never", "--rm",
                     "type=bind,source=/tmp/w,target=/work,readonly"):
            self.assertIn(flag, s)
        self.assertEqual(sum(x == "--mount" for x in a), 1, "exactly one mount")
        self.assertFalse(any(x in ("-v", "--volume", "-e", "--env", "--env-file", "--privileged") for x in a))
        self.assertNotIn("docker.sock", s)
        self.assertEqual(a[-4:], ["python3", "-I", "-S", "/work/harness.py"])

    def test_availability_on_this_machine_is_reported(self):
        b = sandbox.default_backend()
        self.assertFalse(b.verified)
        self.assertIn(b.status, ("blocked", "available_unverified"))


class TestHostJudge(unittest.TestCase):
    """The host computes pass/fail from returned values; a record cannot assert a verdict."""
    tests = TASKS["coding_1"]["tests"]

    def rec(self, values, **kw):
        return {"protocol": sandbox.HARNESS_PROTOCOL, "load_error": None,
                "results": [{"raised": None, "t": type(v).__name__, "v": v} for v in values], **kw}

    def test_correct_values_pass(self):
        self.assertEqual(sandbox.judge(self.rec(["", "a", "cba", "!dc ba"]), self.tests), (4, 4, None))

    def test_old_style_verdict_is_not_accepted(self):
        self.assertEqual(sandbox.judge({"passed": 4, "total": 4}, self.tests), (0, 4, "bad_protocol"))

    def test_record_cannot_assert_counts(self):
        r = self.rec(["x", "x", "x", "x"], passed=4, total=4)
        self.assertEqual(sandbox.judge(r, self.tests)[0], 0)

    def test_wrong_shape_and_type_lies_rejected(self):
        self.assertEqual(sandbox.judge(self.rec(["cba"]), self.tests)[2], "bad_results")
        r = self.rec(["", "a", "cba", "!dc ba"])
        r["results"][2]["t"] = "tuple"                       # non-JSON type: never equal
        self.assertEqual(sandbox.judge(r, self.tests)[0], 3)
        b = {"protocol": sandbox.HARNESS_PROTOCOL, "load_error": None,
             "results": [{"raised": None, "t": "int", "v": True}]}
        self.assertEqual(sandbox.judge(b, [{"args": [], "expected": 1}])[0], 0)   # type/value disagree
        self.assertEqual(sandbox.judge({**r, "load_error": "SyntaxError"}, self.tests), (0, 4, None))

    def test_bool_strictness_kept(self):
        t = [{"args": [""], "expected": True}]
        one = {"protocol": sandbox.HARNESS_PROTOCOL, "load_error": None, "results": [{"raised": None, "t": "int", "v": 1}]}
        self.assertEqual(sandbox.judge(one, t)[0], 0)

    def test_expected_values_never_written_into_container_inputs(self):
        b = sandbox.DockerBackend("python@sha256:" + "0" * 64)
        written = {}
        real_open = open

        def fake_run(args, name, timeout, cap=sandbox.OUTPUT_CAP_BYTES):
            work = args[args.index("--mount") + 1].split("source=")[1].split(",")[0]
            for f in ("tests.json", "candidate.py", "harness.py"):
                with real_open(f"{work}/{f}") as fh:
                    written[f] = fh.read()
            return {"exit_code": 0, "timed_out": False, "output_limited": False, "stdout": b"", "stdout_bytes": 0,
                    "stderr_tail": "", "stderr_bytes": 0, "container_left_behind": False}
        with mock.patch.object(sandbox, "run_container", fake_run):
            r = b._run_unchecked("def reverse_string(s):\n    return s", "reverse_string", self.tests)
        self.assertNotIn("expected", written["tests.json"])
        self.assertEqual(json.loads(written["tests.json"])["args"], [c["args"] for c in self.tests])
        self.assertEqual((r["outcome"], r["passed"]), ("no_harness_result", 0))


# ---------------------------------------------------------------- task data

class TestBoundedParsing(unittest.TestCase):
    """Regression for the MemoryError ("Parser stack overflowed") seen on a ~10k-character
    invalid response. Oversized input must be rejected before any parser sees it."""

    def test_largest_parsed_input_does_not_crash(self):
        # Just under MAX_RESPONSE_CHARS, invalid, the same shape that crashed at ~9k characters.
        text = "lorem ipsum " * ((scoring.MAX_RESPONSE_CHARS - 20) // 12)
        self.assertLess(len(text), scoring.MAX_RESPONSE_CHARS)
        for tid in ("coding_1", "math_1", "extraction_1", "evidence_qa_1"):
            r = P(tid, text)   # must not raise
            self.assertIsNotNone(r["score_status"])

    def test_oversized_never_reaches_parser(self):
        with mock.patch("ast.parse", side_effect=AssertionError("parser called")):
            for tid in ("coding_1", "math_1", "extraction_1", "evidence_qa_1"):
                r = P(tid, "lorem ipsum " * 1000)
                self.assertEqual(r["score_status"], "response_oversized")

    def test_malformed_types(self):
        for resp in (None, 123, b"391"):
            self.assertEqual(P("math_1", resp)["score_status"], "response_malformed")

    def test_source_gate(self):
        self.assertEqual(scoring._bounded_source("(" * (scoring.MAX_BRACKET_DEPTH + 1)), "source_too_complex")
        self.assertIsNone(scoring._bounded_source("x = [1, (2, 3)]"))


class TestFreshConversation(unittest.TestCase):
    """Every Stage 2 call is a single, independent request: system + user only, no history."""

    def test_each_request_has_only_system_and_user(self):
        import io
        import json as _json
        captured = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append(_json.loads(req.data))
            return Resp(b'{"message": {"content": "OK"}}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            for _ in range(2):
                runner._ollama_chat("http://127.0.0.1:11434", "m", "SYS", "USER", runner.STAGE2_OPTIONS)
        self.assertEqual(len(captured), 2)
        for body in captured:
            self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
            self.assertNotIn("context", body)
            self.assertEqual(body["messages"][1]["content"], "USER")

    def test_oversized_http_body_is_rejected_before_parse(self):
        import io
        class Resp(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        big = b'{"message": {"content": "' + b"a" * (runner.MAX_RAW_BYTES + 10) + b'"}}'
        with mock.patch("urllib.request.urlopen", return_value=Resp(big)):
            raw, _ = runner._ollama_chat("http://127.0.0.1:11434", "m", "S", "U", runner.STAGE2_OPTIONS)
        self.assertGreater(len(raw), runner.MAX_RAW_BYTES)   # caller must reject; read was bounded to cap+1


class TestPromptLeakage(unittest.TestCase):
    """Model inputs are the task's prompt only. Hidden answers, rubrics and tests stay out."""

    def test_prompt_contains_no_hidden_fields(self):
        for t in TASKS.values():
            for hidden in ("verdict", "support_spans", "background_knowledge", "reference_solution",
                           "buggy_solution", "accepted", "distractors", "expected"):
                self.assertNotIn(hidden, t["prompt"].lower(), f"{t['id']} prompt mentions {hidden}")
            # The passage is part of the input by design; the answer KEY (verdict, support_spans) is not.
            if t["type"] == "evidence_qa":
                self.assertIn(t["passage"], t["prompt"])
            if t["type"] == "coding":
                self.assertNotIn(t["reference_solution"].strip(), t["prompt"])

    def test_evidence_items_have_no_not_established_category(self):
        for t in TASKS.values():
            if t["type"] == "evidence_qa":
                self.assertIn(t["verdict"], ("yes", "no"))
                self.assertTrue(t["answer_from_passage_only"], t["id"])


class TestTaskData(unittest.TestCase):
    def test_validate_all_pass(self):
        failures = [(n, d) for n, ok, d in runner.validate() if not ok]
        self.assertEqual(failures, [])

    def test_every_type_rule_is_destination_independent(self):
        for t in TASKS.values():
            self.assertNotIn("destination", json.dumps(t))


# ---------------------------------------------------------------- dry run, order, review sheet

class NoNetwork:
    def __enter__(self):
        def refuse(*a, **k):
            raise AssertionError("network connection attempted")
        self.p = [mock.patch.object(socket.socket, "connect", refuse),
                  mock.patch.object(socket, "create_connection", refuse),
                  mock.patch.object(socket, "getaddrinfo", refuse)]
        for p in self.p:
            p.start()

    def __exit__(self, *a):
        for p in self.p:
            p.stop()


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.art = mock.patch.object(runner, "ART", Path(cls.tmp.name))
        cls.art.start()
        with NoNetwork():
            cls.path = runner.dry_run(runner.DEFAULT_ORDER_SEED)
        cls.rows = [json.loads(l) for l in cls.path.read_text().splitlines()]

    @classmethod
    def tearDownClass(cls):
        cls.art.stop()
        cls.tmp.cleanup()

    def test_32_schema_valid_dry_run_rows(self):
        self.assertEqual(len(self.rows), 32)
        for r in self.rows:
            self.assertEqual(runner.validate_row(r), [])
            self.assertEqual(r["status"], "dry_run")
            self.assertTrue(r["dry_run"])
            self.assertEqual(r["response_source"], "synthetic_canned_reference_answer")
            self.assertTrue(r["run_id"].startswith("DRYRUN-"))
            self.assertEqual(r["token_source"], "unavailable")
            self.assertIsNone(r["prompt_tokens"])
        self.assertEqual(self.path.parent.name, "dry_run")

    def test_manifest_is_labelled_and_path_free(self):
        m = json.loads(self.path.with_name(self.path.name.replace(".jsonl", ".manifest.json")).read_text())
        self.assertTrue(m["dry_run"])
        text = json.dumps(m)
        self.assertNotIn(str(Path.home()), text)
        self.assertNotIn(str(runner.ROOT), text)

    def test_conditions_and_coding_unscored(self):
        for r in self.rows:
            self.assertEqual(r["label_agreement"], r["condition"] == "reference")
            if r["task_type"] == "coding":
                self.assertEqual(r["reason"], "sandbox_unavailable")
                self.assertFalse(r["scored"])

    def test_order_counterbalanced(self):
        o = runner.execution_order(runner.DEFAULT_ORDER_SEED)
        cells = o["cells"]
        self.assertEqual(len(cells), 32)
        firsts = cells[0::2]
        for ty in runner.TYPES:
            ref_first = [c for c in firsts if c["task_id"].startswith(ty) and c["condition"] == "reference"]
            self.assertEqual(len(ref_first), 2, ty)
        for a, b in zip(cells[0::2], cells[1::2]):
            self.assertEqual(a["task_id"], b["task_id"])
            self.assertNotEqual(a["condition"], b["condition"])
        self.assertEqual(o, runner.execution_order(runner.DEFAULT_ORDER_SEED), "reproducible from seed")

    def test_review_sheet_hides_condition_destination_score(self):
        sheet, key = runner.review_sheet(self.path, seed=1)
        with sheet.open() as f:
            rows = list(csv.reader(f))
        header, body = rows[0], rows[1:]
        self.assertEqual(len(body), 32)
        self.assertEqual(header, runner.REVIEW_HEADER)
        self.assertTrue(header[4].startswith("human_correct") and header[5].startswith("human_compliant"))
        blob = (" ".join(header) + " ".join(" ".join(r) for r in body)).lower()
        for leak in ("reference", "fixed_wrong", "fixed-wrong", "condition", "destination", "pass\"", "sandbox_unavailable",
                     "math expert", "coding expert", "information extractor", "qa expert", "score", "latency", "token"):
            self.assertNotIn(leak, blob)
        self.assertEqual(key.parent.name, "private")
        mapping = json.loads(key.read_text())["mapping"]
        self.assertEqual(set(mapping), {r[0] for r in body})

    def test_report_marks_dry_run(self):
        rep = runner.report(self.path).read_text()
        self.assertIn("DRY RUN — NOT RESULTS", rep)
        self.assertNotIn("cost", rep.lower())


if __name__ == "__main__":
    unittest.main()
