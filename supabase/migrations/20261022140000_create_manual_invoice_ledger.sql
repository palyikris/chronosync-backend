create schema if not exists private;

revoke all on schema private from public;
grant usage on schema private to authenticated, service_role;

create or replace function private.can_manage_company_invoices(p_company_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles as profile
    join public.companies as company on company.id = p_company_id
    where profile.id = (select auth.uid())
      and profile.is_active = true
      and company.is_active = true
      and company.is_deleted = false
      and (
        profile.role = 'super_admin'
        or (
          profile.role = 'company_admin'
          and profile.company_id = p_company_id
        )
      )
  );
$$;

revoke all on function private.can_manage_company_invoices(uuid) from public;
grant execute on function private.can_manage_company_invoices(uuid)
  to authenticated, service_role;

create table if not exists public.invoice_ledger (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references public.companies(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete restrict,
  period_start date not null,
  period_end date not null,
  status text not null default 'issuing',
  provider text not null default 'szamlazz_hu',
  external_id text not null,
  review_fingerprint text not null,
  invoice_snapshot jsonb not null,
  recipient_name text not null,
  recipient_email text,
  send_email boolean not null default false,
  billing_model text not null,
  quantity numeric(14, 2) not null,
  unit text not null,
  net_unit_price numeric(18, 4) not null,
  vat_code text not null,
  currency text not null,
  net_total numeric(18, 4) not null,
  vat_total numeric(18, 4) not null,
  gross_total numeric(18, 4) not null,
  invoice_number text,
  pdf_bytes bytea,
  pdf_available boolean not null default false,
  provider_error_code text,
  provider_error_message text,
  attempt_count integer not null default 1,
  created_by uuid not null references public.profiles(id) on delete restrict,
  issued_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint invoice_ledger_period_check check (period_start <= period_end),
  constraint invoice_ledger_status_check
    check (status in ('issuing', 'issued', 'uncertain', 'failed')),
  constraint invoice_ledger_provider_check check (provider = 'szamlazz_hu'),
  constraint invoice_ledger_billing_model_check
    check (billing_model in ('hourly', 'fixed_monthly')),
  constraint invoice_ledger_quantity_check check (quantity > 0),
  constraint invoice_ledger_amounts_check
    check (
      net_unit_price >= 0
      and net_total >= 0
      and vat_total >= 0
      and gross_total = net_total + vat_total
    ),
  constraint invoice_ledger_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint invoice_ledger_non_empty_check
    check (
      btrim(external_id) <> ''
      and btrim(review_fingerprint) <> ''
      and btrim(recipient_name) <> ''
      and btrim(unit) <> ''
      and btrim(vat_code) <> ''
    ),
  constraint invoice_ledger_company_client_period_key
    unique (company_id, client_id, period_start, period_end),
  constraint invoice_ledger_external_id_key unique (external_id)
);

create unique index if not exists invoice_ledger_provider_number_key
  on public.invoice_ledger (company_id, provider, invoice_number)
  where invoice_number is not null;

create index if not exists invoice_ledger_company_created_idx
  on public.invoice_ledger (company_id, created_at desc);

create index if not exists invoice_ledger_client_period_idx
  on public.invoice_ledger (client_id, period_start desc, period_end desc);

create table if not exists public.invoice_ledger_entries (
  invoice_id uuid not null references public.invoice_ledger(id) on delete cascade,
  timesheet_id uuid not null references public.timesheets(id) on delete restrict,
  user_id uuid not null references public.profiles(id) on delete restrict,
  project_id uuid references public.projects(id) on delete restrict,
  work_date date not null,
  hours_logged numeric(12, 2) not null,
  description text not null,
  primary key (invoice_id, timesheet_id),
  constraint invoice_ledger_entries_timesheet_key unique (timesheet_id),
  constraint invoice_ledger_entries_hours_check check (hours_logged > 0)
);

create index if not exists invoice_ledger_entries_invoice_idx
  on public.invoice_ledger_entries (invoice_id, work_date, timesheet_id);

create index if not exists timesheets_manual_invoice_idx
  on public.timesheets (company_id, client_id, work_date, id)
  where status = 'approved';

alter table public.invoice_ledger enable row level security;
alter table public.invoice_ledger_entries enable row level security;

drop policy if exists "Invoice admins can read invoice ledger"
  on public.invoice_ledger;
create policy "Invoice admins can read invoice ledger"
  on public.invoice_ledger
  for select
  to authenticated
  using ((select private.can_manage_company_invoices(company_id)));

drop policy if exists "Invoice admins can read invoice ledger entries"
  on public.invoice_ledger_entries;
create policy "Invoice admins can read invoice ledger entries"
  on public.invoice_ledger_entries
  for select
  to authenticated
  using (
    exists (
      select 1
      from public.invoice_ledger as invoice
      where invoice.id = invoice_ledger_entries.invoice_id
        and (select private.can_manage_company_invoices(invoice.company_id))
    )
  );

revoke all on table public.invoice_ledger from anon, authenticated;
revoke all on table public.invoice_ledger_entries from anon, authenticated;
grant select on table public.invoice_ledger to authenticated;
grant select on table public.invoice_ledger_entries to authenticated;

create or replace function private.set_invoice_ledger_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

revoke all on function private.set_invoice_ledger_updated_at()
  from public, anon, authenticated;

drop trigger if exists set_invoice_ledger_updated_at on public.invoice_ledger;
create trigger set_invoice_ledger_updated_at
before update on public.invoice_ledger
for each row execute function private.set_invoice_ledger_updated_at();

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

  if p_invoice_id is null
    or p_period_start is null
    or p_period_end is null
    or p_period_start > p_period_end
    or p_period_end - p_period_start > 366
    or nullif(btrim(p_review_fingerprint), '') is null
    or nullif(btrim(p_recipient_name), '') is null
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

revoke all on function public.reserve_manual_invoice(
  uuid, uuid, uuid, date, date, text, text, text, boolean, text,
  numeric, text, numeric, text, text, numeric, numeric, numeric, jsonb, jsonb
) from public, anon;
grant execute on function public.reserve_manual_invoice(
  uuid, uuid, uuid, date, date, text, text, text, boolean, text,
  numeric, text, numeric, text, text, numeric, numeric, numeric, jsonb, jsonb
) to authenticated;

create or replace function public.mark_manual_invoice_attempt(
  p_invoice_id uuid,
  p_status text,
  p_error_code text,
  p_error_message text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  invoice_row public.invoice_ledger%rowtype;
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

  if invoice_row.status <> 'issuing' or p_status not in ('failed', 'uncertain') then
    raise exception 'Invoice attempt is not in a reconcilable state'
      using errcode = '40001';
  end if;

  update public.invoice_ledger as invoice
  set status = p_status,
      provider_error_code = nullif(left(btrim(p_error_code), 100), ''),
      provider_error_message = nullif(left(btrim(p_error_message), 500), '')
  where invoice.id = p_invoice_id;

  if p_status = 'failed' then
    delete from public.invoice_ledger_entries as entry
    where entry.invoice_id = p_invoice_id;
  end if;
end;
$$;

revoke all on function public.mark_manual_invoice_attempt(uuid, text, text, text)
  from public, anon;
grant execute on function public.mark_manual_invoice_attempt(uuid, text, text, text)
  to authenticated;

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

  if invoice_row.status not in ('issuing', 'uncertain') then
    raise exception 'Invoice cannot be finalized from its current state'
      using errcode = '40001';
  end if;

  if nullif(btrim(p_invoice_number), '') is null
    or round(p_net_total, 4) <> invoice_row.net_total
    or round(p_gross_total, 4) <> invoice_row.gross_total
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

  update public.timesheets as timesheet
  set status = 'invoiced'
  from public.invoice_ledger_entries as entry
  where entry.invoice_id = p_invoice_id
    and entry.timesheet_id = timesheet.id
    and timesheet.company_id = invoice_row.company_id
    and timesheet.client_id = invoice_row.client_id
    and timesheet.status = 'approved';

  get diagnostics updated_entry_count = row_count;
  if updated_entry_count <> expected_entry_count then
    raise exception 'Time entry state changed before invoice finalization'
      using errcode = '40001';
  end if;

  update public.invoice_ledger as invoice
  set status = 'issued',
      invoice_number = btrim(p_invoice_number),
      pdf_bytes = coalesce(decoded_pdf, invoice.pdf_bytes),
      pdf_available = decoded_pdf is not null or invoice.pdf_bytes is not null,
      provider_error_code = null,
      provider_error_message = null,
      issued_at = now()
  where invoice.id = p_invoice_id;

  invoice_id := p_invoice_id;
  invoice_number := btrim(p_invoice_number);
  status := 'issued';
  issued_at := now();
  return next;
end;
$$;

revoke all on function public.finalize_manual_invoice(
  uuid, text, numeric, numeric, text
) from public, anon;
grant execute on function public.finalize_manual_invoice(
  uuid, text, numeric, numeric, text
) to authenticated;

create or replace function public.resolve_manual_invoice_not_issued(
  p_invoice_id uuid
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  invoice_row public.invoice_ledger%rowtype;
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

  if invoice_row.status <> 'uncertain' then
    raise exception 'Only an uncertain invoice can be resolved as not issued'
      using errcode = '40001';
  end if;

  delete from public.invoice_ledger_entries as entry
  where entry.invoice_id = p_invoice_id;

  update public.invoice_ledger as invoice
  set status = 'failed',
      provider_error_code = 'manually_verified_not_issued',
      provider_error_message = 'Verified in Számlázz.hu as not issued; retry is allowed.'
  where invoice.id = p_invoice_id;
end;
$$;

revoke all on function public.resolve_manual_invoice_not_issued(uuid)
  from public, anon;
grant execute on function public.resolve_manual_invoice_not_issued(uuid)
  to authenticated;

do $$
begin
  if to_regclass('public.client_billing_profiles') is not null then
    update public.client_billing_profiles
    set automatic_invoicing_enabled = false
    where automatic_invoicing_enabled = true;
  end if;
end;
$$;
