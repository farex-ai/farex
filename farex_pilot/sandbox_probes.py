"""Runtime isolation probes for the Docker sandbox (PLAN.md C.3).

Each control runs non-sensitive probe snippets inside the pinned image with the same
docker flags as candidate code (only the command differs). Each individual check is
recorded as {name, ok, observed}. A control passes only if all of its checks pass.
`run_all` returns status "verified" only if every REQUIRED control passes.

What this does NOT show: that arbitrary code is safe. It shows that the listed controls
behaved as configured for the probes listed. Canaries are inert: a random string in a
host temp file and a fake environment variable. No real credentials or private files
are touched.
"""

from __future__ import annotations

import errno
import os
import socket
import threading
import time
import uuid

from . import sandbox

RUN_CONTROLS = ("nonroot_no_privileges", "network_enforced", "readonly_root_and_mounts", "scratch_space_limits",
                "host_and_credential_isolation", "memory_and_swap_limits", "cpu_pids_and_rlimits",
                "fork_bomb_bounded", "output_limit_enforced", "wall_clock_timeout_cleanup", "reference_code_checks",
                "result_integrity")

# Positive-control listener for the network check. Bound to host LOOPBACK only: under Colima
# (VZ, user-mode networking) a bridge container reaches host 127.0.0.1 through
# host.docker.internal (192.168.5.2), so nothing is exposed on the host's other interfaces.
# Checked 2026-11-01: a loopback-bound listener was reached from a bridge container.
LISTENER_BIND = "127.0.0.1"

CANARY_ENV = "FAREX_CANARY_NOT_FORWARDED"

# ---------------------------------------------------------------- snippets (run INSIDE the container)

SNIP_IDENTITY = r'''
import os, json
st = dict(l.split(":", 1) for l in open("/proc/self/status").read().splitlines() if ":" in l)
print(json.dumps({"uid": os.getuid(), "gid": os.getgid(), "capeff": st["CapEff"].strip(),
                  "capbnd": st["CapBnd"].strip(), "nonewprivs": st["NoNewPrivs"].strip()}))
'''

SNIP_GATEWAY = r'''
import socket, json, sys
out = {}
try:
    out["gw"] = socket.gethostbyname("host.docker.internal")
except OSError as e:
    out["gw"] = None; out["gw_error"] = str(e)
if out.get("gw"):
    try:
        socket.create_connection((out["gw"], PORT), 5).close()
        out["connect"] = "connected"
    except OSError as e:
        out["connect"] = f"error:{e.errno}"
print(json.dumps(out))
'''

SNIP_NETWORK_NONE = r'''
import socket, json, errno
res = {"interfaces": [n for _, n in socket.if_nameindex()]}
def attempt(host, port):
    try:
        socket.create_connection((host, port), 3).close()
        return "connected"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
res["gateway_connect"] = attempt("GW", PORT)
res["external_connect"] = attempt("1.1.1.1", 80)
try:
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = socket.create_connection(("127.0.0.1", srv.getsockname()[1]), 3); c.close(); srv.close()
    res["loopback"] = "ok"
except OSError as e:
    res["loopback"] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps(res))
'''

SNIP_READONLY = r'''
import errno, json
res = {}
def wr(path):
    try:
        open(path, "w").write("x"); return "written"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
res["root_write"] = wr("/probe_root_write")
res["work_write"] = wr("/work/probe_write")
mounts = [l.split() for l in open("/proc/mounts")]
res["root_opts"] = [m[3] for m in mounts if m[1] == "/"][0]
res["work_opts"] = [m[3] for m in mounts if m[1] == "/work"][0] if any(m[1] == "/work" for m in mounts) else None
res["tmp_opts"] = [m[3] for m in mounts if m[1] == "/tmp"][0] if any(m[1] == "/tmp" for m in mounts) else None
res["mount_points"] = sorted({m[1] for m in mounts})
print(json.dumps(res))
'''

SNIP_SCRATCH = r'''
import os, json, errno, stat
res = {}
def fill(path, size):
    try:
        with open(path, "wb") as f:
            f.write(b"0" * size)
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
res["write_512k"] = fill("/tmp/s512k", 512 * 1024)
res["write_2m_single_file"] = fill("/tmp/s2m", 2 * 1024 * 1024)
written = 0
for i in range(40):
    r = fill("/tmp/fill_%d" % i, 1024 * 1024)
    if r != "ok":
        res["fill_stop_error"] = r
        break
    written += 1
res["mb_written_before_limit"] = written
for i in range(written):
    os.remove("/tmp/fill_%d" % i)
for f in ("/tmp/s512k", "/tmp/s2m"):
    if os.path.exists(f):
        os.remove(f)
src = "/tmp/probe_exec.sh"
try:
    with open(src, "w") as f:
        f.write("#!/bin/sh\necho hi\n")
    os.chmod(src, 0o755)
    os.execv(src, [src])
except OSError as e:
    res["exec_from_tmp"] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps(res))
'''

SNIP_HOST_ISOLATION = r'''
import os, json, errno
res = {}
canary = os.environ.get("CANARY_PATH_PLACEHOLDER", "")
try:
    open(canary).read(); res["canary_file"] = "readable"
except OSError as e:
    res["canary_file"] = errno.errorcode.get(e.errno, str(e.errno))
res["canary_env_present"] = "CANARY_ENV_PLACEHOLDER" in os.environ
res["env_keys"] = sorted(os.environ.keys())
res["users_dir_exists"] = os.path.exists("/Users")
res["docker_socket_exists"] = os.path.exists("/var/run/docker.sock")
home = os.path.expanduser("~")
res["home"] = home
try:
    res["home_entries"] = len(os.listdir(home))
except OSError as e:
    res["home_entries"] = errno.errorcode.get(e.errno, str(e.errno))
mounts = [l.split() for l in open("/proc/mounts")]
res["fs_types"] = sorted({m[2] for m in mounts})
res["host_backed_mounts"] = sorted(m[1] for m in mounts if m[2] == "virtiofs")
res["repo_mounted"] = any("Farex" in l or "routing-pilot" in l for l in open("/proc/mounts"))
print(json.dumps(res))
'''

SNIP_CGROUP = r'''
import json, os
def rd(p):
    try:
        return open(p).read().strip()
    except OSError as e:
        return "unavailable:" + str(e.errno)
print(json.dumps({"memory_max": rd("/sys/fs/cgroup/memory.max"), "memory_swap_max": rd("/sys/fs/cgroup/memory.swap.max"),
                  "cpu_max": rd("/sys/fs/cgroup/cpu.max"), "pids_max": rd("/sys/fs/cgroup/pids.max")}))
'''

SNIP_MEM_512 = r'''
import json
x = bytearray(512 * 1024 * 1024)
for i in range(0, len(x), 4096):
    x[i] = 1
print(json.dumps({"allocated_and_touched_mb": 512}))
'''

SNIP_RLIMITS = r'''
import json, resource
print(json.dumps({"nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
                  "fsize": resource.getrlimit(resource.RLIMIT_FSIZE),
                  "nproc": resource.getrlimit(resource.RLIMIT_NPROC)}))
'''

SNIP_FORK = r'''
import os, json, time, errno
children, ok, err = [], 0, None
for _ in range(120):
    try:
        pid = os.fork()
    except OSError as e:
        err = errno.errorcode.get(e.errno, str(e.errno)); break
    if pid == 0:
        time.sleep(2); os._exit(0)
    children.append(pid); ok += 1
for pid in children:
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
print(json.dumps({"forks_ok": ok, "stopped_by": err}))
'''

SNIP_OUTPUT = r'''
import sys
chunk = "x" * 65536
for _ in range(80):                     # 5 MiB to stdout
    sys.stdout.write(chunk)
sys.stdout.flush()
print("UNREACHABLE_IF_LIMIT_ENFORCED")
'''

SNIP_STDERR_FLOOD = r'''
import sys
chunk = "e" * 65536
for _ in range(80):                     # 5 MiB to stderr
    sys.stderr.write(chunk)
sys.stderr.flush()
'''

SNIP_ENDLESS_OUTPUT = "import sys\nwhile True:\n    sys.stdout.write('y' * 65536)\n"

# Candidate-code fixtures for result integrity. All are WRONG functions (they do not reverse).
FORGE_PRINT_AND_EXIT = (
    "def reverse_string(s):\n"
    "    __builtins__['print']('{\"passed\": 4, \"total\": 4}', flush=True)\n"
    "    __builtins__['__import__']('os')._exit(0)\n")
FORGE_FAKE_RECORD = (
    "def reverse_string(s):\n"
    "    __builtins__['print']('{\"protocol\": \"farex-harness-2\", \"load_error\": null, \"results\": "
    "[{\"raised\": null, \"t\": \"str\", \"v\": \"PASS\"}]}', flush=True)\n"
    "    __builtins__['__import__']('os')._exit(0)\n")
FORGE_READ_EXPECTED = (
    "def reverse_string(s):\n"
    "    j = __builtins__['__import__']('json')\n"
    "    spec = j.load(__builtins__['open']('/work/tests.json'))\n"
    "    return [c['expected'] for c in spec['cases'] if c['args'] == [s]][0]\n")
FORGE_PRINT_THEN_RETURN = (
    "def reverse_string(s):\n"
    "    print('{\"passed\": 4, \"total\": 4}')\n"
    "    return 'WRONG'\n")
CANDIDATE_FLOOD = "def reverse_string(s):\n    while True:\n        print('z' * 65536)\n"

SNIP_SPIN = "while True:\n    pass\n"



# ---------------------------------------------------------------- helpers

def _check(name, ok, observed):
    return {"name": name, "ok": bool(ok), "observed": observed}


def _control(name, checks, required=True, note=""):
    passed = bool(checks) and all(c["ok"] for c in checks)
    return {"status": "pass" if passed else "fail", "required": required,
            "checks": checks, "assertion_count": len(checks), "note": note, "_name": name}


def _errs(*names):
    return names


# ---------------------------------------------------------------- controls

def _unexpected_mounts(points):
    allowed_exact = {"/", "/tmp", "/work", "/etc/resolv.conf", "/etc/hostname", "/etc/hosts", "/dev/shm"}
    allowed_prefix = ("/proc", "/sys", "/dev")
    return sorted(p for p in (points or []) if p not in allowed_exact and not p.startswith(allowed_prefix))


def control_nonroot(b):
    d = b.run_probe(SNIP_IDENTITY).get("data") or {}
    return _control("nonroot_no_privileges", [
        _check("uid is 65534 (nobody)", d.get("uid") == 65534, d.get("uid")),
        _check("gid is 65534", d.get("gid") == 65534, d.get("gid")),
        _check("effective capabilities are empty", d.get("capeff", "").strip("0") == "", d.get("capeff")),
        _check("bounding capabilities are empty", d.get("capbnd", "").strip("0") == "", d.get("capbnd")),
        _check("no-new-privileges set", d.get("nonewprivs") == "1", d.get("nonewprivs")),
    ])


def control_network(b, gw_port):
    gw = b.run_probe(SNIP_GATEWAY.replace("PORT", str(gw_port)), network="bridge")
    gwd = gw.get("data") or {}
    positive = gwd.get("connect") == "connected"
    if not gwd.get("gw"):
        return _control("network_enforced", [_check("positive control: gateway resolves", False, gwd)], note="inconclusive")
    d = b.run_probe(SNIP_NETWORK_NONE.replace("GW", gwd["gw"]).replace("PORT", str(gw_port)), network="none").get("data") or {}
    return _control("network_enforced", [
        _check("positive control: bridge container reaches host listener", positive, gwd.get("connect")),
        _check("network=none: only loopback interface exists", d.get("interfaces") == ["lo"], d.get("interfaces")),
        _check("network=none: host listener NOT reachable", d.get("gateway_connect") not in (None, "connected"),
               d.get("gateway_connect")),
        _check("network=none: external address NOT reachable", d.get("external_connect") not in (None, "connected"),
               d.get("external_connect")),
        _check("network=none: loopback still works (sanity)", d.get("loopback") == "ok", d.get("loopback")),
    ], note="negative results are attributed to the network setting only because the bridge positive control succeeds")


def control_readonly(b):
    d = b.run_probe(SNIP_READONLY).get("data") or {}
    return _control("readonly_root_and_mounts", [
        _check("root mount is read-only", "ro" in (d.get("root_opts") or "").split(","), d.get("root_opts")),
        _check("write to / refused", d.get("root_write") == "EROFS", d.get("root_write")),
        _check("write to /work refused", d.get("work_write") in ("EROFS", "EACCES"), d.get("work_write")),
        _check("/work mount is read-only", "ro" in (d.get("work_opts") or "").split(","), d.get("work_opts")),
        _check("no unexpected mount points (runtime /proc, /sys, /dev masks allowed)",
               _unexpected_mounts(d.get("mount_points")) == [], _unexpected_mounts(d.get("mount_points"))),
    ])


def control_scratch(b):
    d = b.run_probe(SNIP_SCRATCH).get("data") or {}
    return _control("scratch_space_limits", [
        _check("512 KiB file written to /tmp", d.get("write_512k") == "ok", d.get("write_512k")),
        _check("2 MiB single file refused (fsize limit)", d.get("write_2m_single_file") in ("EFBIG", "ENOSPC"),
               d.get("write_2m_single_file")),
        _check("total /tmp fill capped at about 16 MiB (tmpfs size)", 8 <= (d.get("mb_written_before_limit") or 0) <= 16,
               d.get("mb_written_before_limit")),
        _check("/tmp is noexec: exec refused", d.get("exec_from_tmp") in ("EACCES", "EPERM"), d.get("exec_from_tmp")),
    ])


def control_isolation(b):
    canary_dir = sandbox._make_work_dir()
    canary_path = os.path.join(canary_dir, "canary.txt")
    token = "CANARY-" + uuid.uuid4().hex
    with open(canary_path, "w") as f:
        f.write(token)
    old = os.environ.get(CANARY_ENV)
    os.environ[CANARY_ENV] = token          # set in the HOST environment; must not be forwarded
    try:
        snip = SNIP_HOST_ISOLATION.replace('"CANARY_PATH_PLACEHOLDER"', repr(canary_path))
        snip = snip.replace('"CANARY_ENV_PLACEHOLDER"', repr(CANARY_ENV))
        d = b.run_probe(snip).get("data") or {}
    finally:
        if old is None:
            os.environ.pop(CANARY_ENV, None)
        else:
            os.environ[CANARY_ENV] = old
        import shutil
        shutil.rmtree(canary_dir, ignore_errors=True)
    secret_like = [k for k in d.get("env_keys") or [] if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASS", "AWS", "CLAUDE", "OPENAI"))]
    return _control("host_and_credential_isolation", [
        _check("host canary file not readable", d.get("canary_file") in ("ENOENT", "EACCES", "EPERM"), d.get("canary_file")),
        _check("host canary environment variable not forwarded", d.get("canary_env_present") is False, d.get("canary_env_present")),
        _check("no credential-like environment variables", secret_like == [], secret_like),
        _check("/Users (host home tree) not visible", d.get("users_dir_exists") is False, d.get("users_dir_exists")),
        _check("home directory has no host entries", d.get("home_entries") in (0, "ENOENT"), d.get("home_entries")),
        _check("no Docker socket", d.get("docker_socket_exists") is False, d.get("docker_socket_exists")),
        _check("no repository mount", d.get("repo_mounted") is False, d.get("repo_mounted")),
        _check("only /work (the one input bind) is a host-backed mount", d.get("host_backed_mounts") == ["/work"],
               d.get("host_backed_mounts")),
    ], note="The Colima VM shares $HOME into the VM by default; the container sees none of it (tested here).")


def control_memory(b):
    cg = b.run_probe(SNIP_CGROUP).get("data") or {}
    big = b.run_probe(SNIP_MEM_512)
    survived = big.get("exit_code") == 0 and not big.get("timed_out")
    return _control("memory_and_swap_limits", [
        _check("memory.max = 256 MiB", cg.get("memory_max") == str(sandbox.MEMORY_BYTES), cg.get("memory_max")),
        _check("swap is disabled (memory.swap.max = 0)", cg.get("memory_swap_max") == "0", cg.get("memory_swap_max")),
        _check("touching 512 MiB does not succeed (killed or refused)", not survived,
               {"exit_code": big.get("exit_code"), "timed_out": big.get("timed_out")}),
    ])


def control_cpu_rlimits(b):
    cg = b.run_probe(SNIP_CGROUP).get("data") or {}
    rl = b.run_probe(SNIP_RLIMITS).get("data") or {}
    return _control("cpu_pids_and_rlimits", [
        _check("cpu.max = 1 CPU (100000/100000)", cg.get("cpu_max") == "100000 100000", cg.get("cpu_max")),
        _check("pids.max = 32", cg.get("pids_max") == str(sandbox.PIDS_LIMIT), cg.get("pids_max")),
        _check("RLIMIT_NOFILE = 64", rl.get("nofile") == [64, 64], rl.get("nofile")),
        _check("RLIMIT_FSIZE = 1 MiB", rl.get("fsize") == [1048576, 1048576], rl.get("fsize")),
    ])


def control_fork(b):
    d = b.run_probe(SNIP_FORK).get("data") or {}
    return _control("fork_bomb_bounded", [
        _check("fork loop stopped by the process limit", d.get("stopped_by") in ("EAGAIN", "ENOMEM"), d.get("stopped_by")),
        _check("concurrent children bounded by pids limit", 0 < (d.get("forks_ok") or 0) <= sandbox.PIDS_LIMIT,
               d.get("forks_ok")),
    ])


def control_output(b):
    cap = sandbox.OUTPUT_CAP_BYTES
    t0 = time.monotonic()
    out = b.run_probe(SNIP_OUTPUT, timeout=sandbox.WALL_CLOCK_S)
    err = b.run_probe(SNIP_STDERR_FLOOD, timeout=sandbox.WALL_CLOCK_S)
    endless = b.run_probe(SNIP_ENDLESS_OUTPUT, timeout=sandbox.WALL_CLOCK_S)
    elapsed = round(time.monotonic() - t0, 1)
    t = _coding_task()
    cand = b._run_unchecked(CANDIDATE_FLOOD, t["function_name"], t["tests"])
    return _control("output_limit_enforced", [
        _check("stdout flood: collection stopped with output_limited", out.get("output_limited") is True,
               {"output_limited": out.get("output_limited"), "bytes_read": out.get("stdout_bytes")}),
        _check("stdout flood: host retained <= cap", (out.get("stdout_retained") or 0) <= cap, out.get("stdout_retained")),
        _check("stdout flood: text after the cap is not retained (sentinel line absent)",
               out.get("data") is None and out.get("timed_out") is False, out.get("exit_code")),
        _check("stderr flood: collection stopped with output_limited", err.get("output_limited") is True,
               err.get("output_limited")),
        _check("endless output: stopped by the output limit, not the wall clock",
               endless.get("output_limited") is True and endless.get("timed_out") is False,
               {"output_limited": endless.get("output_limited"), "timed_out": endless.get("timed_out")}),
        _check("three floods finished well inside 3 x wall clock", elapsed < 3 * sandbox.WALL_CLOCK_S, elapsed),
        _check("no container left behind", not any(x.get("container_left_behind") for x in (out, err, endless)),
               [x.get("container_left_behind") for x in (out, err, endless)]),
        _check("candidate path: printing loop -> outcome output_limit, 0 passed",
               cand.get("outcome") == "output_limit" and cand.get("passed") == 0,
               {"outcome": cand.get("outcome"), "passed": cand.get("passed")}),
    ], note="Bounded readers on the host (64 KiB per stream). Data already in flight inside the docker "
            "CLI/daemon when the limit is detected is drained and discarded, not retained.")


def _coding_task():
    from . import runner
    return {t["id"]: t for t in runner.load_tasks()}["coding_1"]


def control_result_integrity(b):
    """Wrong functions that try to make the host accept a passing verdict."""
    t = _coding_task()
    fn, tests = t["function_name"], t["tests"]
    r1 = b._run_unchecked(FORGE_PRINT_AND_EXIT, fn, tests)
    r2 = b._run_unchecked(FORGE_FAKE_RECORD, fn, tests)
    r3 = b._run_unchecked(FORGE_READ_EXPECTED, fn, tests)
    r4 = b._run_unchecked(FORGE_PRINT_THEN_RETURN, fn, tests)
    o = lambda r: {"outcome": r.get("outcome"), "passed": r.get("passed"), "total": r.get("total")}
    return _control("result_integrity", [
        _check("printed old-style verdict + os._exit: not accepted (no_harness_result, 0 passed)",
               r1.get("outcome") == "no_harness_result" and r1.get("passed") == 0, o(r1)),
        _check("forged harness record with wrong shape: rejected by host judge",
               r2.get("outcome") == "no_harness_result" and r2.get("passed") == 0, o(r2)),
        _check("reading /work/tests.json yields no expected values (0 passed)",
               r3.get("outcome") == "completed" and r3.get("passed") == 0, o(r3)),
        _check("printed verdict before a wrong return: host counts the return values (0 passed)",
               r4.get("outcome") == "completed" and r4.get("passed") == 0, o(r4)),
    ], note="Static policy was bypassed on purpose (_run_unchecked). Verdicts are computed on the host from "
            "returned values; expected outputs never enter the container.")


def control_timeout(b):
    t0 = time.monotonic()
    r = b.run_probe(SNIP_SPIN, timeout=sandbox.WALL_CLOCK_S)
    return _control("wall_clock_timeout_cleanup", [
        _check("infinite loop reported as timed out", r.get("timed_out") is True, r.get("timed_out")),
        _check("finished within wall clock + 5 s", time.monotonic() - t0 < sandbox.WALL_CLOCK_S + 5, round(time.monotonic() - t0, 1)),
        _check("no container left behind after timeout", r.get("container_left_behind") is False, r.get("container_left_behind")),
    ])


def control_reference(b):
    t = _coding_task()
    # Same execution path as candidate code, minus the `verified` gate (verification is in progress).
    ref = b._run_unchecked(t["reference_solution"], t["function_name"], t["tests"])
    bug = b._run_unchecked(t["buggy_solution"], t["function_name"], t["tests"])
    return _control("reference_code_checks", [
        _check("reference solution passes all hidden tests", ref.get("passed") == ref.get("total") and ref.get("total", 0) > 0,
               {"passed": ref.get("passed"), "total": ref.get("total")}),
        _check("buggy solution fails", bug.get("passed", 0) < bug.get("total", 0), {"passed": bug.get("passed"), "total": bug.get("total")}),
        _check("no timeout on reference", not ref.get("timed_out"), ref.get("timed_out")),
    ])


# ---------------------------------------------------------------- orchestration

def run_all(b) -> dict:
    """Run every control. Returns a dict; status 'verified' only if all required controls pass."""
    controls = {}
    def add(fn, *args):
        c = fn(b, *args)
        controls[c.pop("_name")] = c
    listener = _Listener(LISTENER_BIND)
    try:
        add(control_nonroot)
        add(control_network, listener.port)
    finally:
        listener.close()
    add(control_readonly)
    add(control_scratch)
    add(control_isolation)
    add(control_memory)
    add(control_cpu_rlimits)
    add(control_fork)
    add(control_output)
    add(control_timeout)
    add(control_reference)
    add(control_result_integrity)
    missing = [c for c in RUN_CONTROLS if c not in controls]
    status = "verified" if not missing and all(c["status"] == "pass" for c in controls.values() if c["required"]) else "failed"
    ver = _runtime_facts(b)
    ver["listener"] = {"bind": LISTENER_BIND, "closed": listener.closed}
    return {"status": status, "image": b.image, "runtime": ver, "controls": controls,
            "assertions_total": sum(c["assertion_count"] for c in controls.values()),
            "assertions_passed": sum(sum(1 for x in c["checks"] if x["ok"]) for c in controls.values())}


class _Listener:
    """Accept-and-close TCP listener for the network positive control. Closed by close() (always
    called from a finally block); the serving thread is joined."""
    def __init__(self, host: str):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, 0))
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        self.closed = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
                c.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        self.srv.close()
        self.closed = self.srv.fileno() == -1 and not self.thread.is_alive()


def _runtime_facts(b):
    import subprocess
    def sh(args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=30, env=sandbox._cli_env()).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"unavailable: {type(e).__name__}"
    probe = b.run_probe("import sys, platform, json; print(json.dumps({'python': sys.version.split()[0], 'impl': platform.python_implementation(), 'machine': platform.machine()}))")
    return {"docker_server": sh(["docker", "version", "--format", "{{.Server.Version}}"]),
            "docker_arch": sh(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]),
            "container_python": probe.get("data"),
            "platform": sandbox.PLATFORM}
