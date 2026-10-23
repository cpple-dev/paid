#!/usr/bin/env python3
"""Direct (NO PROXY) delivery for the GitHub runner.

Microsoft: Graph sendMail for <=20 MB inline attachments, createUploadSession
chunked upload above that.
GMX: classic SMTP submission (smtp.gmx.com:587, STARTTLS).

There is deliberately no proxy handling anywhere in this module: when mail
is sent through GitHub the connection leaves from the runner directly.
"""
from __future__ import annotations

import base64
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage

import requests

from .filenames import attachment_name

GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"

INLINE_MAX_BYTES = 20 * 1024 * 1024      # proven live for inline Graph
UPLOAD_MAX_BYTES = 150 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 3 * 1024 * 1024

SMTP_HOST = "smtp.gmx.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 180
GMX_MAX_BYTES = 50 * 1024 * 1024

# MIME subtype per attachment extension (matches attachment_name()).
_SUBTYPES = {
    "pdf": "pdf",
    "xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def unique_body(body):
    """One random suffix per mail (never stored anywhere)."""
    return f"{body} {uuid.uuid4()}"


def _graph_addresses(values):
    return [{"emailAddress": {"address": v}} for v in values]


def _graph_recipient_fields(recipients):
    """Graph message fields for To, plus CC/BCC when present."""
    fields = {"toRecipients": _graph_addresses(recipients["to"])}
    if recipients.get("cc"):
        fields["ccRecipients"] = _graph_addresses(recipients["cc"])
    if recipients.get("bcc"):
        fields["bccRecipients"] = _graph_addresses(recipients["bcc"])
    return fields


# ------------------------------------------------------------- microsoft
def send_graph_inline(token, recipients, subject, body, filename, data,
                      save_to_sent=True):
    msg = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        **_graph_recipient_fields(recipients),
    }
    if data:
        msg["attachments"] = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentBytes": base64.b64encode(data).decode(),
            "isInline": False,
        }]
    return requests.post(
        GRAPH_SEND_URL,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"message": msg, "saveToSentItems": bool(save_to_sent)},
        timeout=180)


def send_graph_upload(token, recipients, subject, body, filename, data,
                      save_to_sent=True):
    hdr = {"Authorization": f"Bearer {token}",
           "Content-Type": "application/json"}
    r = requests.post(f"{GRAPH_ME_URL}/messages", headers=hdr, json={
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        **_graph_recipient_fields(recipients),
    }, timeout=120)
    r.raise_for_status()
    mid = r.json()["id"]

    r = requests.post(
        f"{GRAPH_ME_URL}/messages/{mid}/attachments/createUploadSession",
        headers=hdr, json={"attachmentItem": {
            "attachmentType": "file", "name": filename, "size": len(data)}},
        timeout=120)
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]

    total = len(data)
    off = 0
    for _ in range(500):
        chunk = data[off:off + UPLOAD_CHUNK_BYTES]
        last = off + len(chunk) - 1
        pr = requests.put(upload_url, headers={
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {off}-{last}/{total}",
            "Content-Type": "application/octet-stream",
        }, data=chunk, timeout=600)
        if pr.status_code not in (200, 201, 202):
            raise RuntimeError(f"chunk upload failed: {pr.status_code} "
                               f"{pr.text[:200]}")
        try:
            nxt = pr.json().get("nextExpectedRanges") or []
        except ValueError:
            nxt = []
        if not nxt:
            break
        off = int(nxt[0].split("-")[0])
    else:
        raise RuntimeError("upload session did not complete")

    ok = False
    for _ in range(15):
        rr = requests.get(f"{GRAPH_ME_URL}/messages/{mid}/attachments"
                          "?$select=name,size", headers=hdr, timeout=60)
        for a in rr.json().get("value", []):
            if a.get("name") == filename:
                ok = True
                break
        if ok:
            break
        time.sleep(1)
    if not ok:
        raise RuntimeError("attachment did not register on the draft")

    r = requests.post(f"{GRAPH_ME_URL}/messages/{mid}/send", headers=hdr,
                      json={}, timeout=120)
    if r.status_code != 202:
        raise RuntimeError(f"draft send failed: {r.status_code} "
                           f"{r.text[:200]}")

    for attempt in range(2):
        for _ in range(10):
            rr = requests.get(
                f"{GRAPH_ME_URL}/mailFolders/drafts/messages/{mid}"
                "?$select=id", headers=hdr, timeout=60)
            if rr.status_code == 404:
                return r
            time.sleep(1)
        if attempt == 0:
            requests.post(f"{GRAPH_ME_URL}/messages/{mid}/send",
                          headers=hdr, json={}, timeout=120)
    raise RuntimeError("draft stuck in Drafts after send (retried)")


def send_microsoft(token, recipients, subject, body, ext, data):
    body = unique_body(body)
    name = attachment_name(ext) if data else None
    if data and len(data) > INLINE_MAX_BYTES:
        return send_graph_upload(token, recipients, subject, body, name, data)
    return send_graph_inline(token, recipients, subject, body, name, data)


# ------------------------------------------------------------------- gmx
def send_gmx(sender, password, recipients, subject, body, ext, data):
    body = unique_body(body)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients["to"])
    if recipients.get("cc"):
        msg["Cc"] = ", ".join(recipients["cc"])
    if recipients.get("bcc"):
        # send_message() puts these in the SMTP envelope and strips the
        # header from the transmitted copy
        msg["Bcc"] = ", ".join(recipients["bcc"])
    msg["Subject"] = subject
    msg.set_content(body)
    if data:
        msg.add_attachment(
            data, maintype="application",
            subtype=_SUBTYPES.get((ext or "").lower(), "octet-stream"),
            filename=attachment_name(ext))
    smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
    try:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(sender, password)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:                          # noqa: BLE001
            pass
