#!/usr/bin/env python3
"""GitHub Actions entrypoint.

Steps:
  1. fetch this run's job + sender account from the Hwasongpho-19 server
  2. generate the PDF/XLSX attachment in memory
  3. send it directly (no proxy) through Graph or GMX
  4. report the result back to the server

Environment (provided by the workflow; SERVER_URL/SHARED_SECRET are repo
secrets, never workflow inputs):
    SERVER_URL, SHARED_SECRET, JOB_ID
"""
from __future__ import annotations

import os
import random
import sys
import time

import requests

from . import attachments, mailer

SERVER_URL = (os.environ.get("SERVER_URL") or "").rstrip("/")
SHARED_SECRET = os.environ.get("SHARED_SECRET") or ""
JOB_ID = os.environ.get("JOB_ID") or ""

MAX_BYTES = {
    "microsoft": mailer.UPLOAD_MAX_BYTES,
    "gmx": mailer.GMX_MAX_BYTES,
}


def _post(path, payload, attempts=4, timeout=60):
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
            last = RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:160]}")
            if r.status_code in (401, 403):
                break
        if i + 1 < attempts:
            time.sleep(2 * (i + 1))
    raise RuntimeError(str(last))


def fetch_job():
    data = _post("/api/github/job", {"job_id": JOB_ID})
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "job fetch failed")
    return data


def report(success, size_mb=0.0, size_bytes=0, error=""):
    try:
        _post("/api/github/report", {
            "job_id": JOB_ID,
            "success": bool(success),
            "size_mb": size_mb,
            "size_bytes": size_bytes,
            "error": (error or "")[:300],
        })
    except Exception as e:                         # noqa: BLE001
        print(f"report failed: {str(e)[:200]}", flush=True)


def build_target(payload, provider):
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


def main():
    if not (SERVER_URL and SHARED_SECRET and JOB_ID):
        print("missing SERVER_URL / SHARED_SECRET / JOB_ID", flush=True)
        return 2

    job = fetch_job()
    payload = job.get("payload") or {}
    creds = job.get("creds") or {}
    provider = job.get("provider") or "microsoft"
    ext = str(payload.get("extension", "pdf")).lower()
    receiver = str(payload.get("receiver", "")).strip()
    subject = str(payload.get("subject", ""))
    body = str(payload.get("body", ""))

    if not receiver:
        raise RuntimeError("receiver required")

    data = None
    if payload.get("attachment", True):
        data = attachments.build_attachment(
            ext, build_target(payload, provider))

    size_bytes = len(data) if data else 0
    size_mb = round(size_bytes / (1024 * 1024), 6)

    try:
        if provider == "gmx":
            mailer.send_gmx(creds["email"], creds["password"], receiver,
                            subject, body, ext, data)
        else:
            r = mailer.send_microsoft(creds["access_token"], receiver,
                                      subject, body, ext, data)
            if r.status_code != 202:
                try:
                    detail = r.json()
                    err = detail.get("error", {}).get("message", r.text[:300])
                except ValueError:
                    err = r.text[:300]
                raise RuntimeError(f"graph {r.status_code}: {err}")
    except Exception as e:                         # noqa: BLE001
        msg = str(e)[:300]
        report(False, size_mb, size_bytes, msg)
        print(f"send failed: {msg}", flush=True)
        return 1

    report(True, size_mb, size_bytes)
    print(f"sent {size_mb} MB via {provider} ({creds.get('email', '')})",
          flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # noqa: BLE001
        report(False, 0, 0, str(exc)[:300])
        print(f"fatal: {str(exc)[:300]}", flush=True)
        sys.exit(1)
