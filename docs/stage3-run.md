# Stage 3 runbook

How to run the frozen `generic_v2` suite once, with every guard on. This page describes the steps. It does not say
whether the run has been done or approved. The flags below are the ones in `farex_pilot/stage3.py`.

A run makes up to 241 model calls (see [Concepts](concepts.md)). Do not start one without the owner's explicit approval.
A freeze, a passing test suite or a delegated decision is not that approval.

## Prerequisites
1. **Explicit owner approval to run.** The run refuses without `--owner-approved`, and that flag is a statement that the
   approval exists. Pass it only when it does.
2. **A frozen suite.** `freeze/generic_v2.freeze.json` has status `frozen`, and no hashed file has changed since. Check
   with `git diff --name-only generic_v2-freeze HEAD` and compare the output with the hashed files in the freeze.
3. **The order file.** `artifacts/stage3_prep/execution_order.json` must exist and match the freeze. Git ignores it,
   because it reveals conditions. If it is lost, `python3 -m farex_pilot.stage3 --prepare` rebuilds it, but only on the
   same Python version, because the file records the version and the freeze records its hash.
4. **Colima and Docker**, for scoring coding cells. See [Quickstart](quickstart.md) and [Sandbox](sandbox.md).
   ```
   colima start --vm-type vz --cpu 2 --memory 4 --disk 20
   ```
   The pinned sandbox image must already be present. Farex never pulls it.
5. **Ollama**, serving on this machine, with the frozen model installed. The model name and full digest are in the freeze
   file and in `farex_pilot/runner.py` (`SELECTED_MODEL`). The run reads the Ollama address from `FAREX_OLLAMA_URL`,
   default `http://127.0.0.1:11434`, and accepts only a local address.
6. **No `FAREX_SANDBOX_IMAGE` override** in the environment, unless it equals the pinned image.

## Command
```
python3 -m farex_pilot.stage3 --run --stage 3 --owner-approved --model <name> --model-digest <full digest>
```
`--model` and `--model-digest` must equal the frozen values. The digest is the full 64-character manifest digest.

## What preflight refuses
Preflight runs before the first model call. Any one of these stops the run with a `refusing:` message and no call made:
- `--owner-approved` is missing, or `--stage` is not 3.
- The suite is not frozen.
- A hashed file changed since the freeze. The message lists the files.
- The execution configuration differs from the frozen one.
- The order file is missing, its hash differs from the freeze, or it is not the order the frozen seed produces.
- `--model` is not the frozen model, or `--model-digest` is not the full frozen digest.
- `FAREX_SANDBOX_IMAGE` overrides the frozen image, or the pinned image differs from the frozen one.
- The Ollama address is not local.
- The digest Ollama reports for the model differs from the frozen digest, or Ollama cannot be read. This call lists
  models and loads none.
- The sandbox backend's image differs from the frozen one, or a fresh sandbox verification fails or is unavailable.

The full list with the order of checks is in [Freeze and run](freeze.md). Messages and next steps are in
[Troubleshooting](troubleshooting.md).

## During the run
The run sends one warm-up call, then every cell in the frozen order, then writes its files under `artifacts/runs/`:
`generic_v2.<run id>.jsonl` and `generic_v2.<run id>.manifest.json`. The console prints one line per cell with the
sequence number, the task id and the call status. It prints no condition and no score.

Each call has a 120 s timeout. A refused, reset or aborted connection gets one retry after 5 s. Nothing else is retried.

## If a call times out
- The first timeout on any call, including the warm-up and router calls, stops the run. The console prints `STOPPED:`
  with the row id.
- The manifest is written with `aborted: true`. Its `abort` field holds the reason, the row id, and the models Ollama
  reports as loaded. A loaded model does not show that Ollama is still generating.
- The run cannot be resumed. A new run starts from the warm-up under a new run ID, so one run never mixes model sessions.
- Do not edit the log or the manifest. Use `--status` on the stopped run to see how far it got.

## The blinded first report
Right after the run, and until review is joined, use only:
```
python3 -m farex_pilot.stage3 --status artifacts/runs/<file>.jsonl
```
The status output may contain:
- the run ID, whether the manifest is present, and whether the run aborted and why;
- how many worker cells and router calls were written and how many were answered;
- attempt and retry counts, and whether the last line of the log was torn;
- counts of transport or provider problems by status;
- how many cells were left unscored because the sandbox was unavailable;
- the number of rows that fail the schema check.

It must not contain, and you must not read from the log or elsewhere before review: conditions, routes, task types,
pass or fail results, scores, latency, token counts or response text. `--report` refuses to run on a real log without
`--unblind` for this reason. Do not open the log, the order file or any key before the sheets are filled in.

## Review and join
1. Make the first reviewer's sheet. The seed defaults to a random value and is kept in the key.
   ```
   python3 -m farex_pilot.stage3 --review-sheet artifacts/runs/<file>.jsonl
   ```
   The sheet goes to `artifacts/review/` and the key to `artifacts/private/`. A second draw for the same run is refused.
2. For a second reviewer, add `--second-reviewer`. It uses the same cells in a new order with its own key, and needs the
   first sheet to exist.
   ```
   python3 -m farex_pilot.stage3 --review-sheet artifacts/runs/<file>.jsonl --second-reviewer
   ```
3. Reviewers fill in `human_correct`, `human_compliant` and `human_notes`. See [Blinded review](review.md).
4. Join the marks to the key. This writes the scorer audit next to the log as `<file>.human_review.md`.
   ```
   python3 -m farex_pilot.stage3 --join-review artifacts/runs/<file>.jsonl artifacts/review/<sheet>.csv
   python3 -m farex_pilot.stage3 --join-review artifacts/runs/<file>.jsonl artifacts/review/<sheet>.csv --second-sheet artifacts/review/<sheet2>.csv
   ```
   Use the second form when there are two reviewers.
5. Only now, write the full report. It shows conditions and scores, and says whether review was joined.
   ```
   python3 -m farex_pilot.stage3 --report artifacts/runs/<file>.jsonl --unblind
   ```
   The report is written next to the log as `<file>.report.md`.

## Shutdown
- Stop Colima when nothing else needs it:
  ```
  colima stop
  ```
- Stop Ollama the same way you started it.
- Keep the log, manifest, sheets and keys. Do not publish them without a separate decision. Raw run output and keys are
  not meant to be tracked by git.
