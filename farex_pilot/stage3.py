"""Stage 3 runner for suite generic_v2 (PLAN.md "Stage 3 plan", stage3/SPEC.md).

    python3 -m farex_pilot.stage3 --validate
    python3 -m farex_pilot.stage3 --build-suite            tasks/generic_v2.json from v1 + stage3 draft
    python3 -m farex_pilot.stage3 --prepare                execution order file
    python3 -m farex_pilot.stage3 --dry-run                synthetic answers, no model; real sandbox
    python3 -m farex_pilot.stage3 --verify-sandbox
    python3 -m farex_pilot.stage3 --freeze --owner-approved
    python3 -m farex_pilot.stage3 --run --stage 3 --owner-approved --model M --model-digest D
    python3 -m farex_pilot.stage3 --status RESULTS.jsonl   blinded: completion and problems only
    python3 -m farex_pilot.stage3 --review-sheet RESULTS.jsonl [--second-reviewer]
    python3 -m farex_pilot.stage3 --join-review RESULTS.jsonl SHEET.csv [--second-sheet SHEET2.csv]
    python3 -m farex_pilot.stage3 --report RESULTS.jsonl --unblind

This module is additive: farex_pilot/runner.py and scoring.py are the frozen generic_v1 code and are
reused, never edited. Standard library only. Never reads any .env file.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import http.client
import json
import math
import operator
import os
import platform
import random
import sys
import urllib.error
import uuid
from pathlib import Path

from . import runner, sandbox, scoring, scoring_v2
from .runner import ROOT, TYPES, rel, sha256_file

TASKS = ROOT / "tasks" / "generic_v2.json"
DRAFT_TASKS = ROOT / "stage3" / "tasks_new_draft.json"
ROUTER_PROMPT = ROOT / "prompts" / "generic_v2" / "router.txt"
SPEC = ROOT / "stage3" / "SPEC.md"
ART = ROOT / "artifacts"
FREEZE = ROOT / "freeze" / "generic_v2.freeze.json"
SCHEMA_VERSION = "pilot-row-2.0.0"
DEFAULT_ORDER_SEED = 20261103
WARMUP_MESSAGE = "Reply with the word OK."
EXPECTED_CHAT_CALLS = 241       # 1 warm-up + 160 cross + 40 router + 40 worker on the chosen route
REVIEW_FRACTION = 0.25

FEATURES = {"math": {"M1", "M2", "M3", "M4"}, "extraction": {"E1", "E2", "E3", "E4"},
            "evidence_qa": {"Q1", "Q2", "Q3", "Q4"}, "coding": {"C1", "C2", "C3"}}
# Balanced Latin square: every destination in every position once, and every ordered pair of
# neighbouring destinations once, so carry-over from the previous cell is balanced as well.
WILLIAMS = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))


def write_atomic(path: Path, text: str) -> None:
    """Write beside the target, then rename, so an interrupted write can't leave half a record."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_tasks() -> list[dict]:
    return json.loads(TASKS.read_text())["tasks"]


def now_id(prefix: str) -> str:
    return f"{prefix}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def condition_for(task_type: str, dest: str) -> str:
    return "reference" if dest == task_type else f"cross:{dest}"


def all_conditions(task_type: str) -> list[str]:
    return [condition_for(task_type, d) for d in TYPES] + ["router"]


# ------------------------------------------------------------------ suite

def suite_text() -> str:
    v1 = json.loads(runner.TASKS.read_text())["tasks"]
    draft = json.loads(DRAFT_TASKS.read_text())
    data = {"suite": "generic_v2", "status": draft["status"], "source": "tasks/generic_v1.json (unchanged) + stage3/tasks_new_draft.json",
            "tasks": v1 + draft["tasks"]}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def build_suite() -> Path:
    TASKS.write_text(suite_text(), encoding="utf-8")
    return TASKS


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCS = {"ceil": math.ceil, "floor": math.floor}


def safe_arith(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and len(n.args) == 1 and not n.keywords:
            return _FUNCS[n.func.id](ev(n.args[0]))
        raise ValueError(f"unsupported expression node {type(n).__name__}")
    return ev(ast.parse(expr, mode="eval"))


def difficulty_ok(features: set, task_type: str) -> bool:
    """SPEC section 2: at least 2 valid criteria. E4 is recorded but measures length, so it does not count."""
    return features <= FEATURES[task_type] and len(features - {"E4"}) >= 2


def _trusted_passes(task: dict, field: str) -> int:
    """Tests passed by author-written code, or -1 if it can't be loaded. A broken task is a failed
    check, not a crash, so --validate still lists everything else that is wrong."""
    try:
        return runner.run_trusted_reference(task[field], task["function_name"], task["tests"])
    except Exception:
        return -1


def validate() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    add = lambda name, ok, detail="": checks.append((name, bool(ok), detail))
    tasks = load_tasks()
    v1 = json.loads(runner.TASKS.read_text())["tasks"]
    draft = json.loads(DRAFT_TASKS.read_text())["tasks"]
    ids = [t["id"] for t in tasks]
    add("40 tasks", len(tasks) == 40, str(len(tasks)))
    add("unique ids", len(set(ids)) == len(ids))
    add("the 16 v1 tasks are unchanged", tasks[:len(v1)] == v1)
    add("the new tasks match the draft file", tasks[len(v1):] == draft)
    for ty in TYPES:
        add(f"10 tasks of type {ty}", sum(t["type"] == ty for t in tasks) == 10)
        add(f"new {ty} ids are {ty}_h1 to {ty}_h6", sorted(t["id"] for t in draft if t["type"] == ty) == [f"{ty}_h{i}" for i in range(1, 7)])
    add("every task has a requirement", all(str(t.get("requirement", "")).strip() for t in tasks))
    add("tasks/generic_v2.json is what --build-suite writes", TASKS.read_text() == suite_text())
    verdicts = [t["verdict"] for t in draft if t["type"] == "evidence_qa"]
    add("new evidence verdicts: 3 yes, 3 no", verdicts.count("yes") == 3 and verdicts.count("no") == 3)
    for t in tasks:
        tid, ty = t["id"], t["type"]
        add(f"{tid}: prompt has no 'Return only'", "return only" not in t["prompt"].lower())
        if t in draft:
            f = set(t.get("difficulty_features", []))
            add(f"{tid}: >=2 counting difficulty features, all valid for type", difficulty_ok(f, ty), ",".join(sorted(f)))
        if ty == "math":
            v = safe_arith(t["expression"])
            add(f"{tid}: expression == expected", abs(v - t["expected"]) < 1e-9, f"{v}")
        elif ty == "extraction":
            add(f"{tid}: passage verbatim in prompt", t["passage"] in t["prompt"])
            for a in t["accepted_in_passage"]:
                add(f"{tid}: accepted '{a}' in passage", a in t["passage"])
                add(f"{tid}: '{a}' scores pass", scoring_v2.score(t, a)["pass"] == 1)
            for d in t.get("distractors_in_passage", t.get("distractors", [])):
                add(f"{tid}: distractor '{d}' in passage", d in t["passage"])
                if t in draft:
                    add(f"{tid}: answer + distractor '{d}' fails",
                        scoring_v2.score(t, f"{t['accepted_in_passage'][0]}, {d}")["pass"] == 0)
            add(f"{tid}: echoing the passage fails", scoring_v2.score(t, t["passage"])["pass"] == 0)
        elif ty == "evidence_qa":
            add(f"{tid}: passage verbatim in prompt", t["passage"] in t["prompt"])
            for s in t["support_spans"]:
                add(f"{tid}: span '{s}' in passage", s.lower() in t["passage"].lower())
                add(f"{tid}: answer with span scores pass",
                    scoring_v2.score(t, f"{t['verdict'].capitalize()}. \"{s}\"")["pass"] == 1)
            wrong = "no" if t["verdict"] == "yes" else "yes"
            add(f"{tid}: wrong verdict + span fails",
                scoring_v2.score(t, f"{wrong.capitalize()}. \"{t['support_spans'][0]}\"")["pass"] == 0)
        elif ty == "coding":
            n = len(t["tests"])
            add(f"{tid}: function name in prompt", t["function_name"] in t["prompt"])
            if t in draft:
                add(f"{tid}: 5-8 tests", 5 <= n <= 8, str(n))
            add(f"{tid}: tests JSON-safe", json.loads(json.dumps(t["tests"])) == t["tests"])
            add(f"{tid}: reference passes {n}/{n}", _trusted_passes(t, "reference_solution") == n)
            add(f"{tid}: buggy fails >=1 test", 0 <= _trusted_passes(t, "buggy_solution") < n)
            add(f"{tid}: reference passes static policy", scoring.policy_check(t["reference_solution"])[0])
        if ty != "coding":
            add(f"{tid}: canonical answer scores pass, no contract tag",
                (lambda r: r["pass"] == 1 and r["components"]["contract_violation"] is None)(scoring_v2.score(t, runner.synthetic_response(t))))
    router_txt = ROUTER_PROMPT.read_text()
    add("router prompt equals the text in SPEC section 3", f"```\n{router_txt.strip()}\n```" in SPEC.read_text())
    for ty in TYPES:
        add(f"router prompt names {ty}", f"{ty}:" in router_txt)
        add(f"parser accepts {ty}", parse_router(ty) == ty)
        add(f"specialist prompt {ty}.txt is the v1 file", runner.prompt_path(ty).parent.name == "generic_v1")
    order = execution_order(DEFAULT_ORDER_SEED)
    add("order has 200 cells", len(order["cells"]) == 200, str(len(order["cells"])))
    add("order is balanced across the suite", set(order["balance"]["per_position_overall"]) == {10})
    return checks


# ------------------------------------------------------------------ router parser

def parse_router(raw) -> str | None:
    """SPEC section 3: trim, lowercase, drop one pair of quotes or backticks and one trailing period.
    The result must be exactly one label; anything else is None (router_unparseable)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    had_period = s.endswith(".")
    s = s[:-1] if had_period else s
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1]
    if s.endswith(".") and not had_period:
        s = s[:-1]
    return s if s in TYPES else None


# ------------------------------------------------------------------ ordering

def execution_order(seed: int) -> dict:
    """Cross block, then router block. Each task's 4 cross cells run back to back, in a row of a
    balanced Latin square. 10 tasks do not divide evenly by 4, so within a type a destination takes a
    position 2 or 3 times; the types' remainders offset each other, so across the suite it is exactly 10."""
    rng = random.Random(seed)
    tasks = load_tasks()
    dests = list(TYPES)
    rng.shuffle(dests)
    row_of: dict[str, tuple] = {}
    for i, ty in enumerate(TYPES):
        ids = sorted(t["id"] for t in tasks if t["type"] == ty)
        rng.shuffle(ids)
        for j, tid in enumerate(ids):
            row_of[tid] = WILLIAMS[(j + 2 * i) % 4]
    type_of = {t["id"]: t["type"] for t in tasks}
    cross_ids = sorted(type_of)
    rng.shuffle(cross_ids)
    cells = []
    for tid in cross_ids:
        for pos in row_of[tid]:
            d = dests[pos]
            cells.append({"seq": len(cells) + 1, "task_id": tid, "condition": condition_for(type_of[tid], d),
                          "selected_destination": d})
    router_ids = sorted(type_of)
    rng.shuffle(router_ids)
    for tid in router_ids:
        cells.append({"seq": len(cells) + 1, "task_id": tid, "condition": "router", "selected_destination": None})
    counts = {d: [0, 0, 0, 0] for d in TYPES}
    for tid in cross_ids:
        for p, x in enumerate(row_of[tid]):
            counts[dests[x]][p] += 1
    return {"method": "cross block: balanced Latin square rows per task, assigned per type from a seeded shuffle, task order "
                      "seeded; a task's cross cells run consecutively. Router block follows, in its own seeded order.",
            "seed": seed, "rng": f"python random.Random (Python {platform.python_version()})", "cells": cells,
            "balance": {"destination_by_position": counts, "per_position_overall": sorted({c for v in counts.values() for c in v})}}


# ------------------------------------------------------------------ rows

ROW_TYPES = ("warmup", "worker", "router")
ROUTER_OUTCOMES = (None, "parsed", "router_unparseable", "router_call_failed")


def base_row(run_id, stage, task, cond, dest, row_n, row_type="worker") -> dict:
    """The v1 row plus row_type and router_outcome. A router row has its own call_id ('R' prefix), the
    router prompt as its system prompt, and no destination until its output is parsed."""
    row = runner.base_row(run_id, stage, task, cond, dest or task["type"], row_n)
    row.update(schema_version=SCHEMA_VERSION, row_type=row_type, router_outcome=None, scorer_version=scoring_v2.SCORER_VERSION,
               scorer_sha256=sha256_file(ROOT / "farex_pilot" / "scoring_v2.py"))
    if row_type == "router":
        call_id = f"{run_id}-R{row_n:03d}"
        row.update(call_id=call_id, row_id=f"{call_id}-a1", selected_destination=None, label_agreement=None,
                   system_prompt_file=rel(ROUTER_PROMPT), system_prompt_sha256=sha256_file(ROUTER_PROMPT))
    return row


def validate_row(row: dict) -> list[str]:
    errs = [e for e in runner.validate_row(row) if e != "bad condition"]
    rt = row.get("row_type")
    if rt not in ROW_TYPES:
        errs.append(f"bad row_type {rt}")
    if rt == "router":
        errs = [e for e in errs if e not in ("selected_destination: expected str", "label_agreement: expected bool")]
        if row.get("condition") != "router":
            errs.append("router row must have condition 'router'")
        if row.get("scored") or row.get("pass") is not None:
            errs.append("router row must not be scored")
    elif rt == "worker":
        c, ty = row.get("condition"), row.get("task_type")
        if ty not in TYPES or (c != "router" and c not in all_conditions(ty)):
            errs.append("bad condition")
        elif c not in ("router", condition_for(ty, row.get("selected_destination"))):
            errs.append("condition does not match selected_destination")
    if row.get("router_outcome") not in ROUTER_OUTCOMES:
        errs.append(f"bad router_outcome {row.get('router_outcome')}")
    return errs


# ------------------------------------------------------------------ hashes and freeze

def code_files() -> list[Path]:
    """Every module and test file, found by glob, so a new file can't run outside the freeze unnoticed."""
    return [*sorted((ROOT / "farex_pilot").glob("*.py")), *sorted((ROOT / "tests").glob("test_*.py"))]


def frozen_hashes() -> dict:
    files = [TASKS, DRAFT_TASKS, *[runner.prompt_path(t) for t in TYPES], ROUTER_PROMPT, SPEC, *code_files()]
    return {rel(f): sha256_file(f) for f in files}


def execution_config() -> dict:
    return dict(runner.execution_config(), order_seed=DEFAULT_ORDER_SEED, scorer_version=scoring_v2.SCORER_VERSION,
                base_scorer_version=scoring.SCORER_VERSION, review_fraction=REVIEW_FRACTION,
                expected_chat_calls=EXPECTED_CHAT_CALLS,
                stop_policy="the first timeout on any call stops the run; the manifest is written with aborted=true")


def order_file() -> Path:
    return ART / "stage3_prep" / "execution_order.json"


def prepare(seed: int = DEFAULT_ORDER_SEED) -> Path:
    p = order_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(execution_order(seed), indent=2) + "\n")
    return p


def freeze_record(ver: dict) -> dict:
    hashes = frozen_hashes()
    of = order_file()
    return {"suite": "generic_v2", "status": "frozen", "frozen_at": runner.now(), "hashes": hashes, "covered_files": sorted(hashes),
            "execution": execution_config(), "order_sha256": sha256_file(of) if of.exists() else None,
            "sandbox_verification": {k: ver.get(k) for k in ("status", "assertions_passed", "assertions_total", "image", "recorded")},
            "scorer_validity": "NOT established for the new tasks: human review of Stage 3 output is step 3.6",
            "note": "Stage 3 re-checks every hash, the execution config, the order file, the model identity and the live "
                    "Ollama digest, and re-runs sandbox verification, before any model call."}


def freeze(args) -> Path:
    """Refuses without owner approval, a clean validation, a matching sandbox record and the frozen seed's order."""
    if not args.owner_approved:
        raise SystemExit("refusing to freeze: needs --owner-approved")
    if FREEZE.exists():
        raise SystemExit(f"refusing to freeze: {FREEZE.name} already exists; move it to freeze/superseded/ to re-freeze deliberately")
    bad = [n for n, ok, _ in validate() if not ok]
    if bad:
        raise SystemExit(f"refusing to freeze: validation failed: {bad}")
    sv = runner.SANDBOX_VERIFY
    if not sv.exists():
        raise SystemExit("refusing to freeze: no recorded sandbox verification (run --verify-sandbox)")
    ver = json.loads(sv.read_text())
    cur = {rel(f): sha256_file(f) for f in [ROOT / "farex_pilot" / "sandbox.py", ROOT / "farex_pilot" / "sandbox_probes.py"]}
    if ver.get("status") != "verified" or ver.get("sandbox_file_hashes") != cur or ver.get("image") != sandbox.PINNED_IMAGE:
        raise SystemExit("refusing to freeze: sandbox verification missing, failed, or made for different code or image")
    of = order_file()
    if not of.exists() or json.loads(of.read_text()).get("cells") != execution_order(DEFAULT_ORDER_SEED)["cells"]:
        raise SystemExit("refusing to freeze: order file missing or not produced by the frozen seed (run --prepare)")
    write_atomic(FREEZE, json.dumps(freeze_record(ver), indent=2) + "\n")
    return FREEZE


def preflight(args) -> dict:
    """Every check that must hold before the first model call. Raises SystemExit on any mismatch."""
    if not args.owner_approved:
        raise SystemExit("refusing: Stage 3 needs --owner-approved (explicit owner sign-off)")
    if args.stage != 3:
        raise SystemExit("refusing: --run needs --stage 3")
    fz = json.loads(FREEZE.read_text()) if FREEZE.exists() else {}
    if fz.get("status") != "frozen":
        raise SystemExit(f"refusing: suite not frozen (status {fz.get('status')!r})")
    cur = frozen_hashes()
    if fz.get("hashes") != cur:
        changed = sorted(k for k in set(cur) | set(fz.get("hashes") or {}) if (fz.get("hashes") or {}).get(k) != cur.get(k))
        raise SystemExit(f"refusing: files changed since freeze: {changed}")
    if fz.get("execution") != execution_config():
        raise SystemExit("refusing: execution configuration differs from the frozen configuration")
    of = order_file()
    if not of.exists() or not fz.get("order_sha256") or sha256_file(of) != fz["order_sha256"]:
        raise SystemExit("refusing: execution order file missing or its hash differs from the freeze")
    order = json.loads(of.read_text())
    if order.get("cells") != execution_order(fz["execution"]["order_seed"])["cells"]:
        raise SystemExit("refusing: execution order file is not the order the frozen seed produces")
    model = runner.SELECTED_MODEL
    if args.model != model["name"]:
        raise SystemExit(f"refusing: --model {args.model!r} is not the frozen model {model['name']!r}")
    if (args.model_digest or "").lower() != model["manifest_sha256"]:
        raise SystemExit("refusing: --model-digest is not the full frozen manifest digest")
    override = os.environ.get("FAREX_SANDBOX_IMAGE")
    if override and override != sandbox.PINNED_IMAGE:
        raise SystemExit("refusing: FAREX_SANDBOX_IMAGE overrides the frozen sandbox image")
    if sandbox.PINNED_IMAGE != fz["execution"]["sandbox_image"]:
        raise SystemExit("refusing: pinned sandbox image differs from the frozen image")
    base = os.environ.get("FAREX_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    if not runner._is_local_url(base):
        raise SystemExit("refusing: Stage 3 is local-Ollama only (http://127.0.0.1, localhost or [::1])")
    observed = runner.local_model_digest(base, args.model)
    if observed != model["manifest_sha256"]:
        raise SystemExit("refusing: local Ollama model digest differs from the frozen digest")
    backend = sandbox.default_backend()
    if backend.image != fz["execution"]["sandbox_image"]:
        raise SystemExit("refusing: sandbox backend image differs from the frozen image")
    ver = backend.verify_isolation()
    if not backend.verified or ver.get("status") != "verified":
        raise SystemExit(f"refusing: sandbox verification failed or unavailable ({ver.get('status')}: "
                         f"{backend.detail or ver.get('reason', '')})")
    return {"backend": backend, "order": order, "base": base, "observed_model_digest": observed,
            "sandbox_verification": {k: ver.get(k) for k in ("status", "assertions_passed", "assertions_total", "image")}}


# ------------------------------------------------------------------ execution

class _Stopped(Exception):
    def __init__(self, row):
        self.row = row


def ollama_ps(base: str) -> dict:
    """GET /api/ps after a timeout: what Ollama still has loaded. Metadata only; a loaded model is not
    proof that it is still generating, so the abort note records this as observed."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=10) as r:
            raw = r.read(runner.MAX_TAGS_BYTES + 1)
        models = json.loads(raw).get("models") or []
        return {"loaded_models": [m.get("name") for m in models if isinstance(m, dict)]}
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, AttributeError) as e:
        return {"error": type(e).__name__}


def ollama_answer(base: str, model: str, opts: dict):
    """One attempt, plus one retry after RETRY_DELAY_S on connection refused, reset or aborted only.
    Same protocol as the v1 runner; there it is a closure inside the frozen run_stage2."""
    def answer(row, system, user) -> list[dict]:
        out = []
        for attempt in (1, 2):
            r = dict(row, attempt=attempt, row_id=f"{row['call_id']}-a{attempt}", timestamp=runner.now())
            try:
                raw, ms = runner._ollama_chat(base, model, system, user, opts)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as e:
                st, retry, cls = runner._classify_error(e)
                err = f"> {opts['timeout_s']} s" if st == "timeout" else f"{cls}: {str(e)[:200]}"
                out.append(dict(r, status=st, error=err, error_class=cls, retry_eligible=retry))
                if retry and attempt == 1:
                    runner._sleep(runner.RETRY_DELAY_S)
                    continue
                return out
            r["latency_ms"] = ms
            if len(raw) > runner.MAX_RAW_BYTES:
                out.append(dict(r, status="provider_oversized", error=f"body > {runner.MAX_RAW_BYTES} bytes; not parsed",
                                retry_eligible=False))
                return out
            try:
                j = json.loads(raw)
                content = j["message"]["content"]
                if not isinstance(content, str):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                out.append(dict(r, status="provider_malformed", error="response is not JSON with message.content",
                                retry_eligible=False))
                return out
            r.update(status="ok", response=content, response_source="model", retry_eligible=False,
                     **{k: j.get(k) for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")})
            if isinstance(j.get("prompt_eval_count"), int) and isinstance(j.get("eval_count"), int):
                r.update(prompt_tokens=j["prompt_eval_count"], completion_tokens=j["eval_count"], token_source="provider")
            out.append(r)
            return out
        return out
    return answer


def synthetic_answer(by_id: dict):
    """Dry run: canned correct answers, never model output. The router 'chooses' the author label."""
    def answer(row, system, user) -> list[dict]:
        if row["row_type"] == "warmup":
            resp = "OK"
        elif row["row_type"] == "router":
            resp = row["task_type"]
        else:
            resp = runner.synthetic_response(by_id[row["task_id"]])
        return [dict(row, status="dry_run", response=resp, response_source="synthetic_canned_reference_answer")]
    return answer


def execute(path: Path, run_id: str, stage: str, order: dict, answer, backend, fields: dict, m: dict, ps=None) -> Path:
    """Warm-up, then every cell of the order. Stops at the first timeout. The manifest is written on
    every exit, so an interrupted run still says how it ended."""
    by_id = {t["id"]: t for t in load_tasks()}
    router_system = ROUTER_PROMPT.read_text()
    completed = ("ok", "dry_run")
    fh = path.open("a")

    def emit(rows):
        for r in rows:
            errs = validate_row(r)
            if errs:
                raise SystemExit(f"schema errors in row {r.get('row_id')}: {errs}")
            runner.append_row(fh, r)
        timed_out = [r for r in rows if r["status"] == "timeout"]
        if timed_out:
            raise _Stopped(timed_out[0])

    abort = None
    try:
        first = order["cells"][0]["selected_destination"]
        warm = base_row(run_id, stage, None, None, first, 0, "warmup")
        warm.update(fields)
        emit(answer(warm, runner.prompt_path(first).read_text(), WARMUP_MESSAGE))
        for n, cell in enumerate(order["cells"], 1):
            task = by_id[cell["task_id"]]
            dest, router_info = cell["selected_destination"], {"type": "fixed", "model": None, "raw_output": None,
                                                               "latency_ms": None, "prompt_tokens": None, "completion_tokens": None}
            shown = None
            if cell["condition"] == "router":
                rr = base_row(run_id, stage, task, "router", None, n, "router")
                rr.update(fields, router=dict(router_info, type="llm", model=fields.get("model")))
                attempts = answer(rr, router_system, task["prompt"])
                final = attempts[-1]
                dest = parse_router(final["response"]) if final["status"] in completed else None
                final.update(selected_destination=dest, label_agreement=None if dest is None else dest == task["type"],
                             router_outcome=("router_call_failed" if final["status"] not in completed
                                             else "parsed" if dest else "router_unparseable"))
                emit(attempts)
                shown = final["status"]
                router_info = dict(rr["router"], raw_output=final["response"], latency_ms=final["latency_ms"],
                                   prompt_tokens=final["prompt_tokens"], completion_tokens=final["completion_tokens"])
            if dest is not None:
                row = base_row(run_id, stage, task, cell["condition"], dest, n)
                row.update(fields, router=router_info)
                attempts = answer(row, runner.prompt_path(dest).read_text(), task["prompt"])
                final = attempts[-1]
                if final["status"] in completed:
                    final.update(scoring_v2.score(task, final["response"], backend))
                emit(attempts)
                shown = final["status"]
            print(f"{n:3d}/{len(order['cells'])} {cell['task_id']:<16} {shown}")     # no condition or score printed
    except _Stopped as s:
        abort = {"reason": "timeout", "row_id": s.row["row_id"], "ollama_ps": None}     # set first: the probe must not lose this
        if ps:
            try:
                abort["ollama_ps"] = ps()
            except Exception as e:
                abort["ollama_ps"] = {"error": type(e).__name__}
        print(f"STOPPED: timeout on {s.row['row_id']}; no further calls were made")
    except BaseException as e:
        abort = {"reason": type(e).__name__, "detail": str(e)[:300]}
        raise
    finally:
        fh.close()
        m.update(ended=runner.now(), aborted=abort is not None, abort=abort)
        write_atomic(path.with_name(path.name.replace(".jsonl", ".manifest.json")), json.dumps(m, indent=2) + "\n")
    return path


def manifest(run_id, stage, order, extra=None) -> dict:
    m = {"run_id": run_id, "suite": "generic_v2", "stage": stage, "dry_run": stage == "dry_run", "started": runner.now(), "ended": None,
         "aborted": None, "abort": None, "git": runner.git_state(), "hashes": frozen_hashes(), "python": platform.python_version(),
         "os": f"{platform.system()} {platform.release()}", "scorer_version": scoring_v2.SCORER_VERSION,
         "execution_order": {k: order[k] for k in ("method", "seed", "rng", "balance")}}
    m.update(extra or {})
    return m


def dry_run(seed: int = DEFAULT_ORDER_SEED, verify: bool = False) -> Path:
    """241 rows through the real execution path with canned answers. No model call. verify=True runs
    sandbox verification first, so the reference solutions go through the real containers."""
    out_dir = ART / "dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = now_id("DRYRUN")
    order = execution_order(seed)
    backend = sandbox.default_backend()
    if verify and backend.status != "blocked":
        backend.verify_isolation()
    m = manifest(run_id, "dry_run", order, {"sandbox": {"backend": backend.name, "status": backend.status, "detail": backend.detail},
                                            "note": "DRY RUN: no model calls; responses are synthetic canned answers, not results."})
    return execute(out_dir / f"generic_v2.{run_id}.jsonl", run_id, "dry_run", order, synthetic_answer({t["id"]: t for t in load_tasks()}),
                   backend, {"provider": "none (dry run)", "options": dict(runner.STAGE2_OPTIONS)}, m)


def run(args) -> Path:
    ctx = preflight(args)               # nothing below runs unless every check passed
    backend, order, base = ctx["backend"], ctx["order"], ctx["base"]
    run_id = now_id("S3")
    out_dir = ART / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = dict(runner.STAGE2_OPTIONS)
    digest = runner.SELECTED_MODEL["manifest_sha256"]
    m = manifest(run_id, "stage3", order, {
        "model": args.model, "model_digest_frozen": digest, "model_digest_observed_local": ctx["observed_model_digest"], "options": opts,
        "ollama_url": base, "order_file": rel(order_file()), "order_sha256": sha256_file(order_file()),
        "sandbox": {"backend": backend.name, "status": backend.status, "verification": ctx["sandbox_verification"]}})
    path = execute(out_dir / f"generic_v2.{run_id}.jsonl", run_id, "stage3", order, ollama_answer(base, args.model, opts), backend,
                   {"provider": "ollama", "model": args.model, "model_digest": digest, "options": opts}, m, ps=lambda: ollama_ps(base))
    print(rel(path))
    return path


# ------------------------------------------------------------------ cli

def main(argv=None) -> int:
    from . import stage3_report as rep
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--validate", action="store_true")
    g.add_argument("--build-suite", action="store_true")
    g.add_argument("--prepare", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--verify-sandbox", action="store_true")
    g.add_argument("--freeze", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--status", type=Path)
    g.add_argument("--review-sheet", type=Path)
    g.add_argument("--join-review", nargs=2, type=Path, metavar=("RESULTS", "SHEET"))
    g.add_argument("--report", type=Path)
    ap.add_argument("--stage", type=int)
    ap.add_argument("--order-seed", type=int, default=DEFAULT_ORDER_SEED)
    ap.add_argument("--review-seed", type=int)
    ap.add_argument("--second-reviewer", action="store_true")
    ap.add_argument("--second-sheet", type=Path)
    ap.add_argument("--unblind", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--model-digest")
    ap.add_argument("--owner-approved", action="store_true")
    a = ap.parse_args(argv)
    if a.validate:
        checks = validate()
        for name, ok, detail in checks:
            if not ok:
                print(f"FAIL  {name}" + (f"  [{detail}]" if detail else ""))
        bad = sum(not ok for _, ok, _ in checks)
        print(f"{len(checks) - bad}/{len(checks)} checks passed")
        return 1 if bad else 0
    if a.build_suite:
        print(rel(build_suite()))
    elif a.prepare:
        print(rel(prepare(a.order_seed)))
    elif a.dry_run:
        print(rel(dry_run(a.order_seed, verify=True)))
    elif a.verify_sandbox:
        print(rel(runner.verify_sandbox()))
    elif a.freeze:
        print(rel(freeze(a)))
    elif a.run:
        run(a)
    elif a.status:
        print(rep.status(a.status))
    elif a.review_sheet:
        print(*[rel(p) for p in rep.review_sheet(a.review_sheet, a.review_seed, a.second_reviewer)])
    elif a.join_review:
        print(rel(rep.join_review(a.join_review[0], a.join_review[1], a.second_sheet)))
    elif a.report:
        print(rel(rep.report(a.report, a.unblind)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
