-- The legacy attach_timesheet_report field remains for older clients and history.
-- New reservations always require the reviewed invoice attachment, regardless
-- of the old saved preference. Existing ledger snapshots remain unchanged.
alter table public.client_billing_profiles
  alter column attach_timesheet_report set default true;

create or replace function private.guard_invoice_attachment()
returns trigger language plpgsql security invoker set search_path='' as $$
declare
  requested boolean;
  saved_language text;
  metadata jsonb;
  reserving boolean;
begin
  reserving:=tg_op='INSERT';
  if tg_op='UPDATE' then
    reserving:=new.status='issuing' and old.status='failed';
  end if;
  if reserving then
    select coalesce(client.invoice_attachment_language,'hu')
      into saved_language
      from public.client_billing_profiles profile join public.clients client
        on client.id=profile.client_id and client.company_id=profile.company_id
      where profile.client_id=new.client_id and profile.company_id=new.company_id;
    if jsonb_typeof(new.invoice_snapshot->'attach_timesheet_report') not in ('boolean')
      and new.invoice_snapshot ? 'attach_timesheet_report' then
      raise exception 'Invalid attachment flag' using errcode='PT409';
    end if;
    requested:=coalesce((new.invoice_snapshot->>'attach_timesheet_report')::boolean,false);
    if requested is not true then
      raise exception 'Every new invoice requires its reviewed invoice attachment' using errcode='PT409';
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
        raise exception 'The required reviewed invoice attachment is missing or invalid' using errcode='PT409';
      end if;
      new.attachment_metadata:=metadata;
    elsif metadata is not null and metadata <> 'null'::jsonb then
      raise exception 'Unexpected invoice attachment' using errcode='PT409';
    end if;
  end if;
  -- Old ledger records omit this field and keep their original behavior.
  if new.status='issued' and coalesce((new.invoice_snapshot->>'attach_timesheet_report')::boolean,false) then
    if new.attachment_bytes is null
      or new.attachment_metadata is distinct from new.invoice_snapshot->'attachment'
      or octet_length(new.attachment_bytes) <> (new.attachment_metadata->>'size_bytes')::integer
      or encode(sha256(new.attachment_bytes),'hex') is distinct from new.attachment_metadata->>'sha256' then
      raise exception 'The required invoice attachment was not saved before issuance' using errcode='PT409';
    end if;
  end if;
  return new;
end; $$;
revoke all on function private.guard_invoice_attachment() from public,anon,authenticated;
