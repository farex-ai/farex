# Contributing

Farex is early. Issues and small pull requests are welcome. Before starting anything large, open an issue first.

## Set up
Python 3.14 and the standard library are enough to run the tests. There's nothing to install.
```
python3 -I -m unittest discover -s tests
python3 -m farex_pilot.runner --validate
python3 -m farex_pilot.stage3 --validate
```
The real-container tests skip without a verified sandbox. See [docs/quickstart.md](docs/quickstart.md) to set one up.

## Rules that protect the measurements
These matter more than any feature:

1. **Don't edit frozen files.** `freeze/generic_v1.freeze.json` lists them. A test fails if any hash changes. Add a new
   module on top instead, as `scoring_v2.py` does.
2. **Never tune tasks, prompts or scorers on model output.** Fix them before a freeze, and record why in the task's
   `notes`. A null result is a result.
3. **Model-written code runs only in the verified sandbox.** No host fallback, in tests or anywhere else.
4. **Scoring depends on the task and the response only,** never on the route.
5. **Keep review blinded.** Output before review shows no conditions, scores or mappings.
6. **Report what was measured.** Tokens come from the provider or are marked unavailable. No estimates and no cost figures.
7. **No paid API calls, and no model calls in tests.** The model boundary is mocked.

## Changes
- Standard library only.
- Each bug fix comes with a test that fails without it.
- Every new module and test file is picked up by the freeze automatically. Anything else a run reads must be added to
  `frozen_hashes()`.
- If you change behaviour, update the matching page in `docs/`. `tests/test_docs.py` checks flags, fields and refusal text.
- Write in the voice in the project's brand kit: plain, exact and calm. No hype and no emojis.

## Before you open a pull request
```
python3 -I -m unittest discover -s tests
python3 -I -m unittest discover -s tools
python3 tools/publish_check.py
```

## Making a public copy
`tools/clean_export.py` writes the committed files at HEAD to a new directory, with no git history or tags, and leaves out
the internal notes and review sheets that hold unpublished results (the list is `EXCLUDE` in the tool). It never pushes or adds a remote.
```
python3 tools/clean_export.py ../farex-export --init --strict
```
It prints what it kept and excluded, runs the `publish_check` rules over the copy, and lists exported lines that still
name an excluded path. The copy gets a short `PLAN.md` stub that says the plan is not published and points to
`docs/design.md`, because several docstrings and tests name the plan. Each such line is classified:
- `RESOLVED`: the named file exists in the copy (the stub). The file exists; the section a line cites does not.
- `OUTPUT`: the line names the review-sheet directory as where your own run writes its sheets.
- `FIXTURE`: a made-up file name in `tests/test_publish_check.py`.
- `DANGLING`: anything else. Only these fail `--strict`.

The copy's own checks need a git repo, so pass `--init` (one local commit, for checking) before running `tools/publish_check.py` there. Its tests are in
`tools/test_clean_export.py`, outside `tests/`, because the freeze hashes every file in `tests/`.
