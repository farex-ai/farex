"""Stage 3 outputs: blinded status, review sheets, review join and the report (PLAN.md "Stage 3 plan").

Blinding order: --status shows completion and problems only. --review-sheet hides conditions and
scorer results. --report needs --unblind and is meant for after --join-review.
"""

from __future__ import annotations

import csv
import json
import math
import random
import secrets
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from . import runner, stage3
from .runner import TYPES, rel

ANSWERED = ("ok", "dry_run")


def read_rows(results: Path) -> tuple[list[dict], int]:
    """(rows, torn). A run killed during a write leaves a partial last line, and --status must still read
    that run. Only the last line may be partial: damage anywhere else means the log can't be trusted."""
    if not results.exists():
        raise SystemExit(f"refusing: results file not found: {results}")
    lines = [l for l in results.read_text().splitlines() if l.strip()]
    rows, torn = [], 0
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            if i != len(lines) - 1:
                raise SystemExit(f"refusing: {results.name} line {i + 1} is not valid JSON")
            torn = 1
    return rows, torn


def load(results: Path) -> dict:
    rows, torn = read_rows(results)
    wrong = {r.get("schema_version") for r in rows} - {stage3.SCHEMA_VERSION}
    if wrong:
        raise SystemExit(f"refusing: {results.name} is not a generic_v2 log (schema {sorted(map(str, wrong))})")
    manifest = results.with_name(results.name.replace(".jsonl", ".manifest.json"))
    m = json.loads(manifest.read_text()) if manifest.exists() else {}
    if not rows and not m:
        raise SystemExit("no rows and no manifest for this results file")
    workers, routers = defaultdict(list), defaultdict(list)
    for r in rows:
        if r.get("row_type") == "worker":
            workers[(r["task_id"], r["condition"])].append(r)
        elif r.get("row_type") == "router":
            routers[r["task_id"]].append(r)
    for group in (*workers.values(), *routers.values()):
        group.sort(key=lambda r: r["attempt"])
    first = rows[0] if rows else m           # a run stopped during the warm-up has no experimental rows
    return {"rows": rows, "attempts": dict(workers), "final": {k: v[-1] for k, v in workers.items()},
            "router": {k: v[-1] for k, v in routers.items()}, "run_id": first["run_id"], "dry": bool(first.get("dry_run")),
            "manifest": m, "torn_lines": torn}


def _is_pass(r) -> bool:
    return bool(r and r["scored"] and r["pass"] == 1)


def _rate(rows: list[dict]) -> str:
    """'p/s (u)': passes over scored cells, with unscored cells in brackets."""
    scored = [r for r in rows if r["scored"]]
    return f"{sum(r['pass'] or 0 for r in scored)}/{len(scored)} ({len(rows) - len(scored)})"


# ------------------------------------------------------------------ blinded status

def status(results: Path) -> str:
    d = load(results)
    rows = [r for r in d["rows"] if not r.get("is_warmup")]
    n_cells = len(d["final"])
    m = d["manifest"]
    probs = Counter(r["status"] for r in rows if r["status"] not in ANSWERED)
    out = [f"run {d['run_id']}", f"manifest: {'present' if m else 'MISSING'}; aborted: {m.get('aborted')}; abort: {m.get('abort')}",
           f"worker cells written: {n_cells}/200; answered: {sum(r['status'] in ANSWERED for r in d['final'].values())}",
           f"router calls written: {len(d['router'])}/40; answered: {sum(r['status'] in ANSWERED for r in d['router'].values())}",
           f"attempts: {len(rows)}; retries: {sum(r['attempt'] > 1 for r in rows)}; torn last line: {d['torn_lines']}",
           f"transport or provider problems: {dict(probs) or 'none'}",
           f"sandbox unavailable: {sum(r['score_status'] == 'sandbox_unavailable' for r in d['final'].values())}",
           f"schema errors: {sum(bool(stage3.validate_row(r)) for r in d['rows'])}"]
    return "\n".join(out)


# ------------------------------------------------------------------ review sheets

def _paths(run_id: str, dry: bool, second: bool) -> tuple[Path, Path]:
    label = "DRYRUN-" if dry else ""
    suffix = ".r2" if second else ""
    rdir = stage3.ART / ("dry_run" if dry else "review")     # dry-run sheets never sit next to real ones
    pdir = stage3.ART / "private"
    rdir.mkdir(parents=True, exist_ok=True)
    pdir.mkdir(parents=True, exist_ok=True)
    return rdir / f"{label}review_sheet.{run_id}{suffix}.csv", pdir / f"{label}review_key.{run_id}{suffix}.json"


def select_cells(final: dict, seed: int) -> list[tuple[dict, str]]:
    """Owner decision D3: every answered cell the scorer did not pass, plus a random 25% of scorer passes
    in each stratum of task type and condition, rounded up. Cells without a response have nothing to review."""
    rng = random.Random(seed)
    answered = sorted((r for r in final.values() if r["status"] in ANSWERED), key=lambda r: r["row_id"])
    picked = [(r, "flagged") for r in answered if not _is_pass(r)]
    strata = defaultdict(list)
    for r in answered:
        if _is_pass(r):
            strata[(r["task_type"], r["condition"])].append(r)
    for key in sorted(strata):
        pool = strata[key]
        picked += [(r, "sampled") for r in rng.sample(pool, math.ceil(len(pool) * stage3.REVIEW_FRACTION))]
    rng.shuffle(picked)
    return picked


def sheet_cell(text: str) -> str:
    """Model text as a spreadsheet will show it. A leading =, +, -, @, tab or CR makes Excel or Numbers
    evaluate the cell, so a reviewer would read a computed value (or trigger a link) instead of the
    response. A leading apostrophe keeps it literal. A plain negative number is left alone."""
    if text[:1] in ("=", "+", "@", "\t", "\r") or (text[:1] == "-" and not text[1:].replace(".", "", 1).isdigit()):
        return "'" + text
    return text


def review_sheet(results: Path, seed: int | None = None, second: bool = False) -> tuple[Path, Path]:
    """First reviewer: draws the sample. Second reviewer: the same cells in a new order with new ids and its own key."""
    d = load(results)
    seed = seed if seed is not None else secrets.randbits(64)
    sheet, key = _paths(d["run_id"], d["dry"], second)
    if sheet.exists() or key.exists():
        raise SystemExit(f"refusing: {sheet.name} or its key already exists; a new draw would overwrite reviewer marks")
    if second:
        _, first_key = _paths(d["run_id"], d["dry"], False)
        if not first_key.exists():
            raise SystemExit("run --review-sheet for the first reviewer before the second")
        by_row = {r["row_id"]: r for r in d["final"].values()}
        picked = [(by_row[m["row_id"]], m["selection"]) for m in json.loads(first_key.read_text())["mapping"].values()]
        random.Random(seed).shuffle(picked)
    else:
        picked = select_cells(d["final"], seed)
    by_id = {t["id"]: t for t in stage3.load_tasks()}
    mapping = {}
    with sheet.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(runner.REVIEW_HEADER)
        for r, selection in picked:
            rid = secrets.token_hex(4)
            mapping[rid] = {"row_id": r["row_id"], "task_id": r["task_id"], "condition": r["condition"], "selection": selection}
            t = by_id[r["task_id"]]
            w.writerow([rid, t["prompt"], t["requirement"], sheet_cell(r["response"]), "", "", ""])
    key.write_text(json.dumps({"run_id": d["run_id"], "results_file": rel(results), "sheet": rel(sheet), "seed": str(seed),
                               "rule": "all answered cells the scorer did not pass, plus 25% of passes per type and condition",
                               "population": len([r for r in d["final"].values() if r["status"] in ANSWERED]),
                               "mapping": mapping}, indent=2) + "\n")
    return sheet, key


def read_sheet(sheet: Path, key: Path) -> dict:
    """{(task_id, condition): review}. The sheet must hold exactly the key's ids, with 0, 1 or blank marks."""
    if not key.exists():
        raise SystemExit(f"refusing: no review key for {sheet.name}; run --review-sheet first")
    if not sheet.exists():
        raise SystemExit(f"refusing: review sheet not found: {sheet}")
    mapping = json.loads(key.read_text())["mapping"]
    with sheet.open(encoding="utf-8-sig", newline="") as f:
        grid = list(csv.reader(f))
    if not grid or grid[0] != runner.REVIEW_HEADER:
        raise SystemExit(f"refusing: {sheet.name} header differs from the review sheet header; columns may have moved")
    body = [x for x in grid[1:] if any(c.strip() for c in x)]
    short = [x[0] for x in body if len(x) < len(runner.REVIEW_HEADER)]
    if short:
        raise SystemExit(f"refusing: {sheet.name} has rows with too few columns: {short[:5]}")
    if sorted(x[0] for x in body) != sorted(mapping):
        raise SystemExit(f"refusing: {sheet.name} does not hold exactly the ids in its key")

    def mark(v):
        v = v.strip()
        if v not in ("", "0", "1"):
            raise SystemExit(f"refusing: review marks must be 0, 1 or blank, found {v!r}")
        return None if v == "" else int(v)
    out = {}
    for x in body:
        m = mapping[x[0]]
        out[(m["task_id"], m["condition"])] = {"correct": mark(x[4]), "compliant": mark(x[5]), "note": x[6].strip(),
                                               "selection": m["selection"]}
    return out


def md_cell(text: str) -> str:
    """Reviewer free text as one Markdown table cell."""
    return " ".join(text.replace("|", "\\|").split()) or "—"


def human_pass(h) -> int | None:
    """0 if either mark is 0, 1 if both are 1, None when undecided (no 0 and at least one blank)."""
    marks = (h["correct"], h["compliant"])
    return 0 if 0 in marks else None if None in marks else 1


def kappa(pairs: list[tuple[int, int]]) -> float | None:
    """Cohen's kappa for two raters on 0/1 marks. None when undefined (no pairs, or chance agreement is 1)."""
    if not pairs:
        return None
    n = len(pairs)
    obs = sum(a == b for a, b in pairs) / n
    p1, p2 = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    chance = p1 * p2 + (1 - p1) * (1 - p2)
    return None if chance == 1 else (obs - chance) / (1 - chance)


def adjudicated(r: dict, h: dict | None) -> int:
    """Human pass where reviewed, otherwise the scorer. Blank, unscored and missing cells count as fails."""
    if h is not None:
        return human_pass(h) or 0
    return 1 if _is_pass(r) else 0


def false_positives(final: dict, H: dict) -> tuple[int, int]:
    """(scorer passes the human failed, sampled scorer passes with a human mark)."""
    marks = [human_pass(H[k]) for k in H if _is_pass(final[k])]
    return marks.count(0), len(marks) - marks.count(None)


def join_review(results: Path, sheet: Path, second_sheet: Path | None = None) -> Path:
    d = load(results)
    run_id, final = d["run_id"], d["final"]
    H = read_sheet(sheet, _paths(run_id, d["dry"], False)[1])
    L = [f"# Human review and scorer audit — `{run_id}`", ""]
    if d["dry"]:
        L += ["> **DRY RUN — NOT RESULTS.**", ""]
    answered = [k for k, r in final.items() if r["status"] in ANSWERED]
    flagged = [k for k in H if H[k]["selection"] == "flagged"]
    sampled = [k for k in H if H[k]["selection"] == "sampled"]
    passes = [k for k in answered if _is_pass(final[k])]
    L += [f"Reviewed {len(H)} of {len(answered)} answered cells: {len(flagged)} the scorer failed or flagged, and {len(sampled)} sampled "
          f"from {len(passes)} scorer passes (25% per type and condition). Scorer validity rests on this sample; "
          f"{len(passes) - len(sampled)} passes were not read. Human pass = correct and compliant.", "",
          "## A. Scorer audit", "", "| scorer outcome | human pass | human fail | blank |", "|---|---|---|---|"]
    groups = (("pass (sampled)", lambda r: _is_pass(r)), ("fail", lambda r: r["scored"] and r["pass"] == 0),
              ("review_needed or unscored", lambda r: not r["scored"]))
    for lab, pred in groups:
        ks = [k for k in H if pred(final[k])]
        hp = [human_pass(H[k]) for k in ks]
        L.append(f"| {lab} | {hp.count(1)} | {hp.count(0)} | {hp.count(None)} |")
    both = [k for k in H if final[k]["scored"] and human_pass(H[k]) is not None]
    dis = [k for k in both if final[k]["pass"] != human_pass(H[k])]
    L += ["", f"Agreement on scored cells: {len(both) - len(dis)}/{len(both)}.", "",
          "| task / condition | scorer | scorer reason | human correct | human compliant | note |", "|---|---|---|---|---|---|"]
    L += [f"| {k[0]} / {k[1]} | {final[k]['pass']} | {final[k]['reason']} | {H[k]['correct']} | {H[k]['compliant']} | {md_cell(H[k]['note'])} |"
          for k in sorted(dis)]
    L += ["", "## B. Component disagreements", "", "| task / condition | component | scorer | human |", "|---|---|---|---|"]
    for k in sorted(H):
        for comp in ("correct", "compliant"):
            s = (final[k].get("components") or {}).get(comp)
            if s is not None and H[k][comp] is not None and int(s) != H[k][comp]:
                L.append(f"| {k[0]} / {k[1]} | {comp} | {s} | {H[k][comp]} |")
    fp, marked = false_positives(final, H)
    L += ["", "## C. Success matrix after review", "",
          "Human pass where reviewed, scorer otherwise. Unscored, blank and unanswered cells count as fails, and every "
          "expected cell is in the denominator, including router cells with no worker call.",
          f"Unread scorer passes are assumed correct, so this matrix may be optimistic. False positives in the sample: {fp}/{marked} "
          "(scorer pass, human fail).", "",
          "| task type | " + " | ".join(TYPES) + " | router |", "|---|---|---|---|---|---|"]
    tasks = stage3.load_tasks()
    for ty in TYPES:
        parts = []
        for c in [stage3.condition_for(ty, x) for x in TYPES] + ["router"]:
            ks = [(t["id"], c) for t in tasks if t["type"] == ty]
            parts.append(f"{sum(adjudicated(final.get(k), H.get(k)) for k in ks)}/{len(ks)}")
        L.append(f"| {ty} | " + " | ".join(parts) + " |")
    if second_sheet:
        H2 = read_sheet(second_sheet, _paths(run_id, d["dry"], True)[1])
        pairs = [(human_pass(H[k]), human_pass(H2[k])) for k in H if k in H2]
        pairs = [p for p in pairs if None not in p]
        kap = kappa(pairs)
        L += ["", "## D. Two reviewers", "", f"Same {len(H2)} cells, separate shuffles. Pass marks agree on "
              f"{sum(a == b for a, b in pairs)}/{len(pairs)} cells. Cohen's kappa: {'undefined' if kap is None else f'{kap:.2f}'}."]
    out = results.with_suffix(".human_review.md")
    out.write_text("\n".join(L) + "\n")
    return out


# ------------------------------------------------------------------ statistics

def sign_test(diffs: list[float]) -> dict:
    """Exact two-sided sign test. Ties are dropped, as is standard."""
    up, down = sum(x > 0 for x in diffs), sum(x < 0 for x in diffs)
    n = up + down
    p = None if n == 0 else min(1.0, 2 * sum(math.comb(n, k) for k in range(min(up, down) + 1)) / 2 ** n)
    return {"higher": up, "lower": down, "ties": len(diffs) - n, "p": p}


def per_task_diffs(final: dict, tasks: list[dict], key: str) -> list[float]:
    """One value per task: the median of its wrong-route cells minus its reference cell. One value per
    task keeps the pairs independent. A task needs its reference cell and all 3 wrong cells measured."""
    out = []
    for t in tasks:
        ref = final.get((t["id"], "reference"))
        wrong = [final.get((t["id"], stage3.condition_for(t["type"], d))) for d in TYPES if d != t["type"]]
        if ref and ref.get(key) is not None and all(w and w.get(key) is not None for w in wrong):
            out.append(statistics.median(w[key] for w in wrong) - ref[key])
    return out


# ------------------------------------------------------------------ report

def _matrix(L: list, final: dict, tasks: list[dict]) -> None:
    L += ["| task type | " + " | ".join(f"→ {d}" for d in TYPES) + " |", "|---|---|---|---|---|"]
    for ty in TYPES:
        parts = []
        for d in TYPES:
            cells = [final[(t["id"], stage3.condition_for(ty, d))] for t in tasks
                     if t["type"] == ty and (t["id"], stage3.condition_for(ty, d)) in final]
            parts.append(("**" + _rate(cells) + "**") if d == ty else _rate(cells))
        L.append(f"| {ty} | " + " | ".join(parts) + " |")
    L.append("")


def spread(values: list) -> str:
    return "—" if not values else f"{runner._fmt(float(statistics.median(values)))} ({runner._fmt(float(min(values)))}–{runner._fmt(float(max(values)))})"


def _spreads(final: dict) -> list[str]:
    out = []
    for ty in TYPES:
        for route, conds in (("reference", {"reference"}), ("wrong", {stage3.condition_for(ty, d) for d in TYPES if d != ty}),
                             ("router", {"router"})):
            rows = [r for (_, c), r in final.items() if r["task_type"] == ty and c in conds]
            lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
            tok = [r["completion_tokens"] for r in rows if r.get("completion_tokens") is not None]
            out.append(f"| {ty} | {route} | {len(lat)} | {spread(lat)} | {spread(tok)} |")
    return out


def policy_rejections(final: dict) -> list[str]:
    return [f"| {tid} | {c} | {md_cell(str(r['components'].get('policy_reason') or ''))} |" for (tid, c), r in sorted(final.items())
            if (r.get("components") or {}).get("policy_rejected")]


def _totals(rows: list[dict], final: dict) -> list[str]:
    """Totals with missing counts next to them, so a sum over partial data can't pass for a full one.
    The experimental rows here include router attempts; the warm-up is excluded."""
    every = [r for r in rows if not r.get("is_warmup")]
    per_cell = Counter(Counter((r["task_id"], r["condition"], r["row_type"]) for r in every).values())
    out = ["Attempts per cell: " + ", ".join(f"{k}: {v}" for k, v in sorted(per_cell.items())) + ".", "",
           "| measure | over | total | missing |", "|---|---|---|---|"]
    for label, key in (("latency ms", "latency_ms"), ("prompt tokens", "prompt_tokens"), ("completion tokens", "completion_tokens")):
        for over, group in (("final attempts", list(final.values())), ("all attempts", every)):
            vals = [r[key] for r in group if r.get(key) is not None]
            out.append(f"| {label} | {over} | {runner._fmt(float(sum(vals)))} | {len(group) - len(vals)} |")
    return out


def _components(final: dict, tasks: list[dict]) -> list[str]:
    """Extraction and evidence QA only: their scorers set `correct` and `compliant` separately."""
    out = []
    for ty in ("extraction", "evidence_qa"):
        ids = [t["id"] for t in tasks if t["type"] == ty]
        for route, conds in (("reference", ["reference"]), ("wrong", [stage3.condition_for(ty, d) for d in TYPES if d != ty])):
            rows = [final[(tid, c)] for tid in ids for c in conds if (tid, c) in final]
            comps = [r.get("components") or {} for r in rows]
            n = len(comps)
            out.append(f"| {ty} | {route} | {sum(c.get('correct') == 1 for c in comps)}/{n} | {sum(c.get('compliant') == 1 for c in comps)}/{n} "
                       f"| {sum(c.get('correct') is None or c.get('compliant') is None for c in comps)} |")
    return out


def _review_state(results: Path) -> str:
    audit = results.with_suffix(".human_review.md")
    if audit.exists():
        return f"Human review: joined, see `{audit.name}`."
    return "Human review: NOT joined. Every number here is the automated scorer alone."


def report(results: Path, unblind: bool = False) -> Path:
    d = load(results)
    if not unblind and not d["dry"]:
        raise SystemExit("refusing: the report shows conditions and scores. Finish blinded review, then pass --unblind. "
                         "Use --status for a blinded summary.")
    final, attempts, router = d["final"], d["attempts"], d["router"]
    tasks = stage3.load_tasks()
    new_ids = {t["id"] for t in json.loads(stage3.DRAFT_TASKS.read_text())["tasks"]}
    expected = {(t["id"], c) for t in tasks for c in stage3.all_conditions(t["type"])}
    no_route = {(tid, "router") for tid, r in router.items() if r["router_outcome"] != "parsed"}
    missing = sorted(expected - set(final) - no_route)
    L = []
    if d["dry"]:
        L += ["> **DRY RUN — NOT RESULTS.** No model was called. Responses are synthetic canned answers.", ""]
    L += [f"# generic_v2 report — `{d['run_id']}`", "",
          f"Worker cells: {len(final)}/200. Router cells with no worker call: {len(no_route)}. Missing cells: {len(missing)} "
          f"{[f'{a}/{b}' for a, b in missing]}. Aborted: {d['manifest'].get('aborted')}.", "",
          _review_state(results), ""]
    L += ["## 1. Success by task type and destination", "",
          "Automated scorer, full requirement. Each cell is passes/scored (unscored). The reference route is bold.", "", "All 40 tasks:", ""]
    _matrix(L, final, tasks)
    L += ["The 16 v1 tasks:", ""]
    _matrix(L, final, [t for t in tasks if t["id"] not in new_ids])
    L += ["The 24 new tasks:", ""]
    _matrix(L, final, [t for t in tasks if t["id"] in new_ids])
    L += ["## 2. Contract violations", "", "The answer came back in another format, such as code for a verdict. Label only; see stage3/SPEC.md.", "",
          "| task type | destination | code | no_code | cells |", "|---|---|---|---|---|"]
    for ty in TYPES:
        for dest in TYPES:
            rows = [r for (tid, c), r in final.items() if r["task_type"] == ty and c == stage3.condition_for(ty, dest)]
            tags = Counter((r["components"] or {}).get("contract_violation") for r in rows)
            L.append(f"| {ty} | {dest} | {tags['code']} | {tags['no_code']} | {len(rows)} |")
    L += ["", "## 3. Paired cost per wrong route", "",
          "Wrong minus reference for the same task, final attempts. Tokens are provider counts. Latency in ms.", "",
          "| task type | destination | n | latency diff median | wrong slower / faster / tie | token diff median | wrong more / fewer / tie |",
          "|---|---|---|---|---|---|---|"]
    for ty in TYPES:
        for dest in (x for x in TYPES if x != ty):
            row = []
            for key in ("latency_ms", "completion_tokens"):
                diffs = []
                for t in tasks:
                    a, b = final.get((t["id"], "reference")), final.get((t["id"], stage3.condition_for(ty, dest)))
                    if t["type"] == ty and a and b and a.get(key) is not None and b.get(key) is not None:
                        diffs.append(b[key] - a[key])
                s = sign_test(diffs)
                row.append((len(diffs), runner._fmt(float(statistics.median(diffs))) if diffs else "—", f"{s['higher']} / {s['lower']} / {s['ties']}"))
            L.append(f"| {ty} | {dest} | {row[0][0]} | {row[0][1]} | {row[0][2]} | {row[1][1]} | {row[1][2]} |")
    L += ["", "## 4. Pre-registered sign test", "",
          "Exact two-sided sign test on one value per task: median of its 3 wrong cells minus its reference cell. Two tests, "
          "latency and tokens, sharing alpha = 0.05 (each judged at 0.025). No significance claims on accuracy.", "",
          "| outcome | tasks | higher on wrong routes | lower | ties | p |", "|---|---|---|---|---|---|"]
    for lab, key in (("latency", "latency_ms"), ("completion tokens", "completion_tokens")):
        diffs = per_task_diffs(final, tasks, key)
        s = sign_test(diffs)
        L.append(f"| {lab} | {len(diffs)} | {s['higher']} | {s['lower']} | {s['ties']} | {'—' if s['p'] is None else format(s['p'], '.4g')} |")
    L += ["", "## 5. Router", ""] + _router_section(final, router, tasks)
    L += ["", "## 6. Per task", "", "1 pass, 0 fail, ? unscored, – no cell. Columns: reference, wrong routes, router.", "",
          "| task | reference | wrong | router |", "|---|---|---|---|"]

    def mark(r):
        return "–" if r is None else "?" if not r["scored"] else str(r["pass"])
    for t in sorted(tasks, key=lambda t: t["id"]):
        wrong = [final.get((t["id"], stage3.condition_for(t["type"], x))) for x in TYPES if x != t["type"]]
        L.append(f"| {t['id']} | {mark(final.get((t['id'], 'reference')))} | {' '.join(mark(w) for w in wrong)} | {mark(final.get((t['id'], 'router')))} |")
    exp_rows = [r for r in d["rows"] if not r.get("is_warmup")]
    L += ["", "## 7. Transport and scoring status", "", "| status | attempts |", "|---|---|"]
    L += [f"| {st} | {n} |" for st, n in sorted(Counter(r["status"] for r in exp_rows).items())]
    L += ["", "| score status | cells |", "|---|---|"]
    L += [f"| {st} | {n} |" for st, n in sorted(Counter(str(r["score_status"]) for r in final.values()).items())]
    retried = sorted(f"{k[0]}/{k[1]}" for k, v in attempts.items() if len(v) > 1)
    L += ["", f"Cells with a retry: {len(retried)} {retried}", "", "## 8. Correctness vs. compliance", "",
          "Automated components, final attempt. Correct = the right answer, compliant = the format, exclusion or citation part "
          "of the requirement. Undecided = a component the scorer left open. Pass needs both.", "",
          "| task type | route | correct | compliant | undecided |", "|---|---|---|---|---|"]
    L += _components(final, tasks)
    L += ["", "## 9. Latency and tokens by route", "", "Final attempts. Median (min–max). Tokens are provider counts; n counts cells "
          "with a latency value.", "", "| task type | route | n | latency ms | completion tokens |", "|---|---|---|---|---|"]
    L += _spreads(final)
    warm = [r for r in d["rows"] if r.get("is_warmup")]
    L += ["", "Warm-up, excluded from everything above: " + (f"{runner._fmt(warm[-1]['latency_ms'])} ms, status {warm[-1]['status']}."
                                                             if warm and warm[-1].get("latency_ms") is not None else "no latency recorded."), ""]
    L += ["## 10. Attempts and totals", ""] + _totals(d["rows"], final)
    rejected = policy_rejections(final)
    L += ["", "## 11. Sandbox policy rejections", "", "Code the static policy refused to run. These are counted apart from ordinary "
          "coding failures. The policy is a filter, not the isolation boundary.", ""]
    L += (["| task | condition | reason |", "|---|---|---|", *rejected] if rejected else ["None."])
    L += ["", "## 12. Limitations", "",
          "One model, same model as router and worker, prompt-only routing. Author labels are the reference. n = 40 tasks, English only. "
          "Scorer validity rests on the reviewed sample. A null result is a valid result.", ""]
    out = results.with_suffix(".report.md")
    out.write_text("\n".join(L))
    return out


def _parts(label: str, rows) -> str:
    """Router and worker are shown separately and never summed into one number (PLAN.md section E)."""
    rows = list(rows)
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    tok = [r["completion_tokens"] for r in rows if r.get("completion_tokens") is not None]
    return f"{label}, latency ms: {spread(lat)}; completion tokens: {spread(tok)}."


def _router_section(final: dict, router: dict, tasks: list[dict]) -> list[str]:
    type_of = {t["id"]: t["type"] for t in tasks}
    outcomes = Counter(r["router_outcome"] for r in router.values())
    parsed = {tid: r for tid, r in router.items() if r["router_outcome"] == "parsed"}
    right = sum(r["selected_destination"] == type_of[tid] for tid, r in parsed.items())
    cells = [r for (tid, c), r in final.items() if c == "router"]
    passed = sum(_is_pass(r) for r in cells)
    scored = sum(r["scored"] for r in cells)
    L = [f"Router calls: {len(router)}. Parsed: {outcomes['parsed']}. Unparseable: {outcomes['router_unparseable']}. "
         f"Call failed: {outcomes['router_call_failed']}.",
         f"Router accuracy against author labels: {right}/{len(router)}.",
         f"End-to-end success: {passed}/{scored} scored; {passed}/{len(router)} with unscored and unrouted cells as fails.",
         _parts("Router calls", router.values()), _parts("Workers on the chosen route", cells), "",
         "Router choices:", "", "| task type | " + " | ".join(TYPES) + " | unparseable or failed |", "|---|---|---|---|---|---|"]
    for ty in TYPES:
        mine = [(tid, r) for tid, r in router.items() if type_of[tid] == ty]
        L.append(f"| {ty} | " + " | ".join(str(sum(r["selected_destination"] == d for _, r in mine)) for d in TYPES)
                 + f" | {sum(r['selected_destination'] is None for _, r in mine)} |")
    L += ["", "Cost of router errors. The reference and matching cross cells show what this task does on each route:", "",
          "| task | chosen | router cell | matching cross cell | reference cell | same answer as cross |", "|---|---|---|---|---|---|"]
    for tid in sorted(parsed):
        chosen = parsed[tid]["selected_destination"]
        if chosen == type_of[tid]:
            continue
        rc, cross, ref = final.get((tid, "router")), final.get((tid, stage3.condition_for(type_of[tid], chosen))), final.get((tid, "reference"))
        def m(r):
            return "–" if r is None else "?" if not r["scored"] else str(r["pass"])
        same = "–" if not rc or not cross else "yes" if rc["response"] == cross["response"] else "no"
        L.append(f"| {tid} | {chosen} | {m(rc)} | {m(cross)} | {m(ref)} | {same} |")
    return L
