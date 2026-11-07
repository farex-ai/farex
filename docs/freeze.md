# Freeze and run

A freeze makes a run checkable: it records exactly what was tested, and a run refuses to start if anything has changed.
Freezing and running are separate decisions. Neither happens automatically.

## Before a freeze
Every model-facing input, every module and every test file is hashed, so editing any of them afterwards blocks the run.
Decide on wording and scoring first.

Each suite's rules are fixed before any model sees its tasks. No task, prompt or scoring rule is tuned on model output.

## Freeze `generic_v2`
```
python3 -m farex_pilot.stage3 --validate
python3 -m farex_pilot.stage3 --verify-sandbox
python3 -m farex_pilot.stage3 --prepare
python3 -m farex_pilot.stage3 --freeze --owner-approved
```
`--freeze` refuses without `--owner-approved`, if validation fails, if the sandbox record is missing or made for different
code or a different image, if the order file wasn't produced by the frozen seed, or if a freeze file already exists. To
freeze again on purpose, move the old file to `freeze/superseded/` first.

The freeze file covers `tasks/generic_v2.json`, the task draft, the specialist and router prompts, `stage3/SPEC.md`, every
module in `farex_pilot/` and every test file. It also records the execution config and the order file's hash. Docs and
this repo's `tools/` aren't covered.

## Run
```
python3 -m farex_pilot.stage3 --run --stage 3 --owner-approved --model <name> --model-digest <full digest>
```
Take the model name and digest from the freeze file. Before the first model call, the run checks, in order:

1. Approval and stage.
2. The freeze status.
3. Every file hash.
4. The execution config.
5. The order file's hash, and that the frozen seed reproduces it.
6. The model name and full digest.
7. No sandbox image override.
8. The pinned image matches the freeze.
9. The Ollama URL is local.
10. The live digest from `GET /api/tags` matches. This call loads no model.
11. The backend image matches the freeze, and a fresh sandbox verification passes.

Any mismatch refuses the run with no model call.

## During a run
- The first timeout on any call stops the run, including the warm-up and router calls.
- The manifest is written on every exit.
- A stopped run can't be resumed. A new run starts from the warm-up under a new run ID, so one run never mixes model
  sessions.
- The console prints task ids and statuses only, never conditions or scores.
