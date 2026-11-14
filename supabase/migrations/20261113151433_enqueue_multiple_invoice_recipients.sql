create or replace function private.enqueue_invoice_email()
returns trigger language plpgsql security definer set search_path='' as $$
declare data jsonb; event_kind text; document_pdf bytea; document_number text;
begin
  -- Missing transport means a historical provider-delivered invoice. Never
  -- backfill those records into Resend, including during reconciliation.
  if not new.send_email or new.invoice_snapshot->>'email_transport' is distinct from 'resend'
    or nullif(trim(new.recipient_email),'') is null then return new; end if;
  if new.status='issued' and new.pdf_bytes is not null and new.attachment_bytes is not null then
    event_kind:='invoice_issued'; document_pdf:=new.pdf_bytes; document_number:=new.invoice_number;
  elsif new.status='reversed' and new.reversal_pdf_bytes is not null then
    event_kind:='invoice_reversed'; document_pdf:=new.reversal_pdf_bytes; document_number:=new.reversal_invoice_number;
  else return new; end if;
  select jsonb_build_object('company_name',c.name,'recipient_name',new.recipient_name,
    'invoice_number',document_number,'period_start',new.period_start,'period_end',new.period_end,
    'gross_total',case when event_kind='invoice_issued' then new.gross_total else new.reversal_gross_total end,
    'currency',new.currency,'language',coalesce(new.invoice_snapshot->>'language','hu'),
    'due_date',new.invoice_snapshot->>'due_date',
    'pdf_base64',replace(encode(document_pdf,'base64'),E'\n','')) into data
    from public.companies c where c.id=new.company_id;
  if event_kind='invoice_issued' then
    data:=data||jsonb_build_object('attachment',new.attachment_metadata,
      'attachment_base64',replace(encode(new.attachment_bytes,'base64'),E'\n',''));
  end if;
  -- Queue a separate delivery per mailbox to keep addresses private and retries independent.
  -- Keep the original event key for the first recipient to preserve idempotency.
  insert into private.transactional_emails(event_key,company_id,kind,source_id,recipient,event_data)
    select event_kind||'/'||new.id||
      case when row_number() over (order by position) = 1 then '' else '/'||position end,
      new.company_id,event_kind,new.id,recipient,data
    from (
      select lower(btrim(address)) as recipient, min(ordinality) as position
      from unnest(string_to_array(new.recipient_email, ',')) with ordinality as addresses(address, ordinality)
      where btrim(address) <> ''
      group by lower(btrim(address))
    ) as recipients
    on conflict(event_key) do nothing;
  return new;
end; $$;
revoke all on function private.enqueue_invoice_email() from public,anon,authenticated,service_role;
