# Submitted timesheet undo

## Cause confirmed

Read-only inspection of the live ChronoSync database confirmed that
`timesheets_update` permits employee updates only when the existing status is
`draft` or `rejected`. The undo button sends `status = 'draft', submitted_at = null`
for a submitted row, so RLS excludes it and PostgreSQL updates zero rows without
raising an error. Administrators are exempt from this restriction.

The frontend checked only `error`, reported success, and invalidated its queries.
The reload correctly displayed the unchanged submitted row, making repeated
clicks appear to do nothing.

Before the fix, the new PostgreSQL regression failed because `UPDATE ... RETURNING
status` returned an empty result instead of `draft`. The service regression also
failed because a zero-row API response resolved successfully.

## Change

- Migration `20261102125349_allow_submitted_timesheet_withdrawal.sql` adds
  submitted rows to the owner's update policy. A security-invoker trigger allows
  only the transition to draft with a cleared submission timestamp; all other
  fields must remain identical. Existing tenant, administrator, approval, leave,
  and invoice protections remain in effect.
- Submit and undo request exact affected-row counts and verify every unique ID
  was updated. Their queries require the expected starting status, preventing
  stale employee or administrator screens from overwriting a concurrent review.
- Failed or partial updates refresh the UI and display a translated error.

## Verification

- 49 PostgreSQL tenant-security regressions passed, including employee
  withdrawal/edit/resubmission, submitted-content protection, other owners,
  inactive members, other tenants, and approved/invoiced rows.
- 26 service/workflow tests passed, using the actual Supabase JavaScript query
  builder for mutation counts and status filters.
- English and Hungarian browser checks passed against an isolated PostgreSQL
  copy with employee RLS: zero-row error, repeated clicks during loading,
  persisted undo after reload, resubmission, and concurrent approval refresh.
  Ancillary auth/catalog endpoints were intercepted with fictional data; no
  hosted records were changed.
- Frontend production build and ESLint for changed files passed.

## Release

The migration was applied to production project `kqihfxhewjgrwoepdsau` on
2026-11-02 as version `20261102125349`. Read-only catalog checks confirmed the
policy, enabled trigger, invoker execution, and revoked direct function access.
No data backfill was required.

Frontend commit `936de44445a1ff4746d04969c30ca9cb7c537150` was pushed to `master`
and started production deployment `dpl_C5S8THvA2WTykHFYx5VLkPw2JRfc` at
`https://chronosync-4a85rs701-palyikris-projects.vercel.app`.
The deployment was building when the user requested that monitoring stop and
said they would check it. No undo action was tested on the live application.

## Repeating the browser check

Run the backend's `tests/test_tenant_security.py` with an isolated PostgreSQL DSN
in `SECURITY_TEST_PG_DSN`, using socket directory `/tmp/chronosync-invoice-audit`
and database `security_audit`. Clone that database into disposable `status_ui`.
Start frontend Vite on port 5194 with `VITE_SUPABASE_URL=http://127.0.0.1:54321`
and a fake publishable key. With Playwright available through `NODE_PATH`, run
`node tests/timesheet-status/smoke.mjs` from the frontend repository, setting
`STATUS_UI_PG_DSN='host=/tmp/chronosync-invoice-audit port=55439 dbname=status_ui'`.
Adjust the port to the local PostgreSQL server. Set `STATUS_UI_LANGUAGE=hu` for
Hungarian, and `PLAYWRIGHT_CHROMIUM_EXECUTABLE` if using an existing browser.
The browser script resets only that explicitly named disposable database.
