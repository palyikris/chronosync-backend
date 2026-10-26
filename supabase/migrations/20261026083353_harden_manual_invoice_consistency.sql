-- Invoice audit: reservation races, recovery, immutable entries and legacy trigger compatibility.

-- Existing timesheet triggers also run inside SECURITY DEFINER invoice RPCs,
-- whose empty search_path deliberately prevents resolving unqualified tables.
create or replace function public.check_timesheet_client_active()
returns trigger language plpgsql set search_path = '' as $$
begin
  -- A confirmed invoice may finalize work for a client archived after issuance.
  -- This changes only billing state, and does not log or edit work.
  if tg_op = 'UPDATE' then
    if old.status = 'approved' and new.status = 'invoiced'
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
  where entry.timesheet_id = old.id and invoice.status <> 'failed';
  if invoice_status is null then
    if tg_op = 'DELETE' then return old; end if;
    return new;
  end if;
  if tg_op = 'UPDATE' then
    if row(new.id, new.user_id, new.company_id, new.client_id, new.project_id,
           new.work_date, new.hours_logged, new.description)
      is not distinct from
       row(old.id, old.user_id, old.company_id, old.client_id, old.project_id,
           old.work_date, old.hours_logged, old.description)
      and (new.status = old.status or (
        invoice_status = 'issued' and old.status = 'approved' and new.status = 'invoiced'
      )) then
      return new;
    end if;
  end if;
  raise exception 'This time entry is reserved for an invoice and cannot be changed'
    using errcode = '40001';
end;
$$;
revoke all on function private.protect_manual_invoice_timesheet() from public, anon, authenticated;
drop trigger if exists protect_manual_invoice_timesheet on public.timesheets;
create trigger protect_manual_invoice_timesheet before update or delete on public.timesheets
for each row execute function private.protect_manual_invoice_timesheet();

create or replace function public.expire_stale_manual_invoice_attempts(p_company_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if not (select private.can_manage_company_invoices(p_company_id)) then
    raise exception 'Not authorized to manage invoices for this company' using errcode = '42501';
  end if;
  -- Retain every reservation. Expiry permits manual reconciliation, never retry.
  update public.invoice_ledger set status = 'uncertain',
    provider_error_code = 'abandoned_invoice_attempt',
    provider_error_message = 'Issuance was interrupted. Stop any abandoned worker and verify the external id in Számlázz.hu before reconciling.'
  where company_id = p_company_id and status = 'issuing'
    and updated_at < now() - interval '10 minutes';
end;
$$;
revoke all on function public.expire_stale_manual_invoice_attempts(uuid) from public, anon;
grant execute on function public.expire_stale_manual_invoice_attempts(uuid) to authenticated;

create or replace function public.reserve_manual_invoice(
  p_invoice_id uuid,
  p_company_id uuid,
  p_client_id uuid,
  p_period_start date,
  p_period_end date,
  p_review_fingerprint text,
  p_recipient_name text,
  p_recipient_email text,
  p_send_email boolean,
  p_billing_model text,
  p_quantity numeric,
  p_unit text,
  p_net_unit_price numeric,
  p_vat_code text,
  p_currency text,
  p_net_total numeric,
  p_vat_total numeric,
  p_gross_total numeric,
  p_invoice_snapshot jsonb,
  p_timesheet_ids jsonb
)
returns table (invoice_id uuid)
language plpgsql
security definer
set search_path = ''
as $$
declare
  existing_invoice public.invoice_ledger%rowtype;
  actual_entry_count integer;
  requested_entry_count integer;
  matched_entry_count integer;
  requested_distinct_count integer;
  approved_hours numeric;
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
    or p_billing_model not in ('hourly', 'fixed_monthly')
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
      and invoice.status <> 'failed'
      and invoice.period_start <= p_period_end and invoice.period_end >= p_period_start
  ) then
    raise exception 'An invoice already covers this period' using errcode = '23505';
  end if;

  if p_billing_model = 'fixed_monthly' and (
    p_period_start <> date_trunc('month', p_period_start)::date
    or p_period_end <> (date_trunc('month', p_period_start) + interval '1 month - 1 day')::date
    or p_quantity <> 1
  ) then
    raise exception 'Fixed monthly billing requires one full calendar month' using errcode = '22023';
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
    raise exception 'Billing profile changed after review' using errcode = '40001';
  end if;

  if jsonb_typeof(p_invoice_snapshot->'approved_entries') is distinct from 'array' then
    raise exception 'Missing reviewed time entry snapshot' using errcode = '22023';
  end if;
  if exists (
    select 1 from public.timesheets as timesheet
    left join public.projects as project on project.id = timesheet.project_id
    left join jsonb_to_recordset(p_invoice_snapshot->'approved_entries') as reviewed(
      id uuid, work_date date, hours_logged numeric, description text, project_name text
    ) on reviewed.id = timesheet.id
    where timesheet.id in (select value::uuid from jsonb_array_elements_text(p_timesheet_ids))
      and (
        reviewed.id is null
        or row(reviewed.work_date, reviewed.hours_logged, reviewed.description, reviewed.project_name)
          is distinct from row(timesheet.work_date, timesheet.hours_logged,
            coalesce(timesheet.description, ''), coalesce(project.name, 'General Task'))
      )
  ) then
    raise exception 'Time entry details changed after review' using errcode = '40001';
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

  select count(*), round(coalesce(sum(timesheet.hours_logged::numeric), 0), 2)
  into matched_entry_count, approved_hours
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
      using errcode = '40001';
  end if;

  if p_billing_model = 'hourly' and approved_hours <> round(p_quantity, 2) then
    raise exception 'Approved hours changed after invoice review'
      using errcode = '40001';
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
$$;

create or replace function public.finalize_manual_invoice(
  p_invoice_id uuid,
  p_invoice_number text,
  p_net_total numeric,
  p_gross_total numeric,
  p_pdf_base64 text default null
)
returns table (
  invoice_id uuid,
  invoice_number text,
  status text,
  issued_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  invoice_row public.invoice_ledger%rowtype;
  expected_entry_count integer;
  updated_entry_count integer;
  decoded_pdf bytea;
begin
  select invoice.*
  into invoice_row
  from public.invoice_ledger as invoice
  where invoice.id = p_invoice_id
  for update;

  if invoice_row.id is null
    or not (select private.can_manage_company_invoices(invoice_row.company_id))
  then
    raise exception 'Invoice was not found or is not accessible'
      using errcode = '42501';
  end if;

  -- A lost HTTP response after COMMIT must be safe to replay locally.
  if invoice_row.status = 'issued'
    and invoice_row.invoice_number = btrim(p_invoice_number)
    and p_net_total = invoice_row.net_total and p_gross_total = invoice_row.gross_total
  then
    invoice_id := invoice_row.id; invoice_number := invoice_row.invoice_number;
    status := invoice_row.status; issued_at := invoice_row.issued_at;
    return next; return;
  end if;

  if invoice_row.status not in ('issuing', 'uncertain') then
    raise exception 'Invoice cannot be finalized from its current state'
      using errcode = '40001';
  end if;

  if nullif(btrim(p_invoice_number), '') is null
    or p_net_total is null or p_gross_total is null
    or p_net_total <> invoice_row.net_total
    or p_gross_total <> invoice_row.gross_total
  then
    raise exception 'Provider invoice details do not match the reviewed invoice'
      using errcode = '40001';
  end if;

  if p_pdf_base64 is not null then
    decoded_pdf := decode(p_pdf_base64, 'base64');
    if octet_length(decoded_pdf) = 0
      or octet_length(decoded_pdf) > 26214400
      or substring(decoded_pdf from 1 for 4) <> decode('25504446', 'hex')
    then
      raise exception 'The invoice PDF is invalid or too large'
        using errcode = '22023';
    end if;
  end if;

  select count(*) into expected_entry_count
  from public.invoice_ledger_entries as entry
  where entry.invoice_id = p_invoice_id;

  update public.invoice_ledger as invoice
  set status = 'issued',
      invoice_number = btrim(p_invoice_number),
      pdf_bytes = coalesce(decoded_pdf, invoice.pdf_bytes),
      pdf_available = decoded_pdf is not null or invoice.pdf_bytes is not null,
      provider_error_code = null,
      provider_error_message = null,
      issued_at = now()
  where invoice.id = p_invoice_id;

  update public.timesheets as timesheet
  set status = 'invoiced'
  from public.invoice_ledger_entries as entry
  where entry.invoice_id = p_invoice_id
    and entry.timesheet_id = timesheet.id
    and timesheet.company_id = invoice_row.company_id
    and timesheet.client_id = invoice_row.client_id
    and timesheet.status = 'approved'
    and timesheet.work_date = entry.work_date
    and timesheet.hours_logged = entry.hours_logged
    and timesheet.user_id = entry.user_id
    and timesheet.project_id is not distinct from entry.project_id
    and coalesce(timesheet.description, '') = entry.description;

  get diagnostics updated_entry_count = row_count;
  if updated_entry_count <> expected_entry_count then
    raise exception 'Time entry state changed before invoice finalization'
      using errcode = '40001';
  end if;



  invoice_id := p_invoice_id;
  invoice_number := btrim(p_invoice_number);
  status := 'issued';
  issued_at := now();
  return next;
end;
$$;
