# Concepts

Farex tests what happens when a task reaches the wrong specialist. This page defines the words used in the rest of the docs.

| Term | Meaning |
|---|---|
| **Worker** | The model that answers a task. |
| **Specialist prompt** | A system prompt for one kind of work. There are four: `coding`, `math`, `extraction` and `evidence_qa`. |
| **Route** or **destination** | The specialist prompt a task is sent to. |
| **Reference route** | The specialist that matches the task's author-assigned type. It is a label, not a proven best route. |
| **Wrong route** | Any other specialist. |
| **Router** | A model call that picks a route for a task. |
| **Suite** | A versioned set of tasks: `generic_v1` (16 tasks) or `generic_v2` (40 tasks). |
| **Cell** | One task on one route. A cell is the unit of scoring. |
| **Call** | One logical request to the model. |
| **Attempt** | One try of a call. A call gets a second attempt only after a refused, reset or aborted connection. |
| **Run** | One execution of a frozen suite, with its own run ID. |
| **Freeze** | A record of the hashes of every input. A run refuses to start if any input has changed. |

## What "routing" means here
Every specialist is the same local model with a different system prompt. Routing only chooses which system prompt the
worker gets. Results describe same-model, prompt-only routing. They don't extend to systems that use different models,
tools, memory or multi-step delegation.

## Suites and conditions
`generic_v1` runs each task on its reference route and on one fixed wrong route: 32 calls and 1 warm-up.

`generic_v2` is frozen but not yet run. Each task runs on all four routes and once through the router:

| Calls | Count |
|---|---|
| Warm-up, never scored | 1 |
| Cross cells (40 tasks × 4 routes) | 160 |
| Router calls | 40 |
| Worker calls on the chosen route, at most | 40 |
| **Total, at most** | **241** |

A router output that can't be parsed gets no worker call, so a run can make fewer than 241 calls.

## Conditions in the logs
| `condition` | Meaning |
|---|---|
| `warmup` | The warm-up call. Excluded from every result. |
| `reference` | The task's own type. |
| `fixed_wrong` | The one wrong route in `generic_v1`. |
| `cross:<destination>` | A wrong route in `generic_v2`. |
| `router` | The route the router chose in `generic_v2`. |

## What stays separate
- **Routing accuracy and task success.** One asks whether the route matched the label, the other whether the answer met the task.
- **Correct and compliant.** Correct is the right answer. Compliant is the format, exclusion or citation part of the requirement.
- **Router cost and worker cost.** They are never summed into one number without the parts next to it.
- **Scorer result and human review.** The reviewer doesn't see the scorer's result.
