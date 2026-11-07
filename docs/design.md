# Design notes

Why Farex is built the way it is: what it sets out to do, what it leaves out, which method choices it made and why,
where it is limited, and what is planned. Planned work is listed under [Roadmap](#roadmap) and is not in the tool.
These notes contain no run results.

## Goals
- Test one question: what happens to an answer when a task reaches the wrong specialist?
- Make a run checkable. Inputs are frozen and hashed, and a run refuses to start if any of them changed.
- Score by what the task asks for, with code, and send a blinded sample to a person before any number is read.
- Report what was measured and what was not.

## Scope
Every specialist is the same local model with a different system prompt. Routing only chooses which system prompt the
worker gets. The tool tests that and nothing broader.

In scope today:
- One local model through Ollama, four specialist prompts (`coding`, `math`, `extraction`, `evidence_qa`).
- Two suites, `generic_v1` (16 tasks) and `generic_v2` (40 tasks, with an LLM router).
- A Python command line with the standard library only.

Out of scope: different models per route, tools, memory, multi-step delegation, paid APIs, hosted services and
dashboards. Results from this tool say nothing about those setups.

## Method decisions
Each decision below is followed by its reason.

**The reference route is the task author's label.**
Each task has a type, and the matching specialist is the reference. The label is not proven to be the best route, so
reports call it the reference route, not the correct one.

**Wrong routes are enumerated, not sampled.**
`generic_v1` pairs each type with one fixed wrong route. That covers four of the twelve wrong pairings, so it describes
those pairs and not misrouting in general. `generic_v2` runs all four routes for every task so each wrong pairing is
tested.

**The router is a separate condition.**
In `generic_v2` the same model, given short descriptions of the four routes, picks one for each task. Its raw output is
logged. An output that is not exactly one route label is recorded as unparseable and gets no worker call. It is never
defaulted to a route, so router errors are not hidden by a fallback.

**Routing accuracy and task success are kept apart.**
Whether the router matched the label and whether the final answer met the task are different questions. Router and
worker cost are reported as parts, not one sum.

**Scoring depends on the task and the response only.**
Scorers never see the route. Each task states its requirement, and the scorer checks that requirement by rule. Correct
(the right answer) and compliant (format, exclusion, citation) are scored separately, and a pass needs both.

**Ambiguous responses go to a person.**
A response the scorer cannot decide is `review_needed`. It is not counted as wrong. A reply that passes evidence QA but
contains code also goes to review, because text inside code is hard to judge by rule.

**Model-written code runs only in a verified container.**
There is no host fallback. If Docker is unavailable, coding cells are left unscored. Expected outputs never enter the
container, and the verdict is computed on the host. See [Sandbox](sandbox.md).

**Inputs are frozen before any model sees them.**
Tasks, prompts, scorers, the order of cells and the run settings are hashed. No task, prompt or scoring rule is changed
after seeing model output. A null result is reported as a result.

**Difficulty criteria for the `generic_v2` tasks were written before the tasks.**
Each type has a written rule for what makes a task harder, such as several candidate entities for extraction or stated
edge cases for coding. Each task is validated with a recomputed answer, a reference solution that passes, and a
known-buggy version that fails. No task was tuned on model output.

**Cell order is counterbalanced with a fixed seed.**
The position of each route within a task rotates through a balanced Latin square, so no route always runs first or
follows the same neighbour. The seed and order are part of the freeze.

**Temperature is 0 with a fixed seed, one sample per cell.**
This keeps a run reproducible in intent. It also means a run shows one draw per cell, not a distribution. Repeated
samples would need their own justification.

**A timeout stops the run.**
The first timeout on any call stops the run and the manifest records it. A stopped run cannot be resumed, so one run
never mixes model sessions.

**Human review is blinded and sampled.**
The reviewer sees the task, the requirement and the response, in shuffled order, without the route or the automated
score. For `generic_v2` the sheet holds every answered cell the scorer did not pass, plus a random 25% of passes per
type and route. The key is kept apart and git ignores it. See [Blinded review](review.md).

**Statistics are descriptive.**
Counts come with denominators. The one planned test is an exact sign test on paired latency and token differences
between each wrong route and the reference. With 40 tasks, no significance claim is made about accuracy.

**Tokens come from the provider.**
Where the provider gives no count, it is marked unavailable. There are no character-based estimates and no cost figures.

## Known limits
- One model, prompt-only routing. Nothing here extends to multi-model or tool-using agents.
- The scorers are rule-based and are tested against fixtures the authors wrote. Fixture coverage does not establish
  general scorer validity. Human review checks the scorer only on the outputs it reads.
- In `generic_v2`, most scorer passes are not read. The after-review figures assume the unread passes are correct, and
  the false-positive rate from the sampled passes is printed next to them.
- Blinding is partial. A response's format often shows which specialist wrote it, both responses to one task can sit
  on the same sheet, the person who ran the suite knows its design, and the sheet shows the requirement.
- The sandbox probes test the controls they cover. They do not show that arbitrary code is safe.
- Task counts are small, and the tasks are generic. They are not drawn from a real workload.
- The reference label is an author's choice, and some tasks could reasonably belong to more than one type.

## Roadmap
Planned, not shipped, and not scheduled:
- A second local model on the `generic_v2` suite, to see whether anything seen on one model repeats on another.
- A published results report for `generic_v1` and `generic_v2`, after review and unblinding are finished for each.
- A second human reviewer on the same sample, with agreement reported.
- Router conditions beyond one LLM prompt, and routing across more than one model.
- Repeated samples or non-zero temperature, if a reason for them is written down first.

Anything on this list needs the same freeze, blinding and review steps as the current suites before it is reported.
