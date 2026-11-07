# Task format

Suites are JSON files in `tasks/`: `{"suite": ..., "tasks": [...]}`. `tasks/generic_v2.json` is built from `tasks/generic_v1.json`
and `stage3/tasks_new_draft.json` by `--build-suite`. Don't edit it by hand: `--validate` compares it with a fresh build.

## Fields every task has
| Field | Purpose |
|---|---|
| `id` | Unique. New `generic_v2` tasks use `<type>_h1` to `<type>_h6`. |
| `type` | `coding`, `math`, `extraction` or `evidence_qa`. This is the reference route. |
| `prompt` | The only task text a model sees. |
| `prompt_original` | The historical wording, kept for traceability, or `null`. Never sent. |
| `requirement` | What counts as correct, shown to the human reviewer. |
| `difficulty_features` | New tasks only: which criteria in `stage3/SPEC.md` section 2 the task meets. |
| `notes` | Optional. Records any change and why. |

Model inputs are the `prompt` only. Answers, spans, tests and notes are never sent.

## Math
| Field | Purpose |
|---|---|
| `expected` | The numeric answer, matched within 1e-9. |
| `expression` | Arithmetic that recomputes `expected`, checked by `--validate`. Allows `+ - * /`, `ceil` and `floor`. |

## Extraction
| Field | Purpose |
|---|---|
| `passage` | The text, which must appear verbatim in the prompt. |
| `accepted` | Accepted spellings of the answer. |
| `accepted_in_passage` | The spellings that appear in the passage, checked by `--validate`. |
| `accepted_numbers` | Numeric answers, matched by value. |
| `distractors`, `distractor_numbers` | Items the answer must not include. |
| `distractors_in_passage` | The distractors that appear in the passage, checked by `--validate`. |

## Evidence QA
| Field | Purpose |
|---|---|
| `passage` | The text, which must appear verbatim in the prompt. |
| `verdict` | `yes` or `no`. |
| `support_spans` | Every phrase that counts as a citation. A longer quote that contains a span passes, and a part of a span fails unless it is listed. |
| `answer_from_passage_only`, `background_knowledge`, `answerable_from_world_knowledge` | How the verdict relates to outside knowledge (v1). |
| `replaces_original` | The historical task this one replaces (v1). |

## Coding
| Field | Purpose |
|---|---|
| `function_name` | Must appear in the prompt. |
| `tests` | `[{"args": [...], "expected": ...}]`. Values must be JSON-safe. New tasks have 5 to 8. |
| `reference_solution` | Must pass every test and the static policy. |
| `buggy_solution` | Must fail at least one test. |

`--validate` runs `reference_solution` and `buggy_solution` in-process. They are author-written, so this is trusted code.
Model output only ever goes to the sandbox.

## Writing a task
1. State the information need and anything the scorer checks. Leave out output-format instructions such as "Return only".
   Format rules belong to the specialist prompt.
2. Avoid trick wording, more than one defensible answer, and anything that needs a tool, the web or today's date.
3. Add it to the draft, run `--build-suite`, and run `--validate`.
4. Never change a task after model output exists for it.
