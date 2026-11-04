# Staging fixes and production rollout — updated 28 October 2026

The user-approved production release is live. Core application operations and
Számlázz.hu billing passed the deployed staging-tenant checks, including creation
of test invoice **TESZT-2026-6** through Railway. Email onboarding and recovery
remain deferred and are not claimed operational or fully tested.

## Released versions

| Component | Verified release |
| --- | --- |
| Backend | [PR #1](https://github.com/palyikris/chronosync-backend/pull/1), merged as `51a679d01c8852a7b4a553c74161eec8b53a14b8` |
| Railway production | Deployment `549f449c-1e4e-4250-ac02-1ca2fdbca280`, SUCCESS; no pending staged/applying work |
| Frontend | [PR #3](https://github.com/palyikris/chronosync/pull/3), merged as `1260dda5f8da06712c977e1c1089df08232da067` |
| Vercel production | Successful deployment `25FhrrWENRcqpypxBHpJkeKMuw3R`; live browser sign-in and billing review verified |
| Shared Supabase | `kqihfxhewjgrwoepdsau` |

The explicitly approved coordinated migration
`20261027133957_trusted_invitations_and_atomic_leave.sql` is **applied**. The
Supabase migration tool twice failed with expired request-state errors without
applying it. The original SQL and migration-history record were then applied in
one transaction through `psql`, with advisory locking, a replay guard and fully
verified TLS. The migration history, leave RPCs and deployed restricted-column
access were read back afterward.

Earlier approved migrations remain applied:

- `20261027091353_avoid_retryable_invoice_business_conflicts.sql`
- `20261027132449_recover_existing_invoice_pdf.sql`
- `20261027133954_secure_tenant_boundaries.sql`
- `20261027141923_protect_approved_leave_cancellation.sql`

## What the release fixes

- Correct Számlázz.hu XML, response validation, currency/rounding comparisons and
  compressed HTTP responses; reviewed totals remain consistent with provider PDFs.
- Existing-invoice PDF recovery fetches and stores the original document without
  creating another invoice. Uncertain outcomes retain their reservations.
- Tenant-scoped authorization, inactive-member restrictions, guarded profile
  changes, last-administrator protection, consistent tenant references and inert
  uninvited signup provisioning.
- Credential columns are unavailable to ordinary Data API reads. Authorized
  backend RPCs still retrieve the encrypted staging key successfully; the UI
  receives configuration status only.
- Trusted invitation records and verified identity claims replace user-controlled
  membership metadata. Invitation sending requires server-only configuration.
- Atomic leave approval and cancellation create/remove linked entries together;
  invoice/reservation guards remain enforced.
- Explicit email action pages, numeric-code entry, safe continuations, password
  setup and URL scrubbing are implemented. Hosted custom templates and real
  delivered-email tests are still deferred.

## Deployed validation on 28 October

`hosted-rollout-results-2026-10-28.json` records **42 distinct passing checks**.
These are hosted API/database/browser checks, separate from the unit suites.

| Workflow | Result |
| --- | --- |
| Admin and worker authentication | Both authenticated to the hosted Auth service and deployed browser app |
| Tenant boundaries | Both identities saw only their staging company/profiles; cross-company backend access returned 403 |
| Sensitive access | Credential-column reads returned 403; worker member-email and invoice-settings access returned 403 |
| Team UI | Exactly the staging administrator and regular worker appeared |
| Client/project operations | Create, edit, deactivate and reactivate passed through the same Data API used by the UI |
| Worker time entry | Created draft, edited to 2.5 hours, submitted, then approved by administrator |
| Leave | Worker self-approval returned 403; admin approval created two linked entries totaling 16 hours; direct approved-leave deletion returned 403; owner cancellation removed leave and entries atomically |
| Review and provider preview | 2.5 hours × 10,000 HUF = 25,000 net + 6,750 VAT = 31,750 gross; actual provider PDF returned 200 and rendered correctly |
| Preview/stale review guards | Both returned 409; browser confirmation and issuance were disabled in preview mode |
| Original issued PDF | TESZT-2026-5 returned the original 20,573-byte document |
| Missing-PDF recovery | Temporarily removed only the staging invoice's PDF with a private backup; deployed endpoint restored the exact original bytes; repeat download matched |
| Hosted provider issuance | One creation request produced TESZT-2026-6; confirmed ledger and PDF, matching totals, captured entry changed to invoiced |
| Duplicate protection | Repeat request returned 404 because no approved uninvoiced work remained; exactly one ledger record for the new client |
| Worker UI | Showed the invoiced entry, hid admin navigation, and redirected a direct billing URL back to Timesheets |
| Hosted signup policy | Public signup disabled and read back through Auth settings; confirmation remains required |

The staging tenant was `ChronoSync staging E2E 2026-10-27`,
`d2026102-7000-4000-8000-000000000001`. Existing companies' business records and
credentials were not replaced. The only temporary account confirmation was for
worker `683d5d37-18d4-4ddc-86fa-206033552ce1`, following the user's explicit
approval; it has been restored to unconfirmed.

The dedicated provider account was visibly verified in Tesztüzem on 27 October;
the user reconfirmed that mode on 28 October before the new creation request.
The current provider browser login had no linked company, so today's provider
mode is recorded as human-confirmed rather than newly observed in its dashboard.
The new PDF identifies `TESZT - Teszt cég`, has number TESZT-2026-6 and the provider's
model watermark. Invoice email was explicitly false. **Zero emails were sent.**
No NAV submission or real customer email delivery is claimed.

New hosted invoice:

- Ledger `a8fbdc28-e814-4dbe-b0fa-e8dd25939a2c`, number `TESZT-2026-6`.
- PDF 20,631 bytes, SHA-256
  `bf56dad70620d2f2a75e05a57a0546d806833333f25a29a62d5ffed9f6709220`.
- Net 25,000 HUF; VAT 6,750 HUF; gross 31,750 HUF. Text and rendered-page values
  agree with review and stored ledger totals.

The recovered original PDF SHA-256 remains
`5565cafe056b6872d7566408a0008b791cb5fe99b84a3497146ad8c06f94fa67`.

Evidence is in `staging-evidence-2026-10-28/`: deployed billing review, worker
invoiced-entry screen, saved signup policy, actual preview and issued PDF/render.
The diagnostic scripts' initial fixture mismatches (three-character client code,
missing test leave mapping, restricted company return columns, PDF bytea column,
and review list field) were corrected before their affected checks passed; they
were harness/setup issues, not additional production code changes.

## Cleanup verified

The company is inactive and remains in preview mode. Both fixture Auth logins are
banned; the worker is inactive and unconfirmed again. Both browser sessions were
signed out, API sessions logged out, and the private credential/session file was
removed. No staging invoice is issuing or uncertain. Test clients, projects,
invoiced entries and the test invoices remain as explicitly labelled evidence.
A staging-only leave mapping was configured through the application Data API.

## Prior source and isolated-database checks

- Backend: **248 passed**, including real isolated PostgreSQL tests.
- Focused security/invoice/leave database suite: **33 passed** after rollout split.
- Frontend: **58 passed**; production build and changed-file lint passed.
- Official XML/XSD contract, gzip handling, uncertain outcomes, reservation
  preservation, idempotent recovery and concurrent database operations passed.
- The earlier actual provider preview matrix covered fixed monthly EUR, private
  HUF rounding, Hungarian VAT groups, EU/non-EU businesses and AAM. Those matrix
  cases used the current source locally with hosted data; today's deployed
  preview/issuance used hourly HUF. Do not conflate the two layers.

The PostgreSQL suite resets only disposable fixtures under
`/tmp/chronosync-invoice-audit`; this is not a claim that every historical Supabase
object can be bootstrapped from an empty project.

## Remaining external configuration and limits

The user explicitly deferred SMTP, hosted email templates, invitations/recovery
email delivery, scanner/prefetch testing with delivered mail, and manual email
codes. Local implementation/tests do not establish these hosted flows.

To finish email onboarding and recovery later:

1. Configure SMTP directly in Supabase, with link tracking disabled. The user has
   no domain; Gmail with a Google App Password was offered for staging. The prior
   unsaved Gmail draft was cancelled; no credential or SMTP change was saved.
2. Set the server-only `SUPABASE_SERVICE_ROLE_KEY` in Railway for trusted
   invitations. Its absence was verified from variable names; no secret was
   revealed. Never place it in frontend variables. Explicitly set `FRONTEND_URL`
   to the verified production origin when completing this configuration.
3. Activate the versioned inert invite/magic/recovery templates from the canonical
   backend `supabase/` directory, then test new/existing invitations, recovery,
   scanner safety, manual codes and continuation behavior using delivered email.
   The existing default templates must not be mistaken for the released custom
   templates. Invitation sending currently fails closed without configuration.

Already saved and verified: Site URL `https://chronosync-lovat.vercel.app`, exact
production `/auth/invite`, `/auth/magic`, `/auth/recover` redirects, minimum password
eight, OTP length eight and expiry 3,600 seconds, confirmation/secure email change
on, anonymous sign-in/manual linking off, public signup off. The existing
localhost redirect was preserved.

Legacy Supabase advisor findings remain: mutable-search-path functions, callable
legacy definer helpers, and leaked-password protection disabled (a paid-plan
feature). Disabled RLS on leave was fixed. Intentional closed private/audit tables
and explicitly authorized definer RPCs also produce advisory notices; this report
does not claim every advisor finding has been eliminated.

No further billing deployment or migration is pending. The remaining onboarding
and recovery configuration is separate from the now-verified hosted invoice and
application-operation paths.
