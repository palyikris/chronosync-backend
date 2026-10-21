create table if not exists public.invoice_attachment_periods (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references public.companies(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  period_start date not null,
  period_end date not null,
  starting_hours numeric(12, 2) not null,
  available_hours numeric(12, 2) not null,
  used_hours numeric(12, 2) not null,
  remaining_hours numeric(12, 2) not null,
  created_by uuid not null references public.profiles(id) on delete restrict,
  created_at timestamptz not null default now(),
  constraint invoice_attachment_periods_valid_range
    check (period_start <= period_end),
  constraint invoice_attachment_periods_full_month
    check (
      period_start = date_trunc('month', period_start)::date
      and period_end =
        (date_trunc('month', period_start) + interval '1 month - 1 day')::date
    ),
  constraint invoice_attachment_periods_company_client_period_key
    unique (company_id, client_id, period_start, period_end)
);

create index if not exists invoice_attachment_periods_company_period_idx
  on public.invoice_attachment_periods (
    company_id,
    period_start,
    period_end,
    client_id
  );

create index if not exists invoice_attachment_periods_client_id_idx
  on public.invoice_attachment_periods (client_id);

create index if not exists invoice_attachment_periods_created_by_idx
  on public.invoice_attachment_periods (created_by);

create index if not exists timesheets_invoice_attachment_idx
  on public.timesheets (company_id, client_id, work_date, id)
  where status in ('approved', 'invoiced');

alter table public.invoice_attachment_periods enable row level security;

drop policy if exists "Company admins can read invoice attachment periods"
  on public.invoice_attachment_periods;
create policy "Company admins can read invoice attachment periods"
  on public.invoice_attachment_periods
  for select
  to authenticated
  using (
    exists (
      select 1
      from public.profiles as profile
      where profile.id = (select auth.uid())
        and profile.is_active = true
        and (
          profile.role = 'super_admin'
          or (
            profile.role = 'company_admin'
            and profile.company_id = invoice_attachment_periods.company_id
          )
        )
    )
  );

drop policy if exists "Company admins can create invoice attachment periods"
  on public.invoice_attachment_periods;

revoke all on table public.invoice_attachment_periods from anon, authenticated;
grant select on table public.invoice_attachment_periods to authenticated;

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

    select round(
      coalesce(sum(round(timesheet.hours_logged::numeric, 2)), 0),
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
