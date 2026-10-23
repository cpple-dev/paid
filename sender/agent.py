#!/usr/bin/env python3
"""GitHub Action agent: join Anywherelan, announce the mesh address to the
VPS, accept the VPS peer, then run the sender backend.

Two modes:

  warm (default)  This run is a shared worker.  The VPS dispatches it when no
                  runner is alive and reuses it for every mail that arrives
                  while it is up.  It stops itself after
                  RUNNER_IDLE_SECONDS (10 min) without a job, or after
                  RUNNER_MAX_SECONDS (well below GitHub's 6 h limit).

  job (WARM_ID unset, JOB_ID set)  Legacy single-job run: one dispatch, one
                  mail.  Only used by servers running the old per-job path.

Flow:
  1. POST /api/awl/warm/bootstrap -> vps peer id, mesh ip + runner token
  2. download + start awl         -> own peer id and 10.66.x.y address
  3. POST /api/awl/warm/register  -> VPS invites this peer
  4. accept the invite            -> mesh path comes up
  5. serve /health, /send         -> VPS pushes jobs over awl, one or many
  6. POST /api/awl/warm/release   -> the slot is freed for the next dispatch

No account credentials are fetched here; the VPS sends them in the job
body only after the encrypted mesh path is up.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import threading
import time

import requests

from . import server

SERVER_URL = (os.environ.get("SERVER_URL") or "").rstrip("/")
SHARED_SECRET = os.environ.get("SHARED_SECRET") or ""
JOB_ID = os.environ.get("JOB_ID") or ""
WARM_ID = os.environ.get("WARM_ID") or ""
PORT = int(os.environ.get("RUNNER_PORT", "8720"))


def _seconds(unit, default_seconds):
    """RUNNER_<UNIT>_SECONDS or RUNNER_<UNIT>_MINUTES, whichever is set."""
    raw = (os.environ.get(f"RUNNER_{unit}_SECONDS") or "").strip()
    if raw:
        return int(raw)
    raw = (os.environ.get(f"RUNNER_{unit}_MINUTES") or "").strip()
    return int(raw) * 60 if raw else default_seconds


IDLE_SECONDS = _seconds("IDLE", 600)                    # 10 min
MAX_SECONDS = _seconds("MAX", 19800)                    # 5 h 30 min
WAIT_SECONDS = int(os.environ.get("RUNNER_WAIT_SECONDS",
                                  str(MAX_SECONDS + 300)))
AWL_VERSION = os.environ.get("AWL_VERSION", "v0.19.0")


def _post(path, payload, attempts=5, timeout=60):
    last = None
    for i in range(attempts):
        try:
            r = requests.post(f"{SERVER_URL}{path}", json=payload, headers={
                "X-Shared-Secret": SHARED_SECRET,
            }, timeout=timeout)
        except requests.RequestException as e:
            last = e
        else:
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code in (401, 403, 404):
                break
        if i + 1 < attempts:
            time.sleep(2 * (i + 1))
    raise RuntimeError(str(last))


def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _arch():
    m = os.uname().machine
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7") or m.startswith("armv6"):
        return "arm"
    raise RuntimeError(f"unsupported architecture {m}")


def install_awl():
    override = os.environ.get("AWL_BIN")
    if override and os.path.isfile(override):
        return override
    base = f"/tmp/awl-{AWL_VERSION}"
    binary = os.path.join(base, "awl")
    if os.path.isfile(binary):
        return binary
    os.makedirs(base, exist_ok=True)
    arch = _arch()
    url = (f"https://github.com/anywherelan/awl/releases/download/"
           f"{AWL_VERSION}/awl-linux-{arch}-{AWL_VERSION}.tar.gz")
    tar_path = os.path.join(base, "awl.tar.gz")
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(tar_path, "wb") as fh:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    fh.write(chunk)
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name.split("/"):
                continue
            tar.extract(member, base)
    if not os.path.isfile(binary):
        cands = [os.path.join(base, n) for n in os.listdir(base)]
        cands = [c for c in cands if os.path.isfile(c) and
                 os.access(c, os.X_OK) and not c.endswith(".gz")]
        if not cands:
            raise RuntimeError("awl binary not found in release archive")
        binary = cands[0]
    os.chmod(binary, 0o755)
    return binary


def start_awl(binary):
    data_dir = "/tmp/awl-data"
    os.makedirs(data_dir, exist_ok=True)
    env = os.environ.copy()
    env["AWL_DATA_DIR"] = data_dir
    cmd = [binary]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    log = open("/tmp/awl.log", "wb")
    proc = subprocess.Popen(cmd, env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    for _ in range(90):
        try:
            r = _run([binary, "cli", "me", "status"], timeout=15)
        except Exception:                          # noqa: BLE001
            r = None
        if r is not None and r.returncode == 0 and "VPN" in r.stdout:
            return proc
        if proc.poll() is not None:
            raise RuntimeError("awl exited during startup (see /tmp/awl.log)")
        time.sleep(1)
    raise RuntimeError("awl did not become ready")


def peer_id(binary):
    out = _run([binary, "cli", "me", "id"]).stdout
    m = re.search(r"your peer id:\s*(\S+)", out)
    if not m:
        raise RuntimeError("could not read awl peer id")
    return m.group(1)


def mesh_ip():
    out = _run(["ip", "-4", "-o", "addr", "show", "awl0"]).stdout
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        raise RuntimeError("could not read awl mesh ip")
    return m.group(1)


def accept_peer(binary, vps_peer_id):
    for _ in range(60):
        try:
            out = _run([binary, "cli", "peers", "requests"]).stdout
        except Exception:                          # noqa: BLE001
            out = ""
        if vps_peer_id in out:
            r = _run([binary, "cli", "peers", "add", "--pid", vps_peer_id,
                      "--name", "vps"])
            if r.returncode == 0:
                return True
        time.sleep(2)
    return False


def main():
    if not (SERVER_URL and SHARED_SECRET):
        print("missing SERVER_URL / SHARED_SECRET", flush=True)
        return 2
    if not (WARM_ID or JOB_ID):
        print("missing WARM_ID (warm runner) or JOB_ID (single job)", flush=True)
        return 2

    if WARM_ID:
        boot = _post("/api/awl/warm/bootstrap", {"warm_id": WARM_ID})
    else:
        boot = _post("/api/awl/bootstrap", {"job_id": JOB_ID})
    if not boot.get("ok"):
        raise RuntimeError(boot.get("error") or "bootstrap failed")
    vps_peer = boot["vps_peer_id"]
    token = boot["job_token"]
    probe = bool(boot.get("probe"))
    print(f"bootstrap ok: vps {vps_peer} mesh {boot.get('vps_mesh_ip')} "
          f"({boot.get('mode') or 'job'})", flush=True)

    state = {}
    binary = install_awl()
    proc = start_awl(binary)
    try:
        pid = peer_id(binary)
        ip = mesh_ip()
        print(f"awl up: {pid} at {ip}", flush=True)

        if WARM_ID:
            # the VPS assigns the address it will use for this run
            reg = _post("/api/awl/warm/register",
                        {"warm_id": WARM_ID, "peer_id": pid})
            exit_after = None
        else:
            reg = _post("/api/awl/register",
                        {"job_id": JOB_ID, "peer_id": pid})
            exit_after = 1
        if not reg.get("ok"):
            raise RuntimeError(reg.get("error") or "registration failed")
        state["peer_id"] = pid
        print(f"registered, vps will use {reg.get('runner_ip')}", flush=True)

        # the server must listen before the VPS health-checks, and the peer
        # must accept the VPS invite before the mesh path comes up
        def run_server():
            state.update(server.serve(token, PORT, WAIT_SECONDS,
                                      idle=IDLE_SECONDS,
                                      max_life=MAX_SECONDS,
                                      probe=probe, exit_after=exit_after))

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        time.sleep(1)
        if not accept_peer(binary, vps_peer):
            print("warning: VPS peer invite not seen", flush=True)
        thread.join()
    finally:
        try:
            proc.terminate()
        except Exception:                          # noqa: BLE001
            pass
        if WARM_ID:
            try:
                _post("/api/awl/warm/release",
                      {"warm_id": WARM_ID,
                       "peer_id": state.get("peer_id") or "",
                       "reason": state.get("reason") or "exited"}, attempts=2)
            except Exception:                      # noqa: BLE001
                pass                               # the VPS reaper notices too

    print(f"runner stopped ({state.get('reason') or 'timeout'}): "
          f"{state.get('jobs', 0)} job(s)", flush=True)
    return 0 if state.get("reason") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # noqa: BLE001
        print(f"agent failed: {str(exc)[:300]}", flush=True)
        sys.exit(1)
