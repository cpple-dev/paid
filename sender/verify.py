#!/usr/bin/env python3
"""Recipient address verification on the GitHub runner - nothing is sent.

The VPS cannot open outbound port 25 (the ISP filters it), so the SMTP stage
of the mailbox check runs here instead: GitHub-hosted runners do reach port
25, measured at a `220` greeting from gmail-smtp-in in 5 ms.

The probe is the same one the local backend performs:

  1. syntax + DNS   - well formed address, existing domain, MX (or an A
                      record, RFC 5321 5.1) that could accept mail
  2. SMTP RCPT      - greet the MX, hand over the envelope sender, offer the
                      receiver with RCPT TO.  The answer (250 = mailbox
                      exists, 5xx = no such user) arrives without a message
                      ever being transmitted: DATA is never sent, the session
                      ends with QUIT.

Verdicts match backend/verify_email.py exactly so the VPS can forward them to
the browser unchanged:

  valid   - the receiving server accepted the recipient
  invalid - no such domain / no mail there, or the mailbox was refused
  unknown - greylisting, a policy refusal, a timeout: never blocks a send
"""
from __future__ import annotations

import os
import re
import smtplib
import threading
import time

import dns.exception
import dns.resolver

SETTINGS = {
    # Greeting name.  Some MXs reject a HELO that is not a resolvable host,
    # so this is the same name the VPS uses.
    "helo_host": os.environ.get("VERIFY_HELO_HOST", "mail.apizio.com"),
    "mail_from": os.environ.get("VERIFY_MAIL_FROM", ""),
    "port": int(os.environ.get("VERIFY_SMTP_PORT", "25")),
    "connect_timeout": float(os.environ.get("VERIFY_CONNECT_TIMEOUT", "8")),
    # Upper bound for one whole check, so a slow MX cannot hold a job slot.
    "deadline": float(os.environ.get("VERIFY_DEADLINE", "15")),
    "cache_ttl": float(os.environ.get("VERIFY_CACHE_TTL", "600")),
    "mx_tries": int(os.environ.get("VERIFY_MX_TRIES", "3")),
}

if not SETTINGS["mail_from"]:
    labels = str(SETTINGS["helo_host"]).strip(".").split(".")
    domain = ".".join(labels[-2:]) if len(labels) >= 2 else labels[0]
    SETTINGS["mail_from"] = "verify@" + domain

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# ------------------------------------------------------------------- syntax
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def syntax_ok(address):
    """RFC 5321/5322-ish check: no quoting, no unicode local parts."""
    if not address or len(address) > 254 or address.count("@") != 1:
        return False
    local, _, domain = address.partition("@")
    if not _LOCAL_RE.match(local):
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    labels = domain.rstrip(".").split(".")
    if len(labels) < 2 or len(domain) > 253:
        return False
    if not all(_LABEL_RE.match(label) for label in labels):
        return False
    tld = labels[-1]
    return len(tld) >= 2 and not tld.isdigit()


# ---------------------------------------------------------------------- dns
def _resolver():
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 6.0
    resolver.timeout = 4.0
    return resolver


def mx_hosts(domain):
    """(hosts, why) - 'mx', 'implicit', 'null_mx', 'no_mail', 'no_domain'
    or 'dns_error'."""
    resolver = _resolver()
    try:
        answer = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return [], "no_domain"
    except dns.resolver.NoAnswer:
        answer = None
    except (dns.resolver.NoNameservers, dns.exception.DNSException):
        return [], "dns_error"

    hosts = []
    if answer is not None:
        for record in answer:
            exchange = str(record.exchange).rstrip(".")
            if exchange in ("", "."):       # RFC 7505 null MX: no mail here
                return [], "null_mx"
            hosts.append((int(record.preference), exchange))
    if hosts:
        hosts.sort()
        return [host for _, host in hosts], "mx"

    # No MX at all: RFC 5321 5.1 says fall back to the address itself.
    try:
        resolver.resolve(domain, "A")
    except dns.resolver.NXDOMAIN:
        return [], "no_domain"
    except dns.exception.DNSException:
        return [], "dns_error"
    return [domain], "implicit"


# ------------------------------------------------------------- probe replies
_NOUSER_RE = re.compile(
    r"does not exist|doesn'?t exist|no such|unknown (user|recipient|mailbox)"
    r"|user unknown|not found|invalid (recipient|mailbox|address|user)"
    r"|recipient (address )?(rejected|unknown)"
    r"|mailbox (unavailable|not found|disabled|inactive|deactivated)"
    r"|address (rejected|does not exist)|no mailbox|unknown local part"
    r"|user not found|account (does not exist|disabled|not found)"
    r"|inactive|deactivated",
    re.I)
_POLICY_RE = re.compile(
    r"spam|block(ed|list)|blacklist|deny ?list|policy|reputation|rbl"
    r"|too many|rate ?limit|throttl|access denied|not permitted|relay"
    r"|virus|malware|greylist|try again|temporar",
    re.I)


def _verdict(status, reason, detail, **extra):
    out = {"status": status,
           "exists": (True if status == "valid"
                      else False if status == "invalid" else None),
           "reason": reason, "detail": detail}
    out.update(extra)
    return out


def classify(code, text, host):
    """Turn one SMTP RCPT reply into a verdict."""
    text = (text or "").strip().replace("\n", " ")
    short = text[:200]
    if 200 <= code <= 251:
        return _verdict("valid", "accepted",
                        "The receiving server accepted this mailbox.",
                        code=code, server=host, reply=short)
    if code in (550, 551, 553, 554, 555, 556):
        if _NOUSER_RE.search(text):
            return _verdict("invalid", "no_mailbox",
                            "That mailbox does not exist on the receiving "
                            "server.", code=code, server=host, reply=short)
        if _POLICY_RE.search(text):
            return _verdict("unknown", "policy_block",
                            "The receiving server refused to answer for this "
                            "address (policy/anti-spam).", code=code,
                            server=host, reply=short)
        if code in (551, 553):
            return _verdict("invalid", "no_mailbox",
                            "That mailbox does not exist on the receiving "
                            "server.", code=code, server=host, reply=short)
        return _verdict("unknown", "rejected_other",
                        "The receiving server refused the address without "
                        "saying why.", code=code, server=host, reply=short)
    if 400 <= code <= 499:
        return _verdict("unknown", "deferred",
                        "The receiving server asked us to try again later "
                        "(greylisting).", code=code, server=host, reply=short)
    return _verdict("unknown", "unexpected_reply",
                    "The receiving server gave an unusual answer.",
                    code=code, server=host, reply=short)


# ------------------------------------------------------------ smtp session
def _one(reply):
    text = (reply or b"")
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return " ".join(str(text).split())[:200]


def _why(exc):
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return "timed out"
    return str(exc)[:120] or exc.__class__.__name__


def _probe(address, hosts):
    helo = SETTINGS["helo_host"]
    sender = SETTINGS["mail_from"]
    port = SETTINGS["port"]
    deadline = time.time() + SETTINGS["deadline"]
    last = None
    for host in hosts[: SETTINGS["mx_tries"]]:
        left = deadline - time.time()
        if left <= 0.5:
            break
        timeout = max(1.0, min(SETTINGS["connect_timeout"], left))
        try:
            smtp = smtplib.SMTP(host, port, local_hostname=helo,
                                timeout=timeout)
        except (OSError, smtplib.SMTPException) as exc:
            last = {"server": host, "error": _why(exc)}
            continue
        try:
            code, msg = smtp.ehlo(helo)
            if code >= 400:
                code, msg = smtp.helo(helo)
            if code >= 400:
                last = {"server": host, "code": code, "reply": _one(msg),
                        "error": "greeting rejected"}
                continue
            code, msg = smtp.mail(sender)
            if code >= 400:
                last = {"server": host, "code": code, "reply": _one(msg),
                        "error": "envelope sender rejected"}
                continue
            code, msg = smtp.rcpt(address)
            return classify(code, _one(msg), host)
        except (OSError, smtplib.SMTPException) as exc:
            last = {"server": host, "error": _why(exc)}
        finally:
            try:
                smtp.quit()
            except Exception:            # noqa: BLE001 - closing best effort
                try:
                    smtp.close()
                except Exception:        # noqa: BLE001
                    pass
    if last is None:
        return _verdict("unknown", "timeout",
                        "The receiving server did not answer in time.")
    if last.get("code"):
        return _verdict("unknown", "rejected_other",
                        "The receiving server refused the check itself: "
                        + last.get("reply", ""), code=last["code"],
                        server=last["server"])
    return _verdict("unknown", "unreachable",
                    "Could not reach the receiving server "
                    f"({last.get('server')}): {last.get('error')}.")


# ------------------------------------------------------------------- public
def check(address, use_cache=True):
    """Verdict for one address.  Never sends mail, never raises."""
    started = time.time()
    address = (address or "").strip()
    key = address.lower()

    if not syntax_ok(key):
        return _verdict("invalid", "syntax",
                        "That is not a valid email address.", email=address,
                        ms=int((time.time() - started) * 1000))

    with _lock:
        hit = _cache.get(key)
        if (use_cache and hit and time.time() - hit[0] < SETTINGS["cache_ttl"]):
            cached = dict(hit[1])
            cached["cached"] = True
            return cached

    local, _, domain = key.partition("@")
    result = _check_uncached(address, domain)
    result["email"] = address
    result.setdefault("ms", int((time.time() - started) * 1000))
    with _lock:
        _cache[key] = (time.time(), result)
    return dict(result)


def _check_uncached(address, domain):
    hosts, why = mx_hosts(domain)
    if why == "no_domain":
        return _verdict("invalid", "no_domain",
                        "The domain of that address does not exist.")
    if why == "null_mx":
        return _verdict("invalid", "no_mail",
                        "That domain does not accept any mail (null MX).")
    if not hosts:
        return _verdict("invalid", "no_mail",
                        "That domain has no mail server to deliver to.")
    return _probe(address, hosts)


def cache_clear():
    with _lock:
        _cache.clear()
