"""Stage 3 tests (stdlib unittest). The model boundary is mocked: runner._ollama_chat, runner._ollama_tags
and stage3.ollama_ps are replaced, so no request reaches Ollama. The sandbox backend is a fake here;
the real-container path is exercised by `python3 -m farex_pilot.stage3 --dry-run`.

Run from the pilot root:  python3 -I -m unittest discover -s tests -v
"""

import argparse
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from farex_pilot import runner, sandbox, scoring, scoring_v2, stage3, stage3_report as rep  # noqa: E402
from test_stage2_entry import FakeBackend  # noqa: E402

DIGEST = runner.SELECTED_MODEL["manifest_sha256"]
TASKS = {t["id"]: t for t in stage3.load_tasks()}
BY_PROMPT = {t["prompt"]: t for t in TASKS.values()}
ROUTER_SYSTEM = stage3.ROUTER_PROMPT.read_text()
CODE_REPLY = '```\nif "opened to traffic in 1937" in text:\n    print("yes")\nelse:\n    print("no")\n```'


class Env:
    """Temp artifacts and freeze with a mocked model. `router` maps a task id to raw router output;
    `script` maps a 0-based chat-call index to an exception or raw bytes."""

    def __init__(self, backend_factory=None, tags=None, router=None, script=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = Path(self.tmp.name) / "artifacts"
        self.freeze_path = Path(self.tmp.name) / "freeze.json"
        self.events, self.calls, self.sleeps = [], [], []
        self.router, self.script = router or {}, script or {}
        self.tags = tags if tags is not None else {"models": [{"name": "llama3:8b", "model": "llama3:8b", "digest": DIGEST}]}
        self.backend_factory = backend_factory or (lambda ev: FakeBackend(ev))
        self.backend = None

    def chat(self, base, model, system, user, options):
        self.events.append("chat")
        i = len(self.calls)
        self.calls.append({"system": system, "user": user})
        b = self.script.get(i)
        if isinstance(b, BaseException):
            raise b
        if isinstance(b, bytes):
            return b, 12.0
        if user == stage3.WARMUP_MESSAGE:
            content = "OK"
        elif system == ROUTER_SYSTEM:
            t = BY_PROMPT[user]
            content = self.router.get(t["id"], t["type"])
        else:
            content = runner.synthetic_response(BY_PROMPT[user])
        body = {"message": {"content": content}, "prompt_eval_count": 20, "eval_count": 5 + i % 7, "total_duration": 1}
        return json.dumps(body).encode(), 10.0 + i

    def ollama_tags(self, base):
        self.events.append("tags")
        if isinstance(self.tags, BaseException):
            raise self.tags
        return self.tags

    def default_backend(self):
        self.backend = self.backend_factory(self.events)
        return self.backend

    def __enter__(self):
        self.patches = [mock.patch.object(stage3, "ART", self.art), mock.patch.object(stage3, "FREEZE", self.freeze_path),
                        mock.patch.object(runner, "_ollama_chat", self.chat), mock.patch.object(runner, "_ollama_tags", self.ollama_tags),
                        mock.patch.object(stage3, "ollama_ps", lambda base: {"loaded_models": ["llama3:8b"]}),
                        mock.patch.object(sandbox, "default_backend", self.default_backend),
                        mock.patch.object(runner, "_sleep", lambda s: self.sleeps.append(s)),
                        mock.patch.dict(os.environ, {}, clear=False)]
        for p in self.patches:
            p.start()
        os.environ.pop("FAREX_SANDBOX_IMAGE", None)
        os.environ.pop("FAREX_OLLAMA_URL", None)
        stage3.prepare()
        self.write_freeze()
        return self

    def write_freeze(self):
        rec = stage3.freeze_record({"status": "verified", "assertions_passed": 1, "assertions_total": 1,
                                    "image": sandbox.PINNED_IMAGE, "recorded": "test"})
        self.freeze_path.write_text(json.dumps(rec))

    def __exit__(self, *a):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run(self, **kw):
        args = argparse.Namespace(owner_approved=True, stage=3, model="llama3:8b", model_digest=DIGEST)
        for k, v in kw.items():
            setattr(args, k, v)
        with contextlib.redirect_stdout(io.StringIO()) as self.printed:
            return stage3.run(args)

    def rows(self):
        return [json.loads(l) for f in (self.art / "runs").glob("*.jsonl") for l in f.read_text().splitlines()]

    def manifest(self):
        return json.loads(next((self.art / "runs").glob("*.manifest.json")).read_text())


def fill_sheet(sheet, mark="1"):
    with sheet.open(newline="") as f:
        rows = list(csv.reader(f))
    for r in rows[1:]:
        r[4], r[5] = mark, mark
    with sheet.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


class TestV1Untouched(unittest.TestCase):
    def test_all_v1_frozen_hashes_still_match(self):
        fz = json.loads(runner.FREEZE.read_text())
        self.assertEqual(fz["hashes"], runner.frozen_hashes())

    def test_v1_order_and_scorer_version_unchanged(self):
        self.assertEqual(scoring.SCORER_VERSION, "generic_v1-scoring-1.2.0")
        self.assertNotEqual(scoring_v2.SCORER_VERSION, scoring.SCORER_VERSION)


class TestScoringV2(unittest.TestCase):
    def test_evidence_answered_with_code_is_a_scored_fail(self):
        for tid in ("evidence_qa_1", "evidence_qa_2", "evidence_qa_3", "evidence_qa_4"):
            s = scoring_v2.score(TASKS[tid], CODE_REPLY)
            self.assertEqual((s["scored"], s["pass"], s["score_status"], s["reason"]), (True, 0, "scored", "verdict_missing"))
            self.assertEqual(s["components"]["contract_violation"], "code")
            self.assertEqual((s["components"]["correct"], s["components"]["compliant"]), (0, 0))

    def test_stage2_disagreement_code_that_echoes_the_span_is_not_compliant(self):
        """evidence_qa_1 / fixed_wrong: scorer compliant 1, reviewer 0. Text inside code is not a citation."""
        t = TASKS["evidence_qa_1"]
        code = f"```\nif text == '{t['passage']}':\n    print('no')\nelse:\n    print('yes')\n```"
        self.assertEqual(scoring.score(t, code)["components"]["compliant"], 1)         # the v1 behaviour
        self.assertEqual(scoring_v2.score(t, code)["components"]["compliant"], 0)

    def test_stage2_disagreement_other_phrase_stays_strict(self):
        """evidence_qa_2 / reference: the reviewer accepted a phrase outside the key. D5 strict keeps compliant 0."""
        t = TASKS["evidence_qa_2"]
        s = scoring_v2.score(t, f"{t['verdict'].capitalize()}. \"some other phrase from nowhere\"")
        self.assertEqual((s["pass"], s["reason"], s["components"]["compliant"]), (0, "no_supporting_quote", 0))

    def test_passing_evidence_reply_with_code_goes_to_review(self):
        t = TASKS["evidence_qa_h1"]
        for reply in ("```python\n# no e-reader for under 16\n```", "No.\n```\nunder 16\n```",
                      'No. The passage says under 16.\n```python\nprint("no")\n```'):
            with self.subTest(reply=reply):
                r = scoring_v2.score(t, reply)
                self.assertEqual((r["scored"], r["pass"], r["score_status"], r["reason"]), (False, None, "review_needed", "answer_inside_code"))

    def test_evidence_reply_without_code_is_unaffected(self):
        r = scoring_v2.score(TASKS["evidence_qa_h1"], "No. The passage says under 16.")
        self.assertEqual((r["pass"], r["reason"]), (1, "ok"))

    def test_the_review_rule_is_only_for_evidence_qa(self):
        t = TASKS["math_1"]
        self.assertEqual(scoring_v2.score(t, "391\n```\nprint(391)\n```")["pass"], scoring.score(t, "391\n```\nprint(391)\n```")["pass"])

    def test_prose_without_verdict_stays_review_needed(self):
        s = scoring_v2.score(TASKS["evidence_qa_1"], "It is hard to say from this.")
        self.assertEqual((s["score_status"], s["pass"]), ("review_needed", None))
        self.assertIsNone(s["components"]["contract_violation"])

    def test_tag_never_changes_a_pass(self):
        t = TASKS["math_1"]
        reply = f"```\nprint({t['expected']})\n```\nAnswer: {t['expected']}"
        self.assertEqual(scoring_v2.score(t, reply)["pass"], scoring.score(t, reply)["pass"])
        self.assertEqual(scoring_v2.score(t, reply)["pass"], 1)
        self.assertIsNone(scoring_v2.score(t, reply)["components"]["contract_violation"])

    def test_code_reply_on_math_and_extraction_tagged_without_changing_outcome(self):
        for tid in ("math_1", "extraction_1"):
            t, reply = TASKS[tid], "def solve():\n    return 0\n"
            a, b = scoring.score(t, reply), scoring_v2.score(t, reply)
            self.assertEqual((a["scored"], a["pass"], a["reason"]), (b["scored"], b["pass"], b["reason"]))
            self.assertEqual(b["components"]["contract_violation"], "code")

    def test_coding_no_code(self):
        t = TASKS["coding_1"]
        self.assertEqual(scoring_v2.score(t, "42")["components"]["contract_violation"], "no_code")
        self.assertIsNone(scoring_v2.score(t, "x" * 200)["components"]["contract_violation"])
        self.assertIsNone(scoring_v2.score(t, "def other(x):\n    return x\n")["components"]["contract_violation"])

    def test_oversized_response_never_parsed(self):
        s = scoring_v2.score(TASKS["math_1"], "1" * (scoring.MAX_RESPONSE_CHARS + 1))
        self.assertEqual(s["score_status"], "response_oversized")
        self.assertIsNone(s["components"]["contract_violation"])


class TestEvidenceSpans(unittest.TestCase):
    """Generic_v2 spans (PLAN.md "Pre-freeze check"): short correct quotes pass, irrelevant or wrong ones don't."""

    def setUp(self):
        self.tasks = {t["id"]: t for t in stage3.load_tasks()}

    def outcome(self, task_id, verdict, quote):
        return scoring_v2.score(self.tasks[task_id], f'{verdict}\n"{quote}"')

    def test_short_and_full_quotes_pass(self):
        for task_id, verdict, quote in [
            ("evidence_qa_h1", "no", "under 16"),
            ("evidence_qa_h1", "no", "except those under 16"),
            ("evidence_qa_h2", "yes", "1,240 metres"),
            ("evidence_qa_h2", "yes", "The Arden Bridge spans 1,240 metres across the river"),
            ("evidence_qa_h3", "no", "after 25 minutes"),
            ("evidence_qa_h3", "no", "another 30 minutes"),
            ("evidence_qa_h4", "yes", "pets may stay"),
            ("evidence_qa_h4", "yes", "The garden rooms are the only rooms where pets may stay"),
            ("evidence_qa_h5", "no", "3.6 million litres"),
            ("evidence_qa_h6", "yes", "four places ahead"),
            ("evidence_qa_h6", "yes", "Dana finished four places ahead of Omar's teammate Lee, who came fifth"),
        ]:
            with self.subTest(task=task_id, quote=quote):
                self.assertEqual(self.outcome(task_id, verdict, quote)["pass"], 1)

    def test_irrelevant_quotes_fail(self):
        for task_id, verdict, quote in [
            ("evidence_qa_h1", "no", "who can borrow audiobooks instead"),
            ("evidence_qa_h2", "yes", "It was repainted in 2019"),
            ("evidence_qa_h3", "no", "The 8:15 train from Carrow"),
            ("evidence_qa_h4", "yes", "Pets are not allowed"),
            ("evidence_qa_h5", "no", "down from 5.1 million in June"),
            ("evidence_qa_h6", "yes", "Omar finished the race in third place"),
        ]:
            with self.subTest(task=task_id, quote=quote):
                result = self.outcome(task_id, verdict, quote)
                self.assertEqual(result["pass"], 0)
                self.assertEqual(result["reason"], "no_supporting_quote")

    def test_wrong_verdict_fails_with_a_valid_quote(self):
        self.assertEqual(self.outcome("evidence_qa_h2", "no", "1,240 metres")["pass"], 0)


class TestRouterParser(unittest.TestCase):
    def test_accepted(self):
        for raw in ("math", " MATH\n", "math.", "'math'", '"math"', "`math`", '"math".', '"math."', "Evidence_QA"):
            self.assertIn(stage3.parse_router(raw), stage3.TYPES, raw)

    def test_rejected(self):
        for raw in ("", "math coding", "The label is math", "math..", "''math''", "evidence qa", "evidence-qa", "**math**", None, 3):
            self.assertIsNone(stage3.parse_router(raw), raw)


class TestSuiteAndOrder(unittest.TestCase):
    def test_validation_passes(self):
        bad = [c for c in stage3.validate() if not c[1]]
        self.assertEqual(bad, [])

    def test_e4_does_not_count_toward_difficulty(self):
        self.assertFalse(stage3.difficulty_ok({"E3", "E4"}, "extraction"))
        self.assertTrue(stage3.difficulty_ok({"E1", "E3", "E4"}, "extraction"))
        self.assertFalse(stage3.difficulty_ok({"M1", "E1"}, "math"))

    def test_d5_matching_rule(self):
        t = {"type": "evidence_qa", "verdict": "no", "support_spans": ["closed on public holidays and Sundays"]}
        cases = {'No. "CLOSED  on public holidays, and Sundays"': 1, 'No. “closed on public holidays and Sundays”': 1,
                 'No. "closed on public holidays and Sundays..."': 1, 'No. "it is closed on public holidays and Sundays too"': 1,
                 'No. "closed on ... Sundays"': 0, 'No. "closed on public holidays"': 0}
        for reply, want in cases.items():
            self.assertEqual(scoring_v2.score(t, reply)["components"]["compliant"], want, reply)

    def test_safe_arith(self):
        self.assertEqual(stage3.safe_arith("ceil(145 / 12) + floor(2.5)"), 15)
        for expr in ("__import__('os')", "round(1.5)", "ceil(1, 2)", "2 ** 8"):
            with self.assertRaises(ValueError):
                stage3.safe_arith(expr)

    def test_order_shape_and_determinism(self):
        o = stage3.execution_order(stage3.DEFAULT_ORDER_SEED)
        cells = o["cells"]
        self.assertEqual(o["cells"], stage3.execution_order(stage3.DEFAULT_ORDER_SEED)["cells"])
        self.assertNotEqual(cells, stage3.execution_order(7)["cells"])
        self.assertEqual(len(cells), 200)
        self.assertEqual({c["condition"] for c in cells[:160]} - {"reference"}, {f"cross:{d}" for d in stage3.TYPES})
        self.assertTrue(all(c["condition"] == "router" and c["selected_destination"] is None for c in cells[160:]))
        self.assertEqual(Counter(c["task_id"] for c in cells), {t: 5 for t in TASKS})
        for i in range(0, 160, 4):                  # a task's cross cells are consecutive and cover all 4 destinations
            block = cells[i:i + 4]
            self.assertEqual(len({c["task_id"] for c in block}), 1)
            self.assertEqual(sorted(c["selected_destination"] for c in block), sorted(stage3.TYPES))

    def test_positions_balanced_overall_and_within_type(self):
        cells = stage3.execution_order(stage3.DEFAULT_ORDER_SEED)["cells"][:160]
        overall = Counter((i % 4, c["selected_destination"]) for i, c in enumerate(cells))
        self.assertEqual(set(overall.values()), {10})
        for ty in stage3.TYPES:
            within = Counter((i % 4, c["selected_destination"]) for i, c in enumerate(cells) if TASKS[c["task_id"]]["type"] == ty)
            self.assertLessEqual(set(within.values()), {2, 3}, ty)

    def test_neighbouring_pairs_balanced(self):
        cells = stage3.execution_order(stage3.DEFAULT_ORDER_SEED)["cells"][:160]
        pairs = Counter((cells[i]["selected_destination"], cells[i + 1]["selected_destination"]) for i in range(160) if i % 4 != 3)
        self.assertEqual(set(pairs.values()), {10})


class TestRunSuccess(unittest.TestCase):
    def test_full_mocked_run(self):
        with Env() as env:
            path = env.run()
            rows = env.rows()
            self.assertEqual(env.events[:2], ["tags", "verify"])
            self.assertLess(env.events.index("verify"), env.events.index("chat"))
            self.assertEqual(len(env.calls), stage3.EXPECTED_CHAT_CALLS)
            self.assertEqual(len(rows), 241)
            self.assertEqual([stage3.validate_row(r) for r in rows if stage3.validate_row(r)], [])
            self.assertEqual(len({r["row_id"] for r in rows}), 241)
            self.assertEqual(Counter(r["row_type"] for r in rows), {"warmup": 1, "worker": 200, "router": 40})
            self.assertEqual(Counter(r["condition"] for r in rows)["router"], 80)
            self.assertTrue(all(r["pass"] == 1 for r in rows if r["row_type"] == "worker"))
            self.assertFalse(any(r["scored"] for r in rows if r["row_type"] != "worker"))
            self.assertEqual(env.backend.runs.__len__(), 50)         # 10 coding tasks x 5 conditions
            m = env.manifest()
            self.assertEqual((m["aborted"], m["abort"], m["model_digest_observed_local"]), (False, None, DIGEST))
            text = rep.report(path, unblind=True).read_text()
            self.assertIn("Worker cells: 200/200", text)
            self.assertIn("Router accuracy against author labels: 40/40", text)
            self.assertNotIn("condition", env.printed.getvalue())
            self.assertNotIn("cross:", env.printed.getvalue())

    def test_router_call_precedes_its_worker_and_is_recorded(self):
        with Env(router={"math_1": "coding"}) as env:
            env.run()
            rows = env.rows()
            r = next(x for x in rows if x["row_type"] == "router" and x["task_id"] == "math_1")
            w = next(x for x in rows if x["row_type"] == "worker" and x["task_id"] == "math_1" and x["condition"] == "router")
            self.assertEqual((r["selected_destination"], r["label_agreement"], r["router_outcome"]), ("coding", False, "parsed"))
            self.assertEqual((w["selected_destination"], w["label_agreement"]), ("coding", False))
            self.assertEqual((w["router"]["type"], w["router"]["raw_output"]), ("llm", "coding"))
            self.assertEqual(w["system_prompt_file"], "prompts/generic_v1/coding.txt")
            self.assertEqual(r["system_prompt_file"], "prompts/generic_v2/router.txt")
            self.assertLess(rows.index(r), rows.index(w))

    def test_unparseable_router_gets_no_worker_call(self):
        with Env(router={"math_1": "I think math"}) as env:
            path = env.run()
            rows = env.rows()
            self.assertEqual(len(env.calls), 240)
            r = next(x for x in rows if x["row_type"] == "router" and x["task_id"] == "math_1")
            self.assertEqual((r["router_outcome"], r["selected_destination"], r["response"]), ("router_unparseable", None, "I think math"))
            self.assertFalse([x for x in rows if x["row_type"] == "worker" and x["task_id"] == "math_1" and x["condition"] == "router"])
            text = rep.report(path, unblind=True).read_text()
            self.assertIn("Unparseable: 1", text)
            self.assertIn("Missing cells: 0", text)
            self.assertIn("39/39 scored; 39/40 with unscored and unrouted cells as fails", text)

    def test_misroute_cost_table(self):
        with Env(router={"math_1": "coding"}) as env:
            text = rep.report(env.run(), unblind=True).read_text()
        self.assertIn("| math_1 | coding |", text)


class TestStopOnTimeout(unittest.TestCase):
    def stopped(self, index):
        with Env(script={index: urllib.error.URLError(TimeoutError())}) as env:
            env.run()
            self.assertEqual(len(env.calls), index + 1, "a call was made after the timeout")
            m = env.manifest()
            self.assertTrue(m["aborted"])
            self.assertEqual(m["abort"]["reason"], "timeout")
            self.assertEqual(m["abort"]["ollama_ps"], {"loaded_models": ["llama3:8b"]})
            rows = env.rows()
            self.assertEqual(rows[-1]["status"], "timeout")
            self.assertEqual(rows[-1]["row_id"], m["abort"]["row_id"])
            self.assertEqual([stage3.validate_row(r) for r in rows if stage3.validate_row(r)], [])
            return rows, env

    def test_on_warmup(self):
        rows, _ = self.stopped(0)
        self.assertEqual(rows[-1]["row_type"], "warmup")

    def test_on_worker(self):
        rows, _ = self.stopped(5)
        self.assertEqual(rows[-1]["row_type"], "worker")

    def test_on_router(self):
        rows, _ = self.stopped(161)
        self.assertEqual((rows[-1]["row_type"], rows[-1]["router_outcome"]), ("router", "router_call_failed"))

    def test_on_worker_after_router(self):
        rows, _ = self.stopped(162)
        self.assertEqual((rows[-1]["row_type"], rows[-1]["condition"]), ("worker", "router"))

    def test_manifest_written_when_the_run_crashes(self):
        with Env() as env:
            with mock.patch.object(scoring_v2, "score", side_effect=ValueError("boom")):
                with self.assertRaises(ValueError):
                    env.run()
            self.assertTrue(env.manifest()["aborted"])
            self.assertEqual(env.manifest()["abort"]["reason"], "ValueError")

    def test_refused_connection_retries_once_and_continues(self):
        with Env(script={1: urllib.error.URLError(ConnectionRefusedError())}) as env:
            env.run()
            self.assertEqual(env.sleeps, [runner.RETRY_DELAY_S])
            self.assertEqual(len(env.calls), stage3.EXPECTED_CHAT_CALLS + 1)
            self.assertFalse(env.manifest()["aborted"])

    def test_failed_router_call_gets_no_worker_call(self):
        with Env(script={161: b"not json"}) as env:
            env.run()
            self.assertEqual(len(env.calls), stage3.EXPECTED_CHAT_CALLS - 1)
            self.assertEqual(Counter(r["router_outcome"] for r in env.rows() if r["row_type"] == "router")["router_call_failed"], 1)


class TestFailsClosed(unittest.TestCase):
    def assertBlocked(self, env, msg, **kw):
        with self.assertRaises(SystemExit) as cm:
            env.run(**kw)
        self.assertIn(msg, str(cm.exception))
        self.assertEqual(env.calls, [], "a model call was made")
        self.assertFalse((env.art / "runs").exists() and list((env.art / "runs").iterdir()))

    def test_approval_and_stage(self):
        with Env() as env:
            self.assertBlocked(env, "--owner-approved", owner_approved=False)
            self.assertBlocked(env, "--stage 3", stage=2)

    def test_not_frozen(self):
        with Env() as env:
            env.freeze_path.unlink()
            self.assertBlocked(env, "not frozen")

    def test_changed_file(self):
        with Env() as env:
            rec = json.loads(env.freeze_path.read_text())
            rec["hashes"]["prompts/generic_v2/router.txt"] = "0" * 64
            env.freeze_path.write_text(json.dumps(rec))
            self.assertBlocked(env, "files changed since freeze: ['prompts/generic_v2/router.txt']")

    def test_freeze_covers_router_prompt_spec_and_suite(self):
        for f in ("prompts/generic_v2/router.txt", "stage3/SPEC.md", "tasks/generic_v2.json", "farex_pilot/scoring_v2.py"):
            self.assertIn(f, stage3.frozen_hashes())

    def test_config_changed(self):
        with Env() as env:
            rec = json.loads(env.freeze_path.read_text())
            rec["execution"]["options"]["temperature"] = 0.7
            env.freeze_path.write_text(json.dumps(rec))
            self.assertBlocked(env, "execution configuration differs")

    def test_order_file(self):
        with Env() as env:
            of = stage3.order_file()
            of.write_text(of.read_text() + " ")
            self.assertBlocked(env, "order file missing or its hash differs")
            of.write_text(json.dumps(stage3.execution_order(7)))
            env.write_freeze()
            self.assertBlocked(env, "not the order the frozen seed produces")

    def test_model_identity(self):
        with Env() as env:
            self.assertBlocked(env, "is not the frozen model", model="mistral:7b")
            self.assertBlocked(env, "full frozen manifest digest", model_digest=DIGEST[:12])
        with Env(tags={"models": [{"name": "llama3:8b", "digest": "a" * 64}]}) as env:
            self.assertBlocked(env, "local Ollama model digest differs")
        with Env(tags=urllib.error.URLError(ConnectionRefusedError())) as env:
            self.assertBlocked(env, "cannot read local Ollama model list")

    def test_sandbox(self):
        with Env(backend_factory=lambda ev: FakeBackend(ev, verify_ok=False)) as env:
            self.assertBlocked(env, "sandbox verification failed")
        with Env(backend_factory=lambda ev: FakeBackend(ev, image="python@sha256:" + "1" * 64)) as env:
            self.assertBlocked(env, "backend image differs")
        with Env() as env:
            os.environ["FAREX_SANDBOX_IMAGE"] = "python@sha256:" + "2" * 64
            self.assertBlocked(env, "FAREX_SANDBOX_IMAGE overrides")

    def test_non_local_ollama(self):
        with Env() as env:
            os.environ["FAREX_OLLAMA_URL"] = "http://10.0.0.5:11434"
            self.assertBlocked(env, "local-Ollama only")

    def test_freeze_needs_approval(self):
        with self.assertRaises(SystemExit) as cm:
            stage3.freeze(argparse.Namespace(owner_approved=False))
        self.assertIn("--owner-approved", str(cm.exception))

    def test_report_is_blocked_until_unblind(self):
        with Env() as env:
            path = env.run()
            with self.assertRaises(SystemExit):
                rep.report(path)
            text = rep.status(path)
            for word in ("cross:", "reference", "pass", "correct"):
                self.assertNotIn(word, text)
            self.assertIn("worker cells written: 200/200", text)


class TestReview(unittest.TestCase):
    def mixed_run(self, env):
        """A real mocked run, with a third of the cells rewritten as scorer fails."""
        path = env.run()
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        for i, r in enumerate(rows):
            if r["row_type"] == "worker" and i % 3 == 0:
                r.update({"pass": 0, "reason": "wrong_final_number"})
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return path, rows

    def test_selection_rule(self):
        with Env() as env:
            path, rows = self.mixed_run(env)
            sheet, key = rep.review_sheet(path, seed=5)
            k = json.loads(key.read_text())
            sel = Counter(m["selection"] for m in k["mapping"].values())
            workers = [r for r in rows if r["row_type"] == "worker"]
            self.assertEqual(sel["flagged"], sum(r["pass"] == 0 for r in workers))
            strata = Counter((r["task_type"], r["condition"]) for r in workers if r["pass"] == 1)
            self.assertEqual(sel["sampled"], sum(-(-n // 4) for n in strata.values()))
            picked = Counter((r["task_type"], r["condition"]) for r in workers for m in k["mapping"].values()
                             if m["row_id"] == r["row_id"] and m["selection"] == "sampled")
            self.assertEqual(set(picked), set(strata))

    def test_sheet_hides_conditions_and_outcomes(self):
        with Env() as env:
            path, _ = self.mixed_run(env)
            sheet, _ = rep.review_sheet(path, seed=5)
            text = sheet.read_text()
            for word in ("cross:", "flagged", "sampled", "wrong_final_number"):
                self.assertNotIn(word, text)

    def test_seed_reproduces_the_sample(self):
        with Env() as env:
            path, _ = self.mixed_run(env)
            def draw():
                sheet, key = rep.review_sheet(path, seed=5)
                ids = {m["row_id"] for m in json.loads(key.read_text())["mapping"].values()}
                sheet.unlink()
                key.unlink()
                return ids
            self.assertEqual(draw(), draw())

    def test_second_reviewer_same_cells_own_ids_and_order(self):
        with Env() as env:
            path, _ = self.mixed_run(env)
            s1, k1 = rep.review_sheet(path, seed=5)
            s2, k2 = rep.review_sheet(path, seed=6, second=True)
            m1, m2 = json.loads(k1.read_text())["mapping"], json.loads(k2.read_text())["mapping"]
            self.assertEqual({v["row_id"] for v in m1.values()}, {v["row_id"] for v in m2.values()})
            self.assertFalse(set(m1) & set(m2))
            self.assertNotEqual(s1, s2)

    def test_second_reviewer_needs_first(self):
        with Env() as env:
            with self.assertRaises(SystemExit):
                rep.review_sheet(env.run(), seed=5, second=True)

    def test_join(self):
        with Env() as env:
            path, _ = self.mixed_run(env)
            s1, _ = rep.review_sheet(path, seed=5)
            s2, _ = rep.review_sheet(path, seed=6, second=True)
            fill_sheet(s1)
            fill_sheet(s2)
            text = rep.join_review(path, s1, s2).read_text()
            self.assertIn("Scorer validity rests on this sample", text)
            self.assertIn("## D. Two reviewers", text)
            self.assertIn("Cohen's kappa: undefined", text)

    def test_join_reports_false_positives_next_to_the_matrix(self):
        with Env() as env:
            path, rows = self.mixed_run(env)
            s1, k1 = rep.review_sheet(path, seed=5)
            fill_sheet(s1, "0")
            sampled = sum(m["selection"] == "sampled" for m in json.loads(k1.read_text())["mapping"].values())
            self.assertIn(f"False positives in the sample: {sampled}/{sampled}", rep.join_review(path, s1).read_text())

    def test_join_refuses_bad_sheets(self):
        with Env() as env:
            path, _ = self.mixed_run(env)
            s1, _ = rep.review_sheet(path, seed=5)
            fill_sheet(s1, "2")
            with self.assertRaises(SystemExit):
                rep.join_review(path, s1)
            lines = s1.read_text().splitlines(True)
            s1.write_text("".join(lines[:-1]))
            with self.assertRaises(SystemExit):
                rep.join_review(path, s1)


class TestStatistics(unittest.TestCase):
    def test_sign_test(self):
        self.assertAlmostEqual(rep.sign_test([1] * 10)["p"], 2 / 1024)
        self.assertEqual(rep.sign_test([1, -1])["p"], 1.0)
        s = rep.sign_test([1, 1, 1, 1, 1, -1, 0, 0])
        self.assertEqual((s["higher"], s["lower"], s["ties"]), (5, 1, 2))
        self.assertAlmostEqual(s["p"], 14 / 64)
        self.assertIsNone(rep.sign_test([0, 0])["p"])

    def test_kappa(self):
        self.assertEqual(rep.kappa([(1, 1), (0, 0)]), 1.0)
        self.assertIsNone(rep.kappa([(1, 1)]))
        self.assertIsNone(rep.kappa([]))
        self.assertAlmostEqual(rep.kappa([(1, 1), (1, 0), (0, 1), (0, 0)]), 0.0)

    def test_per_task_diffs_needs_all_cells(self):
        t = TASKS["math_1"]
        final = {("math_1", "reference"): {"latency_ms": 10}}
        wrong = {"coding": 30, "extraction": 40, "evidence_qa": 90}
        self.assertEqual(rep.per_task_diffs(final, [t], "latency_ms"), [])
        final.update({("math_1", f"cross:{d}"): {"latency_ms": v} for d, v in wrong.items()})
        self.assertEqual(rep.per_task_diffs(final, [t], "latency_ms"), [30])


class TestDryRun(unittest.TestCase):
    def test_dry_run_labels(self):
        with Env() as env:
            with contextlib.redirect_stdout(io.StringIO()):
                path = stage3.dry_run(verify=True)
            rows = [json.loads(l) for l in path.read_text().splitlines()]
            self.assertEqual(len(rows), 241)
            self.assertEqual(env.calls, [])
            self.assertTrue(path.name.startswith("generic_v2.DRYRUN-") and path.parent.name == "dry_run")
            self.assertTrue(all(r["dry_run"] and r["response_source"].startswith("synthetic") for r in rows))
            self.assertIn("DRY RUN", rep.report(path).read_text())


class TestPreFreezeAudit(unittest.TestCase):
    """Defects found by the independent audit (PLAN.md "Independent audit"). Each test fails on the old code."""

    def setUp(self):
        self.tasks = {t["id"]: t for t in stage3.load_tasks()}

    def test_human_pass_marks(self):
        for c, k, want in [(1, 1, 1), (1, 0, 0), (0, 1, 0), (0, None, 0), (None, 0, 0), (1, None, None), (None, 1, None), (None, None, None)]:
            with self.subTest(correct=c, compliant=k):
                self.assertEqual(rep.human_pass({"correct": c, "compliant": k}), want)

    def test_adjudicated(self):
        scorer_pass, scorer_fail = {"scored": True, "pass": 1}, {"scored": True, "pass": 0}
        unscored = {"scored": False, "pass": None}
        blank = {"correct": None, "compliant": None}
        human_yes, human_no = {"correct": 1, "compliant": 1}, {"correct": 0, "compliant": None}
        for r, h, want in [(scorer_pass, None, 1), (scorer_fail, None, 0), (unscored, None, 0), (None, None, 0), (scorer_pass, blank, 0),
                           (scorer_fail, blank, 0), (scorer_pass, human_no, 0), (scorer_fail, human_yes, 1)]:
            with self.subTest(r=r, h=h):
                self.assertEqual(rep.adjudicated(r, h), want)

    def test_partial_human_fail_is_a_false_positive(self):
        final = {("a", "reference"): {"scored": True, "pass": 1}}
        self.assertEqual(rep.false_positives(final, {("a", "reference"): {"correct": 0, "compliant": None}}), (1, 1))

    def test_coding_h5_nesting_test_catches_a_counter(self):
        t = self.tasks["coding_h5"]
        counter = ("def is_balanced(s):\n    return all(s.count(a) == s.count(b) for a, b in ('()', '[]', '{}'))\n")
        self.assertLess(runner.run_trusted_reference(counter, t["function_name"], t["tests"]), len(t["tests"]))

    def test_extraction_h3_spellings(self):
        t = self.tasks["extraction_h3"]
        for answer, want in [("17th of April", 1), ("April 17th", 1), ("April 17 and May 2nd", 0), ("April 17 and April 3rd", 0), ("May 2nd", 0)]:
            with self.subTest(answer=answer):
                self.assertEqual(scoring_v2.score(t, answer)["pass"], want)

    def test_tag_skips_an_accepted_answer_in_code(self):
        r = scoring_v2.score(self.tasks["extraction_h1"], "```\n220 Harbor Road, not 14 Elm Street\n```")
        self.assertIsNone(r["components"]["contract_violation"])

    def test_draft_tasks_are_frozen(self):
        self.assertIn("stage3/tasks_new_draft.json", stage3.frozen_hashes())

    def test_router_matrix_counts_unrouted_cell_as_fail(self):
        with Env(router={"math_1": "I think math"}) as env:
            path = env.run()
            sheet, _ = rep.review_sheet(path, seed=1)
            fill_sheet(sheet)
            text = rep.join_review(path, sheet).read_text()
        math_row = next(l for l in text[text.index("## C."):].splitlines() if l.startswith("| math |"))
        self.assertEqual(math_row.split("|")[-2].strip(), "9/10")

    def test_review_sheet_refuses_to_overwrite(self):
        with Env() as env:
            path = env.run()
            rep.review_sheet(path, seed=1)
            with self.assertRaises(SystemExit) as cm:
                rep.review_sheet(path, seed=2)
            self.assertIn("already exists", str(cm.exception))

    def test_warmup_abort_still_reports(self):
        with Env(script={0: TimeoutError("timed out")}) as env:
            path = env.run()
            text = rep.status(path)
            self.assertIn("aborted: True", text)
            self.assertIn("worker cells written: 0/200", text)
            self.assertIn("Worker cells: 0/200", rep.report(path, unblind=True).read_text())


class TestFreeze(unittest.TestCase):
    """stage3.freeze with a temp freeze file and sandbox record. Never touches the real ones."""

    def record(self, env, **over):
        cur = {runner.rel(f): runner.sha256_file(f) for f in [ROOT / "farex_pilot" / "sandbox.py", ROOT / "farex_pilot" / "sandbox_probes.py"]}
        rec = dict({"status": "verified", "sandbox_file_hashes": cur, "image": sandbox.PINNED_IMAGE, "assertions_passed": 1,
                    "assertions_total": 1, "recorded": "test"}, **over)
        path = env.art / "verification.json"
        path.write_text(json.dumps(rec))
        return mock.patch.object(runner, "SANDBOX_VERIFY", path)

    def freeze(self):
        return stage3.freeze(argparse.Namespace(owner_approved=True))

    def assertRefused(self, text):
        env_path = stage3.FREEZE
        env_path.unlink(missing_ok=True)
        with self.assertRaises(SystemExit) as cm:
            self.freeze()
        self.assertIn(text, str(cm.exception))
        self.assertFalse(env_path.exists())

    def test_success_records_every_hash(self):
        with Env() as env, self.record(env):
            env.freeze_path.unlink()
            rec = json.loads(self.freeze().read_text())
            self.assertEqual(rec["status"], "frozen")
            self.assertEqual(rec["hashes"], stage3.frozen_hashes())
            self.assertEqual(rec["order_sha256"], runner.sha256_file(stage3.order_file()))

    def test_refuses_failed_validation(self):
        with Env() as env, self.record(env), mock.patch.object(stage3, "validate", lambda: [("x", False, "")]):
            self.assertRefused("validation failed")

    def test_refuses_missing_sandbox_record(self):
        with Env() as env, mock.patch.object(runner, "SANDBOX_VERIFY", env.art / "absent.json"):
            self.assertRefused("no recorded sandbox verification")

    def test_refuses_stale_sandbox_record(self):
        for over in ({"status": "failed"}, {"image": "other"}, {"sandbox_file_hashes": {}}):
            with self.subTest(over=over), Env() as env, self.record(env, **over):
                self.assertRefused("sandbox verification missing")

    def test_refuses_to_replace_an_existing_freeze(self):
        with Env() as env, self.record(env):
            before = env.freeze_path.read_text()
            with self.assertRaises(SystemExit) as cm:
                self.freeze()
            self.assertIn("already exists", str(cm.exception))
            self.assertEqual(env.freeze_path.read_text(), before)

    def test_refuses_order_from_another_seed(self):
        with Env() as env, self.record(env):
            stage3.prepare(stage3.DEFAULT_ORDER_SEED + 1)
            self.assertRefused("order file")


if __name__ == "__main__":
    unittest.main()
