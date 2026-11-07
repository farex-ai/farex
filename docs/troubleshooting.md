# Troubleshooting

Each refusal says what failed. These are the common ones and what to do next.

| Message | Cause | Next step |
|---|---|---|
| `refusing: files changed since freeze` | A hashed file changed | Restore the file. Re-freezing needs approval and a new freeze |
| `refusing to freeze: ... already exists` | A freeze file is in place | Move it to `freeze/superseded/` if you mean to replace it |
| `refusing to freeze: no recorded sandbox verification` | No record yet | Run `--verify-sandbox` |
| `refusing to freeze: sandbox verification missing, failed, or made for different code or image` | The record is stale | Run `--verify-sandbox` again |
| `refusing to freeze: order file missing` | No order file, or another seed made it | Run `--prepare` without `--order-seed` |
| `refusing: suite not frozen` | No freeze file | Freeze first. See [freeze.md](freeze.md) |
| `refusing: --model-digest is not the full frozen manifest digest` | A short or wrong digest | Copy the full digest from the freeze file |
| `refusing: local Ollama model digest differs from the frozen digest` | Another model is installed under that name | Stop. Don't re-freeze to get past this without approval |
| `refusing: Stage 3 is local-Ollama only` | `FAREX_OLLAMA_URL` isn't local | Use `http://127.0.0.1:11434` |
| `refusing: FAREX_SANDBOX_IMAGE overrides the frozen sandbox image` | The variable is set | Unset it |
| `refusing: the report shows conditions and scores` | Review isn't done, or `--unblind` is missing | Finish review, then pass `--unblind` |
| `refusing: ... is not a generic_v2 log` | A `generic_v1` log was given to `farex_pilot.stage3` | Use `farex_pilot.runner --report` for v1 logs |
| `refusing: ... header differs` | Columns moved in the sheet | Restore the original column order |
| `refusing: ... rows with too few columns` | A spreadsheet export dropped empty cells | Export with all columns |
| `refusing: ... does not hold exactly the ids in its key` | Rows were added, removed or edited | Restore the rows. Don't change `review_id` |

## Coding cells come out `sandbox_unavailable`
Docker isn't reachable, or the image isn't present. Check `colima status`, `docker context show` (it should be `colima`) and
`docker image ls`. Farex never pulls the image.

## Tests skip 16 cases
Those are the real-container tests, and they skip when the sandbox isn't available. That's expected on a machine without
Colima running. Start Colima to run them.

## Docker commands fail after `colima stop`
Reset the context: `docker context use default`.

## After a timeout
The run has stopped by design. `abort` in the manifest records the row and what `/api/ps` showed. Ollama may keep
generating on the server side after the client gives up.
