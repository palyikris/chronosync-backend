-- The multiplier migrations replaced this function after the earlier PT409 fix.
-- Stale attachment data is a business conflict, not a serialization failure:
-- PostgREST 14 retries SQLSTATE 40001 indefinitely instead of returning HTTP 409.
-- Preserve the function's logic, signature, ownership and existing grants.
do $migration$
declare
  definition text;
begin
  select pg_get_functiondef(
    'public.finalize_invoice_attachment_period(uuid,date,date,jsonb)'::regprocedure
  ) into definition;
  if position('''40001''' in definition) > 0 then
    execute replace(definition, '''40001''', '''PT409''');
  end if;
end;
$migration$;
