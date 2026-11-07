# Scoring

Scoring depends only on the task and the response, never on the route that produced it. `farex_pilot/scoring.py` holds
the frozen `generic_v1` scorers. `farex_pilot/scoring_v2.py` is a thin layer on top for `generic_v2`.

## Result fields
| Field | Meaning |
|---|---|
| `scored` | Whether a pass or fail was decided. |
| `pass` | 1, 0 or `null`. |
| `score_status` | `scored`, `review_needed`, `response_oversized`, `response_malformed` or `sandbox_unavailable`. |
| `reason` | Why, such as `verdict_missing` or `tests_failed`. |
| `components` | Detail, including `correct`, `compliant` and, in v2, `contract_violation`. |

`review_needed` means the scorer couldn't decide. It is not a fail. A parsing limit is never recorded as a wrong answer.

## Before any parsing
A response over 4096 characters is `response_oversized` and isn't parsed. Source code is also checked for size and
bracket depth (64) before `ast.parse`.

## By task type
**Math.** A line gives a candidate only when it decides one value: one number and no others, or an equation whose results
agree. A list number (`1. ...`) is never an answer. The answer comes from the first line or an explicit marker such as
`answer is`. Every candidate must agree, otherwise the cell is `review_needed`. Empty output fails. Prose with no number
goes to review. Number words aren't parsed.

**Extraction.** `correct` means the accepted entity was selected. `compliant` means the response leaves out the excluded
items. Pass needs both. Echoing the passage fails. A distractor named only as a rejected alternative
(`..., not m.lopez@example.com`) goes to review.

**Evidence QA.** The verdict is the only `yes` or `no` outside quotes. `compliant` means a listed support span appears in
the response, ignoring case, whitespace, punctuation and quote style. The words must stay adjacent, so an ellipsis that
skips words fails.

**Coding.** The code is pulled from fenced blocks or the response text, screened by a static policy, and run in the
sandbox. The harness reports only return values. Expected outputs never enter the container, and the host compares them.

## What `generic_v2` adds
- **`contract_violation`.** A label for an answer that came back in another specialist's format: `code` for code on a
  non-coding task, `no_code` for a short reply with no function on a coding task. It is a label only and doesn't change
  `pass`, with the two exceptions below.
- **Evidence QA answered with code.** No verdict, plus code in the reply, is a scored fail, with `correct` and `compliant`
  both 0.
- **Evidence QA that passes and contains code.** The cell becomes `review_needed` with reason `answer_inside_code`, so a
  person reads it. The verdict or the quote may sit inside the code, and the scorer can't tell.

## Limits
- Rules are tested on hand-written fixtures. Passing them shows the rules behave as written on those cases.
- Whether the scorer agrees with people is measured by blinded review, on the outputs reviewed. See [review.md](review.md).
- The scorer can't tell whether reasoning used the passage. A cited span shows the response cited it.
- A coding candidate controls the values it returns, and could hard-code answers to guessable arguments.

The full rules for `generic_v2` are in `stage3/SPEC.md`.
