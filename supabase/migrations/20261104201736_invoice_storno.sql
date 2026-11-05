-- Preserve original invoices and entry snapshots after a confirmed reversal.
alter table public.invoice_ledger drop constraint invoice_ledger_status_check;
alter table public.invoice_ledger add constraint invoice_ledger_status_check
  check (status in ('issuing','issued','uncertain','failed','reversed'));
alter table public.invoice_ledger drop constraint invoice_ledger_company_client_period_key;
create unique index invoice_ledger_active_period_key
  on public.invoice_ledger(company_id,client_id,period_start,period_end)
  where status <> 'reversed';
alter table public.invoice_ledger_entries add column released_at timestamptz;
alter table public.invoice_ledger_entries drop constraint invoice_ledger_entries_timesheet_key;
create unique index invoice_ledger_entries_active_timesheet_key
  on public.invoice_ledger_entries(timesheet_id) where released_at is null;

alter table public.invoice_ledger
  add column reversal_id uuid,
  add column reversal_status text,
  add column reversal_reason text,
  add column reversal_issue_date date,
  add column reversal_e_invoice boolean,
  add column reversal_invoice_number text,
  add column reversal_net_total numeric(18,4),
  add column reversal_gross_total numeric(18,4),
  add column reversal_pdf_bytes bytea,
  add column reversal_pdf_available boolean not null default false,
  add column reversal_error_code text,
  add column reversal_error_message text,
  add column reversal_attempt_count integer not null default 0,
  add column reversal_created_by uuid references public.profiles(id) on delete restrict,
  add column reversal_started_at timestamptz,
  add column reversed_at timestamptz,
  add constraint invoice_ledger_reversal_state_check check (
    (reversal_id is null and reversal_status is null and reversal_attempt_count=0 and status <> 'reversed')
    or (reversal_id is not null and reversal_status in ('reversing','uncertain','failed','reversed')
      and reversal_reason is not null and length(btrim(reversal_reason)) between 1 and 500
      and reversal_issue_date is not null and reversal_e_invoice is not null
      and reversal_started_at is not null and reversal_created_by is not null
      and reversal_attempt_count between 1 and 5
      and ((status='reversed' and reversal_status='reversed'
        and reversal_invoice_number is not null and reversal_invoice_number <> invoice_number
        and reversal_net_total is not null and reversal_gross_total is not null
        and reversal_net_total = -net_total and reversal_gross_total = -gross_total
        and reversal_pdf_available and reversal_pdf_bytes is not null and reversed_at is not null)
        or (status='issued' and reversal_status <> 'reversed'))));
create unique index invoice_ledger_reversal_number_key
  on public.invoice_ledger(company_id,provider,reversal_invoice_number)
  where reversal_invoice_number is not null;

create or replace function public.check_timesheet_client_active()
returns trigger language plpgsql set search_path = '' as $$
begin
  -- A confirmed invoice may finalize work for a client archived after issuance.
  -- This changes only billing state, and does not log or edit work.
  if tg_op = 'UPDATE' then
    if (old.status = 'approved' and new.status = 'invoiced'
        or old.status = 'invoiced' and new.status = 'approved')
      and (to_jsonb(new) - 'status') = (to_jsonb(old) - 'status') then
      return new;
    end if;
  end if;
  if exists (select 1 from public.clients where id = new.client_id and is_active = false) then
    raise exception 'Cannot log time against an inactive client.';
  end if;
  if new.project_id is not null and exists (
    select 1 from public.projects as project
    join public.clients as client on client.id = project.client_id
    where project.id = new.project_id and (not project.is_active or not client.is_active)
  ) then
    raise exception 'Cannot log time against an inactive project or client.';
  end if;
  return new;
end;
$$;


create or replace function private.protect_manual_invoice_timesheet()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  invoice_status text;
begin
  select invoice.status into invoice_status
  from public.invoice_ledger_entries as entry
  join public.invoice_ledger as invoice on invoice.id = entry.invoice_id
  where entry.timesheet_id = old.id and entry.released_at is null and invoice.status not in ('failed', 'reversed');
  if invoice_status is null then
    if tg_op = 'DELETE' then return old; end if;
    return new;
  end if;
  if tg_op = 'UPDATE' then
    if row(new.id, new.user_id, new.company_id, new.client_id, new.project_id,
           new.work_date, new.hours_logged, new.description, new.billing_multiplier)
      is not distinct from
       row(old.id, old.user_id, old.company_id, old.client_id, old.project_id,
           old.work_date, old.hours_logged, old.description, old.billing_multiplier)
      and (new.status = old.status or (
        invoice_status = 'issued' and old.status = 'approved' and new.status = 'invoiced'
      )) then
      return new;
    end if;
  end if;
  raise exception 'This time entry is reserved for an invoice and cannot be changed'
    using errcode = 'PT409';
end;
$$;
revoke all on function private.protect_manual_invoice_timesheet() from public, anon, authenticated;


CREATE OR REPLACE FUNCTION public.reserve_manual_invoice(p_invoice_id uuid, p_company_id uuid, p_client_id uuid, p_period_start date, p_period_end date, p_review_fingerprint text, p_recipient_name text, p_recipient_email text, p_send_email boolean, p_billing_model text, p_quantity numeric, p_unit text, p_net_unit_price numeric, p_vat_code text, p_currency text, p_net_total numeric, p_vat_total numeric, p_gross_total numeric, p_invoice_snapshot jsonb, p_timesheet_ids jsonb)
 RETURNS TABLE(invoice_id uuid)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
  existing_invoice public.invoice_ledger%rowtype;
  actual_entry_count integer;
  requested_entry_count integer;
  matched_entry_count integer;
  requested_distinct_count integer;
  resolved_invoice_id uuid;
begin
  if not (select private.can_manage_company_invoices(p_company_id)) then
    raise exception 'Not authorized to issue invoices for this company'
      using errcode = '42501';
  end if;

  if not exists (select 1 from public.companies where id = p_company_id and szamlazz_test_mode = false) then
    raise exception 'Invoice issuance is disabled in preview mode' using errcode = '42501';
  end if;

  if p_invoice_id is null
    or p_period_start is null
    or p_period_end is null
    or p_period_start > p_period_end
    or p_period_end - p_period_start > 366
    or nullif(btrim(p_review_fingerprint), '') is null
    or nullif(btrim(p_recipient_name), '') is null
    or p_billing_model is null
    or p_billing_model <> 'project'
    or p_quantity is null
    or p_quantity <= 0
    or p_net_unit_price is null
    or p_net_unit_price < 0
    or p_net_total is null
    or p_vat_total is null
    or p_gross_total is null
    or p_net_total < 0
    or p_vat_total < 0
    or p_gross_total <> p_net_total + p_vat_total
    or p_invoice_snapshot is null
    or jsonb_typeof(p_invoice_snapshot) <> 'object'
    or p_timesheet_ids is null
    or jsonb_typeof(p_timesheet_ids) <> 'array'
  then
    raise exception 'Invalid invoice reservation data' using errcode = '22023';
  end if;

  perform 1
    from public.clients as client
    where client.id = p_client_id and client.company_id = p_company_id
    for update;

  if not exists (
    select 1
    from public.clients as client
    where client.id = p_client_id
      and client.company_id = p_company_id
      and client.is_active = true
  ) then
    raise exception 'The selected client is missing or inactive'
      using errcode = '42501';
  end if;

  select invoice.*
  into existing_invoice
  from public.invoice_ledger as invoice
  where invoice.company_id = p_company_id
    and invoice.client_id = p_client_id
    and invoice.period_start = p_period_start
    and invoice.period_end = p_period_end
    and invoice.status <> 'reversed'
  for update;

  if existing_invoice.id is not null
    and existing_invoice.status <> 'failed'
  then
    raise exception 'An invoice already exists for this client and period'
      using errcode = '23505';
  end if;

  if existing_invoice.id is not null and existing_invoice.attempt_count >= 5 then
    raise exception 'Invoice attempt limit reached; contact support' using errcode = '54000';
  end if;

  if exists (
    select 1 from public.invoice_ledger as invoice
    where invoice.company_id = p_company_id and invoice.client_id = p_client_id
      and invoice.status not in ('failed', 'reversed')
      and invoice.period_start <= p_period_end and invoice.period_end >= p_period_start
  ) then
    raise exception 'An invoice already covers this period' using errcode = '23505';
  end if;


  -- Lock reviewed rows before validating and copying them. A concurrent edit
  -- either commits first and is detected below, or waits until reservation ends.
  perform timesheet.id from public.timesheets as timesheet
  where timesheet.company_id = p_company_id and timesheet.client_id = p_client_id
    and timesheet.work_date between p_period_start and p_period_end
    and timesheet.status = 'approved'
  order by timesheet.id for update;

  perform 1 from public.client_billing_profiles as profile
  where profile.company_id = p_company_id and profile.client_id = p_client_id
  for share;
  if not exists (
    select 1 from public.client_billing_profiles as profile
    where profile.company_id = p_company_id and profile.client_id = p_client_id
      and profile.updated_at = (p_invoice_snapshot->>'billing_profile_updated_at')::timestamptz
  ) then
    raise exception 'Billing profile changed after review' using errcode = 'PT409';
  end if;

  if p_quantity <> 1 or p_net_unit_price <> p_net_total then
    raise exception 'Project invoice summary must match its project lines' using errcode = '22023';
  end if;
  perform private.validate_project_invoice_prices(p_company_id,p_client_id,p_period_start,p_period_end,
    p_invoice_snapshot,p_currency,p_vat_code,p_net_total,p_vat_total,p_gross_total);

  if jsonb_typeof(p_invoice_snapshot->'approved_entries') is distinct from 'array' then
    raise exception 'Missing reviewed time entry snapshot' using errcode = '22023';
  end if;
  if exists (
    select 1 from public.timesheets as timesheet
    left join public.projects as project on project.id = timesheet.project_id
    left join jsonb_to_recordset(p_invoice_snapshot->'approved_entries') as reviewed(
      id uuid, project_id uuid, work_date date, hours_logged numeric, description text, project_name text
    ) on reviewed.id = timesheet.id
    where timesheet.id in (select value::uuid from jsonb_array_elements_text(p_timesheet_ids))
      and (
        reviewed.id is null
        or row(reviewed.project_id, reviewed.work_date, reviewed.hours_logged, reviewed.description, reviewed.project_name)
          is distinct from row(timesheet.project_id, timesheet.work_date, timesheet.hours_logged,
            coalesce(timesheet.description, ''), coalesce(project.name, 'General Task'))
      )
  ) then
    raise exception 'Time entry details changed after review' using errcode = 'PT409';
  end if;

  requested_entry_count := jsonb_array_length(p_timesheet_ids);

  select count(distinct value::uuid)
  into requested_distinct_count
  from jsonb_array_elements_text(p_timesheet_ids);

  if requested_distinct_count <> requested_entry_count then
    raise exception 'Duplicate time entry ids are not allowed'
      using errcode = '22023';
  end if;

  select count(*)
  into actual_entry_count
  from public.timesheets as timesheet
  where timesheet.company_id = p_company_id
    and timesheet.client_id = p_client_id
    and timesheet.work_date between p_period_start and p_period_end
    and timesheet.status = 'approved';

  select count(*)
  into matched_entry_count
  from public.timesheets as timesheet
  where timesheet.company_id = p_company_id
    and timesheet.client_id = p_client_id
    and timesheet.work_date between p_period_start and p_period_end
    and timesheet.status = 'approved'
    and timesheet.id in (
      select value::uuid from jsonb_array_elements_text(p_timesheet_ids)
    );

  if requested_entry_count <> actual_entry_count
    or matched_entry_count <> actual_entry_count
  then
    raise exception 'Approved time entries changed after invoice review'
      using errcode = 'PT409';
  end if;


  if existing_invoice.id is null then
    resolved_invoice_id := p_invoice_id;
    insert into public.invoice_ledger (
      id,
      company_id,
      client_id,
      period_start,
      period_end,
      status,
      provider,
      external_id,
      review_fingerprint,
      invoice_snapshot,
      recipient_name,
      recipient_email,
      send_email,
      billing_model,
      quantity,
      unit,
      net_unit_price,
      vat_code,
      currency,
      net_total,
      vat_total,
      gross_total,
      created_by
    ) values (
      resolved_invoice_id,
      p_company_id,
      p_client_id,
      p_period_start,
      p_period_end,
      'issuing',
      'szamlazz_hu',
      'chronosync-' || resolved_invoice_id::text,
      p_review_fingerprint,
      p_invoice_snapshot,
      btrim(p_recipient_name),
      nullif(btrim(p_recipient_email), ''),
      p_send_email,
      p_billing_model,
      round(p_quantity, 2),
      btrim(p_unit),
      round(p_net_unit_price, 4),
      btrim(p_vat_code),
      upper(btrim(p_currency)),
      round(p_net_total, 4),
      round(p_vat_total, 4),
      round(p_gross_total, 4),
      (select auth.uid())
    );
  else
    resolved_invoice_id := existing_invoice.id;
    delete from public.invoice_ledger_entries as entry
    where entry.invoice_id = resolved_invoice_id;

    update public.invoice_ledger as invoice
    set status = 'issuing',
        review_fingerprint = p_review_fingerprint,
        invoice_snapshot = p_invoice_snapshot,
        recipient_name = btrim(p_recipient_name),
        recipient_email = nullif(btrim(p_recipient_email), ''),
        send_email = p_send_email,
        billing_model = p_billing_model,
        quantity = round(p_quantity, 2),
        unit = btrim(p_unit),
        net_unit_price = round(p_net_unit_price, 4),
        vat_code = btrim(p_vat_code),
        currency = upper(btrim(p_currency)),
        net_total = round(p_net_total, 4),
        vat_total = round(p_vat_total, 4),
        gross_total = round(p_gross_total, 4),
        invoice_number = null,
        pdf_bytes = null,
        pdf_available = false,
        provider_error_code = null,
        provider_error_message = null,
        issued_at = null,
        attempt_count = invoice.attempt_count + 1
    where invoice.id = resolved_invoice_id;
  end if;

  insert into public.invoice_ledger_entries (
    invoice_id,
    timesheet_id,
    user_id,
    project_id,
    work_date,
    hours_logged,
    description
  )
  select
    resolved_invoice_id,
    timesheet.id,
    timesheet.user_id,
    timesheet.project_id,
    timesheet.work_date,
    round(timesheet.hours_logged::numeric, 2),
    coalesce(timesheet.description, '')
  from public.timesheets as timesheet
  where timesheet.company_id = p_company_id
    and timesheet.client_id = p_client_id
    and timesheet.status = 'approved'
    and timesheet.id in (
      select value::uuid from jsonb_array_elements_text(p_timesheet_ids)
    )
  order by timesheet.id;

  invoice_id := resolved_invoice_id;
  return next;
end;
$function$;

-- All writes remain behind tenant-scoped administrative RPCs; no table write grants.
create or replace function public.reserve_invoice_storno(
  p_invoice_id uuid, p_request_id uuid, p_original_invoice_number text,
  p_reason text, p_issue_date date, p_e_invoice boolean
) returns table(request_id uuid)
language plpgsql security definer set search_path='' as $$
declare invoice_row public.invoice_ledger%rowtype;
begin
  select * into invoice_row from public.invoice_ledger where id=p_invoice_id for update;
  if invoice_row.id is null or not private.can_manage_company_invoices(invoice_row.company_id) then
    raise exception 'Invoice not accessible' using errcode='42501';
  end if;
  if not exists(select 1 from public.companies where id=invoice_row.company_id and szamlazz_test_mode=false) then
    raise exception 'Storno is disabled in preview mode' using errcode='42501';
  end if;
  if invoice_row.status <> 'issued' or invoice_row.invoice_number is distinct from p_original_invoice_number
    or invoice_row.reversal_status in ('reversing','uncertain','reversed')
    or invoice_row.reversal_attempt_count >= 5 then
    raise exception 'Invoice is not eligible for storno' using errcode='PT409';
  end if;
  if p_request_id is null or p_issue_date is distinct from (now() at time zone 'Europe/Budapest')::date
    or p_e_invoice is null or p_reason is null or length(btrim(p_reason)) not between 1 and 500 then
    raise exception 'Invalid storno request' using errcode='22023';
  end if;
  update public.invoice_ledger set reversal_id=p_request_id, reversal_status='reversing',
    reversal_reason=btrim(p_reason), reversal_issue_date=p_issue_date, reversal_e_invoice=p_e_invoice,
    reversal_attempt_count=reversal_attempt_count+1, reversal_created_by=(select auth.uid()),
    reversal_started_at=now(), reversal_error_code=null, reversal_error_message=null
  where id=p_invoice_id;
  request_id:=p_request_id; return next;
end; $$;

create or replace function public.mark_invoice_storno_attempt(
  p_invoice_id uuid, p_request_id uuid, p_status text, p_error_code text
) returns void language plpgsql security definer set search_path='' as $$
declare invoice_row public.invoice_ledger%rowtype;
begin
  select * into invoice_row from public.invoice_ledger where id=p_invoice_id for update;
  if invoice_row.id is null or not private.can_manage_company_invoices(invoice_row.company_id) then
    raise exception 'Invoice not accessible' using errcode='42501';
  end if;
  if invoice_row.status <> 'issued' or invoice_row.reversal_status <> 'reversing'
    or invoice_row.reversal_id is distinct from p_request_id or p_request_id is null
    or p_status is null or p_status not in ('failed','uncertain') then
    raise exception 'Storno attempt changed' using errcode='PT409';
  end if;
  update public.invoice_ledger set reversal_status=p_status,
    reversal_error_code=left(p_error_code,100),
    reversal_error_message=case when p_status='uncertain'
      then 'Storno outcome unconfirmed. Check Számlázz.hu and reconcile before retrying.'
      else 'Storno was not sent or was rejected. Original invoice remains issued.' end
  where id=p_invoice_id;
end; $$;

create or replace function public.finalize_invoice_storno(
  p_invoice_id uuid, p_request_id uuid, p_invoice_number text,
  p_net_total numeric, p_gross_total numeric, p_pdf_base64 text,
  p_reason text, p_issue_date date, p_e_invoice boolean
) returns table(invoice_id uuid,status text)
language plpgsql security definer set search_path='' as $$
declare
  invoice_row public.invoice_ledger%rowtype;
  decoded_pdf bytea;
  expected_count integer;
  changed_count integer;
begin
  -- Match reservation's lock order: client, ledger, then timesheets.
  perform 1 from public.clients where id=(select client_id from public.invoice_ledger where id=p_invoice_id) for update;
  select * into invoice_row from public.invoice_ledger where id=p_invoice_id for update;
  if invoice_row.id is null or not private.can_manage_company_invoices(invoice_row.company_id) then
    raise exception 'Invoice not accessible' using errcode='42501';
  end if;
  if p_invoice_number is null or length(btrim(p_invoice_number)) not between 1 and 100
    or p_invoice_number=invoice_row.invoice_number
    or p_net_total is distinct from -invoice_row.net_total
    or p_gross_total is distinct from -invoice_row.gross_total
    or p_issue_date is null or p_issue_date > (now() at time zone 'Europe/Budapest')::date
    or p_e_invoice is null or p_reason is null or length(btrim(p_reason)) not between 1 and 500 then
    raise exception 'Storno does not reverse original invoice' using errcode='PT409';
  end if;
  if invoice_row.status='reversed' and invoice_row.reversal_invoice_number=p_invoice_number
    and (invoice_row.reversal_id=p_request_id or p_request_id is null) then
    invoice_id:=p_invoice_id; status:='reversed'; return next; return;
  end if;
  if invoice_row.status <> 'issued' then
    raise exception 'Original invoice is not issued' using errcode='PT409';
  end if;
  if p_request_id is not null then
    if invoice_row.reversal_id is distinct from p_request_id
      or invoice_row.reversal_status not in ('reversing','uncertain','failed') then
      raise exception 'Storno attempt changed' using errcode='PT409';
    end if;
  elsif invoice_row.reversal_id is not null then
    raise exception 'An existing storno attempt must be reconciled' using errcode='PT409';
  end if;
  if exists(select 1 from public.invoice_ledger where company_id=invoice_row.company_id
    and (invoice_number=p_invoice_number or reversal_invoice_number=p_invoice_number)) then
    raise exception 'Storno number already recorded' using errcode='23505';
  end if;
  if p_pdf_base64 is null or length(p_pdf_base64) > 34952536 then
    raise exception 'Storno PDF is missing or too large' using errcode='22023';
  end if;
  decoded_pdf:=decode(p_pdf_base64,'base64');
  if octet_length(decoded_pdf) not between 4 and 26214400
    or substring(decoded_pdf from 1 for 4) <> decode('25504446','hex') then
    raise exception 'Invalid storno PDF' using errcode='22023';
  end if;
  perform timesheet.id from public.timesheets timesheet join public.invoice_ledger_entries entry
    on entry.timesheet_id=timesheet.id where entry.invoice_id=p_invoice_id and entry.released_at is null
    order by timesheet.id for update of timesheet;
  select count(*) into expected_count from public.invoice_ledger_entries
    where invoice_ledger_entries.invoice_id=p_invoice_id and released_at is null;
  -- Release reservations atomically, retaining the historical rows.
  update public.invoice_ledger_entries set released_at=now()
    where invoice_ledger_entries.invoice_id=p_invoice_id and released_at is null;
  update public.invoice_ledger set status='reversed',
    reversal_id=coalesce(reversal_id,gen_random_uuid()), reversal_status='reversed',
    reversal_reason=coalesce(reversal_reason,btrim(p_reason)), reversal_issue_date=p_issue_date,
    reversal_e_invoice=p_e_invoice, reversal_invoice_number=btrim(p_invoice_number),
    reversal_net_total=p_net_total, reversal_gross_total=p_gross_total,
    reversal_pdf_bytes=decoded_pdf,reversal_pdf_available=true,
    reversal_error_code=null,reversal_error_message=null,
    reversal_created_by=coalesce(reversal_created_by,(select auth.uid())),
    reversal_started_at=coalesce(reversal_started_at,now()),
    reversal_attempt_count=greatest(reversal_attempt_count,1),reversed_at=now()
  where id=p_invoice_id;
  update public.timesheets timesheet set status='approved'
    from public.invoice_ledger_entries entry
    where entry.invoice_id=p_invoice_id and entry.timesheet_id=timesheet.id
      and timesheet.company_id=invoice_row.company_id and timesheet.client_id=invoice_row.client_id
      and timesheet.status='invoiced';
  get diagnostics changed_count=row_count;
  if changed_count <> expected_count then
    raise exception 'Invoiced work changed before storno finalization' using errcode='PT409';
  end if;
  -- A scheduled run must not immediately recreate the cancelled invoice.
  update public.client_billing_profiles set automatic_invoicing_enabled=false
    where client_id=invoice_row.client_id and company_id=invoice_row.company_id
      and automatic_invoicing_enabled=true;
  invoice_id:=p_invoice_id; status:='reversed'; return next;
end; $$;

create or replace function public.resolve_invoice_storno_not_reversed(p_invoice_id uuid,p_request_id uuid)
returns void language plpgsql security definer set search_path='' as $$
declare invoice_row public.invoice_ledger%rowtype;
begin
  select * into invoice_row from public.invoice_ledger where id=p_invoice_id for update;
  if invoice_row.id is null or not private.can_manage_company_invoices(invoice_row.company_id) then
    raise exception 'Invoice not accessible' using errcode='42501';
  end if;
  if invoice_row.status <> 'issued' or invoice_row.reversal_status <> 'uncertain'
    or invoice_row.reversal_id is distinct from p_request_id or p_request_id is null then
    raise exception 'Only uncertain storno attempts can be resolved' using errcode='PT409';
  end if;
  update public.invoice_ledger set reversal_status='failed',
    reversal_error_code='verified_not_reversed',
    reversal_error_message='Provider confirmed no storno. Original invoice remains issued.' where id=p_invoice_id;
end; $$;

-- Expiry retains the original and all entry locks, allowing read-only reconciliation.
create or replace function public.expire_stale_manual_invoice_attempts(p_company_id uuid)
returns void language plpgsql security definer set search_path='' as $$
begin
  if not private.can_manage_company_invoices(p_company_id) then
    raise exception 'Not authorized to manage invoices' using errcode='42501';
  end if;
  update public.invoice_ledger set status='uncertain',provider_error_code='abandoned_invoice_attempt',
    provider_error_message='Issuance interrupted. Verify the external id before reconciling.'
    where company_id=p_company_id and status='issuing' and updated_at < now()-interval '10 minutes';
  update public.invoice_ledger set reversal_status='uncertain',reversal_error_code='abandoned_storno_attempt',
    reversal_error_message='Storno interrupted. Check Számlázz.hu and reconcile before retrying.'
    where company_id=p_company_id and reversal_status='reversing'
      and reversal_started_at < now()-interval '10 minutes';
end; $$;

revoke all on function public.reserve_invoice_storno(uuid,uuid,text,text,date,boolean) from public,anon;
revoke all on function public.mark_invoice_storno_attempt(uuid,uuid,text,text) from public,anon;
revoke all on function public.finalize_invoice_storno(uuid,uuid,text,numeric,numeric,text,text,date,boolean) from public,anon;
revoke all on function public.resolve_invoice_storno_not_reversed(uuid,uuid) from public,anon;
grant execute on function public.reserve_invoice_storno(uuid,uuid,text,text,date,boolean) to authenticated;
grant execute on function public.mark_invoice_storno_attempt(uuid,uuid,text,text) to authenticated;
grant execute on function public.finalize_invoice_storno(uuid,uuid,text,numeric,numeric,text,text,date,boolean) to authenticated;
grant execute on function public.resolve_invoice_storno_not_reversed(uuid,uuid) to authenticated;

-- PDF-only recovery never reserves entries, changes status, or reissues an invoice.
create or replace function public.store_manual_invoice_pdf(
  p_invoice_id uuid, p_invoice_number text, p_net_total numeric,
  p_gross_total numeric, p_pdf_base64 text
) returns void language plpgsql security definer set search_path = '' as $$
declare r public.invoice_ledger; v_pdf bytea;
begin
  select * into r from public.invoice_ledger where id = p_invoice_id for update;
  if r.id is null or not private.can_manage_company_invoices(r.company_id) then
    raise sqlstate '42501' using message = 'Invoice was not found or is not accessible';
  end if;
  if r.status not in ('issued','reversed') or r.invoice_number is distinct from p_invoice_number
    or r.net_total is distinct from p_net_total or r.gross_total is distinct from p_gross_total then
    raise sqlstate 'PT409' using message = 'Provider invoice details must match the issued ledger';
  end if;
  if p_pdf_base64 is null or length(p_pdf_base64) > 34952536 then
    raise sqlstate '22023' using message = 'Invoice PDF is missing or too large';
  end if;
  v_pdf := decode(p_pdf_base64, 'base64');
  if octet_length(v_pdf) = 0 or octet_length(v_pdf) > 26214400
    or substring(v_pdf from 1 for 4) <> decode('25504446', 'hex') then
    raise sqlstate '22023' using message = 'Invoice PDF is invalid or too large';
  end if;
  update public.invoice_ledger set pdf_bytes = v_pdf, pdf_available = true
    where id = r.id and pdf_bytes is null;
end $$;
revoke all on function public.store_manual_invoice_pdf(uuid,text,numeric,numeric,text) from public, anon;
grant execute on function public.store_manual_invoice_pdf(uuid,text,numeric,numeric,text) to authenticated;
