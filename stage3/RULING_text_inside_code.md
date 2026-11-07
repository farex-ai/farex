# Ruling: text inside code in evidence QA

**Decided 2026-11-06: option C.** The owner delegated the decision to Claude ("figure it out"), having seen this memo's
recommendation. It was the one item blocking the `generic_v2` freeze (the "Independent audit (2026-11-04)" section of the internal plan, which isn't published). Run
approval is separate and was not part of this.

## The problem
`stage3/SPEC.md` section 4 says text inside code is not a citation, but its rule only applies when a verdict is missing.
So this reply to `evidence_qa_h1` scores `pass = 1`:

    ```python
    # no e-reader for under 16
    ```

It has a verdict word and the span `under 16`, both inside a code comment. Under the review rule (D3) only 25% of scorer
passes are read, so a reply like this can count as a success with nobody looking at it. Evidence QA sent to the coding
prompt is the one route that failed in Stage 2, so this is where code replies are most likely.

## Options
| | Rule | Cost |
|---|---|---|
| **A. Leave it** | Keep the rule as written. | Some code replies pass unread. Report it as a limitation. |
| **B. Ignore code** | Score only the text outside code. If nothing is left, it's a scored fail. | Fails a correct prose verdict whose quote sits in a code block. |
| **C. Send to review** (recommended) | A passing evidence reply that contains code becomes `review_needed`, so a person reads it. | A few more cells to review. |

C doesn't decide the hard case. It sends it to a person, which is what the project already does when a rule can't tell.
Every `review_needed` cell is read under D3, and it needs no new definition of what counts as code: it reuses the
existing contract-tag pattern.

## Measured
Scorer only, no model. The fixtures are hand-written replies to `evidence_qa_h1`, whose verdict is `no`.

| Reply | A | B | C |
|---|---|---|---|
| Comment only, as above | pass | fail | review |
| `No.` then the quote in a code block | pass | fail | review |
| `No. The passage says under 16.` then unrelated code | pass | pass | review |
| `No. The passage says under 16.` | pass | pass | pass |
| Code with no verdict | fail | fail | fail |

All 8 reviewed Stage 2 evidence cells keep their outcome under A, B and C. 4 of them contain code, and all 4 are already
scored fails.

## What changed
`scoring_v2.py`, tests, SPEC section 4 and `SCORER_VERSION` 2.1.0. The rule was fixed before any model saw the new tasks,
and no Stage 3 output informed it.
