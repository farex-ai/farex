# Changelog

Farex has no releases yet. This file records what changed for people using the tool.

## Unreleased
### Added
- `generic_v2` suite tools: 40 tasks, all four routes for each task, and an LLM router. Frozen 2026-11-06 and tested with
  a mocked model. Not run.
- `tools/publish_check.py`, which checks tracked files for machine paths, credential-shaped strings and files that need
  a decision before publishing.
- `tools/clean_export.py`, which writes the committed files at HEAD to a new directory with no git history, leaves out
  the files that hold unpublished results, and reports lines that still name them. It does not push or publish.
- User docs in `docs/`, kept in step with the code by `tests/test_docs.py`.
- `docs/design.md`: goals, scope, method decisions and their reasons, known limits and a roadmap. The roadmap lists planned
  work, not shipped work.
- `docs/stage3-run.md`: a runbook for the guarded `generic_v2` run, from preflight to shutdown.
- `tools/clean_export.py` writes a short `PLAN.md` stub into the export, saying the plan is not published and pointing to
  `docs/design.md`. It classifies each line that names an excluded path as resolved, an output location, a test fixture
  or dangling, and `--strict` fails only on dangling ones.
- Stage 3 report sections: correctness vs. compliance, latency and tokens per route, attempt totals with missing counts,
  sandbox policy rejections, and router and worker cost as separate parts.

### Changed
- `tools/publish_check.py` does not note a `PLAN.md` that is exactly the export stub.
- The README, `docs/freeze.md` and some comments no longer point readers to the unpublished plan.
- `--freeze` for `generic_v2` refuses to replace an existing freeze.
- The freeze hashes every module and test file by glob.
- `generic_v2` review sheet cells that a spreadsheet would evaluate are forced to text.
- The manifest and freeze are written atomically.
- The `generic_v2` report says whether human review was joined.
- Evidence QA replies that would pass and contain code go to human review (`answer_inside_code`, scorer 2.1.0).

### Fixed
- A stopped run could be recorded as not aborted if the Ollama status probe failed.
- `--status` and `--report` crashed on a log with a partial last line.
- `--join-review` read marks from swapped columns without noticing, and crashed on rows with missing columns.
- `--validate` crashed on a coding task whose function name didn't match its solution.
- Reviewer notes containing a pipe or a line break split a row in the scorer audit.
- A missing log, sheet or key ended in a traceback instead of a message.

## 2026-11-02
- `generic_v1`: first run completed (32 of 32 cells returned) and blinded review done.

## 2026-11-01
- `generic_v1`: corrective patch after an independent audit, and re-frozen.
