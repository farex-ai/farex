"""generic_v1 routing pilot runner (PLAN.md sections D-F).

    python3 -m farex_pilot.runner --validate
    python3 -m farex_pilot.runner --dry-run
    python3 -m farex_pilot.runner --prepare-stage2 [--order-seed N]
    python3 -m farex_pilot.runner --review-sheet RESULTS.jsonl
    python3 -m farex_pilot.runner --report RESULTS.jsonl
    python3 -m farex_pilot.runner --freeze-draft
    python3 -m farex_pilot.runner --run --stage 2 ...   (NOT authorized yet; guarded)

Standard library only. Never reads any .env file. Every path is relative to the
pilot root; no absolute paths are written to artifacts.
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import http.client
import json
import operator
import os
import platform
import random
import secrets
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import uuid
from collections import defaultdict
from pathlib import Path

from . import scoring, sandbox

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks" / "generic_v1.json"
PROMPTS = ROOT / "prompts" / "generic_v1"
ART = ROOT / "artifacts"
FREEZE = ROOT / "freeze" / "generic_v1.freeze.json"
SCHEMA_VERSION = "pilot-row-1.0.0"

TYPES = ("coding", "math", "extraction", "evidence_qa")
FIXED_WRONG = {"coding": "math", "math": "extraction", "extraction": "evidence_qa", "evidence_qa": "coding"}
ROLE_KEYWORD = {"coding": "coding expert", "math": "math expert",
                "extraction": "information extractor", "evidence_qa": "qa expert who uses evidence"}
MAX_RAW_BYTES = 64 * 1024   # HTTP body cap, checked before json.loads
MAX_TAGS_BYTES = 1024 * 1024  # /api/tags metadata body cap
RETRY_DELAY_S = 5
STAGE2_OPTIONS = {"temperature": 0, "seed": 1234, "num_predict": 512, "timeout_s": 120}
DEFAULT_ORDER_SEED = 20261031


# ------------------------------------------------------------------ io helpers

def rel(p: Path) -> str:
    """Path relative to the pilot root; never an absolute machine path."""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return f"<outside-pilot-root>/{Path(p).name}"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def load_tasks() -> list[dict]:
    return json.loads(TASKS.read_text())["tasks"]


def prompt_path(dest: str) -> Path:
    return PROMPTS / f"{dest}.txt"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def append_row(fh, row: dict) -> None:
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


SANDBOX_VERIFY = ART / "sandbox_verification" / "verification.json"
SELECTED_MODEL = {"name": "llama3:8b", "manifest_sha256": "365c0bd3c000a25d28ddbf732fe1c6add414de7275464c4e4d1c3b5fcb5d8ad1",
                  "config_sha256": "3f8eb4da87fa7a3c9da615036b0dc418d31fef2a30b115ff33562588b32c691d",
                  "selection_reason": "continuity with the historical runs; not a claim that it is optimal"}


def code_files() -> list[Path]:
    pkg = ROOT / "farex_pilot"
    return [pkg / "__init__.py", pkg / "scoring.py", pkg / "sandbox.py", pkg / "sandbox_probes.py", pkg / "runner.py",
            ROOT / "tests" / "test_scoring.py", ROOT / "tests" / "test_sandbox_probes.py",
            ROOT / "tests" / "test_stage2_entry.py"]


def frozen_hashes() -> dict:
    files = [TASKS, *[prompt_path(t) for t in TYPES], *code_files()]
    return {rel(f): sha256_file(f) for f in files}


def execution_config() -> dict:
    return {"model": SELECTED_MODEL, "options": STAGE2_OPTIONS, "order_seed": DEFAULT_ORDER_SEED,
            "max_response_chars": scoring.MAX_RESPONSE_CHARS, "max_bracket_depth": scoring.MAX_BRACKET_DEPTH,
            "max_raw_bytes": MAX_RAW_BYTES, "sandbox_image": sandbox.PINNED_IMAGE,
            "scorer_version": scoring.SCORER_VERSION, "harness_protocol": sandbox.HARNESS_PROTOCOL,
            "output_cap_bytes": sandbox.OUTPUT_CAP_BYTES,
            "retry_policy": "connection refused/reset/aborted: 1 retry after 5 s; timeout, HTTP error, "
                            "other transport errors, malformed or oversized body, scorer outcomes: none"}


def verify_sandbox() -> Path:
    """Run the runtime probes against the pinned image and record the result (no model calls)."""
    b = sandbox.default_backend()
    result = b.verify_isolation()
    sb_hashes = {rel(f): sha256_file(f) for f in [ROOT / "farex_pilot" / "sandbox.py", ROOT / "farex_pilot" / "sandbox_probes.py"]}
    data = {"recorded": now(), "status": result["status"], "image": result.get("image"),
            "assertions_passed": result.get("assertions_passed"), "assertions_total": result.get("assertions_total"),
            "runtime": result.get("runtime"), "sandbox_file_hashes": sb_hashes,
            "controls": {k: {"status": c["status"], "checks": [{"name": x["name"], "ok": x["ok"]} for x in c["checks"]]}
                         for k, c in result.get("controls", {}).items()}}
    SANDBOX_VERIFY.parent.mkdir(parents=True, exist_ok=True)
    SANDBOX_VERIFY.write_text(json.dumps(data, indent=2) + "\n")
    return SANDBOX_VERIFY


def order_file() -> Path:
    return ART / "stage2_prep" / "execution_order.json"


def freeze_record(ver: dict) -> dict:
    hashes = frozen_hashes()
    of = order_file()
    return {"suite": "generic_v1", "status": "frozen", "frozen_at": now(), "suite_version": "1.0.0",
            "hashes": hashes, "covered_files": sorted(hashes),
            "execution": execution_config(),
            "stage2_order_sha256": sha256_file(of) if of.exists() else None,
            "sandbox_verification": {"status": ver["status"], "assertions_passed": ver["assertions_passed"],
                                     "assertions_total": ver["assertions_total"], "image": ver["image"],
                                     "recorded": ver.get("recorded")},
            "scorer_validity": "NOT established: human review of real outputs is a Stage 2 activity",
            "note": "Stage 2 re-checks every hash, the execution config, the order file, the model identity and "
                    "the live Ollama digest, and re-runs sandbox verification, before any model call."}


def freeze() -> Path:
    """Write status 'frozen' only if every Stage 1 check passes: validation, the order file is the
    one the frozen seed produces, and a recorded sandbox verification that matches the current
    sandbox code. Otherwise refuse."""
    bad = [n for n, ok, _ in validate() if not ok]
    if bad:
        raise SystemExit(f"refusing to freeze: validation failed: {bad}")
    if not SANDBOX_VERIFY.exists():
        raise SystemExit("refusing to freeze: no recorded sandbox verification (run --verify-sandbox)")
    ver = json.loads(SANDBOX_VERIFY.read_text())
    cur = {rel(f): sha256_file(f) for f in [ROOT / "farex_pilot" / "sandbox.py", ROOT / "farex_pilot" / "sandbox_probes.py"]}
    if ver.get("status") != "verified" or ver.get("sandbox_file_hashes") != cur or ver.get("image") != sandbox.PINNED_IMAGE:
        raise SystemExit("refusing to freeze: sandbox verification missing, failed, or made for different code/image")
    of = order_file()
    if not of.exists() or json.loads(of.read_text()).get("cells") != execution_order(DEFAULT_ORDER_SEED)["cells"]:
        raise SystemExit("refusing to freeze: execution order file missing or not produced by the frozen seed "
                         "(run --prepare-stage2)")
    FREEZE.write_text(json.dumps(freeze_record(ver), indent=2) + "\n")
    return FREEZE


def git_state() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        if commit.returncode != 0:
            return {"commit": None, "dirty": None, "note": "not a git repository"}
        return {"commit": commit.stdout.strip(), "dirty": bool(dirty.stdout.strip())}
    except OSError:
        return {"commit": None, "dirty": None, "note": "git unavailable"}


# ------------------------------------------------------------------ validation

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def safe_arith(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        raise ValueError(f"unsupported expression node {type(n).__name__}")
    return ev(ast.parse(expr, mode="eval"))


def run_trusted_reference(src: str, fn_name: str, tests: list[dict]) -> int:
    """In-process run of AUTHOR-WRITTEN reference/buggy solutions from the task
    file. Never used for model output (that goes only through sandbox.py)."""
    ns: dict = {}
    exec(compile(src, "<trusted-reference>", "exec"), ns)
    fn = ns[fn_name]
    passed = 0
    for c in tests:
        try:
            got = fn(*json.loads(json.dumps(c["args"])))
            exp = c["expected"]
            passed += (got == exp) and (not isinstance(exp, bool) or isinstance(got, bool))
        except Exception:
            pass
    return passed


def validate() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    add = lambda name, ok, detail="": checks.append((name, bool(ok), detail))
    tasks = load_tasks()
    ids = [t["id"] for t in tasks]
    add("16 tasks", len(tasks) == 16, str(len(tasks)))
    add("unique ids", len(set(ids)) == len(ids))
    for ty in TYPES:
        add(f"4 tasks of type {ty}", sum(t["type"] == ty for t in tasks) == 4)
    for t in tasks:
        tid = t["id"]
        add(f"{tid}: prompt has no 'Return only'", "return only" not in t["prompt"].lower())
        if t["type"] == "math":
            v = safe_arith(t["expression"])
            add(f"{tid}: expression {t['expression']} == expected", abs(v - t["expected"]) < 1e-9, f"{v}")
            add(f"{tid}: canonical answer scores pass", scoring.score_math(t, str(t["expected"]))["pass"] == 1)
        elif t["type"] == "extraction":
            add(f"{tid}: passage verbatim in prompt", t["passage"] in t["prompt"])
            for a in t["accepted_in_passage"]:
                add(f"{tid}: accepted '{a}' in passage", a in t["passage"])
                add(f"{tid}: '{a}' scores pass", scoring.score_extraction(t, a)["pass"] == 1)
            for d in t.get("distractors", t.get("distractors_in_passage", [])):
                add(f"{tid}: distractor '{d}' in passage", d in t["passage"])
            add(f"{tid}: echoing the passage fails", scoring.score_extraction(t, t["passage"])["pass"] == 0)
        elif t["type"] == "evidence_qa":
            add(f"{tid}: passage verbatim in prompt", t["passage"] in t["prompt"])
            for s in t["support_spans"]:
                add(f"{tid}: span '{s}' in passage", s.lower() in t["passage"].lower())
                ok = scoring.score_evidence_qa(t, f"{t['verdict'].capitalize()}. \"{s}\"")["pass"] == 1
                add(f"{tid}: canonical answer with span scores pass", ok)
            wrong = "no" if t["verdict"] == "yes" else "yes"
            add(f"{tid}: wrong verdict + span fails",
                scoring.score_evidence_qa(t, f"{wrong.capitalize()}. \"{t['support_spans'][0]}\"")["pass"] == 0)
            add(f"{tid}: passage/background metadata present",
                all(k in t for k in ("answer_from_passage_only", "background_knowledge", "answerable_from_world_knowledge")))
        elif t["type"] == "coding":
            n = len(t["tests"])
            add(f"{tid}: function name in prompt", t["function_name"] in t["prompt"])
            add(f"{tid}: reference passes {n}/{n} (trusted, in-process)",
                run_trusted_reference(t["reference_solution"], t["function_name"], t["tests"]) == n)
            add(f"{tid}: buggy solution fails >=1 test (trusted, in-process)",
                run_trusted_reference(t["buggy_solution"], t["function_name"], t["tests"]) < n)
            add(f"{tid}: reference passes static policy", scoring.policy_check(t["reference_solution"])[0])
    for ty in TYPES:
        p = prompt_path(ty)
        txt = p.read_text().lower()
        add(f"prompt {ty}.txt has role keyword '{ROLE_KEYWORD[ty]}'", ROLE_KEYWORD[ty] in txt, sha256_file(p))
        others = [k for o, k in ROLE_KEYWORD.items() if o != ty and k in txt]
        add(f"prompt {ty}.txt has no other role keyword", not others, ",".join(others))
    add("fixed-wrong map is a derangement", all(FIXED_WRONG[t] != t for t in TYPES) and sorted(FIXED_WRONG.values()) == sorted(TYPES))
    return checks


# ------------------------------------------------------------------ ordering

def execution_order(seed: int) -> dict:
    """Counterbalanced order. Within each type, 2 of 4 tasks (chosen by the
    seeded RNG) run reference-first, the other 2 fixed-wrong-first. Task order
    is then shuffled with the same RNG. Both cells of a task run back to back."""
    rng = random.Random(seed)
    tasks = load_tasks()
    first: dict[str, str] = {}
    for ty in TYPES:
        ids = sorted(t["id"] for t in tasks if t["type"] == ty)
        ref_first = set(rng.sample(ids, 2))
        for i in ids:
            first[i] = "reference" if i in ref_first else "fixed_wrong"
    order_ids = [t["id"] for t in tasks]
    rng.shuffle(order_ids)
    by_id = {t["id"]: t for t in tasks}
    cells = []
    for tid in order_ids:
        conds = ["reference", "fixed_wrong"] if first[tid] == "reference" else ["fixed_wrong", "reference"]
        for c in conds:
            ty = by_id[tid]["type"]
            cells.append({"seq": len(cells) + 1, "task_id": tid, "condition": c,
                          "selected_destination": ty if c == "reference" else FIXED_WRONG[ty]})
    return {
        "method": "seeded counterbalancing: per type, random 2/4 tasks reference-first, rest fixed-wrong-first; "
                  "task order shuffled with the same random.Random(seed); a task's two cells run consecutively",
        "seed": seed, "rng": f"python random.Random (Python {platform.python_version()})",
        "cells": cells,
        "balance": {c: sum(1 for i, x in enumerate(cells) if i % 2 == 0 and x["condition"] == c) for c in ("reference", "fixed_wrong")},
    }


# ------------------------------------------------------------------ rows

def base_row(run_id, stage, task, cond, dest, row_n, attempt=1) -> dict:
    """One attempt row. `call_id` is shared by every attempt of one logical call; `row_id` is unique
    per attempt. task=None builds the warm-up row (condition 'warmup', no task fields)."""
    sp = prompt_path(dest)
    call_id = f"{run_id}-{'W' if task is None else ''}{row_n:03d}"
    warm = task is None
    return {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "call_id": call_id, "row_id": f"{call_id}-a{attempt}",
        "timestamp": now(), "stage": stage, "attempt": attempt, "is_warmup": warm, "dry_run": stage == "dry_run",
        "task_id": "__warmup__" if warm else task["id"], "task_type": None if warm else task["type"],
        "task_sha256": None if warm else sha256_text(json.dumps(task, sort_keys=True)),
        "condition": "warmup" if warm else cond, "reference_destination": None if warm else task["type"],
        "selected_destination": dest, "label_agreement": None if warm else dest == task["type"],
        "router": {"type": "fixed", "model": None, "raw_output": None, "latency_ms": None,
                   "prompt_tokens": None, "completion_tokens": None},
        "provider": None, "model": None, "model_digest": None,
        "system_prompt_file": rel(sp), "system_prompt_sha256": sha256_file(sp),
        "options": None, "response": None, "response_source": None, "status": None, "error": None,
        "error_class": None, "retry_eligible": None,
        "latency_ms": None, "total_duration": None, "load_duration": None, "prompt_eval_duration": None,
        "eval_duration": None, "prompt_tokens": None, "completion_tokens": None, "token_source": "unavailable",
        "scored": False, "pass": None, "score_status": None, "components": {}, "reason": None,
        "scorer_version": scoring.SCORER_VERSION, "scorer_sha256": sha256_file(ROOT / "farex_pilot" / "scoring.py"),
    }


ROW_TYPES = {
    "schema_version": str, "run_id": str, "call_id": str, "row_id": str, "timestamp": str, "stage": str, "attempt": int,
    "is_warmup": bool, "dry_run": bool, "task_id": str, "task_type": str, "task_sha256": str,
    "condition": str, "reference_destination": str, "selected_destination": str, "label_agreement": bool,
    "router": dict, "system_prompt_file": str, "system_prompt_sha256": str, "status": str,
    "token_source": str, "scored": bool, "components": dict, "scorer_version": str, "scorer_sha256": str,
}
WARMUP_NULL = ("task_type", "task_sha256", "reference_destination", "label_agreement")
NULLABLE = ("provider", "model", "model_digest", "options", "response", "response_source", "error", "error_class",
            "retry_eligible", "latency_ms", "total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
            "prompt_tokens", "completion_tokens", "pass", "score_status", "reason")
# Every status the runner emits.
STATUSES = {"ok", "transport_error", "timeout", "http_error", "provider_malformed", "provider_oversized", "dry_run"}
SCORE_STATUSES = {None, "scored", "review_needed", "response_oversized", "response_malformed", "sandbox_unavailable"}


def validate_row(row: dict) -> list[str]:
    warm = row.get("is_warmup") is True
    errs = [f"{k}: expected {t.__name__}" for k, t in ROW_TYPES.items()
            if not isinstance(row.get(k), t) and not (warm and k in WARMUP_NULL and row.get(k) is None)]
    errs += [f"missing {k}" for k in NULLABLE if k not in row]
    if row.get("status") not in STATUSES:
        errs.append(f"bad status {row.get('status')}")
    if row.get("score_status") not in SCORE_STATUSES:
        errs.append(f"bad score_status {row.get('score_status')}")
    if warm:
        if row.get("condition") != "warmup" or row.get("task_id") != "__warmup__":
            errs.append("warm-up row must have condition 'warmup' and task_id '__warmup__'")
        if row.get("scored") or row.get("pass") is not None:
            errs.append("warm-up row must not be scored")
    elif row.get("condition") not in ("reference", "fixed_wrong"):
        errs.append("bad condition")
    if row.get("token_source") not in ("provider", "unavailable"):
        errs.append("bad token_source")
    if isinstance(row.get("row_id"), str) and isinstance(row.get("call_id"), str) \
            and row["row_id"] != f"{row['call_id']}-a{row.get('attempt')}":
        errs.append("row_id must be call_id + '-a' + attempt")
    if row.get("pass") not in (None, 0, 1) or (row.get("pass") is not None and not row.get("scored")):
        errs.append("pass must be None unless scored, and 0/1 when scored")
    if "cost" in json.dumps(list(row.keys())):
        errs.append("cost field present")
    return errs


def manifest(run_id, stage, order, extra=None) -> dict:
    m = {"run_id": run_id, "stage": stage, "dry_run": stage == "dry_run", "started": now(), "ended": None,
         "git": git_state(), "hashes": frozen_hashes(), "python": platform.python_version(),
         "os": f"{platform.system()} {platform.release()}", "scorer_version": scoring.SCORER_VERSION,
         "condition_map": {"reference": {t: t for t in TYPES}, "fixed_wrong": FIXED_WRONG},
         "execution_order": {k: order[k] for k in ("method", "seed", "rng")}}
    m.update(extra or {})
    return m


# ------------------------------------------------------------------ dry run

def synthetic_response(task: dict) -> str:
    """Canned correct answer per task. NOT model output; labelled as synthetic."""
    if task["type"] == "math":
        return str(task["expected"])
    if task["type"] == "extraction":
        return task["accepted_in_passage"][0]
    if task["type"] == "evidence_qa":
        return f"{task['verdict'].capitalize()}. \"{task['support_spans'][0]}\""
    return task["reference_solution"]


def dry_run(seed: int, verify_sandbox: bool = False) -> Path:
    """Writes 32 rows with status=dry_run. No model. Responses are the canned correct answers so the
    scoring/report path is exercised; every row says response_source='synthetic_canned_reference_answer'.
    verify_sandbox=True runs the real sandbox verification first, so coding cells exercise the
    real container path (the CLI does this); otherwise coding cells are sandbox_unavailable."""
    out_dir = ART / "dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"DRYRUN-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    order = execution_order(seed)
    by_id = {t["id"]: t for t in load_tasks()}
    backend = sandbox.default_backend()
    if verify_sandbox and backend.status != "blocked":
        backend.verify_isolation()
    path = out_dir / f"{run_id}.jsonl"
    with path.open("a") as fh:
        for n, cell in enumerate(order["cells"], 1):
            task = by_id[cell["task_id"]]
            row = base_row(run_id, "dry_run", task, cell["condition"], cell["selected_destination"], n)
            resp = synthetic_response(task)
            row.update(status="dry_run", response=resp, response_source="synthetic_canned_reference_answer",
                       options=STAGE2_OPTIONS, provider="none (dry run)")
            row.update(scoring.score(task, resp, backend))
            errs = validate_row(row)
            if errs:
                raise SystemExit(f"schema errors in row {n}: {errs}")
            append_row(fh, row)
    m = manifest(run_id, "dry_run", order, {"sandbox": {"backend": backend.name, "status": backend.status, "detail": backend.detail},
                                             "note": "DRY RUN: no model calls; responses are synthetic canned answers, not results."})
    m["ended"] = now()
    (out_dir / f"{run_id}.manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    return path


# ------------------------------------------------------------------ review sheet

def _load_rows(results: Path) -> list[dict]:
    return [json.loads(l) for l in results.read_text().splitlines() if l.strip()]


def _final_cells(rows: list[dict]) -> tuple[dict, dict]:
    """(final, attempts): per (task_id, condition), the highest attempt and all attempts in order."""
    attempts = defaultdict(list)
    for r in rows:
        if not r.get("is_warmup"):
            attempts[(r["task_id"], r["condition"])].append(r)
    for k in attempts:
        attempts[k].sort(key=lambda r: r.get("attempt") or 0)
    return {k: v[-1] for k, v in attempts.items()}, dict(attempts)


REVIEW_HEADER = ["review_id", "task_prompt", "what_counts_as_correct", "response",
                 "human_correct (1/0: the right answer/entity/verdict/behaviour, ignoring format and exclusions)",
                 "human_compliant (1/0: meets the format, exclusion or citation part of the requirement)",
                 "human_notes"]


def review_sheet(results: Path, seed: int | None = None) -> tuple[Path, Path]:
    rows = _load_rows(results)
    final, _ = _final_cells(rows)
    cells = list(final.values())
    if not cells:
        raise SystemExit("no experimental rows in results file")
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    cells.sort(key=lambda r: r["call_id"] if "call_id" in r else r["row_id"])   # deterministic before shuffle
    rng.shuffle(cells)
    by_id = {t["id"]: t for t in load_tasks()}
    run_id = cells[0]["run_id"]
    label = "DRYRUN-" if cells[0].get("dry_run") else ""
    rdir = ART / ("dry_run" if label else "review")   # dry-run sheets never sit next to real ones
    pdir = ART / "private"
    rdir.mkdir(parents=True, exist_ok=True)
    pdir.mkdir(parents=True, exist_ok=True)
    sheet = rdir / f"{label}review_sheet.{run_id}.csv"
    key = pdir / f"{label}review_key.{run_id}.json"
    mapping = {}
    with sheet.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(REVIEW_HEADER)
        for r in cells:
            rid = secrets.token_hex(4)
            mapping[rid] = {"row_id": r["row_id"], "call_id": r.get("call_id"), "task_id": r["task_id"],
                            "condition": r["condition"]}
            t = by_id[r["task_id"]]
            w.writerow([rid, t["prompt"], t["requirement"],
                        r["response"] if r["response"] is not None else "<no response>", "", "", ""])
    key.write_text(json.dumps({"run_id": run_id, "results_file": rel(results), "sheet": rel(sheet), "mapping": mapping}, indent=2) + "\n")
    return sheet, key


# ------------------------------------------------------------------ report

def _fmt(x):
    return "—" if x is None else (f"{x:.0f}" if isinstance(x, float) else str(x))


def _median(xs):
    return statistics.median(xs) if xs else None


def _sum_attempts(atts, key):
    """(total, complete): sum of `key` over attempts that have it; complete=False if any attempt lacks it."""
    vals = [a.get(key) for a in atts]
    have = [v for v in vals if v is not None]
    return (sum(have) if have else None), len(have) == len(vals)


def report(results: Path) -> Path:
    rows = _load_rows(results)
    warm = [r for r in rows if r.get("is_warmup")]
    final, attempts = _final_cells(rows)
    cells = list(final.values())
    if not cells:
        raise SystemExit("no experimental rows in results file")
    tasks = load_tasks()
    conds = ("reference", "fixed_wrong")
    expected_cells = {(t["id"], c) for t in tasks for c in conds}
    missing = sorted(expected_cells - set(final))
    dry = any(r.get("dry_run") for r in cells)
    run_id = cells[0]["run_id"]
    L = []
    if dry:
        L += ["> **DRY RUN — NOT RESULTS.** No model was called. Responses are synthetic canned answers.",
              "> Numbers below only show that the pipeline runs.", ""]
    L += [f"# generic_v1 pilot report — `{run_id}`", "",
          f"Cells present: {len(cells)}/{len(expected_cells)}. Missing cells: {len(missing)} "
          f"{[f'{t}/{c}' for t, c in missing]}", ""]
    L += ["## 1. Label agreement (routing) — separate from task success", ""]
    for c in conds:
        cc = [r for r in cells if r["condition"] == c]
        L.append(f"- {c}: {sum(bool(r['label_agreement']) for r in cc)}/{len(cc)}")
    L += ["", "## 2. Task success (full requirement; automated scorer, final attempt per cell)", "",
          "| condition | scored-only | unscored counted as fail | unscored | missing |", "|---|---|---|---|---|"]
    for c in conds:
        cc = [r for r in cells if r["condition"] == c]
        sc = [r for r in cc if r["scored"]]
        p = sum(r["pass"] or 0 for r in sc)
        L.append(f"| {c} | {p}/{len(sc)} | {p}/{len(cc)} | {len(cc) - len(sc)} | {sum(m[1] == c for m in missing)} |")
    L += ["", "## 2b. Correctness vs. compliance (extraction and evidence QA; automated components)", "",
          "`correct` = right entity/verdict; `compliant` = exclusion/citation part of the requirement. "
          "Counts are 1s over cells where the component is known.", "",
          "| condition | correct | compliant |", "|---|---|---|"]
    for c in conds:
        cc = [r for r in cells if r["condition"] == c and r["task_type"] in ("extraction", "evidence_qa")]
        cor = [r["components"].get("correct") for r in cc if r["components"].get("correct") is not None]
        com = [r["components"].get("compliant") for r in cc if r["components"].get("compliant") is not None]
        L.append(f"| {c} | {sum(cor)}/{len(cor)} (unknown {len(cc) - len(cor)}) | {sum(com)}/{len(com)} (unknown {len(cc) - len(com)}) |")
    L += ["", "## 3. Per type", "", "| type | reference | fixed_wrong (destination) |", "|---|---|---|"]
    for ty in TYPES:
        parts = []
        for c in conds:
            cc = [r for r in cells if r["task_type"] == ty and r["condition"] == c]
            sc = [r for r in cc if r["scored"]]
            parts.append(f"{sum(r['pass'] or 0 for r in sc)}/{len(sc)} scored ({len(cc) - len(sc)} unscored)")
        L.append(f"| {ty} | {parts[0]} | {parts[1]} (→{FIXED_WRONG[ty]}) |")
    L += ["", "## 4. Per task", "",
          "| task | ref pass | ref reason | ref attempts | wrong pass | wrong reason | wrong attempts |", "|---|---|---|---|---|---|---|"]
    disc = {"ref_only": [], "wrong_only": []}
    for t in sorted(tasks, key=lambda t: t["id"]):
        tid = t["id"]
        a, b = final.get((tid, "reference")), final.get((tid, "fixed_wrong"))
        def cell(x, k):
            return ("missing", "missing", "0") if x is None else (_fmt(x["pass"]), str(x["reason"] if x["reason"] else x["status"]),
                                                                 str(len(attempts[k])))
        ca, cb = cell(a, (tid, "reference")), cell(b, (tid, "fixed_wrong"))
        L.append(f"| {tid} | {ca[0]} | {ca[1]} | {ca[2]} | {cb[0]} | {cb[1]} | {cb[2]} |")
        if a and b and a["scored"] and b["scored"]:
            if a["pass"] and not b["pass"]:
                disc["ref_only"].append(tid)
            if b["pass"] and not a["pass"]:
                disc["wrong_only"].append(tid)
    L += ["", "## 5. Discordant pairs (both cells scored; n = 16, no significance claims)", "",
          f"- pass under reference, fail under fixed-wrong: {len(disc['ref_only'])} {disc['ref_only']}",
          f"- pass under fixed-wrong, fail under reference: {len(disc['wrong_only'])} {disc['wrong_only']}", ""]
    L += ["## 6. Latency and tokens (warm-up excluded)", "",
          "Final attempt = the attempt that supplied the outcome. Total = summed over all attempts of a cell; "
          "a total is marked partial when some attempt has no measurement. Missing = no value at all. "
          "Tokens are provider counts only.", "",
          "| condition | attempts | final latency ms median (n, missing) | total latency ms median (n, partial) | "
          "final completion tokens median (n, missing) | total completion tokens median (n, partial) |",
          "|---|---|---|---|---|---|"]
    for c in conds:
        keys = [k for k in attempts if k[1] == c]
        n_att = sum(len(attempts[k]) for k in keys)
        fl = [final[k]["latency_ms"] for k in keys if final[k]["latency_ms"] is not None]
        ft = [final[k]["completion_tokens"] for k in keys if final[k]["completion_tokens"] is not None]
        tl = [_sum_attempts(attempts[k], "latency_ms") for k in keys]
        tt = [_sum_attempts(attempts[k], "completion_tokens") for k in keys]
        tlv = [v for v, _ in tl if v is not None]
        ttv = [v for v, _ in tt if v is not None]
        L.append(f"| {c} | {n_att} | {_fmt(_median(fl))} ({len(fl)}, {len(keys) - len(fl)}) | "
                 f"{_fmt(_median(tlv))} ({len(tlv)}, {sum(1 for v, ok in tl if v is not None and not ok)}) | "
                 f"{_fmt(_median(ft))} ({len(ft)}, {len(keys) - len(ft)}) | "
                 f"{_fmt(_median(ttv))} ({len(ttv)}, {sum(1 for v, ok in tt if v is not None and not ok)}) |")
    wl = [r["latency_ms"] for r in warm if r["latency_ms"] is not None]
    L.append("")
    L.append(f"- warm-up attempts: {len(warm)}; latency {', '.join(_fmt(x) for x in wl) or '—'} ms (excluded from all analysis)")
    L += ["", "## 7. Transport status", "", "| status | attempts (all, excl. warm-up) | final attempts |", "|---|---|---|"]
    exp_rows = [r for r in rows if not r.get("is_warmup")]
    for st in sorted({r["status"] for r in exp_rows}):
        L.append(f"| {st} | {sum(r['status'] == st for r in exp_rows)} | {sum(r['status'] == st for r in cells)} |")
    retried = sorted(f"{k[0]}/{k[1]}" for k, v in attempts.items() if len(v) > 1)
    L += ["", f"- cells with a retry: {len(retried)} {retried}"]
    L += ["", "## 8. Unscored and sandbox outcomes (final attempt per cell)", "", "| score status | cells |", "|---|---|"]
    for st in sorted({str(r.get("score_status")) for r in cells}):
        L.append(f"| {st} | {sum(str(r.get('score_status')) == st for r in cells)} |")
    def ids(pred):
        x = sorted(f"{r['task_id']}/{r['condition']}" for r in cells if pred(r))
        return f"{len(x)} {x}"
    L += ["",
          f"- no model response (transport/provider failure): {ids(lambda r: r['status'] != 'ok' and not r.get('dry_run'))}",
          f"- review_needed: {ids(lambda r: r.get('score_status') == 'review_needed')}",
          f"- coding unscored, sandbox unavailable or sandbox error: {ids(lambda r: r.get('score_status') == 'sandbox_unavailable')}",
          f"  - of which sandbox infrastructure errors: {ids(lambda r: str(r.get('reason') or '').startswith('sandbox_error'))}",
          f"- policy rejections (scored fail, counted separately): {ids(lambda r: r['components'].get('policy_rejected'))}",
          f"- coding timeouts (scored fail): {ids(lambda r: r.get('reason') == 'timeout')}",
          f"- coding output limit exceeded (scored fail): {ids(lambda r: r.get('reason') == 'output_limit_exceeded')}",
          f"- coding harness result missing (scored fail): {ids(lambda r: r.get('reason') == 'no_harness_result')}",
          "", "## 9. Scorer audit", "", "Pending: human review sheet not yet joined to results.", "",
          "## 10. Limitations", "",
          "Same-model prompt-only routing; easy tasks (ceiling); one fixed wrong destination per type; author labels as "
          "reference; single model; n = 16; English-only short tasks; scorer format-tolerance choices (see scoring limitations "
          "in PLAN.md). A null result is a valid result.", ""]
    out = results.with_suffix(".report.md")
    out.write_text("\n".join(L))
    return out


# ------------------------------------------------------------------ freeze

def freeze_draft() -> Path:
    FREEZE.parent.mkdir(parents=True, exist_ok=True)
    data = {"suite": "generic_v1", "status": "draft_pending_owner_approval", "created": now(),
            "hashes": frozen_hashes(), "scorer_version": scoring.SCORER_VERSION,
            "note": "Stage 2 refuses to run unless status == 'frozen' and every hash matches."}
    FREEZE.write_text(json.dumps(data, indent=2) + "\n")
    return FREEZE


# ------------------------------------------------------------------ stage 2 (guarded; not executed in Stage 1)

def _sleep(seconds):
    time.sleep(seconds)


def _ollama_chat(base, model, system, user, options):
    import urllib.request
    body = json.dumps({"model": model, "stream": False, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {k: options[k] for k in ("temperature", "seed", "num_predict")}}).encode()
    req = urllib.request.Request(f"{base}/api/chat", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=options["timeout_s"]) as r:
        raw = r.read(MAX_RAW_BYTES + 1)          # bounded read: never buffer an unbounded body
    return raw, (time.perf_counter() - t0) * 1000


def _ollama_tags(base) -> dict:
    """Model METADATA only (GET /api/tags). Loads no model and runs no inference."""
    import urllib.request
    with urllib.request.urlopen(f"{base}/api/tags", timeout=10) as r:
        raw = r.read(MAX_TAGS_BYTES + 1)
    if len(raw) > MAX_TAGS_BYTES:
        raise ValueError("tags body too large")
    return json.loads(raw)


def local_model_digest(base: str, model: str) -> str:
    """The manifest digest Ollama reports for `model`, or SystemExit."""
    try:
        tags = _ollama_tags(base)
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as e:
        raise SystemExit(f"refusing: cannot read local Ollama model list ({type(e).__name__}); is Ollama running?")
    entries = [m for m in (tags.get("models") or []) if isinstance(m, dict) and model in (m.get("name"), m.get("model"))]
    if len(entries) != 1:
        raise SystemExit(f"refusing: local Ollama has {len(entries)} entries named {model!r}")
    d = str(entries[0].get("digest") or "").lower().removeprefix("sha256:")
    if len(d) != 64:
        raise SystemExit("refusing: local Ollama did not report a full model digest")
    return d


def _is_local_url(base: str) -> bool:
    u = urllib.parse.urlsplit(base)
    return (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1") and not u.username
            and u.path in ("", "/") and not u.query and not u.fragment)


def _classify_error(e: BaseException) -> tuple[str, bool, str]:
    """(status, retry_eligible, error_class). Only connection refused/reset/aborted are retried."""
    if isinstance(e, urllib.error.HTTPError):
        return "http_error", False, f"HTTPError {e.code}"
    reason = getattr(e, "reason", None) if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, TimeoutError):
        return "timeout", False, type(reason).__name__
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return "transport_error", True, type(reason).__name__
    return "transport_error", False, type(reason).__name__ if isinstance(reason, BaseException) else type(e).__name__


def preflight_stage2(args) -> dict:
    """Every check that must hold BEFORE the first model call. Raises SystemExit on any mismatch.
    Order: approvals -> freeze record -> files/config -> order file -> model identity -> sandbox image
    -> Ollama URL -> live Ollama digest (metadata only) -> real sandbox verification of the backend
    that will score coding cells."""
    if not args.owner_approved:
        raise SystemExit("refusing: Stage 2 needs --owner-approved (explicit owner sign-off)")
    if args.stage != 2:
        raise SystemExit("refusing: --run needs --stage 2")
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
    if not of.exists() or not fz.get("stage2_order_sha256") or sha256_file(of) != fz["stage2_order_sha256"]:
        raise SystemExit("refusing: execution order file missing or its hash differs from the freeze")
    order = json.loads(of.read_text())
    if order.get("cells") != execution_order(fz["execution"]["order_seed"])["cells"]:
        raise SystemExit("refusing: execution order file is not the order the frozen seed produces")
    if args.model != SELECTED_MODEL["name"]:
        raise SystemExit(f"refusing: --model {args.model!r} is not the frozen model {SELECTED_MODEL['name']!r}")
    if (args.model_digest or "").lower() != SELECTED_MODEL["manifest_sha256"]:
        raise SystemExit("refusing: --model-digest is not the full frozen manifest digest")
    override = os.environ.get("FAREX_SANDBOX_IMAGE")
    if override and override != sandbox.PINNED_IMAGE:
        raise SystemExit("refusing: FAREX_SANDBOX_IMAGE overrides the frozen sandbox image")
    if sandbox.PINNED_IMAGE != fz["execution"]["sandbox_image"]:
        raise SystemExit("refusing: pinned sandbox image differs from the frozen image")
    base = os.environ.get("FAREX_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    if not _is_local_url(base):
        raise SystemExit("refusing: Stage 2 is local-Ollama only (http://127.0.0.1, localhost or [::1])")
    observed = local_model_digest(base, args.model)
    if observed != SELECTED_MODEL["manifest_sha256"]:
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


def run_stage2(args) -> Path:
    ctx = preflight_stage2(args)        # nothing below runs unless every check passed
    backend, order, base = ctx["backend"], ctx["order"], ctx["base"]
    by_id = {t["id"]: t for t in load_tasks()}
    run_id = f"S2-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    out_dir = ART / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"generic_v1.{run_id}.jsonl"
    opts = dict(STAGE2_OPTIONS)
    m = manifest(run_id, "stage2", order, {"model": args.model, "model_digest_frozen": SELECTED_MODEL["manifest_sha256"],
                                           "model_digest_observed_local": ctx["observed_model_digest"],
                                           "options": opts, "ollama_url": base, "order_file": rel(order_file()),
                                           "order_sha256": sha256_file(order_file()),
                                           "sandbox": {"backend": backend.name, "status": backend.status,
                                                       "verification": ctx["sandbox_verification"]}})

    def call(row, system, user) -> list[dict]:
        """One attempt, plus one retry after RETRY_DELAY_S on connection refused/reset/aborted only."""
        out = []
        for attempt in (1, 2):
            r = dict(row, attempt=attempt, row_id=f"{row['call_id']}-a{attempt}", timestamp=now())
            try:
                raw, ms = _ollama_chat(base, args.model, system, user, opts)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as e:
                st, retry, cls = _classify_error(e)
                err = f"> {opts['timeout_s']} s" if st == "timeout" else f"{cls}: {str(e)[:200]}"
                out.append(dict(r, status=st, error=err, error_class=cls, retry_eligible=retry))
                if retry and attempt == 1:
                    _sleep(RETRY_DELAY_S)
                    continue
                return out
            r["latency_ms"] = ms
            if len(raw) > MAX_RAW_BYTES:
                out.append(dict(r, status="provider_oversized", error=f"body > {MAX_RAW_BYTES} bytes; not parsed",
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

    def write(fh, r):
        errs = validate_row(r)
        if errs:
            raise SystemExit(f"schema errors in row {r.get('row_id')}: {errs}")
        append_row(fh, r)

    first = order["cells"][0]
    with path.open("a") as fh:
        # Warm-up: same system prompt as the first scored cell, non-suite user message, condition 'warmup'.
        warm = base_row(run_id, "stage2", None, None, first["selected_destination"], 0)
        warm.update(provider="ollama", model=args.model, model_digest=SELECTED_MODEL["manifest_sha256"], options=opts)
        for r in call(warm, prompt_path(first["selected_destination"]).read_text(), "Reply with the word OK."):
            write(fh, r)
        for n, cell in enumerate(order["cells"], 1):
            task = by_id[cell["task_id"]]
            row = base_row(run_id, "stage2", task, cell["condition"], cell["selected_destination"], n)
            row.update(provider="ollama", model=args.model, model_digest=SELECTED_MODEL["manifest_sha256"], options=opts)
            attempts = call(row, prompt_path(cell["selected_destination"]).read_text(), task["prompt"])
            final = attempts[-1]
            if final["status"] == "ok":
                final.update(scoring.score(task, final["response"], backend))
            for r in attempts:
                write(fh, r)
            print(f"{n:2d}/{len(order['cells'])} {cell['task_id']:<14} {final['status']}")  # no condition/score printed
    m["ended"] = now()
    (out_dir / f"generic_v1.{run_id}.manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    print(rel(path))
    return path


# ------------------------------------------------------------------ cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--validate", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--prepare-stage2", action="store_true")
    g.add_argument("--review-sheet", type=Path)
    g.add_argument("--report", type=Path)
    g.add_argument("--freeze-draft", action="store_true")
    g.add_argument("--verify-sandbox", action="store_true")
    g.add_argument("--freeze", action="store_true")
    g.add_argument("--run", action="store_true")
    ap.add_argument("--stage", type=int)
    ap.add_argument("--order-seed", type=int, default=DEFAULT_ORDER_SEED)
    ap.add_argument("--review-seed", type=int)
    ap.add_argument("--model")
    ap.add_argument("--model-digest")
    ap.add_argument("--owner-approved", action="store_true")
    a = ap.parse_args(argv)
    if a.validate:
        checks = validate()
        for name, ok, detail in checks:
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        bad = sum(not ok for _, ok, _ in checks)
        print(f"\n{len(checks) - bad}/{len(checks)} checks passed")
        return 1 if bad else 0
    if a.dry_run:
        print(rel(dry_run(a.order_seed, verify_sandbox=True)))
    elif a.prepare_stage2:
        d = ART / "stage2_prep"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "execution_order.json"
        p.write_text(json.dumps(execution_order(a.order_seed), indent=2) + "\n")
        print(rel(p))
    elif a.review_sheet:
        s, k = review_sheet(a.review_sheet, a.review_seed)
        print(rel(s), rel(k))
    elif a.report:
        print(rel(report(a.report)))
    elif a.freeze_draft:
        print(rel(freeze_draft()))
    elif a.verify_sandbox:
        print(rel(verify_sandbox()))
    elif a.freeze:
        print(rel(freeze()))
    elif a.run:
        run_stage2(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
