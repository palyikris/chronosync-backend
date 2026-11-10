-- Defaults preserve existing invoice behavior; existing RLS/admin authorization remains in force.
alter table public.client_billing_profiles
  add column invoice_note text check (invoice_note is null or (
    length(btrim(invoice_note)) between 1 and 450
    and invoice_note !~ U&'[\0001-\0008\000B\000C\000E-\001F\FFFE\FFFF]')),
  add column payment_due_policy text not null default 'days_after_issue'
    check (payment_due_policy in ('days_after_issue','month_end')),
  add column payment_due_reference text not null default 'issue_date'
    check (payment_due_reference in ('issue_date','period_end')),
  add column payment_due_month_offset smallint not null default 1
    check (payment_due_month_offset between 0 and 2);

-- Revalidate notes and resolved dates under the reservation's existing profile/time/price locks.
-- Keep the project, money, tenant and entry guards from the preceding validator.
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
  issue_day date;
  due_day date;
  fulfillment_day date;
  anchor_day date;
  overrides jsonb;
  expected_comment text;
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
  if jsonb_typeof(p_snapshot->'approved_entries') is distinct from 'array' then
    raise exception 'Missing reviewed time entry snapshot' using errcode = '22023';
  end if;
  if exists (
    select 1 from public.timesheets entry
    left join jsonb_to_recordset(p_snapshot->'approved_entries') reviewed(id uuid, billing_multiplier integer)
      on reviewed.id = entry.id
    where entry.company_id = p_company_id and entry.client_id = p_client_id
      and entry.status = 'approved' and entry.work_date between p_period_start and p_period_end
      and (reviewed.id is null or reviewed.billing_multiplier is distinct from entry.billing_multiplier)
  ) then
    raise exception 'Time entry multipliers changed after review' using errcode = 'PT409';
  end if;
  -- The public reservation already authenticated the tenant and locked approved
  -- time and the recipient profile. Keep rate/name locks until reservation ends.
  perform project.id from public.projects project
    where project.company_id=p_company_id and project.client_id=p_client_id
    order by project.id for share of project;
  perform price.project_id from public.project_pricing price
    join public.projects project on project.id=price.project_id
    where price.company_id=p_company_id and project.client_id=p_client_id
      and price.pricing_year=extract(year from p_period_start)
    order by price.project_id for share of price;
  select * into strict buyer from public.client_billing_profiles
    where company_id=p_company_id and client_id=p_client_id;
  if p_vat_code is distinct from buyer.vat_code or buyer.vat_code='K.AFA' then
    raise exception 'Invoice tax settings changed after review' using errcode = 'PT409';
  end if;
  issue_day := (p_snapshot->>'issue_date')::date;
  overrides := coalesce(p_snapshot->'date_overrides', '{}'::jsonb);
  if jsonb_typeof(overrides) is distinct from 'object' or exists (
    select 1 from jsonb_object_keys(overrides) k where k not in ('due_date','fulfillment_date')
  ) then
    raise exception 'Invalid invoice date overrides' using errcode = '22023';
  end if;
  if exists (select 1 from jsonb_each(overrides) e where e.value <> 'null'::jsonb
    and (jsonb_typeof(e.value) <> 'string' or (e.value #>> '{}') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')) then
    raise exception 'Invalid invoice override date format' using errcode = '22023';
  end if;
  if buyer.payment_due_policy = 'days_after_issue' then
    due_day := issue_day + buyer.payment_term_days;
  else
    anchor_day := case buyer.payment_due_reference when 'period_end' then p_period_end else issue_day end;
    due_day := (date_trunc('month', anchor_day) + make_interval(months => buyer.payment_due_month_offset + 1)
      - interval '1 day')::date;
  end if;
  due_day := coalesce((overrides->>'due_date')::date, due_day);
  fulfillment_day := coalesce((overrides->>'fulfillment_date')::date, case buyer.fulfillment_date_policy
    when 'period_end' then p_period_end when 'due_date' then due_day when 'issue_date' then issue_day end);
  if due_day < issue_day
    or ((overrides->>'due_date') is not null and due_day > issue_day + 366)
    or ((overrides->>'fulfillment_date') is not null and abs(fulfillment_day - issue_day) > 366) then
    raise exception 'Invoice override dates are outside the allowed range' using errcode = 'PT409';
  end if;
  expected_comment := case when buyer.invoice_language='hu' then 'Elszámolási időszak' else 'Service period' end
    || ': ' || p_period_start::text || ' - ' || p_period_end::text
    || case when nullif(btrim(buyer.invoice_note),'') is not null then E'\n' || btrim(buyer.invoice_note) else '' end;
  if p_snapshot->>'invoice_comment' is distinct from expected_comment then
    raise exception 'Invoice note changed; review again' using errcode = 'PT409';
  end if;
  if issue_day is distinct from (now() at time zone 'Europe/Budapest')::date
    or (p_snapshot->>'due_date')::date is distinct from due_day
    or (p_snapshot->>'fulfillment_date')::date is distinct from fulfillment_day
    or coalesce((p_snapshot->>'e_invoice')::boolean,false) is distinct from buyer.invoice_electronic
    or nullif(p_snapshot->>'invoice_prefix','') is distinct from buyer.invoice_prefix
    or nullif(p_snapshot->>'seller_bank_name','') is distinct from buyer.seller_bank_name
    or nullif(p_snapshot->>'seller_bank_account','') is distinct from buyer.seller_bank_account then
    raise exception 'Invoice document or date settings changed; review again' using errcode = 'PT409';
  end if;
  -- An active recurring service must not disappear when it has no time entries.
  if exists (
    select 1 from public.project_pricing price
    join public.projects project on project.id=price.project_id and project.company_id=price.company_id
    left join jsonb_to_recordset(p_snapshot->'items') reviewed(project_id uuid) on reviewed.project_id=project.id
    where price.company_id=p_company_id and project.client_id=p_client_id and project.is_active
      and price.pricing_year=extract(year from p_period_start)
      and (lower(btrim(price.unit)) in ('hó','hónap','month','/fő/hó','fő/hó','person/month','person/months')
        or price.recurring_quantity is not null)
      and reviewed.project_id is null
  ) then
    raise exception 'A recurring project is missing from the invoice' using errcode = 'PT409';
  end if;
  tax_rate := case when buyer.vat_code ~ '^[0-9]+(\.[0-9]+)?$' then buyer.vat_code::numeric else 0 end;
  precision_digits := case when p_currency='HUF' then 0 else 2 end;
  for item in select * from jsonb_to_recordset(p_snapshot->'items') as reviewed(
    project_id uuid,project_name text,pricing_year smallint,pricing_updated_at timestamptz,
    quantity numeric,unit text,net_unit_price numeric,vat_code text,currency text,is_payroll boolean,
    invoice_name text,invoice_note text,po_number text,
    net_total numeric,vat_total numeric,gross_total numeric
  ) loop
    select price.*,project.name,project.client_id,project.is_active,project.is_payroll into source_price
      from public.project_pricing price join public.projects project
        on project.id=price.project_id and project.company_id=price.company_id
      where price.project_id=item.project_id and price.company_id=p_company_id
        and project.client_id=p_client_id and price.pricing_year=extract(year from p_period_start);
    if not found or source_price.is_active is not true or source_price.indexed_fee is null
      or row(item.project_name,item.pricing_year,item.pricing_updated_at,item.net_unit_price,item.unit,item.currency)
        is distinct from row(source_price.name,source_price.pricing_year,source_price.updated_at,
          source_price.indexed_fee,source_price.unit,source_price.currency)
      or row(item.invoice_name,item.invoice_note,item.po_number)
        is distinct from row(source_price.invoice_name,source_price.attachment_note,source_price.po_number)
      or item.is_payroll is distinct from source_price.is_payroll
      or item.currency is distinct from p_currency or item.vat_code is distinct from p_vat_code
      or item.quantity is null or not (item.quantity > 0 and item.quantity <= 999999)
    then
      raise exception 'Project prices changed or are missing; review again' using errcode = 'PT409';
    end if;
    select coalesce(sum(hours_logged * billing_multiplier),0) into expected_hours from public.timesheets
      where company_id=p_company_id and client_id=p_client_id and project_id=item.project_id
        and status='approved' and work_date between p_period_start and p_period_end;
    if lower(btrim(source_price.unit)) in ('óra','hour','hours','h','hr') then
      if item.quantity <> expected_hours or (p_snapshot->'project_quantities') ? item.project_id::text then
        raise exception 'Hourly quantities must match approved entry hours multiplied by each entry billing multiplier' using errcode = 'PT409';
      end if;
    else
      manual_quantity := (p_snapshot->'project_quantities'->>item.project_id::text)::numeric;
      if manual_quantity is distinct from item.quantity then
        raise exception 'Project invoice quantities changed after review' using errcode = 'PT409';
      end if;
      if (lower(btrim(source_price.unit)) in ('hó','hónap','month','/fő/hó','fő/hó','person/month','person/months')
          or source_price.recurring_quantity is not null) and (
        (lower(btrim(source_price.unit)) in ('hó','hónap','month') and item.quantity <> 1) or p_period_start <> date_trunc('month',p_period_start)::date
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
