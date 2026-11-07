# Security

## Reporting a problem
Please don't open a public issue for a security problem. Use GitHub's private vulnerability reporting on this repository
and include the steps to reproduce it. Farex is an early project with no support timeline. Reports are read on a
best-effort basis.

## What's in scope
- A way for model-written code to get past the sandbox controls in [docs/sandbox.md](docs/sandbox.md).
- A way for a candidate to change its own verdict.
- A way for a run to start after a frozen input has changed.
- A way for model output to affect the reviewer's spreadsheet or the reports.

## What isn't claimed
Farex runs untrusted model output in a locked-down container and tests the controls listed in the docs. Those probes
show the controls held on one runtime. They don't show that arbitrary code is safe, and Farex isn't a general-purpose
sandbox. Don't use it to run code you wouldn't otherwise be willing to run in a container on that machine.

## Credentials
Farex needs no credentials and doesn't read `.env` files. If you find one in this repository, report it privately.
