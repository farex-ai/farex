"""Stage 3 pre-freeze hardening tests (stdlib unittest). The model boundary is mocked, as in test_stage3.py.

Run from the pilot root:  python3 -I -m unittest discover -s tests -v
"""

import contextlib
import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from farex_pilot import runner, stage3, stage3_report as rep  # noqa: E402
from test_stage2_entry import FakeBackend  # noqa: E402
from test_stage3 import BY_PROMPT, ROUTER_SYSTEM, Env, fill_sheet  # noqa: E402


class TestFreezeCoverage(unittest.TestCase):
    def test_freeze_covers_every_module_and_test_file(self):
        covered = set(stage3.frozen_hashes())
        on_disk = {runner.rel(p) for p in [*ROOT.glob("farex_pilot/*.py"), *ROOT.glob("tests/test_*.py")]}
        self.assertEqual(on_disk - covered, set())


class TestAbortRecord(unittest.TestCase):
    def test_ollama_ps_survives_a_non_object_body(self):
        class Body:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"[1]"
        with mock.patch("urllib.request.urlopen", return_value=Body()):
            self.assertEqual(stage3.ollama_ps("http://127.0.0.1:1"), {"error": "AttributeError"})

    def test_manifest_says_aborted_when_the_ps_probe_itself_fails(self):
        def bad_ps(base):
            raise RuntimeError("probe failed")
        with Env(script={0: TimeoutError("timed out")}) as env, mock.patch.object(stage3, "ollama_ps", bad_ps):
            env.run()
            m = env.manifest()
            self.assertTrue(m["aborted"])
            self.assertEqual(m["abort"]["reason"], "timeout")
            self.assertEqual(m["abort"]["ollama_ps"], {"error": "RuntimeError"})


class TestLoad(unittest.TestCase):
    def rows(self, run_id="r"):
        return json.dumps({"run_id": run_id, "schema_version": stage3.SCHEMA_VERSION, "row_type": "warmup", "is_warmup": True,
                           "status": "ok", "dry_run": False})

    def test_torn_last_line_is_dropped_and_counted(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.jsonl"
            p.write_text(self.rows() + '\n{"run_id"')
            d = rep.load(p)
            self.assertEqual(len(d["rows"]), 1)
            self.assertEqual(d["torn_lines"], 1)

    def test_bad_line_in_the_middle_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.jsonl"
            p.write_text(self.rows() + '\n{"run_id"\n' + self.rows() + "\n")
            with self.assertRaises(SystemExit):
                rep.load(p)

    def test_status_reports_a_torn_line(self):
        with Env() as env:
            path = env.run()
            with path.open("a") as f:
                f.write('{"run_id"')
            self.assertIn("torn last line: 1", rep.status(path))


    def test_refuses_a_generic_v1_log(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.jsonl"
            p.write_text(json.dumps({"run_id": "r", "schema_version": runner.SCHEMA_VERSION, "is_warmup": True, "status": "ok"}) + "\n")
            with self.assertRaises(SystemExit) as cm:
                rep.load(p)
            self.assertIn("not a generic_v2 log", str(cm.exception))


class TestReadSheet(unittest.TestCase):
    def sheet(self, header, *rows):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        key = d / "k.json"
        key.write_text(json.dumps({"mapping": {"ab": {"task_id": "t", "condition": "c", "selection": "flagged"}}}))
        sheet = d / "s.csv"
        with sheet.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return sheet, key

    def test_good_sheet(self):
        got = rep.read_sheet(*self.sheet(runner.REVIEW_HEADER, ["ab", "p", "c", "r", "1", "0", "note"]))
        self.assertEqual(got[("t", "c")], {"correct": 1, "compliant": 0, "note": "note", "selection": "flagged"})

    def test_short_row_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            rep.read_sheet(*self.sheet(runner.REVIEW_HEADER, ["ab", "p", "c", "r"]))
        self.assertIn("columns", str(cm.exception))


    def test_swapped_mark_columns_refuse(self):
        header = list(runner.REVIEW_HEADER)
        header[4], header[5] = header[5], header[4]
        with self.assertRaises(SystemExit) as cm:
            rep.read_sheet(*self.sheet(header, ["ab", "p", "c", "r", "1", "0", ""]))
        self.assertIn("header", str(cm.exception))

    def test_excel_bom_is_accepted(self):
        sheet, key = self.sheet(runner.REVIEW_HEADER, ["ab", "p", "c", "r", "1", "1", ""])
        sheet.write_bytes(b"\xef\xbb\xbf" + sheet.read_bytes())
        self.assertEqual(rep.read_sheet(sheet, key)[("t", "c")]["correct"], 1)


class TestReviewCells(unittest.TestCase):
    def test_formula_like_text_stays_text(self):
        for raw in ("=17*23", "+1+1", "-1+2", "@SUM(A1)", "\t=1", "\r=1"):
            self.assertEqual(rep.sheet_cell(raw), "'" + raw)

    def test_ordinary_text_is_unchanged(self):
        for raw in ("391", "-5", "-2.5", "Yes. The passage says so.", "def f():\n    return 1", ""):
            self.assertEqual(rep.sheet_cell(raw), raw)

    def test_review_sheet_writes_a_formula_answer_as_text(self):
        with Env() as env:
            with mock.patch.object(runner, "synthetic_response", lambda t: "=HYPERLINK(1)"):
                path = env.run()
            sheet, _ = rep.review_sheet(path, 1)
            with sheet.open(newline="") as f:
                cells = [r[3] for r in list(csv.reader(f))[1:]]
            self.assertTrue(cells)
            self.assertFalse([c for c in cells if c.startswith("=")])


class TestEncoding(unittest.TestCase):
    def test_review_sheet_is_utf8_under_a_non_utf8_locale(self):
        real_open = Path.open

        def ascii_default(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            if "b" not in mode and encoding is None:
                encoding = "ascii"
            return real_open(self, mode, buffering, encoding, errors, newline)
        with Env() as env:
            with mock.patch.object(runner, "synthetic_response", lambda t: "17 × 23 = 391"):
                path = env.run()
            with mock.patch.object(Path, "open", ascii_default):
                sheet, _ = rep.review_sheet(path, 1)
            self.assertIn("×", sheet.read_text(encoding="utf-8"))


class TestMarkdownCells(unittest.TestCase):
    def test_note_cannot_break_a_table(self):
        self.assertEqual(rep.md_cell("a | b\nc"), "a \\| b c")
        self.assertEqual(rep.md_cell(""), "—")

    def test_join_keeps_a_piped_note_on_one_row(self):
        with Env() as env:
            path = env.run()
            sheet, _ = rep.review_sheet(path, 1)
            with sheet.open(newline="") as f:
                grid = list(csv.reader(f))
            for r in grid[1:]:
                r[4], r[5], r[6] = "0", "0", "uses a | b\nand more"
            with sheet.open("w", newline="") as f:
                csv.writer(f).writerows(grid)
            text = rep.join_review(path, sheet).read_text()
            row = next(l for l in text.splitlines() if "uses a" in l)
            self.assertIn("uses a \\| b and more |", row)


class TestReportSections(unittest.TestCase):
    def report(self, **kw):
        with Env(**kw) as env:
            return rep.report(env.run(), unblind=True).read_text()

    def test_limitations_is_the_last_section(self):
        heads = [l for l in self.report().splitlines() if l.startswith("## ")]
        self.assertTrue(heads[-1].endswith("Limitations"))
        self.assertEqual([h.split(".")[0] for h in heads], [f"## {i}" for i in range(1, len(heads) + 1)])

    def test_correct_and_compliant_are_counted_separately(self):
        text = self.report()
        self.assertIn("Correctness vs. compliance", text)
        self.assertIn("| extraction | reference | 10/10 | 10/10 | 0 |", text)


    def test_latency_and_tokens_by_route_with_warmup_apart(self):
        text = self.report()
        self.assertIn("Latency and tokens by route", text)
        self.assertRegex(text, r"\| coding \| reference \| 10 \| \d")
        self.assertRegex(text, r"\| coding \| wrong \| 30 \| \d")
        self.assertRegex(text, r"Warm-up, excluded from everything above: \d+ ms, status ok\.")

    def test_spread(self):
        self.assertEqual(rep.spread([]), "—")
        self.assertEqual(rep.spread([3, 1, 2]), "2 (1–3)")


    def test_attempt_totals_show_final_and_all_with_missing_and_partial(self):
        text = self.report(script={1: ConnectionRefusedError("refused")})
        self.assertIn("Attempts and totals", text)
        self.assertRegex(text, r"\| latency ms \| final attempts \| \d+ \| 0 \|")
        self.assertRegex(text, r"\| latency ms \| all attempts \| \d+ \| 1 \|")     # the refused attempt has no latency
        self.assertRegex(text, r"Attempts per cell: 1: \d+, 2: 1\.")


    def test_policy_rejection_count(self):
        final = {("a", "reference"): {"reason": "sandbox_policy_rejected", "task_type": "coding", "components": {"policy_rejected": True, "policy_reason": "import os"}},
                 ("b", "reference"): {"reason": "tests_failed", "task_type": "coding", "components": {"policy_rejected": False}}}
        self.assertEqual(rep.policy_rejections(final), ["| a | reference | import os |"])
        text = self.report()
        self.assertIn("Sandbox policy rejections", text)
        self.assertIn("None.", text)


    def test_router_and_worker_costs_are_shown_as_parts(self):
        text = self.report()
        self.assertRegex(text, r"Router calls, latency ms: \d+ \(\d+–\d+\); completion tokens: \d+ \(\d+–\d+\)\.")
        self.assertRegex(text, r"Workers on the chosen route, latency ms: \d+ \(\d+–\d+\); completion tokens: \d+ \(\d+–\d+\)\.")


class TestValidatorChecks(unittest.TestCase):
    def names(self):
        return {n: ok for n, ok, _ in stage3.validate()}

    def test_router_prompt_must_match_the_spec_text(self):
        self.assertTrue(self.names()["router prompt equals the text in SPEC section 3"])
        with tempfile.TemporaryDirectory() as t:
            other = Path(t) / "router.txt"
            other.write_text(stage3.ROUTER_PROMPT.read_text().replace("Reply with exactly", "Reply with"))
            with mock.patch.object(stage3, "ROUTER_PROMPT", other):
                self.assertFalse(self.names()["router prompt equals the text in SPEC section 3"])


    def test_new_task_ids_follow_the_spec(self):
        names = self.names()
        for ty in runner.TYPES:
            self.assertTrue(names[f"new {ty} ids are {ty}_h1 to {ty}_h6"])

    def test_every_task_states_a_requirement(self):
        self.assertTrue(self.names()["every task has a requirement"])

    def test_v2_suite_file_matches_a_fresh_build(self):
        self.assertTrue(self.names()["tasks/generic_v2.json is what --build-suite writes"])


class TestReadingCopy(unittest.TestCase):
    """stage3/TASKS_REVIEW.md is what the owner signs off, so it must show what the suite really accepts."""

    def test_every_task_is_shown_with_what_it_accepts(self):
        text = (ROOT / "stage3" / "TASKS_REVIEW.md").read_text()
        for t in json.loads(stage3.DRAFT_TASKS.read_text())["tasks"]:
            shown = [t["id"], t["prompt"], t["requirement"]] + [str(x) for x in t.get("accepted", []) + t.get("distractors", []) + t.get("support_spans", [])]
            self.assertEqual([x for x in shown if x not in text], [], t["id"])

    def test_every_hidden_test_is_shown(self):
        text = (ROOT / "stage3" / "TASKS_REVIEW.md").read_text()
        for t in json.loads(stage3.DRAFT_TASKS.read_text())["tasks"]:
            if t["type"] == "coding":
                for case in t["tests"]:
                    call = f"{t['function_name']}({', '.join(json.dumps(a, ensure_ascii=False) for a in case['args'])})"
                    self.assertIn(call, text, t["id"])


class TestPreflightSandbox(unittest.TestCase):
    def refused(self, verified, status):
        class Half:
            image = runner.sandbox.PINNED_IMAGE
            name, detail = "fake", ""

            def __init__(self):
                self.verified, self.status = False, "unverified"

            def verify_isolation(self):
                self.verified = verified
                return {"status": status, "image": self.image}
        with Env(backend_factory=lambda ev: Half()) as env:
            with self.assertRaises(SystemExit) as cm:
                env.run()
            self.assertIn("sandbox verification failed", str(cm.exception))
            self.assertEqual(env.calls, [])

    def test_backend_flag_alone_is_not_enough(self):
        self.refused(True, "failed")

    def test_record_alone_is_not_enough(self):
        self.refused(False, "verified")


class TestOllamaAnswer(unittest.TestCase):
    def call(self, *replies):
        """replies: bytes bodies or exceptions, one per attempt."""
        seq = list(replies)

        def chat(base, model, system, user, opts):
            r = seq.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r, 7.0
        row = runner.base_row("r", "stage3", None, None, "math", 0)
        row.update(row_type="warmup", router_outcome=None)
        sleeps = []
        with mock.patch.object(runner, "_ollama_chat", chat), mock.patch.object(runner, "_sleep", sleeps.append):
            out = stage3.ollama_answer("http://127.0.0.1:1", "m", dict(runner.STAGE2_OPTIONS))(row, "s", "u")
        return out, sleeps

    def body(self, **kw):
        return json.dumps(dict({"message": {"content": "ok"}}, **kw)).encode()

    def test_provider_tokens_are_kept(self):
        r = self.call(self.body(prompt_eval_count=3, eval_count=4))[0][0]
        self.assertEqual((r["prompt_tokens"], r["completion_tokens"], r["token_source"]), (3, 4, "provider"))

    def test_partial_token_counts_are_unavailable_never_estimated(self):
        for kw in ({"prompt_eval_count": 3}, {"eval_count": 4}, {"prompt_eval_count": "3", "eval_count": 4}, {}):
            with self.subTest(kw=kw):
                r = self.call(self.body(**kw))[0][0]
                self.assertEqual((r["prompt_tokens"], r["completion_tokens"], r["token_source"]), (None, None, "unavailable"))

    def test_oversized_body_is_not_parsed(self):
        out, sleeps = self.call(b"x" * (runner.MAX_RAW_BYTES + 1))
        self.assertEqual([r["status"] for r in out], ["provider_oversized"])
        self.assertIsNone(out[0]["response"])
        self.assertEqual(sleeps, [])

    def test_malformed_bodies_are_not_retried(self):
        for raw in (b"not json", b"[]", b"{}", b'{"message": {"content": 1}}', b'{"message": null}'):
            with self.subTest(raw=raw):
                out, sleeps = self.call(raw)
                self.assertEqual([r["status"] for r in out], ["provider_malformed"])
                self.assertEqual(sleeps, [])

    def test_refused_twice_stops_after_two_attempts(self):
        out, sleeps = self.call(ConnectionRefusedError("a"), ConnectionRefusedError("b"))
        self.assertEqual([(r["attempt"], r["status"]) for r in out], [(1, "transport_error"), (2, "transport_error")])
        self.assertEqual(sleeps, [runner.RETRY_DELAY_S])

    def test_refused_then_answered_keeps_both_attempts(self):
        out, _ = self.call(ConnectionRefusedError("a"), self.body())
        self.assertEqual([r["status"] for r in out], ["transport_error", "ok"])
        self.assertEqual([r["row_id"].rsplit("-", 1)[1] for r in out], ["a1", "a2"])

    def test_timeout_is_not_retried(self):
        out, sleeps = self.call(TimeoutError("slow"))
        self.assertEqual([r["status"] for r in out], ["timeout"])
        self.assertIn("120", out[0]["error"])
        self.assertEqual(sleeps, [])

    def test_http_error_is_not_retried(self):
        import urllib.error
        out, sleeps = self.call(urllib.error.HTTPError("u", 500, "boom", None, None))
        self.assertEqual([r["status"] for r in out], ["http_error"])
        self.assertEqual(sleeps, [])


class TestRouterParserSpec(unittest.TestCase):
    """SPEC section 3, case by case."""

    def test_accepted_forms(self):
        for raw, want in (("math", "math"), ("  Math\n", "math"), ("EVIDENCE_QA", "evidence_qa"), ("coding.", "coding"),
                          ('"coding"', "coding"), ("'math'", "math"), ("`extraction`", "extraction"),
                          ('"math".', "math"), ('"math."', "math")):
            with self.subTest(raw=raw):
                self.assertEqual(stage3.parse_router(raw), want)

    def test_refused_forms(self):
        for raw in ("", " ", '"', "'", ".", "math coding", "the label is math", "math..", '"math\'', "``math``", '""math""',
                    "math\ncoding", "evidence qa", "evidence-qa", "mathematics", None, 7, ["math"]):
            with self.subTest(raw=raw):
                self.assertIsNone(stage3.parse_router(raw))

    def test_single_character_is_not_a_quote_pair(self):
        self.assertIsNone(stage3.parse_router('"'))


class TestValidateRow(unittest.TestCase):
    def worker(self, task="math_1", cond="reference", dest="math"):
        t = {x["id"]: x for x in stage3.load_tasks()}[task]
        row = stage3.base_row("r", "stage3", t, cond, dest, 1)
        row.update(status="ok", response="1", provider="ollama", model="m", model_digest="d", options=dict(runner.STAGE2_OPTIONS))
        return row

    def router(self):
        t = {x["id"]: x for x in stage3.load_tasks()}["math_1"]
        row = stage3.base_row("r", "stage3", t, "router", None, 1, "router")
        row.update(status="ok", response="math", provider="ollama", model="m", model_digest="d", options=dict(runner.STAGE2_OPTIONS))
        return row

    def test_clean_rows(self):
        self.assertEqual(stage3.validate_row(self.worker()), [])
        self.assertEqual(stage3.validate_row(self.worker(cond="cross:coding", dest="coding")), [])
        self.assertEqual(stage3.validate_row(self.worker(cond="router", dest="coding")), [])
        self.assertEqual(stage3.validate_row(self.router()), [])

    def test_bad_row_type(self):
        self.assertIn("bad row_type nope", stage3.validate_row(dict(self.worker(), row_type="nope")))

    def test_condition_must_name_the_destination_it_ran(self):
        self.assertIn("condition does not match selected_destination", stage3.validate_row(self.worker(cond="cross:coding", dest="math")))
        self.assertIn("condition does not match selected_destination", stage3.validate_row(self.worker(cond="reference", dest="coding")))

    def test_unknown_condition(self):
        self.assertIn("bad condition", stage3.validate_row(self.worker(cond="cross:law", dest="math")))
        self.assertIn("bad condition", stage3.validate_row(dict(self.worker(cond="router"), task_type="law")))

    def test_router_row_cannot_be_scored(self):
        for over in ({"scored": True}, {"pass": 1}, {"pass": 0}):
            with self.subTest(over=over):
                self.assertIn("router row must not be scored", stage3.validate_row(dict(self.router(), **over)))

    def test_router_row_must_carry_the_router_condition(self):
        self.assertIn("router row must have condition 'router'", stage3.validate_row(dict(self.router(), condition="reference")))

    def test_bad_router_outcome(self):
        self.assertIn("bad router_outcome maybe", stage3.validate_row(dict(self.router(), router_outcome="maybe")))


class TestCli(unittest.TestCase):
    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = stage3.main(list(argv))
        return code, out.getvalue()

    def test_a_command_is_required_and_commands_exclude_each_other(self):
        for argv in ([], ["--validate", "--prepare"], ["--stage", "3"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                stage3.main(argv)
            self.assertEqual(cm.exception.code, 2)

    def test_validate_passes(self):
        code, out = self.main("--validate")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^(\d+)/\1 checks passed\n$")

    def test_validate_prints_failures_and_exits_1(self):
        with mock.patch.object(stage3, "validate", lambda: [("good", True, ""), ("bad", False, "why")]):
            code, out = self.main("--validate")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  bad  [why]", out)
        self.assertNotIn("FAIL  good", out)
        self.assertIn("1/2 checks passed", out)

    def test_build_suite_writes_the_suite(self):
        with mock.patch.object(stage3, "build_suite", lambda: ROOT / "tasks" / "generic_v2.json"):
            self.assertEqual(self.main("--build-suite"), (0, "tasks/generic_v2.json\n"))

    def test_prepare_passes_the_seed(self):
        seen = []
        with mock.patch.object(stage3, "prepare", lambda s: seen.append(s) or ROOT / "x"):
            self.assertEqual(self.main("--prepare", "--order-seed", "7"), (0, "x\n"))
        self.assertEqual(seen, [7])

    def test_dry_run_verifies_the_sandbox(self):
        seen = []
        with mock.patch.object(stage3, "dry_run", lambda seed, verify=False: seen.append((seed, verify)) or ROOT / "x"):
            self.main("--dry-run")
        self.assertEqual(seen, [(stage3.DEFAULT_ORDER_SEED, True)])

    def test_verify_sandbox(self):
        with mock.patch.object(runner, "verify_sandbox", lambda: ROOT / "x"):
            self.assertEqual(self.main("--verify-sandbox"), (0, "x\n"))

    def test_freeze_without_approval_writes_nothing(self):
        with Env() as env:
            env.freeze_path.unlink()
            with self.assertRaises(SystemExit) as cm:
                self.main("--freeze")
            self.assertIn("--owner-approved", str(cm.exception))
            self.assertFalse(env.freeze_path.exists())

    def test_run_without_approval_makes_no_call(self):
        with Env() as env:
            with self.assertRaises(SystemExit) as cm:
                self.main("--run", "--stage", "3", "--model", "llama3:8b", "--model-digest", stage3.runner.SELECTED_MODEL["manifest_sha256"])
            self.assertIn("--owner-approved", str(cm.exception))
            self.assertEqual(env.calls, [])

    def test_status_review_join_and_report_commands(self):
        with Env() as env:
            path = env.run()
            code, out = self.main("--status", str(path))
            self.assertEqual(code, 0)
            self.assertIn("worker cells written: 200/200", out)
            with self.assertRaises(SystemExit):
                self.main("--report", str(path))
            self.assertFalse(path.with_suffix(".report.md").exists())
            self.assertEqual(self.main("--review-sheet", str(path), "--review-seed", "1")[0], 0)
            sheet = next((env.art / "review").glob("review_sheet.*.csv"))
            fill_sheet(sheet)
            self.assertEqual(self.main("--join-review", str(path), str(sheet))[0], 0)
            self.assertTrue(path.with_suffix(".human_review.md").exists())
            self.assertEqual(self.main("--report", str(path), "--unblind")[0], 0)
            self.assertTrue(path.with_suffix(".report.md").exists())


class TestValidatorCatchesDamage(unittest.TestCase):
    """Each validator check has to be able to fail. A tampered copy of the suite goes in through temp files."""

    def failures(self, tamper):
        full = json.loads(stage3.TASKS.read_text())
        draft = json.loads(stage3.DRAFT_TASKS.read_text())
        by_id = {t["id"]: t for t in full["tasks"]}
        tamper(by_id)
        new_ids = {t["id"] for t in draft["tasks"]}
        draft["tasks"] = [by_id[t["id"]] for t in draft["tasks"] if t["id"] in new_ids]
        with tempfile.TemporaryDirectory() as t:
            tp, dp = Path(t) / "suite.json", Path(t) / "draft.json"
            tp.write_text(json.dumps(full))
            dp.write_text(json.dumps(draft))
            with mock.patch.object(stage3, "TASKS", tp), mock.patch.object(stage3, "DRAFT_TASKS", dp):
                return [n for n, ok, _ in stage3.validate() if not ok]

    def assertCaught(self, name, tamper):
        self.assertIn(name, self.failures(tamper))

    def test_edited_v1_task(self):
        self.assertCaught("the 16 v1 tasks are unchanged", lambda t: t["math_1"].update(expected=392))

    def test_wrong_math_answer(self):
        self.assertCaught("math_h1: expression == expected", lambda t: t["math_h1"].update(expected=t["math_h1"]["expected"] + 1e-6))

    def test_return_only_in_a_prompt(self):
        self.assertCaught("math_h2: prompt has no 'Return only'", lambda t: t["math_h2"].update(prompt=t["math_h2"]["prompt"] + " Return only the number."))

    def test_verdict_balance(self):
        self.assertCaught("new evidence verdicts: 3 yes, 3 no", lambda t: t["evidence_qa_h1"].update(verdict="yes" if t["evidence_qa_h1"]["verdict"] == "no" else "no"))

    def test_too_few_difficulty_features(self):
        self.assertCaught("math_h1: >=2 counting difficulty features, all valid for type", lambda t: t["math_h1"].update(difficulty_features=["M1"]))

    def test_passage_missing_from_prompt(self):
        self.assertCaught("extraction_h1: passage verbatim in prompt", lambda t: t["extraction_h1"].update(prompt="Find it."))

    def test_support_span_not_in_passage(self):
        self.assertCaught("evidence_qa_h2: span 'made up words' in passage", lambda t: t["evidence_qa_h2"]["support_spans"].append("made up words"))

    def test_buggy_solution_that_passes(self):
        self.assertCaught("coding_h1: buggy fails >=1 test", lambda t: t["coding_h1"].update(buggy_solution=t["coding_h1"]["reference_solution"]))

    def test_broken_reference(self):
        names = self.failures(lambda t: t["coding_h2"].update(reference_solution=t["coding_h2"]["buggy_solution"]))
        self.assertTrue([n for n in names if n.startswith("coding_h2: reference passes") and "/" in n], names)

    def test_too_few_hidden_tests(self):
        self.assertCaught("coding_h3: 5-8 tests", lambda t: t["coding_h3"].update(tests=t["coding_h3"]["tests"][:4]))

    def test_function_name_missing_from_prompt(self):
        self.assertCaught("coding_h4: function name in prompt", lambda t: t["coding_h4"].update(function_name="other_name"))

    def test_duplicate_id(self):
        self.assertCaught("unique ids", lambda t: t["math_h3"].update(id="math_h4"))


class TestScorerAudit(unittest.TestCase):
    """The join is what establishes scorer validity, so each table is pinned on a small known case."""

    def join(self, marks, env_kw=None, second=None):
        """marks(key_entry) -> (correct, compliant). env_kw is passed to the mocked run."""
        with Env(**(env_kw or {})) as env:
            path = env.run()
            sheet, key = rep.review_sheet(path, 1)
            self.write(sheet, key, marks)
            kw = {}
            if second:
                s2, k2 = rep.review_sheet(path, 2, True)
                self.write(s2, k2, second)
                kw["second_sheet"] = s2
            return rep.join_review(path, sheet, **kw).read_text(), json.loads(key.read_text())

    @staticmethod
    def write(sheet, key, marks):
        mapping = json.loads(key.read_text())["mapping"]
        with sheet.open(newline="") as f:
            grid = list(csv.reader(f))
        for r in grid[1:]:
            r[4], r[5] = marks(mapping[r[0]])
        with sheet.open("w", newline="") as f:
            csv.writer(f).writerows(grid)

    def test_counts_and_agreement_when_human_matches(self):
        text, key = self.join(lambda m: ("1", "1"))
        n = len(key["mapping"])
        self.assertIn(f"| pass (sampled) | {n} | 0 | 0 |", text)
        self.assertIn(f"Agreement on scored cells: {n}/{n}.", text)
        self.assertIn(f"Reviewed {n} of 200 answered cells: 0 the scorer failed or flagged, and {n} sampled from 200 scorer passes", text)

    def test_a_blank_mark_is_not_counted_as_agreement(self):
        text, key = self.join(lambda m: ("", "1"))
        n = len(key["mapping"])
        self.assertIn(f"| pass (sampled) | 0 | 0 | {n} |", text)
        self.assertIn("Agreement on scored cells: 0/0.", text)

    def test_disagreement_is_listed_with_the_note_column(self):
        text, key = self.join(lambda m: ("0", "0"))
        self.assertIn(f"Agreement on scored cells: 0/{len(key['mapping'])}.", text)
        self.assertIn(f"False positives in the sample: {len(key['mapping'])}/{len(key['mapping'])}", text)

    def test_component_disagreement_needs_a_set_component_and_a_mark(self):
        def compliant_only(m):
            return ("1", "0") if m["task_id"].startswith(("extraction", "evidence")) else ("1", "1")
        text, _ = self.join(compliant_only)
        section = text.split("## B. Component disagreements")[1].split("## C.")[0]
        rows = [l for l in section.splitlines() if l.startswith("| ") and "component" not in l]
        self.assertTrue(rows)
        self.assertTrue(all("| compliant | 1 | 0 |" in l for l in rows), rows)

    def test_agreeing_or_blank_components_are_not_listed(self):
        for marks in (("1", "1"), ("", "")):
            with self.subTest(marks=marks):
                text, _ = self.join(lambda m: marks)
                section = text.split("## B. Component disagreements")[1].split("## C.")[0]
                self.assertEqual([l for l in section.splitlines() if l.startswith("| ") and "component" not in l], [])

    def test_two_reviewers_section(self):
        text, key = self.join(lambda m: ("1", "1"), second=lambda m: ("1", "1"))
        n = len(key["mapping"])
        self.assertIn(f"Same {n} cells, separate shuffles. Pass marks agree on {n}/{n} cells.", text)
        self.assertIn("Cohen's kappa: undefined", text)

    def test_second_reviewer_blanks_drop_out_of_the_pairs(self):
        text, _ = self.join(lambda m: ("1", "1"), second=lambda m: ("", ""))
        self.assertIn("Pass marks agree on 0/0 cells.", text)

    def test_dry_run_join_carries_the_banner_and_real_join_does_not(self):
        self.assertNotIn("DRY RUN", self.join(lambda m: ("1", "1"))[0])
        with Env() as env:
            with contextlib.redirect_stdout(io.StringIO()):
                path = stage3.dry_run()
            sheet, _ = rep.review_sheet(path, 1)
            fill_sheet(sheet)
            self.assertIn("DRY RUN — NOT RESULTS", rep.join_review(path, sheet).read_text())
            self.assertEqual(sheet.parent.name, "dry_run")


class TestBlindedStatus(unittest.TestCase):
    def test_clean_run_says_none(self):
        with Env() as env:
            text = rep.status(env.run())
            self.assertIn("transport or provider problems: none", text)
            self.assertIn("attempts: 240; retries: 0", text)
            self.assertIn("router calls written: 40/40; answered: 40", text)

    def test_problems_are_named_by_status(self):
        with Env(script={1: ConnectionRefusedError("x"), 2: ConnectionRefusedError("y")}) as env:
            text = rep.status(env.run())
            self.assertIn("transport or provider problems: {'transport_error': 2}", text)
            self.assertIn("retries: 1", text)

    def test_status_names_no_condition_score_or_destination(self):
        with Env() as env:
            text = rep.status(env.run()).lower()
        for word in ("cross:", "reference", "pass", "fail", "correct", "evidence_qa", "coding", "extraction"):
            self.assertNotIn(word, text)


class TestRouterErrorTable(unittest.TestCase):
    def table(self, router):
        with Env(router=router) as env:
            return rep.report(env.run(), unblind=True).read_text()

    def test_a_wrong_route_is_compared_with_its_cross_cell(self):
        text = self.table({"math_1": "coding"})
        row = next(l for l in text.splitlines() if l.startswith("| math_1 | coding |"))
        self.assertTrue(row.endswith("| yes |"), row)      # the mock gives one canned answer per task and route

    def test_correct_routes_are_not_listed(self):
        text = self.table({})
        section = text.split("Cost of router errors")[1].split("## 6.")[0]
        self.assertEqual([l for l in section.splitlines() if l.startswith("| math_1")], [])

    def test_router_accuracy_counts_only_the_author_label(self):
        text = self.table({"math_1": "coding", "math_2": "banana"})
        self.assertIn("Router accuracy against author labels: 38/40.", text)
        self.assertIn("Parsed: 39. Unparseable: 1.", text)


class TestReviewSheetRefusals(unittest.TestCase):
    def test_a_leftover_key_alone_refuses_a_new_draw(self):
        with Env() as env:
            path = env.run()
            sheet, key = rep.review_sheet(path, 1)
            sheet.unlink()
            with self.assertRaises(SystemExit):
                rep.review_sheet(path, 2)
            self.assertTrue(key.exists())


class TestManifestFlags(unittest.TestCase):
    def test_real_run_is_not_marked_dry(self):
        with Env() as env:
            env.run()
            m = env.manifest()
            self.assertIs(m["dry_run"], False)
            self.assertEqual(m["suite"], "generic_v2")
            self.assertIs(m["aborted"], False)
            self.assertIsNone(m["abort"])
            self.assertEqual(m["hashes"], stage3.frozen_hashes())

    def test_dry_run_is_marked_dry_on_rows_and_manifest(self):
        with Env() as env:
            with contextlib.redirect_stdout(io.StringIO()):
                path = stage3.dry_run()
            m = json.loads(path.with_name(path.name.replace(".jsonl", ".manifest.json")).read_text())
            self.assertIs(m["dry_run"], True)
            self.assertTrue(all(r["dry_run"] and r["status"] == "dry_run" for r in rep.load(path)["rows"]))

    def test_dry_run_verifies_only_when_asked_and_only_a_real_backend(self):
        for verify, status, want in ((False, "unverified", 0), (True, "unverified", 1), (True, "blocked", 0)):
            calls = []

            def factory(ev, st=status):
                b = FakeBackend(ev)
                b.status = st
                b.verify_isolation = lambda: calls.append(1) or {"status": "verified"}
                return b
            with self.subTest(verify=verify, status=status), Env(backend_factory=factory):
                with contextlib.redirect_stdout(io.StringIO()):
                    stage3.dry_run(verify=verify)
                self.assertEqual(len(calls), want)


class TestMissingFiles(unittest.TestCase):
    """A wrong path should say what's missing, not print a traceback."""

    def test_status_on_a_missing_log(self):
        with self.assertRaises(SystemExit) as cm:
            rep.status(Path("/nonexistent/x.jsonl"))
        self.assertIn("not found", str(cm.exception))

    def test_join_with_a_missing_sheet(self):
        with Env() as env:
            path = env.run()
            rep.review_sheet(path, 1)
            with self.assertRaises(SystemExit) as cm:
                rep.join_review(path, env.art / "absent.csv")
            self.assertIn("not found", str(cm.exception))

    def test_join_before_any_sheet_was_made(self):
        with Env() as env:
            path = env.run()
            sheet = env.art / "s.csv"
            sheet.write_text("x")
            with self.assertRaises(SystemExit) as cm:
                rep.join_review(path, sheet)
            self.assertIn("no review key", str(cm.exception))

    def test_join_with_a_second_sheet_but_no_second_key(self):
        with Env() as env:
            path = env.run()
            sheet, _ = rep.review_sheet(path, 1)
            fill_sheet(sheet)
            with self.assertRaises(SystemExit) as cm:
                rep.join_review(path, sheet, sheet)
            self.assertIn("no review key", str(cm.exception))


class TestAtomicWrites(unittest.TestCase):
    def test_a_failed_write_leaves_the_old_file(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "m.json"
            p.write_text("old")
            with mock.patch("os.replace", side_effect=OSError("disk")), self.assertRaises(OSError):
                stage3.write_atomic(p, "new")
            self.assertEqual(p.read_text(), "old")
            self.assertEqual([x.name for x in Path(t).iterdir()], ["m.json"])

    def test_it_writes(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "m.json"
            stage3.write_atomic(p, "new ×")
            self.assertEqual(p.read_text(encoding="utf-8"), "new ×")

    def test_manifest_and_freeze_use_it(self):
        src = (ROOT / "farex_pilot" / "stage3.py").read_text()
        self.assertIn('write_atomic(path.with_name(path.name.replace(".jsonl", ".manifest.json"))', src)
        self.assertIn("write_atomic(FREEZE,", src)


class TestInterrupt(unittest.TestCase):
    def test_ctrl_c_is_recorded_and_stops_the_run(self):
        with Env(script={5: KeyboardInterrupt()}) as env:
            with self.assertRaises(KeyboardInterrupt):
                env.run()
            m = env.manifest()
            self.assertTrue(m["aborted"])
            self.assertEqual(m["abort"]["reason"], "KeyboardInterrupt")
            self.assertEqual(len(env.calls), 6)
            self.assertIsNotNone(m["ended"])


class TestReportReviewState(unittest.TestCase):
    def test_says_when_review_is_not_joined(self):
        with Env() as env:
            text = rep.report(env.run(), unblind=True).read_text()
            self.assertIn("Human review: NOT joined. Every number here is the automated scorer alone.", text)

    def test_names_the_audit_when_it_exists(self):
        with Env() as env:
            path = env.run()
            sheet, _ = rep.review_sheet(path, 1)
            fill_sheet(sheet)
            audit = rep.join_review(path, sheet)
            text = rep.report(path, unblind=True).read_text()
            self.assertIn(f"Human review: joined, see `{audit.name}`.", text)


class TestDeterminism(unittest.TestCase):
    def test_order_does_not_depend_on_the_hash_seed(self):
        code = "import json;from farex_pilot import stage3;print(json.dumps(stage3.execution_order(stage3.DEFAULT_ORDER_SEED)['cells']))"
        runs = {subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={"PYTHONHASHSEED": s}, capture_output=True, check=True).stdout
                for s in ("1", "2", "3")}
        self.assertEqual(len(runs), 1)

    def test_prepared_file_matches_the_order_in_memory(self):
        with Env():
            data = json.loads(stage3.order_file().read_text())
            self.assertEqual(data["cells"], stage3.execution_order(stage3.DEFAULT_ORDER_SEED)["cells"])

    def test_different_seeds_differ(self):
        a, b = (stage3.execution_order(s)["cells"] for s in (1, 2))
        self.assertNotEqual(a, b)

    def test_each_task_has_one_of_each_condition(self):
        by_task = {}
        for c in stage3.execution_order(1)["cells"]:
            by_task.setdefault(c["task_id"], []).append(c)
        types = {t["id"]: t["type"] for t in stage3.load_tasks()}
        for tid, cells in by_task.items():
            self.assertEqual(sorted(c["condition"] for c in cells), sorted(stage3.all_conditions(types[tid])), tid)

    def test_router_cells_all_come_after_the_cross_block(self):
        cells = stage3.execution_order(1)["cells"]
        self.assertTrue(all(c["condition"] == "router" for c in cells[160:]))
        self.assertFalse([c for c in cells[:160] if c["condition"] == "router"])

    def test_a_tasks_cross_cells_run_consecutively(self):
        cells = stage3.execution_order(1)["cells"][:160]
        for i in range(0, 160, 4):
            self.assertEqual(len({c["task_id"] for c in cells[i:i + 4]}), 1)

    def test_router_cells_have_no_preset_destination(self):
        self.assertTrue(all(c["selected_destination"] is None for c in stage3.execution_order(1)["cells"][160:]))


class ScriptedEnv(Env):
    """A run with a known shape. Evidence QA sent to coding returns code. Reference cells cost 100 ms and
    5 tokens, wrong routes 300 ms and 8, router calls 50 ms and 1, and evidence_qa_h1's router call is wrong."""

    DEST = {runner.prompt_path(d).read_text(): d for d in runner.TYPES}

    def chat(self, base, model, system, user, options):
        self.calls.append({"system": system, "user": user})
        if user == stage3.WARMUP_MESSAGE:
            body, ms = {"message": {"content": "OK"}}, 77.0
        elif system == ROUTER_SYSTEM:
            t = BY_PROMPT[user]
            body = {"message": {"content": "coding" if t["id"] == "evidence_qa_h1" else t["type"]}, "prompt_eval_count": 9, "eval_count": 1}
            ms = 50.0
        else:
            t, dest = BY_PROMPT[user], self.DEST[system]
            content = "```python\nprint(1)\n```" if (t["type"], dest) == ("evidence_qa", "coding") else runner.synthetic_response(t)
            right = dest == t["type"]
            body = {"message": {"content": content}, "prompt_eval_count": 20, "eval_count": 5 if right else 8}
            ms = 100.0 if right else 300.0
        return json.dumps(body).encode(), ms


class TestKnownAnswers(unittest.TestCase):
    """Every primary table, against values worked out by hand from ScriptedEnv."""

    @classmethod
    def setUpClass(cls):
        with ScriptedEnv() as env:
            cls.text = rep.report(env.run(), unblind=True).read_text()

    def section(self, title, end):
        return self.text.split(title)[1].split(end)[0]

    def test_full_matrix(self):
        m = self.section("All 40 tasks:", "The 16 v1 tasks:")
        self.assertIn("| evidence_qa | 0/10 (0) | 10/10 (0) | 10/10 (0) | **10/10 (0)** |", m)
        self.assertIn("| coding | **10/10 (0)** | 10/10 (0) | 10/10 (0) | 10/10 (0) |", m)
        self.assertIn("| math | 10/10 (0) | **10/10 (0)** | 10/10 (0) | 10/10 (0) |", m)

    def test_v1_and_new_tasks_are_split(self):
        self.assertIn("| evidence_qa | 0/4 (0) | 4/4 (0) | 4/4 (0) | **4/4 (0)** |", self.section("The 16 v1 tasks:", "The 24 new tasks:"))
        self.assertIn("| evidence_qa | 0/6 (0) | 6/6 (0) | 6/6 (0) | **6/6 (0)** |", self.section("The 24 new tasks:", "## 2."))

    def test_contract_table(self):
        s = self.section("## 2. Contract violations", "## 3.")
        self.assertIn("| evidence_qa | coding | 10 | 0 | 10 |", s)
        self.assertIn("| evidence_qa | math | 0 | 0 | 10 |", s)
        self.assertIn("| coding | coding | 0 | 0 | 10 |", s)

    def test_paired_cost_has_each_wrong_route_only(self):
        s = self.section("## 3. Paired cost per wrong route", "## 4.")
        rows = [l for l in s.splitlines() if l.startswith("| ") and "destination" not in l]
        self.assertEqual(len(rows), 12)
        self.assertIn("| math | coding | 10 | 200 | 10 / 0 / 0 | 3 | 10 / 0 / 0 |", rows)
        self.assertFalse([l for l in rows if l.startswith("| math | math")])

    def test_sign_test(self):
        s = self.section("## 4. Pre-registered sign test", "## 5.")
        self.assertIn("| latency | 40 | 40 | 0 | 0 |", s)
        self.assertIn("| completion tokens | 40 | 40 | 0 | 0 |", s)
        self.assertIn(f"| {rep.sign_test([1] * 40)['p']:.4g} |", s)

    def test_router_section(self):
        s = self.section("## 5. Router", "## 6.")
        self.assertIn("Router accuracy against author labels: 39/40.", s)
        self.assertIn("End-to-end success: 39/40 scored; 39/40", s)
        self.assertIn("| evidence_qa | 1 | 0 | 0 | 9 | 0 |", s)
        self.assertIn("| coding | 10 | 0 | 0 | 0 | 0 |", s)
        self.assertIn("| evidence_qa_h1 | coding | 0 | 0 | 1 | yes |", s)
        self.assertIn("Router calls, latency ms: 50 (50–50); completion tokens: 1 (1–1).", s)

    def test_per_task_view(self):
        s = self.section("## 6. Per task", "## 7.")
        self.assertIn("| evidence_qa_1 | 1 | 0 1 1 | 1 |", s)
        self.assertIn("| evidence_qa_h1 | 1 | 0 1 1 | 0 |", s)
        self.assertIn("| math_1 | 1 | 1 1 1 | 1 |", s)

    def test_status_tables(self):
        s = self.section("## 7. Transport and scoring status", "## 8.")
        self.assertIn("| ok | 240 |", s)
        self.assertIn("| scored | 200 |", s)
        self.assertIn("Cells with a retry: 0 []", s)

    def test_components(self):
        s = self.section("## 8. Correctness vs. compliance", "## 9.")
        self.assertIn("| evidence_qa | reference | 10/10 | 10/10 | 0 |", s)
        self.assertIn("| evidence_qa | wrong | 20/30 | 20/30 | 0 |", s)
        self.assertIn("| extraction | wrong | 30/30 | 30/30 | 0 |", s)

    def test_spreads(self):
        s = self.section("## 9. Latency and tokens by route", "## 10.")
        self.assertIn("| math | reference | 10 | 100 (100–100) | 5 (5–5) |", s)
        self.assertIn("| math | wrong | 30 | 300 (300–300) | 8 (8–8) |", s)
        self.assertIn("| math | router | 10 | 100 (100–100) | 5 (5–5) |", s)
        self.assertIn("| evidence_qa | router | 10 | 100 (100–300) | 5 (5–8) |", s)
        self.assertIn("Warm-up, excluded from everything above: 77 ms, status ok.", s)

    def test_totals(self):
        s = self.section("## 10. Attempts and totals", "## 11.")
        self.assertIn("Attempts per cell: 1: 240.", s)
        # 40 reference at 100, 120 wrong at 300, then 39 router cells at 100 and 1 misrouted at 300
        self.assertIn("| latency ms | final attempts | 44200 | 0 |", s)
        self.assertIn("| latency ms | all attempts | 46200 | 0 |", s)       # plus 40 router calls at 50


class TestFailurePathCounts(unittest.TestCase):
    """Non-uniform failures: the counts that --status and the report print for damaged runs."""

    def run_with(self, **kw):
        env = Env(**kw)
        with env:
            path = env.run()
            return rep.status(path), rep.report(path, unblind=True).read_text(), rep.load(path)

    def test_malformed_body_is_unanswered_and_unscored(self):
        status, text, d = self.run_with(script={1: b"not json"})
        self.assertIn("worker cells written: 200/200; answered: 199", status)
        self.assertIn("transport or provider problems: {'provider_malformed': 1}", status)
        self.assertIn("| provider_malformed | 1 |", text)

    def test_retried_cell_is_named(self):
        _, text, d = self.run_with(script={1: ConnectionRefusedError("x")})
        first = next(r for r in d["rows"] if r["attempt"] == 2)
        self.assertIn(f"Cells with a retry: 1 ['{first['task_id']}/{first['condition']}']", text)

    def test_unscored_cells_are_kept_apart_from_fails(self):
        self.assertEqual(rep._rate([{"scored": False, "pass": None, "score_status": "sandbox_unavailable"}]), "0/0 (1)")

    def test_rate_counts_fails_and_unscored_apart(self):
        rows = [{"scored": True, "pass": 1}, {"scored": True, "pass": 0}, {"scored": False, "pass": None}]
        self.assertEqual(rep._rate(rows), "1/2 (1)")

    def test_key_records_the_population(self):
        with Env(script={1: b"not json"}) as env:
            _, key = rep.review_sheet(env.run(), 1)
            self.assertEqual(json.loads(key.read_text())["population"], 199)

    def test_warmup_without_latency_is_stated(self):
        with Env(script={0: ConnectionRefusedError("a"), 1: ConnectionRefusedError("b")}) as env:
            text = rep.report(env.run(), unblind=True).read_text()
            self.assertIn("Warm-up, excluded from everything above: no latency recorded.", text)

    def test_scorer_fail_is_flagged_for_review_and_shown_in_the_audit(self):
        with ScriptedEnv() as env:
            path = env.run()
            sheet, key = rep.review_sheet(path, 1)
            fill_sheet(sheet, "0")
            text = rep.join_review(path, sheet).read_text()
            flagged = sum(m["selection"] == "flagged" for m in json.loads(key.read_text())["mapping"].values())
            self.assertEqual(flagged, 11)                     # 10 evidence on coding, 1 misrouted by the router
            self.assertIn("| fail | 0 | 11 | 0 |", text)


if __name__ == "__main__":
    unittest.main()
