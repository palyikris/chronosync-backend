-- The reviewed annex is stored before contacting the invoice provider.
alter table public.invoice_ledger
  add column attachment_metadata jsonb,
  add column attachment_bytes bytea,
  add column attachment_available boolean generated always as (attachment_bytes is not null) stored,
  add constraint invoice_attachment_storage_check check (
    attachment_bytes is null or (
      attachment_metadata is not null and jsonb_typeof(attachment_metadata)='object'
      and octet_length(attachment_bytes) between 1 and 2000000
      and substring(attachment_bytes from 1 for 4)=decode('504b0304','hex')
      and (attachment_metadata->>'size_bytes')::integer=octet_length(attachment_bytes)
      and attachment_metadata->>'sha256'=encode(sha256(attachment_bytes),'hex')));

create or replace function private.guard_invoice_attachment()
returns trigger language plpgsql security invoker set search_path='' as $$
declare
  requested boolean;
  saved_preference boolean;
  saved_language text;
  metadata jsonb;
  reserving boolean;
begin
  reserving:=tg_op='INSERT';
  if tg_op='UPDATE' then
    reserving:=new.status='issuing' and old.status='failed';
  end if;
  if reserving then
    select profile.attach_timesheet_report,coalesce(client.invoice_attachment_language,'hu')
      into saved_preference,saved_language
      from public.client_billing_profiles profile join public.clients client
        on client.id=profile.client_id and client.company_id=profile.company_id
      where profile.client_id=new.client_id and profile.company_id=new.company_id;
    if jsonb_typeof(new.invoice_snapshot->'attach_timesheet_report') not in ('boolean')
      and new.invoice_snapshot ? 'attach_timesheet_report' then
      raise exception 'Invalid report preference' using errcode='PT409';
    end if;
    requested:=coalesce((new.invoice_snapshot->>'attach_timesheet_report')::boolean,false);
    if requested is distinct from saved_preference then
      raise exception 'Report preference changed; review again' using errcode='PT409';
    end if;
    new.attachment_bytes:=null;
    new.attachment_metadata:=null;
    metadata:=new.invoice_snapshot->'attachment';
    if requested then
      if metadata is null or jsonb_typeof(metadata) is distinct from 'object'
        or jsonb_typeof(metadata->'filename') is distinct from 'string'
        or (metadata->>'filename') !~ '^[A-Za-z0-9_-]{1,120}\.xlsx$'
        or metadata->>'media_type' is distinct from 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        or jsonb_typeof(metadata->'sha256') is distinct from 'string'
        or (metadata->>'sha256') !~ '^[0-9a-f]{64}$'
        or jsonb_typeof(metadata->'size_bytes') is distinct from 'number'
        or (metadata->>'size_bytes') !~ '^[0-9]{1,7}$'
        or (metadata->>'size_bytes')::integer not between 1 and 2000000
        or metadata->>'language' is distinct from saved_language
        or saved_language not in ('hu','en') then
        raise exception 'The required reviewed report is missing or invalid' using errcode='PT409';
      end if;
      new.attachment_metadata:=metadata;
    elsif metadata is not null and metadata <> 'null'::jsonb then
      raise exception 'Unexpected billing report' using errcode='PT409';
    end if;
  end if;
  -- Old ledger records omit this field and keep their original behavior.
  if new.status='issued' and coalesce((new.invoice_snapshot->>'attach_timesheet_report')::boolean,false) then
    if new.attachment_bytes is null
      or new.attachment_metadata is distinct from new.invoice_snapshot->'attachment'
      or octet_length(new.attachment_bytes) <> (new.attachment_metadata->>'size_bytes')::integer
      or encode(sha256(new.attachment_bytes),'hex') is distinct from new.attachment_metadata->>'sha256' then
      raise exception 'The required billing report was not saved before issuance' using errcode='PT409';
    end if;
  end if;
  return new;
end; $$;
revoke all on function private.guard_invoice_attachment() from public,anon,authenticated;
create trigger guard_invoice_attachment before insert or update on public.invoice_ledger
  for each row execute function private.guard_invoice_attachment();

create or replace function public.store_invoice_attachment(p_invoice_id uuid,p_content_base64 text)
returns table(invoice_id uuid,sha256 text)
language plpgsql security definer set search_path='' as $$
declare invoice_row public.invoice_ledger%rowtype; content bytea; digest text;
begin
  select * into invoice_row from public.invoice_ledger where id=p_invoice_id for update;
  if invoice_row.id is null or not private.can_manage_company_invoices(invoice_row.company_id) then
    raise exception 'Invoice not accessible' using errcode='42501';
  end if;
  if invoice_row.status <> 'issuing' or invoice_row.attachment_metadata is null
    or invoice_row.invoice_snapshot->>'attach_timesheet_report' is distinct from 'true'
    or p_content_base64 is null or length(p_content_base64) > 2666668 then
    raise exception 'Invoice report is not eligible for storage' using errcode='PT409';
  end if;
  content:=decode(p_content_base64,'base64');
  digest:=encode(pg_catalog.sha256(content),'hex');
  if octet_length(content) not between 1 and 2000000
    or substring(content from 1 for 4) <> decode('504b0304','hex')
    or octet_length(content) <> (invoice_row.attachment_metadata->>'size_bytes')::integer
    or digest is distinct from invoice_row.attachment_metadata->>'sha256'
    or (invoice_row.attachment_bytes is not null and invoice_row.attachment_bytes <> content) then
    raise exception 'Report bytes do not match the reviewed report' using errcode='PT409';
  end if;
  update public.invoice_ledger set attachment_bytes=content where id=p_invoice_id;
  invoice_id:=p_invoice_id; sha256:=digest; return next;
end; $$;
revoke all on function public.store_invoice_attachment(uuid,text) from public,anon;
grant execute on function public.store_invoice_attachment(uuid,text) to authenticated;
