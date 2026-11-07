# Output files

| Path | What | Tracked in git |
|---|---|---|
| `artifacts/runs/<suite>.<run_id>.jsonl` | One row per attempt, appended and fsynced | No |
| `artifacts/runs/<suite>.<run_id>.manifest.json` | One record per run | No |
| `artifacts/runs/<suite>.<run_id>.report.md` | Generated report | No |
| `artifacts/runs/<suite>.<run_id>.human_review.md` | Scorer audit from the join | No |
| `artifacts/review/` | Blinded review sheets | Yes |
| `artifacts/private/` | Blinding keys | Never |
| `artifacts/stage2_prep/`, `artifacts/stage3_prep/` | Execution order, which reveals conditions | No |
| `artifacts/dry_run/` | Dry-run output, labelled `DRYRUN` | No |
| `artifacts/sandbox_verification/` | Sandbox verification record | Yes |

## Rows
Every attempt is one JSON line. `call_id` is shared by a call's attempts, and `row_id` is `<call_id>-a<attempt>`. The last
attempt supplies a cell's outcome.

| Group | Fields |
|---|---|
| Identity | `schema_version`, `run_id`, `call_id`, `row_id`, `timestamp`, `stage`, `attempt`, `is_warmup`, `dry_run`, `row_type` |
| Task | `task_id`, `task_type`, `task_sha256` |
| Route | `condition`, `reference_destination`, `selected_destination`, `label_agreement`, `router`, `router_outcome` |
| Call | `provider`, `model`, `model_digest`, `system_prompt_file`, `system_prompt_sha256`, `options`, `response`, `response_source` |
| Outcome | `status`, `error`, `error_class`, `retry_eligible` |
| Measures | `latency_ms`, `total_duration`, `load_duration`, `prompt_eval_duration`, `eval_duration`, `prompt_tokens`, `completion_tokens`, `token_source` |
| Score | `scored`, `pass`, `score_status`, `components`, `reason`, `scorer_version`, `scorer_sha256` |

`row_type` is `warmup`, `worker` or `router`. `router_outcome` is `parsed`, `router_unparseable` or `router_call_failed` on
router rows. `row_type` and `router_outcome` exist in `generic_v2` rows only.

## Status values
| `status` | Meaning | Retried |
|---|---|---|
| `ok` | The model answered | No |
| `dry_run` | Canned answer, no model call | No |
| `transport_error` | Connection refused, reset or aborted, or another transport failure | Once, for refused, reset or aborted only, after 5 s |
| `timeout` | No answer within 120 s | No. In `generic_v2` the run stops |
| `http_error` | Ollama returned an error status | No |
| `provider_malformed` | Body wasn't JSON with `message.content` | No |
| `provider_oversized` | Body over 64 KiB, not parsed | No |

Token counts come from the provider. If they're missing, the fields are `null` and `token_source` is `unavailable`.
Farex doesn't estimate tokens and has no cost field.

## Manifest
Written on every exit, including a crash or an interrupt. It records the run ID, start and end times, whether the run
aborted and why, the git state, a hash of every frozen file, Python and OS versions, and the scorer version. Real runs
also record the model name, the frozen digest, the live digest and the sandbox verification.

After a timeout, `abort.ollama_ps` shows what `/api/ps` reported. A loaded model isn't proof that it was still generating.

## A run that was killed
If a run is killed during a write, its last line may be incomplete. The Stage 3 reports drop one partial last line and
count it (`torn last line: 1` in `--status`). A bad line anywhere else stops them with an error.
