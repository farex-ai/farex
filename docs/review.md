# Blinded review

Automated scores only count once people have checked them. Review happens before anyone sees conditions or scores.

## Order of steps
1. `--status`: completion and problems only. It shows no conditions, scores or task types.
2. `--review-sheet`: write the blinded sheet.
3. The reviewer fills it in.
4. `--join-review`: join marks to the key and write the scorer audit.
5. `--report --unblind`: the full report.

Dry runs need no `--unblind`, because their numbers aren't results.

## Make a sheet
```
python3 -m farex_pilot.stage3 --review-sheet artifacts/runs/<file>.jsonl
```
The sheet goes to `artifacts/review/` and its key to `artifacts/private/`, which git ignores. A second draw for the same
run is refused, so it can't overwrite a reviewer's marks.

In `generic_v2`, the sheet holds every answered cell the scorer didn't pass, plus a random 25% of scorer passes per type
and condition, rounded up. The seed is kept in the key. In `generic_v1`, it holds all 32 cells.

## Fill it in
Open the CSV in a spreadsheet. Don't change `review_id` or the column order.

| Column | Enter |
|---|---|
| `human_correct` | 1 or 0: the right answer, entity, verdict or behaviour, ignoring format and exclusions |
| `human_compliant` | 1 or 0: meets the format, exclusion or citation part of the requirement |
| `human_notes` | Anything worth keeping |

Leave a mark blank if you can't decide.

In `generic_v2` sheets, a response that a spreadsheet would evaluate (one starting with `=`, `+`, `@`, a tab, or a `-` that
isn't a plain negative number) gets a leading apostrophe, so you see the text the model wrote. The apostrophe isn't part
of the response. `generic_v1` sheets come from frozen code and have no such protection, so open those with care.

## Join
```
python3 -m farex_pilot.stage3 --join-review artifacts/runs/<file>.jsonl artifacts/review/<sheet>.csv
```
The join refuses a sheet whose header has changed, whose ids differ from its key, whose rows are missing columns, or
whose marks aren't 0, 1 or blank.

A reviewed cell is a human pass only if both marks are 1. Either mark being 0 makes it a fail, and a blank with no 0
leaves it undecided.

## Second reviewer
```
python3 -m farex_pilot.stage3 --review-sheet artifacts/runs/<file>.jsonl --second-reviewer
python3 -m farex_pilot.stage3 --join-review artifacts/runs/<file>.jsonl <sheet>.csv --second-sheet <sheet2>.csv
```
The second reviewer gets the same cells in a new order, with new ids and a separate key. The join reports agreement and
Cohen's kappa.

## What blinding doesn't hide
- A response's format often shows which specialist wrote it, such as code in answer to a yes-or-no question.
- A task can have several responses on one sheet, so they can be compared.
- The person who ran the suite knows its design.
- The sheet shows the requirement, so review is closer to checking than to independent judging.

## Reading the result
Agreement applies to the outputs that were read. In `generic_v2`, most scorer passes aren't read, so the after-review
matrix assumes they're correct and may be optimistic. The false-positive rate from the sample is printed next to it.
