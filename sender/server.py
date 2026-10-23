#!/usr/bin/env python3
"""Sender backend that runs inside the GitHub Action.

The VPS pushes a job (payload + sender account) over Anywherelan to
POST /send.  The attachment is generated in memory and mailed directly
(no proxy).

The runner is **warm**: one workflow run keeps serving jobs and stops
itself only after IDLE_SECONDS (10 minutes by default) without a new one,
so a burst of mails reuses the same runner instead of starting one Action
per mail.  /health lets the VPS spot a dead runner, /shutdown ends a run
without sending anything, and "dry_run": true builds the attachment
without sending it (verification path).
"""
from __future__ import annotations

import hmac
import random
import re
import threading
import time

from flask import Flask, jsonify, request

from . import attachments, mailer, verify

MAX_BYTES = {
    "microsoft": mailer.UPLOAD_MAX_BYTES,
    "gmx": mailer.GMX_MAX_BYTES,
}

DEFAULT_IDLE_SECONDS = 600


def _as_list(value):
    """Accept a list or a comma/semicolon separated string."""
    if isinstance(value, str):
        value = re.split(r"[,;\n]+", value)
    if not isinstance(value, (list, tuple)):
        value = [value] if value else []
    return [str(v).strip() for v in value if str(v).strip()]


def _recipients(payload, receiver):
    """To / CC / BCC of one job; `receiver` is the fallback To address."""
    to = _as_list(payload.get("to")) or ([receiver] if receiver else [])
    return {"to": to,
            "cc": _as_list(payload.get("cc")),
            "bcc": _as_list(payload.get("bcc"))}


def _target_bytes(payload, provider):
    if not payload.get("attachment", True):
        return 0
    try:
        lo = max(float(payload.get("min_size_mb", 0) or 0), 0.0)
        hi = max(float(payload.get("max_size_mb", 0) or 0), lo)
    except (TypeError, ValueError):
        lo = hi = 0.0
    if hi <= 0:
        return 0
    target = int(random.uniform(lo * 1024 * 1024, hi * 1024 * 1024))
    return min(target, MAX_BYTES[provider])


def build_app(token, state, done, lock, idle=DEFAULT_IDLE_SECONDS,
              exit_after=None):
    app = Flask(__name__)

    def authorized():
        got = request.headers.get("X-Job-Token", "")
        return bool(token) and hmac.compare_digest(got, token)

    def finish(reason):
        state["reason"] = reason
        state["handled"] = True
        done.set()

    def handle(d):
        """Run one job.  Returns (body, http status)."""
        # Mailbox check: the VPS cannot open port 25, the runner can.  No
        # attachment, no account, no mail - just the RCPT answer.
        if str(d.get("action") or "") == "verify":
            addr = str(d.get("receiver") or d.get("email") or "").strip()
            if not addr:
                return {"success": False, "error": "receiver required"}, 400
            return {"success": True,
                    "verify": verify.check(addr,
                                           use_cache=not d.get("fresh"))
                    }, 200

        provider = str(d.get("provider", "microsoft")).lower()
        receiver = str(d.get("receiver", "")).strip()
        subject = str(d.get("subject", ""))
        body = str(d.get("body", ""))
        ext = str(d.get("extension", "pdf")).lower()
        creds = d.get("creds") or {}
        if provider not in MAX_BYTES:
            return {"success": False, "error": "bad provider"}, 400
        if not receiver:
            return {"success": False, "error": "receiver required"}, 400
        if ext not in ("pdf", "xlsx"):
            return {"success": False, "error": "bad extension"}, 400
        recipients = _recipients(d, receiver)

        size_mb, size_bytes = 0.0, 0
        try:
            data = None
            if d.get("attachment", True):
                data = attachments.build_attachment(
                    ext, _target_bytes(d, provider))
            size_bytes = len(data) if data else 0
            size_mb = round(size_bytes / (1024 * 1024), 6)

            if d.get("dry_run"):
                return {"success": True, "dry_run": True, "size_mb": size_mb,
                        "size_bytes": size_bytes}, 200

            if provider == "gmx":
                mailer.send_gmx(creds["email"], creds["password"],
                                recipients, subject, body, ext, data)
            else:
                r = mailer.send_microsoft(creds["access_token"], recipients,
                                          subject, body, ext, data)
                if r.status_code != 202:
                    try:
                        detail = r.json()
                        err = detail.get("error", {}).get(
                            "message", r.text[:300])
                    except ValueError:
                        err = r.text[:300]
                    raise RuntimeError(f"graph {r.status_code}: {err}")
        except Exception as e:                     # noqa: BLE001
            return {"success": False, "error": str(e)[:300],
                    "size_mb": size_mb, "size_bytes": size_bytes}, 200

        return {"success": True, "size_mb": size_mb,
                "size_bytes": size_bytes}, 200

    @app.get("/health")
    def health():
        # Never counts as activity: the VPS polls this to see if the runner
        # is still there, and polling must not keep it alive.
        if not authorized():
            return jsonify(ok=False, error="unauthorized"), 401
        with lock:
            return jsonify(ok=True, jobs=state["jobs"],
                           busy=state["inflight"] > 0,
                           idle_seconds=round(time.time()
                                              - state["last_activity"], 1),
                           idle_timeout=idle)

    @app.post("/send")
    def send():
        if not authorized():
            return jsonify(success=False, error="unauthorized"), 401
        d = request.get_json(force=True, silent=True) or {}
        with lock:
            state["inflight"] += 1
        try:
            payload, status = handle(d)
        finally:
            with lock:
                state["inflight"] = max(state["inflight"] - 1, 0)
                state["jobs"] += 1
                state["last_activity"] = time.time()
        state["result"] = payload
        if exit_after and state["jobs"] >= exit_after:
            finish("job-done")
        return jsonify(payload), status

    @app.post("/shutdown")
    def shutdown():
        if not authorized():
            return jsonify(ok=False, error="unauthorized"), 401
        finish("shutdown")
        return jsonify(ok=True)

    return app


def _watchdog(state, lock, done, idle, max_life):
    """End the run when there is nothing left to do: no job for `idle`
    seconds, or the maximum lifetime (stay clear of GitHub's 6 h cap)."""
    while not done.is_set():
        time.sleep(1)
        with lock:
            if state["inflight"] > 0:
                continue
            idle_for = time.time() - state["last_activity"]
            life = time.time() - state["started"]
        if max_life and life >= max_life:
            state["reason"] = "max-lifetime"
            done.set()
            return
        if idle and idle_for >= idle:
            state["reason"] = "idle"
            done.set()
            return


def serve(token, port, timeout, probe=False, idle=DEFAULT_IDLE_SECONDS,
          max_life=None, exit_after=None):
    """Serve jobs until idle / max_life / timeout / /shutdown."""
    from werkzeug.serving import make_server

    done = threading.Event()
    lock = threading.RLock()
    state = {"handled": False, "result": None, "probe": probe,
             "reason": "", "jobs": 0, "inflight": 0,
             "started": time.time(), "last_activity": time.time()}
    app = build_app(token, state, done, lock, idle=idle,
                    exit_after=exit_after)
    srv = make_server("0.0.0.0", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_watchdog,
                     args=(state, lock, done, idle, max_life),
                     daemon=True).start()
    done.wait(timeout)
    srv.shutdown()
    return state
