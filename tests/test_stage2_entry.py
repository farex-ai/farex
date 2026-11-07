"""Stage 2 entry-path tests (stdlib unittest). These drive runner.run_stage2 / preflight_stage2
end to end with the MODEL BOUNDARY MOCKED: runner._ollama_chat and runner._ollama_tags are
replaced, so no request reaches Ollama and no inference happens. The sandbox backend is a fake
here; tests/test_sandbox_probes.py repeats the success path with the real Docker backend.

Run from the pilot root:  python3 -I -m unittest discover -s tests -v
"""

import argparse
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from farex_pilot import runner, sandbox  # noqa: E402

DIGEST = runner.SELECTED_MODEL["manifest_sha256"]
TASKS = {t["id"]: t for t in runner.load_tasks()}
BY_PROMPT = {t["prompt"]: t for t in TASKS.values()}


class FakeBackend:
    """Stands in for DockerBackend. run() refuses unless verify_isolation() succeeded."""
    name = "fake-docker"

    def __init__(self, events, verify_ok=True, available=True, image=sandbox.PINNED_IMAGE):
        self.events, self.verify_ok, self.available, self.image = events, verify_ok, available, image
        self.verified, self.status, self.detail, self.runs = False, "available_unverified", "", []

    def verify_isolation(self):
        self.events.append("verify")
        if not self.available:
            self.status, self.detail = "blocked", "docker CLI not found on PATH"
            return {"status": "blocked", "reason": self.detail, "controls": {}}
        self.verified = self.verify_ok
        self.status = "verified" if self.verify_ok else "failed"
        self.detail = "" if self.verify_ok else "network_enforced"
        return {"status": self.status, "image": self.image, "assertions_passed": 1, "assertions_total": 1, "controls": {}}

    def run(self, code, fn, tests):
        if not self.verified:
            raise RuntimeError("unverified backend used")
        self.events.append("sandbox_run")
        self.runs.append(fn)
        return {"passed": len(tests), "total": len(tests), "exit_code": 0, "timed_out": False, "stderr_tail": "",
                "outcome": "completed"}


def canned(user):
    if user == "Reply with the word OK.":
        return "OK"
    return runner.synthetic_response(BY_PROMPT[user])


class Stage2Env:
    """Temp ART/FREEZE with a valid frozen record and order file; mocked model boundary.
    `script`: list of per-call behaviours (an exception to raise, raw bytes to return, or None = canned)."""

    def __init__(self, backend_factory=None, tags=None, script=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = Path(self.tmp.name) / "artifacts"
        self.freeze_path = Path(self.tmp.name) / "freeze.json"
        self.events, self.chat_calls, self.sleeps = [], [], []
        self.script = list(script or [])
        self.tags = tags if tags is not None else {"models": [{"name": "llama3:8b", "model": "llama3:8b", "digest": DIGEST}]}
        self.backend_factory = backend_factory or (lambda ev: FakeBackend(ev))
        self.backend = None
        self._patches = []

    def chat(self, base, model, system, user, options):
        self.events.append("chat")
        self.chat_calls.append({"model": model, "system": system, "user": user})
        b = self.script.pop(0) if self.script else None
        if isinstance(b, BaseException):
            raise b
        if isinstance(b, bytes):
            return b, 12.0
        body = {"message": {"content": canned(user)}, "prompt_eval_count": 20, "eval_count": 5, "total_duration": 1}
        return json.dumps(body).encode(), 10.0

    def ollama_tags(self, base):
        self.events.append("tags")
        if isinstance(self.tags, BaseException):
            raise self.tags
        return self.tags

    def default_backend(self):
        self.backend = self.backend_factory(self.events)
        return self.backend

    def __enter__(self):
        ps = [mock.patch.object(runner, "ART", self.art), mock.patch.object(runner, "FREEZE", self.freeze_path),
              mock.patch.object(runner, "_ollama_chat", self.chat), mock.patch.object(runner, "_ollama_tags", self.ollama_tags),
              mock.patch.object(runner.sandbox, "default_backend", self.default_backend),
              mock.patch.object(runner, "_sleep", lambda s: self.sleeps.append(s)),
              mock.patch.dict(os.environ, {}, clear=False)]
        for p in ps:
            p.start()
        self._patches = ps
        os.environ.pop("FAREX_SANDBOX_IMAGE", None)
        os.environ.pop("FAREX_OLLAMA_URL", None)
        of = runner.order_file()
        of.parent.mkdir(parents=True, exist_ok=True)
        of.write_text(json.dumps(runner.execution_order(runner.DEFAULT_ORDER_SEED), indent=2) + "\n")
        self.write_freeze()
        return self

    def write_freeze(self, **overrides):
        rec = runner.freeze_record({"status": "verified", "assertions_passed": 1, "assertions_total": 1,
                                    "image": sandbox.PINNED_IMAGE, "recorded": "test"})
        rec.update(overrides)
        self.freeze_path.write_text(json.dumps(rec))
        return rec

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def run(self, **kw):
        args = argparse.Namespace(owner_approved=True, stage=2, model="llama3:8b", model_digest=DIGEST)
        for k, v in kw.items():
            setattr(args, k, v)
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.run_stage2(args)

    def rows(self):
        files = list((self.art / "runs").glob("*.jsonl")) if (self.art / "runs").exists() else []
        return [json.loads(l) for f in files for l in f.read_text().splitlines() if l.strip()]


class TestStage2SuccessPath(unittest.TestCase):
    def test_verified_backend_reaches_model_boundary_and_scores_coding(self):
        with Stage2Env() as env:
            path = env.run()
            rows = env.rows()
            # verification of the actual backend happens before the first model call
            self.assertEqual(env.events[:2], ["tags", "verify"])
            self.assertLess(env.events.index("verify"), env.events.index("chat"))
            self.assertEqual(len(env.chat_calls), 33)                       # 1 warm-up + 32 cells
            self.assertTrue(all(c["model"] == "llama3:8b" for c in env.chat_calls))
            # coding scoring received the verified backend (FakeBackend.run refuses otherwise)
            self.assertEqual(sorted(env.backend.runs), sorted([t["function_name"] for t in TASKS.values()
                                                               if t["type"] == "coding"] * 2))
            coding = [r for r in rows if r["task_type"] == "coding"]
            self.assertEqual(len(coding), 8)
            self.assertTrue(all(r["scored"] and r["pass"] == 1 for r in coding), [r["reason"] for r in coding])
            # schema, ids, warm-up
            for r in rows:
                self.assertEqual(runner.validate_row(r), [], r["row_id"])
            self.assertEqual(len({r["row_id"] for r in rows}), len(rows))
            warm = [r for r in rows if r["is_warmup"]]
            self.assertEqual(len(warm), 1)
            self.assertEqual((warm[0]["condition"], warm[0]["task_id"], warm[0]["task_type"], warm[0]["scored"]),
                             ("warmup", "__warmup__", None, False))
            self.assertTrue(all(r["condition"] in ("reference", "fixed_wrong") for r in rows if not r["is_warmup"]))
            m = json.loads(path.with_name(path.name.replace(".jsonl", ".manifest.json")).read_text())
            self.assertEqual(m["model_digest_observed_local"], DIGEST)
            self.assertEqual(m["sandbox"]["verification"]["status"], "verified")
            rep = runner.report(path).read_text()
            self.assertIn("Cells present: 32/32", rep)
            sheet, _ = runner.review_sheet(path, seed=3)
            import csv
            header = next(csv.reader(sheet.open()))
            self.assertEqual([h.split(" ")[0] for h in header[4:6]], ["human_correct", "human_compliant"])


class TestStage2FailsClosed(unittest.TestCase):
    def assertBlocked(self, env, msg_part, **run_kw):
        with self.assertRaises(SystemExit) as cm:
            env.run(**run_kw)
        self.assertIn(msg_part, str(cm.exception))
        self.assertEqual(env.chat_calls, [], "a model call was made")
        self.assertEqual(env.rows(), [])

    def test_failed_verification_prevents_every_model_call(self):
        with Stage2Env(backend_factory=lambda ev: FakeBackend(ev, verify_ok=False)) as env:
            self.assertBlocked(env, "sandbox verification failed")
            self.assertIn("verify", env.events)

    def test_unavailable_sandbox_prevents_every_model_call(self):
        with Stage2Env(backend_factory=lambda ev: FakeBackend(ev, available=False)) as env:
            self.assertBlocked(env, "sandbox verification failed or unavailable")

    def test_backend_with_other_image_blocks(self):
        with Stage2Env(backend_factory=lambda ev: FakeBackend(ev, image="python@sha256:" + "1" * 64)) as env:
            self.assertBlocked(env, "backend image differs")
            self.assertNotIn("verify", env.events)

    def test_not_approved_or_wrong_stage(self):
        with Stage2Env() as env:
            self.assertBlocked(env, "--owner-approved", owner_approved=False)
            self.assertBlocked(env, "--stage 2", stage=1)
            self.assertBlocked(env, "--stage 2", stage=None)

    def test_freeze_not_frozen(self):
        with Stage2Env() as env:
            env.write_freeze(status="invalidated")
            self.assertBlocked(env, "not frozen")
            env.freeze_path.unlink()
            self.assertBlocked(env, "not frozen")

    def test_frozen_file_changed(self):
        with Stage2Env() as env:
            rec = json.loads(env.freeze_path.read_text())
            rec["hashes"]["farex_pilot/scoring.py"] = "0" * 64
            env.freeze_path.write_text(json.dumps(rec))
            self.assertBlocked(env, "files changed since freeze: ['farex_pilot/scoring.py']")

    def test_execution_config_changed(self):
        with Stage2Env() as env:
            rec = json.loads(env.freeze_path.read_text())
            rec["execution"]["options"]["temperature"] = 0.7
            env.freeze_path.write_text(json.dumps(rec))
            self.assertBlocked(env, "execution configuration differs")

    def test_order_file_hash_mismatch(self):
        with Stage2Env() as env:
            of = runner.order_file()
            of.write_text(of.read_text() + " ")
            self.assertBlocked(env, "order file missing or its hash differs")
            of.unlink()
            self.assertBlocked(env, "order file missing or its hash differs")

    def test_order_file_not_from_frozen_seed(self):
        with Stage2Env() as env:
            of = runner.order_file()
            of.write_text(json.dumps(runner.execution_order(7)))
            env.write_freeze()                                  # hash re-recorded, but content is another seed
            self.assertBlocked(env, "not the order the frozen seed produces")

    def test_model_identity_mismatch(self):
        with Stage2Env() as env:
            self.assertBlocked(env, "is not the frozen model", model="mistral:7b")
            self.assertBlocked(env, "is not the frozen model", model=None)
            self.assertBlocked(env, "full frozen manifest digest", model_digest=DIGEST[:12])
            self.assertBlocked(env, "full frozen manifest digest", model_digest="f" * 64)
            self.assertBlocked(env, "full frozen manifest digest", model_digest=None)

    def test_live_ollama_digest_mismatch(self):
        with Stage2Env(tags={"models": [{"name": "llama3:8b", "digest": "a" * 64}]}) as env:
            self.assertBlocked(env, "local Ollama model digest differs")

    def test_live_ollama_model_missing_or_unreachable(self):
        with Stage2Env(tags={"models": [{"name": "mistral:7b", "digest": DIGEST}]}) as env:
            self.assertBlocked(env, "0 entries named 'llama3:8b'")
        with Stage2Env(tags={"models": [{"name": "llama3:8b", "digest": DIGEST[:20]}]}) as env:
            self.assertBlocked(env, "full model digest")
        with Stage2Env(tags=urllib.error.URLError(ConnectionRefusedError())) as env:
            self.assertBlocked(env, "cannot read local Ollama model list")

    def test_sandbox_image_override_blocks(self):
        with Stage2Env() as env:
            os.environ["FAREX_SANDBOX_IMAGE"] = "python@sha256:" + "2" * 64
            self.assertBlocked(env, "FAREX_SANDBOX_IMAGE overrides")
            self.assertNotIn("verify", env.events)
            os.environ["FAREX_SANDBOX_IMAGE"] = sandbox.PINNED_IMAGE       # same image: allowed
            env.run()
            self.assertEqual(len(env.chat_calls), 33)

    def test_non_local_ollama_url_blocks(self):
        with Stage2Env() as env:
            for url in ("http://127.0.0.1.example.com:11434", "https://127.0.0.1:11434", "http://10.0.0.5:11434",
                        "http://user@127.0.0.1:11434"):
                os.environ["FAREX_OLLAMA_URL"] = url
                self.assertBlocked(env, "local-Ollama only")
            self.assertNotIn("tags", env.events)

    def test_local_url_rule(self):
        for ok in ("http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434", "http://127.0.0.1:11434/"):
            self.assertTrue(runner._is_local_url(ok), ok)


class TestStage2TransportProtocol(unittest.TestCase):
    """Retry protocol (PLAN.md 'Stage 2 protocol'): only connection refused/reset/aborted are retried,
    once, after 5 s. Every attempt is its own row with a unique row_id and the shared call_id."""

    def cell_rows(self, env, call_n=1):
        rows = [r for r in env.rows() if r["call_id"].endswith(f"-{call_n:03d}") and not r["is_warmup"]]
        return sorted(rows, key=lambda r: r["attempt"])

    def run_with(self, first_cell_behaviours):
        env = Stage2Env(script=[None, *first_cell_behaviours])   # call 0 = warm-up
        env.__enter__()
        self.addCleanup(env.__exit__)
        env.run()
        return env

    def test_connection_refused_is_retried_once(self):
        env = self.run_with([urllib.error.URLError(ConnectionRefusedError(61, "refused"))])
        a = self.cell_rows(env)
        self.assertEqual([r["status"] for r in a], ["transport_error", "ok"])
        self.assertEqual([r["attempt"] for r in a], [1, 2])
        self.assertEqual(len({r["call_id"] for r in a}), 1)
        self.assertEqual([r["row_id"] for r in a], [a[0]["call_id"] + "-a1", a[0]["call_id"] + "-a2"])
        self.assertTrue(a[0]["retry_eligible"])
        self.assertEqual(env.sleeps, [runner.RETRY_DELAY_S])
        self.assertTrue(a[1]["scored"] or a[1]["score_status"] is not None)
        self.assertFalse(a[0]["scored"])

    def test_reset_twice_gives_two_failed_attempts_and_stops(self):
        env = self.run_with([ConnectionResetError(), http.client.RemoteDisconnected("closed")])
        a = self.cell_rows(env)
        self.assertEqual([r["status"] for r in a], ["transport_error", "transport_error"])
        self.assertEqual(len(env.chat_calls), 34)                    # 33 + one retry

    def test_not_retried(self):
        cases = [(urllib.error.HTTPError("u", 500, "err", {}, None), "http_error"),
                 (urllib.error.URLError(TimeoutError()), "timeout"),
                 (TimeoutError(), "timeout"),
                 (http.client.IncompleteRead(b"x"), "transport_error"),
                 (urllib.error.URLError(OSError(65, "no route")), "transport_error"),
                 (b"x" * (runner.MAX_RAW_BYTES + 1), "provider_oversized"),
                 (b"not json", "provider_malformed"),
                 (b'{"message": {"content": 5}}', "provider_malformed")]
        for behaviour, status in cases:
            with self.subTest(status=status, behaviour=type(behaviour).__name__), \
                    Stage2Env(script=[None, behaviour]) as env:
                env.run()
                a = self.cell_rows(env)
                self.assertEqual([r["status"] for r in a], [status])
                self.assertFalse(a[0]["retry_eligible"])
                self.assertEqual(env.sleeps, [])
                self.assertEqual(len(env.chat_calls), 33)
                self.assertFalse(a[0]["scored"])
                self.assertEqual(runner.validate_row(a[0]), [])


class TestReportAndRows(unittest.TestCase):
    def test_report_handles_missing_cells_retries_and_resources(self):
        env = Stage2Env(script=[None, urllib.error.URLError(ConnectionRefusedError())])
        with env:
            path = env.run()
            lines = path.read_text().splitlines()
            rows = [json.loads(l) for l in lines]
            keep = [l for l, r in zip(lines, rows) if not (r["task_id"] == "math_1" and r["condition"] == "reference")]
            path.write_text("\n".join(keep) + "\n")
            rep = runner.report(path).read_text()
        self.assertIn("Cells present: 31/32", rep)
        self.assertIn("math_1/reference", rep)
        self.assertIn("| math_1 | missing | missing | 0 |", rep)
        self.assertIn("cells with a retry: 1", rep)
        self.assertIn("total latency ms median", rep)
        self.assertIn("warm-up attempts: 1", rep)

    def test_sandbox_error_counted_as_sandbox_unscored(self):
        rows = []
        for i, (tid, cond) in enumerate([("coding_1", "reference"), ("coding_1", "fixed_wrong")]):
            t = TASKS[tid]
            r = runner.base_row("S2-x", "stage2", t, cond, t["type"] if cond == "reference" else runner.FIXED_WRONG[t["type"]], i + 1)
            r.update(status="ok", response="def reverse_string(s): return s[::-1]")
            r.update({"scored": False, "pass": None, "score_status": "sandbox_unavailable",
                      "reason": "sandbox_error: docker_run_exit_125" if i == 0 else "sandbox_unavailable", "components": {}})
            rows.append(r)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            rep = runner.report(p).read_text()
        self.assertIn("coding unscored, sandbox unavailable or sandbox error: 2", rep)
        self.assertIn("of which sandbox infrastructure errors: 1", rep)

    def test_warmup_row_schema(self):
        w = runner.base_row("S2-x", "stage2", None, None, "math", 0)
        w["status"] = "ok"
        self.assertEqual(runner.validate_row(w), [])
        bad = dict(w, condition="reference")
        self.assertIn("warm-up row must have condition 'warmup' and task_id '__warmup__'", runner.validate_row(bad))

    def test_every_emitted_status_is_valid(self):
        t = TASKS["math_1"]
        for st in runner.STATUSES:
            r = runner.base_row("S2-x", "stage2", t, "reference", "math", 1)
            r["status"] = st
            self.assertEqual(runner.validate_row(r), [], st)
        r["status"] = "something_else"
        self.assertTrue(runner.validate_row(r))

    def test_local_model_digest_reads_tags_bounded(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        body = json.dumps({"models": [{"name": "llama3:8b", "digest": "sha256:" + DIGEST}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=Resp(body)) as u:
            self.assertEqual(runner.local_model_digest("http://127.0.0.1:11434", "llama3:8b"), DIGEST)
        self.assertTrue(u.call_args[0][0].endswith("/api/tags"))     # metadata endpoint, not /api/chat
        with mock.patch("urllib.request.urlopen", return_value=Resp(b"[" * (runner.MAX_TAGS_BYTES + 5))):
            with self.assertRaises(SystemExit):
                runner.local_model_digest("http://127.0.0.1:11434", "llama3:8b")


if __name__ == "__main__":
    unittest.main()
