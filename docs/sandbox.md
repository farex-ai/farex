# Sandbox

Model-written code runs only in a Docker container. There is no host fallback and no `sandbox-exec` backend. If the
sandbox isn't available, coding cells are unscored with `sandbox_unavailable`. They aren't failed or run another way.

## Controls
| Control | Setting |
|---|---|
| Image | `python:3.14-slim`, pinned by digest, `--pull never` |
| Platform | `linux/arm64` |
| Network | `--network none` |
| User | `65534:65534`, `--cap-drop ALL`, `no-new-privileges` |
| Filesystem | Read-only root; `/tmp` is a 16 MB `noexec` tmpfs; `/work` is a read-only mount of one temp dir |
| Inputs | `harness.py`, `candidate.py`, `tests.json`. Expected outputs are not included |
| Memory and CPU | 256 MiB, no swap, 1 CPU |
| Processes and files | 32 PIDs, 64 open files, 1 MiB file size |
| Time | 10 s wall clock, then `docker kill` and `rm -f` |
| Output | 64 KiB per stream, read through bounded readers. Going over kills the container |
| Environment | No variables passed; the docker CLI starts with a minimal environment |

## Verification
```
python3 -m farex_pilot.runner --verify-sandbox
```
This runs probes in real containers and records them in `artifacts/sandbox_verification/verification.json`, with the
image and the hashes of the sandbox code. `--freeze` refuses a record made for different code or a different image, and
every real run verifies again before its first model call.

Each probe checks a control by trying to break it, for example by reaching the network, writing to the root filesystem,
reading a host canary file or allocating 512 MiB. Positive controls show that a probe can succeed, so a failed probe
isn't just a broken probe.

## Result integrity
The harness runs in the candidate's interpreter, so nothing it prints is trusted as a verdict. It reports only the
returned values for each set of test arguments. The host compares them with the expected outputs, which never enter the
container. Non-JSON types and type mismatches never match.

## Limits
- The probes test the listed controls on one runtime. They don't show that arbitrary code is safe or can't escape.
- Colima shares the host home directory with its VM by default. Tests show the container can't see it. The VM can.
- The static policy in `scoring.py` is a filter. The container is the isolation boundary.
- A candidate controls the values it returns, and that is what gets scored.
- Output in flight inside the docker CLI or daemon when the limit trips is discarded, not kept.
- The kernel and runtime are the Colima VM's. Results hold for this setup only.
