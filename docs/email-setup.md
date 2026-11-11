# Resend and Cloudflare email setup

ChronoSync sends application email from the backend using Resend HTTPS. Cloudflare
hosts the sender's DNS records and can forward replies to an existing mailbox.
No Cloudflare API token or Worker is needed for this setup. Keep every email and
service-role secret on the backend; there are no new `VITE_` variables.

## Required values

Only **two new backend values** are needed, in addition to the existing Supabase
server configuration:

```dotenv
RESEND_API_KEY=re_...
EMAIL_FROM=ChronoSync <notifications@updates.your-domain.com>
```

`EMAIL_FROM` must use a domain verified in your own Resend account. The domain
above is an example, not a configured identity. Use a sending-only Resend API key
scoped to that domain. `SUPABASE_SERVICE_ROLE_KEY` must already be present for
invitations and is also required by the queue worker. `SUPABASE_URL` and
`FRONTEND_URL` must point at your existing project and actual frontend origin.

Optional defaults are already provided in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMAIL_REPLY_TO` | empty | A monitored mailbox or Cloudflare forwarding alias |
| `EMAIL_WORKER_ENABLED` | `true` | Process durable queued messages when credentials exist |
| `EMAIL_POLL_SECONDS` | `15` | Delay when no message is due |
| `INVOICE_EMAIL_TRANSPORT` | `auto` | Resend when configured; otherwise existing Számlázz.hu delivery |

Set `EMAIL_WORKER_ENABLED=false` in local processes connected to a hosted
database unless you intentionally want those processes to send its pending mail.
Multiple production instances are supported through leased `SKIP LOCKED` claims.
For an explicit rollout, set `INVOICE_EMAIL_TRANSPORT=provider` while testing the
leave queue, then switch to `resend` when DNS and sender identity are verified.
The explicit `resend` value fails startup on missing credentials; partially
configured email also fails startup, naming only missing variable names.

## Cloudflare DNS and replies

1. Add your sending subdomain, such as `updates.your-domain.com`, in Resend.
   Use its **Sign in to Cloudflare** setup or copy the exact DNS values from that
   domain's Resend screen. DKIM keys and regional MX targets are account-specific.
2. Add the supplied DKIM TXT, return-path MX, and SPF TXT records at their exact
   hostnames. A subdomain changes these names: `send.updates` and
   `resend._domainkey.updates` are typical relative names. Keep email-related
   CNAME records DNS-only if Resend supplies any.
3. Preserve existing root-domain MX records used by your mailbox or Cloudflare
   Email Routing. Resend's sending return-path MX belongs at its supplied `send`
   hostname. Do not enable Resend receiving at a hostname already routed through
   Cloudflare. Do not create two SPF TXT records at the same hostname; merge
   authorized senders if that hostname already has an SPF policy.
4. Review the domain's DMARC policy and alignment with the existing mailbox
   provider. Wait until Resend reports the domain as verified.
5. If replies should be forwarded, create a Cloudflare Email Routing alias such
   as `billing@your-domain.com`, verify its destination mailbox, and set
   `EMAIL_REPLY_TO=billing@your-domain.com`. The outgoing sender remains the
   Resend-verified address; the forwarding alias receives replies.

References: [Resend's Cloudflare DNS guide](https://resend.com/docs/guides/dns/cloudflare),
[Cloudflare Email Routing](https://developers.cloudflare.com/email-service/get-started/route-emails/).

## Supabase Auth email uses the same Resend account

Invitations, magic links, and password recovery continue through Supabase Auth,
which owns their token lifecycle and existing inert application landing pages.
Configure **Custom SMTP** in the hosted project's Auth settings:

| Setting | Value |
| --- | --- |
| SMTP host | `smtp.resend.com` |
| Port | `465` (TLS) |
| Username | `resend` |
| Password | The same `RESEND_API_KEY` |
| Sender address | The plain email address from `EMAIL_FROM` |
| Sender name | `ChronoSync` |

Disable click and open tracking in Resend for the auth sending domain. Preserve
the existing Site URL, exact redirect allow-list and email templates in the
canonical backend `supabase/` directory. The commented SMTP block in
`supabase/config.toml` now shows Resend settings while retaining local mail
capture. Setting backend env variables does **not** update hosted Auth SMTP;
enter the values in the dashboard or deploy a deliberately enabled SMTP block.
Do not push the whole Auth configuration just to change SMTP without reviewing
the project's existing settings.

References: [Supabase custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp),
[Resend SMTP](https://resend.com/docs/send-with-smtp).

## Leave requests, review decisions and invoice delivery

The migration enqueues leave notifications in the same database transaction as
request creation and meaningful pending-request edits. Each active, confirmed
company admin gets an individual bilingual message with the employee, dates and
`/leaves` review link. Admin recipients are discovered from trusted profiles and
Auth emails; no recipient list needs an env variable. Other companies' admins
are excluded. Global super admins without membership in that company do not
receive its employee notifications. Before claiming a job the database cancels
obsolete dates, resolved/deleted requests and revoked admin memberships.

When a pending leave request is approved or rejected, its employee receives an
individual bilingual decision email with the dates and `/leaves` link. It is
queued in the same transaction as the decision, so a failed approval or rolled
back review sends nothing. Repeating the same status does not queue another
message. Deleted or superseded requests cancel unsent decision notifications.

When a submitted timesheet entry is rejected, the employee receives the work
date, hours, client/project, entry description and stored rejection reason, with
`/timesheet?view=attention` opening the Needs attention view on desktop and mobile.
Signed-out recipients return to the linked page after password sign-in through
the existing sanitized internal-continuation helper.
This covers rejected entries from any month. Each actual submitted-to-rejected
transition queues one message. A rejection after correction/resubmission is a
new event; any older unsent rejection is superseded. Resubmitted, approved,
deleted or materially changed entries cancel obsolete notifications before
they are claimed.

Both employee flows discover the recipient from the entry/request owner and
trusted Auth email. Only active, confirmed users still belonging to that company
receive messages; membership and email are rechecked before sending. No new
environment variables or browser-side email requests are needed.

Invoice review fingerprints now include the selected email transport. Existing
reviews must be refreshed after deployment. The saved client `send_invoice_email`
preference still decides whether any customer message is sent. In Resend mode,
Számlázz.hu explicitly receives `sendEmail=false` and no email attachment upload.
After confirmed issuance/finalization, a job contains the archived invoice PDF
and the exact reviewed XLSX attachment, including its saved digest. Confirmed
storno documents get a separate email with the archived reversal PDF. A reversal
cancels an original invoice email that has not yet been claimed.

Provider delivery remains available with `INVOICE_EMAIL_TRANSPORT=provider` and
keeps its existing attachment behavior. Historical snapshots without an explicit
Resend transport are never backfilled or emailed through Resend. Preview,
rejected, issuing and uncertain invoice attempts never enqueue customer mail.
Email failure never retries invoice creation or changes financial totals.

The worker freezes the complete outgoing message before calling Resend, uses a
stable `chronosync/<job-id>` idempotency key and retries network errors, rate
limits and server failures with bounded backoff. Claims expire after five
minutes so interrupted processes recover. Recovery stops before Resend's
24-hour idempotency window expires; ambiguous old jobs become `uncertain`.
Permanent failures stop and remain visible. Do not reset uncertain jobs without
checking the Resend dashboard by message ID/idempotency key first.

References: [Resend idempotency](https://resend.com/docs/dashboard/emails/idempotency-keys),
[Számlázz.hu email flag](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/email-notification).

## Deploy and verify

1. Apply `supabase/migrations/20261110202649_transactional_email_outbox.sql`
   after the existing leave, storno and mandatory invoice-attachment migrations.
   This creates a private, RLS-enabled queue and service-role-only sender RPCs.
   It does not queue historical requests or invoices.
   Then apply `supabase/migrations/20261110213624_employee_decision_emails.sql`
   for leave decisions and timesheet rejections. Deploy the matching backend
   before enabling the worker so it can render the new event types, and deploy
   the frontend to enable the timesheet email's Needs attention link.
2. Verify Cloudflare DNS and Resend domain status, set the backend env values,
   configure hosted Auth SMTP, and deploy the compatible backend. The worker
   checks its service-role RPC at startup and refuses to run without the migration.
   An always-running backend process is required; a static frontend alone cannot
   drain the queue.
3. Run from the backend directory:

   ```bash
   venv/bin/python scripts/check_email_setup.py
   venv/bin/python scripts/check_email_setup.py --database
   ```

   These checks never send messages or print credentials. The database option
   verifies installed queue access with a read-only service RPC. It does not
   establish Resend key validity, DNS verification, hosted SMTP or inbox receipt.
4. As an authenticated company admin, inspect
   `GET /api/v1/company/email-status?company_id=<company-id>`. It returns missing
   env names, chosen transport, queue counts and recent safe failure codes only.
   `accepted` means Resend accepted the request; actual delivery/bounces are
   checked in Resend. No delivery-webhook secret is required for basic sending.
5. In staging, use consented test addresses to verify a leave request, approval,
   rejection, timesheet rejection/correction/resubmission, invitation, recovery
   email, emailed issued invoice with both attachments, email-disabled invoice
   and confirmed reversal. Inspect actual inbox receipt and reply routing.
   Resend's `resend.dev` test sender cannot establish arbitrary-recipient delivery.
   Számlázz.hu's provider test-account redirection does not apply to emails sent
   independently by Resend; staging clients must use deliberate test recipients.

Tests: `venv/bin/python -m pytest -q tests/test_email_service.py` and the opt-in
`tests/test_email_database.py` using
`EMAIL_TEST_PG_DSN='host=/tmp/chronosync-email-tests port=55440 dbname=email_tests'`.
The database suite destroys only its explicitly named local fixture database and
rejects hosted DSNs. No automated test sends real email or issues a real invoice.

## Current hosted readiness (10 November 2026)

Read-only checks found the ChronoSync Supabase project and required leave,
invoice, archived-attachment and reversal columns. The new email queue migration
is not yet installed. The Railway production service `chronosync-backend`
has `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `ENCRYPTION_MASTER_KEY`; its missing
email-related values are `RESEND_API_KEY`, `EMAIL_FROM` and
`SUPABASE_SERVICE_ROLE_KEY`. The frontend origin already has a matching default.
This implementation has not been deployed, and no live email was sent during
verification. DNS verification and hosted Auth SMTP still require your chosen
domain/sender and Resend key.
