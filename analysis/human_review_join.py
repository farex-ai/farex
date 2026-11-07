"""Join a completed blinded review sheet to its private key and the run file (Stage 2 unblinding).

Usage: python3 -I analysis/human_review_join.py artifacts/runs/<run>.jsonl artifacts/review/<sheet>_reviewed.csv
Writes artifacts/runs/<run>.human_review.md. Reads the frozen run file and key; modifies nothing.
Human pass = human_correct == 1 and human_compliant == 1 (the full requirement, same definition as the scorer).
"""
import csv, json, sys
from pathlib import Path

run, sheet = Path(sys.argv[1]), Path(sys.argv[2])
rows = [json.loads(l) for l in run.open()]
cells = {}
for r in rows:
    if r.get("is_warmup"):
        continue
    k = (r["task_id"], r["condition"])
    if k not in cells or r["attempt"] > cells[k]["attempt"]:
        cells[k] = r
run_id = next(iter(cells.values()))["run_id"]
key = json.loads((run.parent.parent / "private" / f"review_key.{run_id}.json").read_text())["mapping"]
rev = list(csv.reader(sheet.open(encoding="utf-8-sig")))
hdr, body = rev[0], [x for x in rev[1:] if any(c.strip() for c in x)]
assert len(body) == len(key) == len(cells), (len(body), len(key), len(cells))

def b(v):
    v = v.strip()
    return None if v == "" else int(v)

H = {}
for x in body:
    m = key[x[0]]
    H[(m["task_id"], m["condition"])] = {"id": x[0], "correct": b(x[4]), "compliant": b(x[5]), "note": x[6].strip()}
conds = ("reference", "fixed_wrong")
types = ("coding", "math", "extraction", "evidence_qa")
def hpass(h):
    return None if h["correct"] is None or h["compliant"] is None else int(h["correct"] == 1 and h["compliant"] == 1)

L = [f"# Human review and scorer audit — `{run_id}`", "",
     f"Review sheet: `{sheet}` ({len(body)} rows, blinded; joined to `artifacts/private/review_key.{run_id}.json`).",
     "Human pass = human_correct = 1 and human_compliant = 1. One reviewer (the project owner). n = 16 tasks; no significance claims.", "",
     "## A. Task success by human judgement", "",
     "| condition | human pass | human correct | human compliant | blank |", "|---|---|---|---|---|"]
for c in conds:
    hs = [H[k] for k in H if k[1] == c]
    hp = [hpass(h) for h in hs]
    L.append(f"| {c} | {sum(v for v in hp if v)}/{sum(v is not None for v in hp)} | "
             f"{sum(h['correct'] == 1 for h in hs)}/{len(hs)} | {sum(h['compliant'] == 1 for h in hs)}/{len(hs)} | "
             f"{sum(v is None for v in hp)} |")
L += ["", "## B. Per type (human pass)", "", "| type | reference | fixed_wrong |", "|---|---|---|"]
for t in types:
    parts = []
    for c in conds:
        hp = [hpass(H[k]) for k in H if k[1] == c and cells[k]["task_type"] == t]
        parts.append(f"{sum(v for v in hp if v)}/{len(hp)}")
    L.append(f"| {t} | {parts[0]} | {parts[1]} |")
L += ["", "## C. Paired view (human pass)", ""]
ro = sorted(t for (t, c) in H if c == "reference" and hpass(H[(t, c)]) == 1 and hpass(H[(t, "fixed_wrong")]) == 0)
wo = sorted(t for (t, c) in H if c == "reference" and hpass(H[(t, c)]) == 0 and hpass(H[(t, "fixed_wrong")]) == 1)
both_f = sorted(t for (t, c) in H if c == "reference" and hpass(H[(t, c)]) == 0 and hpass(H[(t, "fixed_wrong")]) == 0)
L += [f"- pass under reference, fail under fixed-wrong: {len(ro)} {ro}",
      f"- pass under fixed-wrong, fail under reference: {len(wo)} {wo}",
      f"- fail under both: {len(both_f)} {both_f}", ""]
L += ["## D. Scorer audit (automated full-requirement pass vs. human pass)", "",
      "| | human pass | human fail | human blank |", "|---|---|---|---|"]
for lab, pred in (("scorer pass", lambda r: r["scored"] and r["pass"] == 1), ("scorer fail", lambda r: r["scored"] and r["pass"] == 0),
                  ("scorer review_needed / unscored", lambda r: not r["scored"])):
    ks = [k for k in cells if pred(cells[k])]
    L.append(f"| {lab} | {sum(hpass(H[k]) == 1 for k in ks)} | {sum(hpass(H[k]) == 0 for k in ks)} | {sum(hpass(H[k]) is None for k in ks)} |")
dis = [k for k in cells if cells[k]["scored"] and hpass(H[k]) is not None and cells[k]["pass"] != hpass(H[k])]
agree = sum(1 for k in cells if cells[k]["scored"] and hpass(H[k]) is not None) - len(dis)
L += ["", f"Agreement on scored cells: {agree}/{agree + len(dis)}. Disagreements: {len(dis)}.", "",
      "| task / condition | scorer pass | scorer reason | human correct | human compliant | reviewer note |", "|---|---|---|---|---|---|"]
for k in sorted(dis):
    r, h = cells[k], H[k]
    L.append(f"| {k[0]} / {k[1]} | {r['pass']} | {r['reason'] or r['score_status']} | {h['correct']} | {h['compliant']} | {h['note'] or '—'} |")
L += ["", "### Component disagreements (extraction and evidence QA, where the scorer set the component)", "",
      "| task / condition | component | scorer | human |", "|---|---|---|---|"]
for k in sorted(cells):
    r = cells[k]
    if r["task_type"] not in ("extraction", "evidence_qa"):
        continue
    for comp in ("correct", "compliant"):
        s = (r.get("components") or {}).get(comp)
        if s is not None and H[k][comp] is not None and int(s) != H[k][comp]:
            L.append(f"| {k[0]} / {k[1]} | {comp} | {s} | {H[k][comp]} |")
L += ["", "### Cells the scorer left for review", "", "| task / condition | human correct | human compliant | reviewer note |", "|---|---|---|---|"]
for k in sorted(k for k in cells if not cells[k]["scored"]):
    h = H[k]
    L.append(f"| {k[0]} / {k[1]} | {h['correct']} | {h['compliant']} | {h['note'] or '—'} |")
out = run.with_suffix(".human_review.md")
out.write_text("\n".join(L) + "\n")
print(out)
