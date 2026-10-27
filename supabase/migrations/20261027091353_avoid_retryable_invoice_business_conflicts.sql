-- Business conflicts are permanent until the caller reviews or reconciles them.
-- SQLSTATE 40001 tells PostgREST 14 to retry the transaction indefinitely.
-- PT409 returns the intended conflict immediately without changing reservations,
-- authorization, function signatures, ownership, ACLs, or successful behavior.
-- https://supabase.com/kb/troubleshooting/high-cpu-and-infinite-transaction-retries-when-using-custom-error-codes-in-rpc-functions
do $migration$
declare
  target record;
begin
  for target in
    select pg_get_functiondef(proc.oid) as definition
    from pg_catalog.pg_proc as proc
    join pg_catalog.pg_namespace as namespace on namespace.oid = proc.pronamespace
    where (namespace.nspname = 'private' and proc.proname = 'protect_manual_invoice_timesheet')
       or (namespace.nspname = 'public' and proc.proname in (
         'reserve_manual_invoice', 'finalize_manual_invoice',
         'resolve_manual_invoice_not_issued', 'finalize_invoice_attachment_period'
       ))
  loop
    if position('''40001''' in target.definition) > 0 then
      execute replace(target.definition, '''40001''', '''PT409''');
    end if;
  end loop;
end;
$migration$;
