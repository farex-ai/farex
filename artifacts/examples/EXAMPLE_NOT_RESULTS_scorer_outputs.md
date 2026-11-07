# Scorer outputs on hand-written inputs — NOT RESULTS

> **NOT RESULTS.** Hand-written inputs run through the scoring rules, to show how each rule behaves.
> No model was called. No row here measures any model, condition or prompt.

| task | input (abbreviated) | purpose | pass | score_status | reason |
|---|---|---|---|---|---|
| math_1 | `391` | canonical | 1 | scored | ok |
| math_1 | `391 (that is 17 * 23)` | answer first, restated operands after | 1 | scored | ok |
| math_1 | `17 * 23 = 391 (check: 17 * 20 = 340, 17 * 3 = 51)` | equation chain with conflicting values | — | review_needed | conflicting_candidates |
| math_4 | `25 pencils = 5 packs, so 5 × $1.20 = $6.00` | regression: restated operands before the answer | — | review_needed | conflicting_candidates |
| math_1 | `17 * 23 = 391 ⏎ Answer: 390` | marker disagrees with line | — | review_needed | conflicting_answers |
| math_1 | `three hundred ninety-one` | number words (parse limitation) | — | review_needed | no_number |
| math_1 | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…` | oversized response | — | response_oversized | response_oversized |
| extraction_3 | `$1,250.00, payable within 30 days` | answer plus unrelated numbers | 1 | scored | ok_other_entity_in_context |
| extraction_3 | `4471, total $1,250.00` | distractor first, answer later | — | review_needed | conflicting_candidates |
| extraction_2 | `Dr. Smith works at Stanford University` | passage echo | 0 | scored | passage_echo |
| extraction_4 | `maria.lopez@example.org, not m.lopez@example.com` | negated alternative | 1 | scored | ok |
| evidence_qa_1 | `No. "Sunday service is suspended for maintenance"` | verdict and quote | 1 | scored | ok |
| evidence_qa_1 | `No. Trains rarely run on Sundays.` | verdict without passage quote | 0 | scored | no_supporting_quote |
| evidence_qa_2 | `Yes and no. "The Greenvale reservoir holds 3.2 million li…` | ambiguous verdict (parse limitation) | — | review_needed | verdict_ambiguous |

Scorer version: `generic_v1-scoring-1.1.0-draft`. Response cap: 4096 characters, checked before parsing.
