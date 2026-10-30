# Payroll entry multiplier rollout — 30 October 2026

Applied to the hosted ChronoSync project (`kqihfxhewjgrwoepdsau`):

1. `20261030130954_move_payroll_multiplier_to_entries.sql`
2. `20261030130958_invoice_entry_multipliers.sql`

The local migration filenames and references match the hosted migration history.
PostgREST received a schema-cache reload notification. Its project PATCH handler
recognizes `is_payroll`; anonymous access remains denied as before. An
administrator project update was verified inside a transaction and rolled back.

Both new columns exist, all existing timesheet trigger modes were restored, and
invoice validation and attachment finalization now use entry multipliers. The
invoice reservation guard preserves its existing non-retryable `PT409` conflict.

Pre/post checks confirmed unchanged logged hours, billable hours, existing
project values, client balances, invoice ledger records and time-entry content
(excluding the new multiplier field). No invalid multipliers were found.
