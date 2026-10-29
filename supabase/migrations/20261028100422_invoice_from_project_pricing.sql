-- Project pricing is the only editable source for new client invoices.
alter table public.invoice_ledger drop constraint invoice_ledger_billing_model_check;
alter table public.invoice_ledger add constraint invoice_ledger_billing_model_check
  check (billing_model in ('hourly', 'fixed_monthly', 'project'));

create or replace function private.reject_legacy_client_pricing()
returns trigger language plpgsql set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    if new.net_unit_price is not null or new.billing_model <> 'hourly'
      or new.currency <> 'HUF' or new.invoice_unit <> 'óra'
      or new.invoice_item_name <> 'Szolgáltatás' then
      raise exception 'Manage prices, currency and units in project pricing' using errcode = 'PT409';
    end if;
  elsif row(new.net_unit_price,new.billing_model,new.currency,new.invoice_unit,new.invoice_item_name)
    is distinct from row(old.net_unit_price,old.billing_model,old.currency,old.invoice_unit,old.invoice_item_name) then
    raise exception 'Manage prices, currency and units in project pricing' using errcode = 'PT409';
  end if;
  return new;
end;
$$;
revoke all on function private.reject_legacy_client_pricing() from public, anon, authenticated;
create trigger reject_legacy_client_pricing
before insert or update on public.client_billing_profiles
for each row execute function private.reject_legacy_client_pricing();

alter table public.client_billing_profiles drop constraint if exists client_billing_profiles_automation_ready_check;
alter table public.client_billing_profiles add constraint client_billing_profiles_automation_ready_check check (
  not automatic_invoicing_enabled or (
    nullif(btrim(billing_name),'') is not null
    and nullif(btrim(billing_postal_code),'') is not null
    and nullif(btrim(billing_city),'') is not null
    and nullif(btrim(billing_address),'') is not null
    and tax_subject is not null
    and (tax_subject <> 1 or nullif(btrim(tax_number),'') is not null)
    and (tax_subject <> 6 or nullif(btrim(eu_vat_number),'') is not null)
    and (not send_invoice_email or nullif(btrim(billing_email),'') is not null)
  )
);

create or replace function private.validate_project_invoice_prices(
  p_company_id uuid, p_client_id uuid, p_period_start date, p_period_end date,
  p_snapshot jsonb, p_currency text, p_vat_code text,
  p_net numeric, p_vat numeric, p_gross numeric
)
returns void language plpgsql set search_path = '' as $$
declare
  item record;
  buyer record;
  source_price record;
  expected_hours numeric;
  manual_quantity numeric;
  tax_rate numeric;
  precision_digits integer;
  line_net numeric;
  line_vat numeric;
  line_gross numeric;
  sum_net numeric := 0;
  sum_vat numeric := 0;
  sum_gross numeric := 0;
begin
  if p_snapshot->>'billing_model' is distinct from 'project'
    or extract(year from p_period_start) <> extract(year from p_period_end)
    or jsonb_typeof(p_snapshot->'items') is distinct from 'array'
    or jsonb_array_length(p_snapshot->'items') not between 1 and 1000
    or jsonb_typeof(p_snapshot->'project_quantities') is distinct from 'object' then
    raise exception 'A project-price review is required' using errcode = 'PT409';
  end if;
  if (select count(distinct e->>'project_id') from jsonb_array_elements(p_snapshot->'items') e)
    <> jsonb_array_length(p_snapshot->'items') then
    raise exception 'Duplicate or missing invoice projects' using errcode = '22023';
  end if;
  -- The public reservation already authenticated the tenant and locked approved
  -- time and the recipient profile. Keep rate/name locks until reservation ends.
  perform project.id from public.projects project
    join jsonb_to_recordset(p_snapshot->'items') reviewed(project_id uuid) on reviewed.project_id=project.id
    order by project.id for share of project;
  perform price.project_id from public.project_pricing price
    join jsonb_to_recordset(p_snapshot->'items') reviewed(project_id uuid,pricing_year smallint)
      on reviewed.project_id=price.project_id and reviewed.pricing_year=price.pricing_year
    order by price.project_id for share of price;
  select * into strict buyer from public.client_billing_profiles
    where company_id=p_company_id and client_id=p_client_id;
  if p_vat_code is distinct from buyer.vat_code or buyer.vat_code='K.AFA' then
    raise exception 'Invoice tax settings changed after review' using errcode = 'PT409';
  end if;
  tax_rate := case when buyer.vat_code ~ '^[0-9]+(\.[0-9]+)?$' then buyer.vat_code::numeric else 0 end;
  precision_digits := case when p_currency='HUF' then 0 else 2 end;
  for item in select * from jsonb_to_recordset(p_snapshot->'items') as reviewed(
    project_id uuid,project_name text,pricing_year smallint,pricing_updated_at timestamptz,
    quantity numeric,unit text,net_unit_price numeric,vat_code text,currency text,
    net_total numeric,vat_total numeric,gross_total numeric
  ) loop
    select price.*,project.name,project.client_id,project.is_active into source_price
      from public.project_pricing price join public.projects project
        on project.id=price.project_id and project.company_id=price.company_id
      where price.project_id=item.project_id and price.company_id=p_company_id
        and project.client_id=p_client_id and price.pricing_year=extract(year from p_period_start);
    if not found or source_price.is_active is not true or source_price.indexed_fee is null
      or row(item.project_name,item.pricing_year,item.pricing_updated_at,item.net_unit_price,item.unit,item.currency)
        is distinct from row(source_price.name,source_price.pricing_year,source_price.updated_at,
          source_price.indexed_fee,source_price.unit,source_price.currency)
      or item.currency is distinct from p_currency or item.vat_code is distinct from p_vat_code
      or item.quantity is null or not (item.quantity > 0 and item.quantity <= 999999)
    then
      raise exception 'Project prices changed or are missing; review again' using errcode = 'PT409';
    end if;
    select coalesce(sum(hours_logged),0) into expected_hours from public.timesheets
      where company_id=p_company_id and client_id=p_client_id and project_id=item.project_id
        and status='approved' and work_date between p_period_start and p_period_end;
    if lower(btrim(source_price.unit)) in ('óra','hour','hours','h','hr') then
      if item.quantity <> expected_hours or (p_snapshot->'project_quantities') ? item.project_id::text then
        raise exception 'Hourly quantities must match approved project hours' using errcode = 'PT409';
      end if;
    else
      manual_quantity := (p_snapshot->'project_quantities'->>item.project_id::text)::numeric;
      if manual_quantity is distinct from item.quantity then
        raise exception 'Project invoice quantities changed after review' using errcode = 'PT409';
      end if;
      if lower(btrim(source_price.unit)) in ('hó','hónap','month') and (
        item.quantity <> 1 or p_period_start <> date_trunc('month',p_period_start)::date
        or p_period_end <> (date_trunc('month',p_period_start)+interval '1 month - 1 day')::date
      ) then
        raise exception 'Monthly projects require one complete calendar month and quantity 1' using errcode = '22023';
      end if;
    end if;
    if p_currency='HUF' and buyer.tax_subject=-1 then
      line_gross := round(item.quantity*source_price.indexed_fee*(100+tax_rate)/100,0);
      line_vat := round(line_gross*tax_rate/(100+tax_rate),0);
      line_net := line_gross-line_vat;
    else
      line_net := round(item.quantity*source_price.indexed_fee,precision_digits);
      line_vat := round(line_net*tax_rate/100,precision_digits);
      line_gross := line_net+line_vat;
    end if;
    if row(item.net_total,item.vat_total,item.gross_total) is distinct from row(line_net,line_vat,line_gross) then
      raise exception 'Project line totals differ from saved pricing' using errcode = 'PT409';
    end if;
    sum_net:=sum_net+line_net; sum_vat:=sum_vat+line_vat; sum_gross:=sum_gross+line_gross;
  end loop;
  if row(p_net,p_vat,p_gross) is distinct from row(sum_net,sum_vat,sum_gross) then
    raise exception 'Invoice totals differ from saved project prices' using errcode = 'PT409';
  end if;
  if exists (select 1 from public.timesheets entry
    left join jsonb_to_recordset(p_snapshot->'items') reviewed(project_id uuid) on reviewed.project_id=entry.project_id
    where entry.company_id=p_company_id and entry.client_id=p_client_id
      and entry.status='approved' and entry.work_date between p_period_start and p_period_end
      and reviewed.project_id is null) then
    raise exception 'An approved project is missing from the invoice' using errcode = 'PT409';
  end if;
end;
$$;
revoke all on function private.validate_project_invoice_prices(uuid,uuid,date,date,jsonb,text,text,numeric,numeric,numeric)
  from public,anon,authenticated;

comment on table public.project_pricing is
  'Authoritative annual project rates for invoice review, preview and issuance. NULL indexed_fee blocks billing the project; original_fee is historical source data only.';

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
$function$
