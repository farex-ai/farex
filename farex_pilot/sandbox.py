"""Isolation backend for running model-written code (PLAN.md C.3).

Only Docker is supported. There is deliberately NO host-execution fallback and
NO sandbox-exec backend. `DockerBackend.verified` is set True only by
`verify_isolation()`, which runs the runtime probes below against the pinned
image and requires every required control to pass.

Controls (all in `docker_run_args`):
  platform     --platform linux/arm64
  network      --network none (default); a bridge network is used only by the
               verification's positive control
  user         --user 65534:65534, --cap-drop ALL, no-new-privileges
  filesystem   --read-only root; /tmp is tmpfs 16 MB, noexec; /work is a read-only bind of one temp dir
  inputs       one temp dir (under the sandbox work root, outside the repo) with harness.py,
               candidate.py, tests.json. Nothing else is mounted.
  environment  no -e/--env-file; the image default env only; the docker CLI itself is started with a minimal env
  limits       --memory 256m --memory-swap 256m, --cpus 1, --pids-limit 32, nofile 64,
               fsize 1 MiB; wall clock 10 s then docker kill + rm -f
  pull         --pull never. The image is referenced by digest only.

  output       stdout and stderr are collected through bounded readers (64 KiB each). Exceeding
               either kills and removes the container and returns outcome "output_limit".
  verdict      the in-container harness reports return values only; expected outputs never enter
               the container and pass/fail is computed on the host (judge()).

Limitations (see PLAN.md): Colima's VM shares $HOME into the VM by default; the docker
CLI/daemon still buffer some output in flight before the host reader sees it (bounded by the
time to detect the limit); the probes establish only the controls they test; candidate code
fully controls the return values it reports (that is what is scored).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

WALL_CLOCK_S = 10
OUTPUT_CAP_BYTES = 64 * 1024     # per stream (stdout, stderr); exceeding it terminates the container
READ_CHUNK = 8192
PLATFORM = "linux/arm64"
MEMORY_BYTES = 256 * 1024 * 1024
PIDS_LIMIT = 32
# Official python:3.14-slim, linux/arm64 platform. Index digest verified against the registry
# (docker-content-digest == sha256 of the raw index body); arm64 child 29434a59...; see PLAN.md.
PINNED_IMAGE = "python@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8"
DEFAULT_WORK_ROOT = os.path.join(os.path.expanduser("~"), "Library", "Caches", "farex-sandbox")
HARNESS_PROTOCOL = "farex-harness-2"

# Runs INSIDE the container, in the same interpreter as the candidate, so NOTHING it prints is
# trusted as a verdict. It reports only the candidate's RETURN VALUES (type name + JSON value) for
# the test ARGUMENTS. The expected outputs are never written into the container; the host compares
# (see judge()). A candidate can therefore forge only its own return values, which is what is
# being scored anyway.
HARNESS = r"""
import json, sys
src = open("/work/candidate.py").read()
spec = json.load(open("/work/tests.json"))
out = sys.__stdout__
rec = {"protocol": "farex-harness-2", "load_error": None, "results": []}
ns = {"__name__": "candidate"}
try:
    exec(compile(src, "candidate.py", "exec"), ns)
    fn = ns[spec["function_name"]]
except BaseException as e:
    rec["load_error"] = type(e).__name__
    fn = None
if fn is not None:
    for args in spec["args"]:
        try:
            got = fn(*args)
            try:
                rec["results"].append({"raised": None, "t": type(got).__name__, "v": json.loads(json.dumps(got))})
            except BaseException:
                rec["results"].append({"raised": None, "t": type(got).__name__, "unserializable": True})
        except BaseException as e:
            rec["results"].append({"raised": type(e).__name__})
out.write("\n" + json.dumps(rec) + "\n")
out.flush()
"""

_JSON_TYPES = {"bool": bool, "int": int, "float": float, "str": str, "list": list, "dict": dict, "NoneType": type(None)}


class _Opaque:
    """Stands in for a returned value of a non-JSON type (tuple, set, ...): never equal to an expected value."""
    def __eq__(self, other):
        return False


def _decode(res: dict):
    t, v = res.get("t"), res.get("v")
    py = _JSON_TYPES.get(t)
    if py is None or res.get("unserializable") or "v" not in res:
        return _Opaque()
    if py is int and isinstance(v, bool) or not isinstance(v, py) and not (py is float and type(v) is int):
        return _Opaque()               # reported type and value disagree: not trusted
    return v


def judge(record, tests: list[dict]) -> tuple[int, int, str | None]:
    """HOST-side verdict. Returns (passed, total, problem). problem is set if the record is not a
    well-formed harness report for exactly these tests (then passed is 0)."""
    total = len(tests)
    if not isinstance(record, dict) or record.get("protocol") != HARNESS_PROTOCOL:
        return 0, total, "bad_protocol"
    if record.get("load_error"):
        return 0, total, None
    results = record.get("results")
    if not isinstance(results, list) or len(results) != total or not all(isinstance(r, dict) for r in results):
        return 0, total, "bad_results"
    passed = 0
    for case, res in zip(tests, results):
        if res.get("raised") is not None:
            continue
        got, exp = _decode(res), case["expected"]
        try:
            ok = (got == exp) and (not isinstance(exp, bool) or isinstance(got, bool))
        except Exception:
            ok = False
        passed += bool(ok)
    return passed, total, None


def docker_run_args(image: str, name: str, work_dir: str, cmd: list[str] | None = None,
                    network: str = "none") -> list[str]:
    if network not in ("none", "bridge"):
        raise ValueError("network must be 'none' or 'bridge'")
    return [
        "docker", "run", "--rm", "--name", name, "--pull", "never",
        "--platform", PLATFORM,
        "--network", network,
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--memory", "256m", "--memory-swap", "256m",
        "--cpus", "1",
        "--pids-limit", str(PIDS_LIMIT),
        "--ulimit", "nofile=64:64",
        "--ulimit", "fsize=1048576:1048576",
        "--mount", f"type=bind,source={work_dir},target=/work,readonly",
        "--workdir", "/tmp",
        image,
    ] + (cmd or ["python3", "-I", "-S", "/work/harness.py"])


def _cli_env() -> dict:
    # Minimal environment for the docker CLI on the host. No canaries, keys or tokens are forwarded.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.path.expanduser("~")}
    return env


def _make_work_dir(root: str | None = None) -> str:
    root = root or os.environ.get("FAREX_SANDBOX_WORKROOT") or DEFAULT_WORK_ROOT
    os.makedirs(root, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix="sbx-", dir=root)


class _Capture:
    """Bounded capture of one stream. Keeps at most `cap` bytes (head for stdout, tail for stderr),
    counts every byte read, and sets `over` once the count exceeds the cap. After that it keeps
    draining (and discarding) so the writer never blocks on a full pipe."""
    def __init__(self, cap: int, keep: str, over: threading.Event):
        self.cap, self.keep, self.over = cap, keep, over
        self.buf = bytearray()
        self.total = 0

    def pump(self, stream):
        try:
            while True:
                chunk = stream.read1(READ_CHUNK) if hasattr(stream, "read1") else stream.read(READ_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                if self.keep == "head":
                    room = self.cap - len(self.buf)
                    if room > 0:
                        self.buf += chunk[:room]
                else:
                    self.buf += chunk
                    if len(self.buf) > self.cap:
                        del self.buf[:len(self.buf) - self.cap]
                if self.total > self.cap:
                    self.over.set()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass


def _remove_container(name: str) -> None:
    for cmd in (["docker", "kill", name], ["docker", "rm", "-f", name]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, env=_cli_env())
        except (OSError, subprocess.TimeoutExpired):
            pass


def _container_exists(name: str) -> bool | None:
    try:
        r = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=30, env=_cli_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(r.stdout.strip())


def run_container(args: list[str], name: str, timeout: float, cap: int = OUTPUT_CAP_BYTES) -> dict:
    """Run `docker run ...` with bounded output collection and a wall clock.
    On timeout or when either stream exceeds `cap`, the container is killed and removed.
    Host memory use is bounded by about 2 * cap regardless of what the container prints."""
    over = threading.Event()
    out_c, err_c = _Capture(cap, "head", over), _Capture(cap, "tail", over)
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_cli_env())
    readers = [threading.Thread(target=out_c.pump, args=(proc.stdout,), daemon=True),
               threading.Thread(target=err_c.pump, args=(proc.stderr,), daemon=True)]
    for t in readers:
        t.start()
    deadline = time.monotonic() + timeout
    timed_out = output_limited = False
    try:
        while proc.poll() is None:
            if over.is_set():
                output_limited = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            over.wait(0.05)
        if over.is_set() and not timed_out:
            output_limited = True          # also when the limit was hit just before the process exited
    finally:
        if timed_out or output_limited or proc.poll() is None:
            _remove_container(name)
            proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for t in readers:
            t.join(timeout=10)
    left = _container_exists(name)
    if left:
        _remove_container(name)
        left = _container_exists(name)
    return {"exit_code": proc.returncode, "timed_out": timed_out, "output_limited": output_limited,
            "stdout": bytes(out_c.buf), "stdout_bytes": out_c.total,
            "stderr_tail": bytes(err_c.buf).decode("utf-8", "replace")[-500:], "stderr_bytes": err_c.total,
            "container_left_behind": left}


class DockerBackend:
    name = "docker"

    def __init__(self, image: str | None):
        self.image = image
        self.verified = False          # set True only by verify_isolation()
        self.status = "unchecked"
        self.detail = ""
        self.verification = None       # dict written by verify_isolation()

    # -------------------------------------------------- availability
    def check_available(self) -> tuple[bool, str]:
        if shutil.which("docker") is None:
            return False, "docker CLI not found on PATH"
        if not self.image or "@sha256:" not in self.image:
            return False, "no sandbox image pinned by digest configured"
        try:
            r = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True,
                               timeout=30, env=_cli_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"docker not usable: {type(e).__name__}"
        if r.returncode != 0:
            return False, "image not present locally (pulling is not authorized here)"
        return True, "ok"

    # -------------------------------------------------- execution
    def run(self, code: str, function_name: str, tests: list[dict]) -> dict:
        """Run candidate code against the test ARGUMENTS in the container; judge on the host.
        outcome: completed | timeout | output_limit | no_harness_result | infrastructure_error."""
        if not self.verified:
            raise RuntimeError("sandbox not verified; refusing to execute")
        return self._run_unchecked(code, function_name, tests)

    def _run_unchecked(self, code: str, function_name: str, tests: list[dict]) -> dict:
        work = _make_work_dir()
        name = f"farex-sbx-{uuid.uuid4().hex[:12]}"
        try:
            spec = {"function_name": function_name, "args": [c["args"] for c in tests]}   # no expected values
            for fname, content in (("harness.py", HARNESS), ("candidate.py", code), ("tests.json", json.dumps(spec))):
                with open(os.path.join(work, fname), "w") as f:
                    f.write(content)
                os.chmod(os.path.join(work, fname), 0o444)
            os.chmod(work, 0o555)
            r = run_container(docker_run_args(self.image, name, work), name, WALL_CLOCK_S)
        finally:
            os.chmod(work, 0o755)
            shutil.rmtree(work, ignore_errors=True)
        res = {"exit_code": r["exit_code"], "timed_out": r["timed_out"], "output_limited": r["output_limited"],
               "stdout_bytes": r["stdout_bytes"], "stderr_tail": r["stderr_tail"],
               "container_left_behind": r["container_left_behind"],
               "passed": 0, "total": len(tests), "error": None, "outcome": None}
        if r["container_left_behind"]:
            res.update(outcome="infrastructure_error", error="container_left_behind")
        elif r["timed_out"]:
            res["outcome"] = "timeout"
        elif r["output_limited"]:
            res["outcome"] = "output_limit"
        elif r["exit_code"] in (125, 126, 127):   # docker run itself failed (daemon, image, command)
            res.update(outcome="infrastructure_error", error=f"docker_run_exit_{r['exit_code']}")
        else:
            record = None
            for line in reversed(r["stdout"].decode("utf-8", "replace").splitlines()):
                if line.strip():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        record = None
                    break
            passed, total, problem = judge(record, tests)
            if problem:
                res.update(outcome="no_harness_result", harness_problem=problem)
            else:
                res.update(outcome="completed", passed=passed, total=total,
                           load_error=record.get("load_error"))
        return res

    def run_probe(self, snippet: str, network: str = "none", timeout: int = WALL_CLOCK_S) -> dict:
        """Run a probe snippet with the SAME docker flags and the SAME bounded collector as candidate
        code (only the command differs). Probe snippets print one JSON object on the last line."""
        work = _make_work_dir()
        name = f"farex-probe-{uuid.uuid4().hex[:12]}"
        args = docker_run_args(self.image, name, work, cmd=["python3", "-I", "-S", "-c", snippet], network=network)
        try:
            r = run_container(args, name, timeout)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        data = None
        try:
            data = json.loads(r["stdout"].decode("utf-8", "replace").strip().splitlines()[-1])
        except (IndexError, ValueError):
            pass
        return {"exit_code": r["exit_code"], "timed_out": r["timed_out"], "output_limited": r["output_limited"],
                "stdout_bytes": r["stdout_bytes"], "stdout_retained": len(r["stdout"]),
                "stderr_tail": r["stderr_tail"][-300:], "data": data,
                "container_left_behind": r["container_left_behind"]}

    def _container_exists(self, name: str) -> bool:
        return bool(_container_exists(name))

    # -------------------------------------------------- verification
    def verify_isolation(self) -> dict:
        """Run the runtime probes (sandbox_probes.PROBES) against the pinned image.
        Sets `verified` only if every required control passes. The result is kept in self.verification."""
        from . import sandbox_probes  # local import: probes depend on this module
        ok, why = self.check_available()
        if not ok:
            self.status, self.detail = "blocked", why
            self.verified = False
            self.verification = {"status": "blocked", "reason": why, "controls": {}}
            return self.verification
        self.verification = sandbox_probes.run_all(self)
        self.verified = self.verification["status"] == "verified"
        self.status = "verified" if self.verified else "failed"
        self.detail = "" if self.verified else "; ".join(c for c, r in self.verification["controls"].items()
                                                         if r["status"] != "pass")
        return self.verification


def default_backend() -> DockerBackend:
    b = DockerBackend(os.environ.get("FAREX_SANDBOX_IMAGE") or PINNED_IMAGE)
    ok, why = b.check_available()
    b.status, b.detail = ("available_unverified" if ok else "blocked"), why
    return b
