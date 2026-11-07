# Stage 3, step 3.1: task criteria, router prompt and scoring rules (owner approved)

Status: approved by the owner 2026-11-03. Nothing here has been shown to a model. This file is frozen with `generic_v2`. The plan and the owner's decisions are in `../PLAN.md` under "Stage 3 plan".

## 1. Rules for writing the 24 new tasks
- **Who writes them and how:**
  - Claude drafts them and the owner reads every one before the freeze.
  - No task is run on any model, or changed after model output exists.
  - If validation fails, the task is fixed before the freeze, and the fix is recorded in its `notes`.
- **IDs and schema:**
  - IDs are `<type>_h1` … `<type>_h6`, 6 per type.
  - Same schema as v1, plus `difficulty_features`: the list of criteria below that the task meets.
- **Style:**
  - Same as v1. The task prompt asks the question. Output format rules come from the specialist prompt, not the task.
  - All tasks are English, self-contained, and answerable from the prompt alone (no world knowledge).
- **Not allowed:**
  - Trick wording.
  - Ambiguous answers.
  - More than one defensible answer.
  - Anything that requires a tool, the web or dates relative to "today".
- **Validation, the same as v1, applied to all 40 tasks:**
  - Math answers are recomputed from `expression`.
  - Every accepted answer, distractor and support span appears verbatim in its passage.
  - Coding reference solutions pass all tests, and a known-buggy solution fails at least one.

## 2. Difficulty criteria per type
A new task must meet **at least 2** of its type's criteria and name them in `difficulty_features`. E4 is recorded but
does not count toward the 2, because it measures length, not difficulty.

**Math** (one numeric answer, checked by `expected` within 1e-9)
- M1: 3 or more operations.
- M2: a percentage, ratio or unit conversion.
- M3: distractor numbers in the prompt that are not used in the answer.
- M4: an intermediate result that must be rounded or handled as a whole unit (e.g. whole packs), with the rule stated
  in the prompt.

**Extraction** (one accepted entity, plus listed distractors that must not appear)
- E1: 3 or more candidates of the same kind in the passage (e.g. three emails).
- E2: the target is defined by a condition, not by kind alone (e.g. "the address after the move").
- E3: a distractor appears before the target.
- E4: the passage is 2 to 4 sentences long.

**Evidence QA** (verdict yes or no, plus a cited support span)
- Q1: the verdict needs negation or an exception ("except", "unless").
- Q2: the verdict needs a numeric comparison.
- Q3: the verdict needs combining 2 sentences.
- Q4: the passage also contains a true but irrelevant statement close to the claim.
- Balance: 3 "yes" and 3 "no" across the six tasks.

**Coding** (Python function; hidden tests run in the sandbox; host judges)
- C1: at least 2 edge cases stated in the prompt (empty input, ties, mixed case, negative numbers), each covered by
  a hidden test.
- C2: a small algorithm beyond one built-in call (e.g. run-length encoding, a running maximum, a grouping).
- C3: the output type differs from the input type (e.g. string in, dict out).
- Each task has 5 to 8 tests. Arguments must be JSON-safe.

## 3. Router prompt (`prompts/generic_v2/router.txt`)
```
You are a router. Decide which specialist should handle the user's message.
Specialists:
coding: writes code when the user asks for a program or a function.
math: solves arithmetic and word problems that have one numeric answer.
extraction: finds one specific piece of information in a text the user gives.
evidence_qa: decides yes or no about a claim using a text the user gives, and cites a phrase.
Reply with exactly one label: coding, math, extraction or evidence_qa. No other words.
```
- **What each call sends:**
  - The router receives the task prompt exactly as the worker would.
  - The router call uses the same model and options as the worker. It is logged with `row_type: router` and its raw
    output.
- **Parser, strict and fixed now:**
  - Trim whitespace and lowercase.
  - Remove one pair of surrounding quotes or backticks, and one trailing period.
  - The result must equal one of the four labels exactly. Anything else is `router_unparseable`.
  - An unparseable cell gets no worker call. It is reported as its own bucket and counted as an end-to-end failure in
    the "unscored as fail" view.
- **Two separate outcomes:**
  - Router accuracy: chosen label equals the author label.
  - End-to-end success: the worker's answer on the chosen route.

## 4. Scoring changes (`SCORER_VERSION` bump; tested on fixtures before the freeze)
- **D5, evidence-QA `compliant` (default: strict):**
  - The quote must contain one of the task's `support_spans`.
  - **Matching:** case, repeated whitespace, punctuation and quote style (straight or smart) are ignored. The words must
    otherwise appear in order, next to each other.
  - **Ellipsis:** an ellipsis that skips words fails, because the words are no longer next to each other. A complete span
    followed by "..." passes.
  - **Partial spans:** quoting only part of a span fails, unless that part is listed as its own span. Quoting more than
    a span passes.
  - The check looks for the span anywhere in the response, quoted or not.
  - New tasks list every acceptable span up front.
  - The v1 spans are not changed. The Stage 2 case where the reviewer accepted a different phrase stays a documented
    disagreement.
- **Contract-violation tag (`components.contract_violation`, secondary outcome only):**
  - Set for a non-coding task when the response contains a fenced code block, or a line starting with `def `,
    `import ` or `print(`, and no answer candidate the type's scorer accepts. The value is `"code"`.
  - Set for a coding task when no function definition is found and the response is under 200 characters. The value
    is `"no_code"`.
  - Never changes `pass`, except for the evidence-QA rule below. It only labels why a cell failed.
- **Evidence QA sent to coding:** Stage 2 left these as `review_needed`. They become a scored fail with reason
  `verdict_missing` plus the contract tag. The reviewer judged all 4 Stage 2 cases as fails, and this rule is fixed
  before Stage 3 data exists.
  - The rule looks at the response only (no verdict, and code in the reply), never at the route.
  - `correct` and `compliant` are both 0. Text inside code is not a citation, which settles the Stage 2 case where the
    scorer gave `compliant` = 1 to code that echoed the passage.
- **Evidence QA that passes and contains code (option C, decided 2026-11-06 by Claude; the owner delegated this ruling):** a response that would pass and matches the
  contract tag's code pattern becomes `review_needed` with reason `answer_inside_code`, so a person reads it. The verdict
  or the quote may be inside the code, and the scorer can't tell. See `stage3/RULING_text_inside_code.md`.

## 5. Details fixed while building step 3.3 (owner check)
- **Order:** each task's four cross cells use a row of a balanced Latin square. Ten tasks don't divide evenly by four,
  so within a type a destination takes each position 2 or 3 times. Across all 40 tasks it is exactly 10 times, and each
  ordered pair of neighbouring destinations also occurs 10 times.
- **Sign test:** one value per task, the median of its 3 wrong cells minus its reference cell, so the 40 pairs are
  independent. There are two tests, latency and completion tokens, sharing alpha = 0.05 (Bonferroni: each is judged at
  0.025). Per-pairing tables are descriptive, with no p-values.
- **Stop rule:** the first timeout on any call stops the run, including the warm-up and router calls. The manifest is
  written on every exit and records the abort. `/api/ps` shows what is still loaded, which is not proof of generation.
- **No resume:** a stopped run can't be resumed. Its rows are kept and it is reported as aborted. Running again needs new
  owner approval and starts from the warm-up under a new run ID, so one run never mixes two model sessions.
- **Failed router call:** a router call that fails without a timeout gets no worker call and is reported as
  `router_call_failed`, separate from `router_unparseable`.
- **Review sample:** every answered cell the scorer did not pass, plus 25% of scorer passes per type and condition,
  rounded up. Cells with no response are left out, because there is nothing to read. The seed is kept in the private key.
- **Blinding:** `--status` shows completion and problems only. `--report` refuses real runs without `--unblind`.
- **After-review matrix:** human pass where reviewed, scorer otherwise. Unscored and blank cells count as fails. Unread
  scorer passes are assumed correct, so the matrix may be optimistic. The false-positive rate from the sample (scorer
  passes that the human failed, over sampled passes with a mark) is printed next to it.

## 6. Owner checklist for this step
- [x] Difficulty criteria in section 2 are acceptable, including "at least 2 per task".
- [x] Router prompt wording and parser.
- [x] D5 strict rule.
- [x] Contract-violation definitions.
- [x] Evidence-QA-to-coding becomes a scored fail, with `compliant` = 0.
- [x] The details in section 5.
