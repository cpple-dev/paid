# paid plan sender

This repository is the **paid** sender for the Hwasongpho-19 service.

# Hwasongpho-19 sender (GitHub Actions)

This repo runs the **sender backend** for the Hwasongpho-19 service. It is triggered
only by `workflow_dispatch`; there are no push or pull request triggers.

One dispatch is a **warm runner**, not one mail: the run joins Anywherelan,
serves every job the server pushes to it, and stops itself after 10 minutes
without a job (`idle_minutes` input) or after 5 h 30 min in total.

What the job does:

1. fetches its warm-runner bootstrap from the Hwasongpho-19 VPS
   (`/api/awl/warm/bootstrap`: VPS peer id, mesh IP, runner token),
2. downloads Anywherelan, joins the mesh, and registers its peer id,
3. accepts the VPS peer and serves `POST /send` on the mesh at port `8720`,
4. for each job: receives the payload + sender account **over the encrypted
   mesh**, builds the PDF/XLSX in memory, and sends it directly,
5. stops when no job arrived for `RUNNER_IDLE_MINUTES` (10), at
   `RUNNER_MAX_MINUTES` (330), or on `/shutdown`; then it calls
   `/api/awl/warm/release` so the VPS can dispatch a fresh run later.

- Mail is sent **without any proxy** through Microsoft Graph or GMX SMTP.
- No accounts are stored here, and none are passed through workflow inputs.
- `SERVER_URL` (public bootstrap URL) and `SHARED_SECRET` are repo secrets and
  are masked in logs.

`GET /health` lets the VPS spot a dead runner and never counts as activity, so
polling cannot keep a runner alive. `POST /shutdown` ends a run without sending
anything, and `POST /send` with `{"dry_run": true}` builds the attachment
without sending it. The sender code lives in
`sender/{agent,server,mailer,attachments}.py`; `sender/run.py` is the legacy
callback-mode runner used only when the VPS has `AWL_ENABLED=0` (one dispatch
per mail).
