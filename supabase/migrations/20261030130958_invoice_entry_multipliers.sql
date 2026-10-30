-- Requires frontend 20261030130954_move_payroll_multiplier_to_entries.sql.
-- Freeze and validate each reviewed multiplier, including changes that leave
-- the aggregate quantity unchanged. Reservation already locks approved entries.
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
    quantity numeric,unit text,net_unit_price numeric,vat_code text,currency text,is_payroll boolean,
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


-- Attachment carry-over must use the same billable hours as the workbook.
create or replace function public.finalize_invoice_attachment_period(
  p_company_id uuid,
  p_period_start date,
  p_period_end date,
  p_client_updates jsonb
)
returns table (client_id uuid, applied boolean)
language plpgsql
security definer
set search_path = ''
as $$
declare
  update_row record;
  client_row record;
  snapshot_row record;
  computed_used_hours numeric(12, 2);
  computed_remaining_hours numeric(12, 2);
  inserted_period_id uuid;
  update_count integer;
  distinct_client_count integer;
begin
  if p_period_start is null
    or p_period_end is null
    or p_period_start <> date_trunc('month', p_period_start)::date
    or p_period_end <>
      (date_trunc('month', p_period_start) + interval '1 month - 1 day')::date
  then
    raise exception 'Carry-over can only be finalized for a complete calendar month'
      using errcode = '22023';
  end if;

  if p_client_updates is null
    or jsonb_typeof(p_client_updates) <> 'array'
    or jsonb_array_length(p_client_updates) = 0
    or jsonb_array_length(p_client_updates) > 100
  then
    raise exception 'Between 1 and 100 client updates are required'
      using errcode = '22023';
  end if;

  if not exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.is_active = true
      and (
        profile.role = 'super_admin'
        or (
          profile.role = 'company_admin'
          and profile.company_id = p_company_id
        )
      )
  ) then
    raise exception 'Not authorized to finalize invoice attachments for this company'
      using errcode = '42501';
  end if;

  select
    count(*),
    count(distinct item.client_id)
  into update_count, distinct_client_count
  from jsonb_to_recordset(p_client_updates) as item(client_id uuid);

  if update_count <> distinct_client_count then
    raise exception 'Duplicate client updates are not allowed'
      using errcode = '22023';
  end if;

  for update_row in
    select item.*
    from jsonb_to_recordset(p_client_updates) as item(
      client_id uuid,
      starting_hours numeric,
      available_hours numeric,
      used_hours numeric,
      remaining_hours numeric
    )
    order by item.client_id
  loop
    if update_row.client_id is null
      or update_row.starting_hours is null
      or update_row.available_hours is null
      or update_row.used_hours is null
      or update_row.remaining_hours is null
      or abs(update_row.starting_hours) > 1000000
      or abs(update_row.available_hours) > 1000000
      or abs(update_row.used_hours) > 1000000
      or abs(update_row.remaining_hours) > 1000000
      or update_row.starting_hours::text in ('NaN', 'Infinity', '-Infinity')
      or update_row.available_hours::text in ('NaN', 'Infinity', '-Infinity')
      or update_row.used_hours::text in ('NaN', 'Infinity', '-Infinity')
      or update_row.remaining_hours::text in ('NaN', 'Infinity', '-Infinity')
      or update_row.used_hours < 0
    then
      raise exception 'Invalid invoice attachment carry-over values'
        using errcode = '22023';
    end if;

    select
      client.id,
      round(coalesce(client.hours_from_previous_month, 0)::numeric, 2)
        as starting_hours,
      round(coalesce(client.available_hours_per_month, 0)::numeric, 2)
        as available_hours
    into client_row
    from public.clients as client
    where client.id = update_row.client_id
      and client.company_id = p_company_id
      and client.is_active = true
    for update;

    if client_row.id is null then
      raise exception 'Selected client is missing, inactive, or outside the company'
        using errcode = '42501';
    end if;

    if exists (
      select 1
      from public.timesheets as timesheet
      where timesheet.company_id = p_company_id
        and timesheet.client_id = update_row.client_id
        and timesheet.work_date between p_period_start and p_period_end
        and timesheet.status in ('approved', 'invoiced')
        and (timesheet.hours_logged is null or timesheet.hours_logged <= 0)
    ) then
      raise exception 'An approved or invoiced entry contains invalid hours'
        using errcode = '22023';
    end if;

    -- Keep entry factors stable until the carry-over snapshot is persisted.
    perform entry.id from public.timesheets entry
      where entry.company_id = p_company_id and entry.client_id = update_row.client_id
        and entry.work_date between p_period_start and p_period_end
        and entry.status in ('approved', 'invoiced')
      order by entry.id for share;

    select round(
      coalesce(sum(round(timesheet.hours_logged::numeric, 2) * timesheet.billing_multiplier), 0),
      2
    )
    into computed_used_hours
    from public.timesheets as timesheet
    where timesheet.company_id = p_company_id
      and timesheet.client_id = update_row.client_id
      and timesheet.work_date between p_period_start and p_period_end
      and timesheet.status in ('approved', 'invoiced');

    select period.*
    into snapshot_row
    from public.invoice_attachment_periods as period
    where period.company_id = p_company_id
      and period.client_id = update_row.client_id
      and period.period_start = p_period_start
      and period.period_end = p_period_end
    for update;

    if snapshot_row.id is not null then
      if computed_used_hours <> snapshot_row.used_hours
        or client_row.available_hours <> snapshot_row.available_hours
        or round(update_row.starting_hours, 2) <> snapshot_row.starting_hours
        or round(update_row.available_hours, 2) <> snapshot_row.available_hours
        or round(update_row.used_hours, 2) <> snapshot_row.used_hours
        or round(update_row.remaining_hours, 2) <> snapshot_row.remaining_hours
      then
        raise exception 'The finalized invoice period no longer matches its snapshot'
          using errcode = '40001';
      end if;

      client_id := update_row.client_id;
      applied := false;
      return next;
      continue;
    end if;

    computed_remaining_hours := round(
      client_row.starting_hours
      + client_row.available_hours
      - computed_used_hours,
      2
    );

    if round(update_row.starting_hours, 2) <> client_row.starting_hours
      or round(update_row.available_hours, 2) <> client_row.available_hours
      or round(update_row.used_hours, 2) <> computed_used_hours
      or round(update_row.remaining_hours, 2) <> computed_remaining_hours
    then
      raise exception 'Invoice attachment data changed before finalization'
        using errcode = '40001';
    end if;

    inserted_period_id := null;
    insert into public.invoice_attachment_periods (
      company_id,
      client_id,
      period_start,
      period_end,
      starting_hours,
      available_hours,
      used_hours,
      remaining_hours,
      created_by
    ) values (
      p_company_id,
      update_row.client_id,
      p_period_start,
      p_period_end,
      client_row.starting_hours,
      client_row.available_hours,
      computed_used_hours,
      computed_remaining_hours,
      (select auth.uid())
    )
    on conflict on constraint invoice_attachment_periods_company_client_period_key
      do nothing
    returning id into inserted_period_id;

    if inserted_period_id is not null then
      update public.clients as client
      set hours_from_previous_month = computed_remaining_hours
      where client.id = update_row.client_id
        and client.company_id = p_company_id;

      if not found then
        raise exception 'Could not update the selected client carry-over'
          using errcode = '42501';
      end if;
    end if;

    client_id := update_row.client_id;
    applied := inserted_period_id is not null;
    return next;
  end loop;
end;
$$;

revoke all on function public.finalize_invoice_attachment_period(
  uuid,
  date,
  date,
  jsonb
) from public, anon;
grant execute on function public.finalize_invoice_attachment_period(
  uuid,
  date,
  date,
  jsonb
) to authenticated;

-- Reserved and issued time entries also freeze the billing multiplier.
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

-- A multiplier edit is a billing edit and must respect approved leave locks.
create or replace function private.guard_approved_leave_timesheet()
returns trigger language plpgsql security invoker set search_path = '' as $$
declare v_user uuid; v_changed boolean := true;
begin
  if current_user not in ('authenticated', 'anon') then
    if tg_op = 'DELETE' then return old; end if;
    return new;
  end if;
  if tg_op = 'DELETE' then v_user := old.user_id; else v_user := new.user_id; end if;
  perform pg_advisory_xact_lock(hashtextextended('chronosync-leave:' || v_user::text, 0));
  if tg_op = 'DELETE' then
    if old.leave_request_id is not null then
      raise sqlstate 'PT409' using message = 'Cancel the leave request to remove its time entries';
    end if;
    return old;
  end if;
  if tg_op = 'UPDATE' then
    v_changed := (new.work_date, new.hours_logged, new.description, new.client_id, new.project_id, new.billing_multiplier)
      is distinct from (old.work_date, old.hours_logged, old.description, old.client_id, old.project_id, old.billing_multiplier);
    if new.leave_request_id is distinct from old.leave_request_id or (old.leave_request_id is not null and v_changed) then
      raise sqlstate '42501' using message = 'Leave entries can only change through the leave operation';
    end if;
  elsif new.leave_request_id is not null then
    raise sqlstate '42501' using message = 'Only leave approval can create linked entries';
  end if;
  if v_changed and exists (select 1 from public.leave_requests where user_id = new.user_id
    and status = 'APPROVED' and new.work_date between start_date and end_date) then
    raise sqlstate 'PT409' using message = 'Date is locked by approved leave';
  end if;
  return new;
end $$;
revoke all on function private.guard_approved_leave_timesheet() from public, anon, authenticated;
