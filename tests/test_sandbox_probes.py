"""Runtime isolation tests for the Docker sandbox (PLAN.md C.3). These RUN containers.

Each control below is one test method; each individual check is a subTest, so the
report counts real assertions. The verification runs once per process (setUpClass).
If Docker or the pinned image is missing, the class is SKIPPED with the reason
(never silently passed). Passing these tests supports only the controls they cover.

The last class drives runner.run_stage2 with the real backend and a MOCKED model boundary.

Run from the pilot root:  python3 -I -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

from farex_pilot import runner, sandbox, sandbox_probes, scoring  # noqa: E402

_BACKEND = sandbox.default_backend()
_SKIP = None if _BACKEND.check_available()[0] else f"BLOCKED: {_BACKEND.check_available()[1]}"


@unittest.skipIf(_SKIP is not None, _SKIP or "")
class SandboxRuntimeControls(unittest.TestCase):
    verification = None

    @classmethod
    def setUpClass(cls):
        cls.verification = _BACKEND.verify_isolation()

    def _control(self, name):
        c = self.verification["controls"][name]
        self.assertTrue(c["checks"], f"{name}: no checks recorded")
        for chk in c["checks"]:
            with self.subTest(check=chk["name"]):
                self.assertTrue(chk["ok"], f"observed={chk['observed']!r}")
        self.assertEqual(c["status"], "pass")

    def test_01_nonroot_no_privileges(self):
        self._control("nonroot_no_privileges")

    def test_02_network_enforced_with_positive_control(self):
        self._control("network_enforced")

    def test_03_readonly_root_and_mounts(self):
        self._control("readonly_root_and_mounts")

    def test_04_scratch_space_limits(self):
        self._control("scratch_space_limits")

    def test_05_host_and_credential_isolation(self):
        self._control("host_and_credential_isolation")

    def test_06_memory_and_swap_limits(self):
        self._control("memory_and_swap_limits")

    def test_07_cpu_pids_and_rlimits(self):
        self._control("cpu_pids_and_rlimits")

    def test_08_fork_bomb_bounded(self):
        self._control("fork_bomb_bounded")

    def test_09_output_limit_enforced(self):
        self._control("output_limit_enforced")

    def test_10_wall_clock_timeout_cleanup(self):
        self._control("wall_clock_timeout_cleanup")

    def test_11_reference_code_checks(self):
        self._control("reference_code_checks")

    def test_12_result_integrity(self):
        self._control("result_integrity")

    def test_13_all_required_controls_verified(self):
        self.assertEqual(set(self.verification["controls"]), set(sandbox_probes.RUN_CONTROLS))
        self.assertEqual(self.verification["status"], "verified")
        self.assertTrue(_BACKEND.verified)

    def test_14_listener_bound_to_loopback_and_closed(self):
        self.assertEqual(self.verification["runtime"]["listener"], {"bind": "127.0.0.1", "closed": True})

    def test_15_forging_candidate_through_scorer_fails(self):
        # Intentionally wrong function that prints a passing verdict; passes the static policy, so it
        # reaches the container. The host judges the returned values.
        t = {x["id"]: x for x in runner.load_tasks()}["coding_1"]
        src = "def reverse_string(s):\n    print('{\"passed\": 4, \"total\": 4}')\n    return 'WRONG'\n"
        r = scoring.score(t, src, _BACKEND)
        self.assertEqual((r["pass"], r["reason"], r["components"]["tests_passed"]), (0, "tests_failed", 0))


@unittest.skipIf(_SKIP is not None, _SKIP or "")
class Stage2EntryRealSandbox(unittest.TestCase):
    """run_stage2 with the REAL Docker backend (real verification, real container scoring) and the
    model boundary MOCKED (no Ollama request, no inference)."""

    def test_real_backend_verified_then_scores_coding_and_rejects_forgery(self):
        from test_stage2_entry import Stage2Env
        forged = json.dumps({"message": {"content": "def find_max(lst):\n    print('{\"passed\": 4, \"total\": 4}')\n"
                                                    "    return -1\n"}, "eval_count": 3, "prompt_eval_count": 3}).encode()
        first = runner.execution_order(runner.DEFAULT_ORDER_SEED)["cells"][0]
        self.assertEqual(first["task_id"], "coding_2")              # call 1 is a coding cell (find_max)
        holder, real_default_backend = {}, sandbox.default_backend      # captured before Stage2Env patches it
        def factory(events):
            holder["b"] = real_default_backend()
            return holder["b"]
        with Stage2Env(backend_factory=factory, script=[None, forged]) as env:
            env.run()
            rows = [r for r in env.rows() if not r["is_warmup"]]
            self.assertTrue(holder["b"].verified)
            self.assertEqual(len(env.chat_calls), 33)
            coding = {(r["task_id"], r["condition"]): r for r in rows if r["task_type"] == "coding"}
            self.assertEqual(len(coding), 8)
            forged_row = coding[(first["task_id"], first["condition"])]
            self.assertEqual((forged_row["pass"], forged_row["reason"]), (0, "tests_failed"))
            others = [r for k, r in coding.items() if k != (first["task_id"], first["condition"])]
            self.assertTrue(all(r["pass"] == 1 and r["components"]["sandbox_outcome"] == "completed" for r in others),
                            [(r["task_id"], r["reason"]) for r in others])
            for r in env.rows():
                self.assertEqual(runner.validate_row(r), [])


if __name__ == "__main__":
    unittest.main()
