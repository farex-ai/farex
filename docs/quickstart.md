# Quickstart

Run these from the repo root. Nothing here calls a model. Farex uses the Python standard library, so there is nothing to
`pip install`, and it runs from a checkout rather than as an installed package.

## 1. Check the suites
You need Python 3.14.
```
python3 -m farex_pilot.runner --validate
python3 -m farex_pilot.stage3 --validate
```
Each command prints a line such as `97/97 checks passed` and exits 1 if any check fails. These checks recompute math
answers, run the author's reference and buggy solutions, and confirm that spans appear in their passages.

## 2. Run the tests
```
python3 -I -m unittest discover -s tests
```
Without a verified sandbox, the 16 real-container tests are skipped and reported as skipped.

## 3. Set up the sandbox
Scoring coding tasks needs Docker. Model-written code never runs on the host, and there is no fallback. On macOS on
Apple silicon:
```
brew install colima docker docker-buildx
colima start --vm-type vz --cpu 2 --memory 4 --disk 20
docker pull --platform linux/arm64 python:3.14-slim
```
The image is pinned by digest in `farex_pilot/sandbox.py` (`PINNED_IMAGE`), and Farex never pulls it for you. Then verify:
```
python3 -m farex_pilot.runner --verify-sandbox
```
This starts containers and checks each control by probing it. See [sandbox.md](sandbox.md).

## 4. Try a dry run
```
python3 -m farex_pilot.stage3 --dry-run
python3 -m farex_pilot.stage3 --status artifacts/dry_run/<file>.jsonl
python3 -m farex_pilot.stage3 --report artifacts/dry_run/<file>.jsonl
```
A dry run sends canned correct answers through the real execution path and scorers, including the sandbox. It makes no
model call. Every row and report is labelled as a dry run, and the numbers in it are not results.

When you finish, stop the VM:
```
colima stop
docker context use default
```

## 5. Real runs
A real run needs a frozen suite, explicit approval and a local Ollama model whose digest matches the freeze. See
[freeze.md](freeze.md). Then blinded review follows, described in [review.md](review.md).
