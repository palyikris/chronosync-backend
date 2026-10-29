-- Effective-year rates preserve past prices when next year's configuration changes.
create table public.company_price_indexes (
  company_id uuid not null references public.companies(id) on delete cascade,
  index_code text not null check (index_code in ('ksh', 'eu')),
  effective_year smallint not null check (effective_year between 2000 and 9999),
  rate numeric(10, 6) not null check (rate between -1 and 10),
  source_sha256 text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (company_id, index_code, effective_year)
);

create trigger set_company_price_indexes_updated_at
before update on public.company_price_indexes for each row
execute function private.set_project_pricing_updated_at();

alter table public.company_price_indexes enable row level security;
create policy "Company admins can read price indexes" on public.company_price_indexes
  for select to authenticated using (company_id = (select private.current_billing_admin_company_id()));
create policy "Company admins configure next year indexes" on public.company_price_indexes
  for insert to authenticated with check (
    company_id = (select private.current_billing_admin_company_id()) and
    effective_year = extract(year from now() at time zone 'Europe/Budapest')::int + 1);
create policy "Company admins update next year indexes" on public.company_price_indexes
  for update to authenticated using (
    company_id = (select private.current_billing_admin_company_id()) and
    effective_year = extract(year from now() at time zone 'Europe/Budapest')::int + 1)
  with check (company_id = (select private.current_billing_admin_company_id()) and
    effective_year = extract(year from now() at time zone 'Europe/Budapest')::int + 1);
revoke all on public.company_price_indexes from public, anon, authenticated;
grant select, insert, update on public.company_price_indexes to authenticated;
grant all on public.company_price_indexes to service_role;

alter table public.project_pricing
  add column index_code text check (index_code in ('ksh', 'eu', 'none')),
  add column price_origin text not null default 'manual' check (price_origin in ('manual', 'import', 'rollover')),
  add column generated_from_year smallint,
  add column applied_index_rate numeric(10, 6);
update public.project_pricing set
  index_code = case lower(btrim(indexation_basis))
    when 'ksh infláció' then 'ksh' when 'eu infláció' then 'eu'
    when 'nincs' then 'none' else null end,
  price_origin = case when source_sha256 is not null then 'import' else 'manual' end;

-- Also prevents callers with direct table access from changing historical index rates.
create function private.protect_price_index_history() returns trigger
language plpgsql set search_path = '' as $$
begin
  if current_user in ('authenticated', 'anon') and (
    new.effective_year <> extract(year from now() at time zone 'Europe/Budapest')::int + 1
    or (tg_op = 'UPDATE' and (old.company_id, old.index_code, old.effective_year)
      is distinct from (new.company_id, new.index_code, new.effective_year))
  ) then raise exception 'Only next year price indexes can be changed' using errcode = '42501'; end if;
  return new;
end;
$$;
revoke all on function private.protect_price_index_history() from public, anon, authenticated;
create trigger protect_price_index_history before insert or update on public.company_price_indexes
for each row execute function private.protect_price_index_history();

-- Private target-year argument makes catch-up deterministic and testable. Public callers
-- may only ensure the actual business year, never manufacture a future annual increase.
create function private.rollover_company_project_pricing(p_company_id uuid, p_year integer)
returns integer language plpgsql security definer set search_path = '' as $$
declare v_year integer; v_first integer; v_inserted integer; v_total integer := 0;
begin
  if p_year not between 2000 and 9999 then raise exception 'Invalid pricing year'; end if;
  if not exists (select 1 from public.companies where id=p_company_id and is_active and not is_deleted)
    then return 0; end if;
  perform pg_advisory_xact_lock(hashtextextended('project-price-rollover:' || p_company_id::text, 0));
  select min(pricing_year) into v_first from public.project_pricing where company_id=p_company_id;
  if v_first is null or v_first >= p_year then return 0; end if;
  for v_year in (v_first + 1)..p_year loop
    -- Materialize each year's inherited rates so later configuration cannot change
    -- the rate used by a project that catches up after New Year.
    insert into public.company_price_indexes(company_id,index_code,effective_year,rate)
      select distinct on (index_code) company_id,index_code,v_year,rate
      from public.company_price_indexes where company_id=p_company_id and effective_year<v_year
      order by index_code,effective_year desc on conflict do nothing;
    perform 1 from public.company_price_indexes where company_id=p_company_id
      and effective_year=v_year for share;
    perform 1 from public.project_pricing where company_id=p_company_id
      and pricing_year=v_year-1 order by project_id for share;
    insert into public.project_pricing(project_id,company_id,pricing_year,original_fee,original_fee_text,
      original_currency,original_unit,indexed_fee,unit,currency,contract_indexation,indexation_basis,
      attachment_note,po_number,index_code,price_origin,generated_from_year,applied_index_rate)
    select p.project_id,p.company_id,v_year,p.original_fee,p.original_fee_text,p.original_currency,p.original_unit,
      case when p.index_code='none' then p.indexed_fee
        when r.rate is not null then round(p.indexed_fee*(1+r.rate),4) else null end,
      p.unit,p.currency,p.contract_indexation,p.indexation_basis,p.attachment_note,p.po_number,p.index_code,
      'rollover',p.pricing_year,case when p.index_code='none' then 0 else r.rate end
    from public.project_pricing p
    left join public.company_price_indexes r on r.company_id=p.company_id and r.index_code=p.index_code
      and r.effective_year=v_year
    where p.company_id=p_company_id and p.pricing_year=v_year-1
    on conflict (project_id,pricing_year) do nothing;
    get diagnostics v_inserted = row_count;
    v_total := v_total + v_inserted;
  end loop;
  return v_total;
end;
$$;
revoke all on function private.rollover_company_project_pricing(uuid,integer) from public, anon, authenticated;

create function public.ensure_company_project_pricing(p_company_id uuid)
returns integer language plpgsql security definer set search_path = '' as $$
begin
  if p_company_id is distinct from private.current_billing_admin_company_id() then
    raise exception 'Not authorized to manage project pricing' using errcode='42501';
  end if;
  return private.rollover_company_project_pricing(p_company_id,
    extract(year from now() at time zone 'Europe/Budapest')::int);
end;
$$;
revoke all on function public.ensure_company_project_pricing(uuid) from public, anon, authenticated;
grant execute on function public.ensure_company_project_pricing(uuid) to authenticated;

create function private.rollover_project_pricing() returns integer
language plpgsql security definer set search_path = '' as $$
declare v_company uuid; v_total integer := 0;
begin
  for v_company in select id from public.companies where is_active and not is_deleted order by id loop
    v_total := v_total + private.rollover_company_project_pricing(v_company,
      extract(year from now() at time zone 'Europe/Budapest')::int);
  end loop;
  return v_total;
end;
$$;
revoke all on function private.rollover_project_pricing() from public, anon, authenticated;

comment on table public.company_price_indexes is
  'Fractional annual indexes effective in the specified year. Admin edits apply next New Year only. Historical rates are immutable to authenticated users.';
comment on column public.project_pricing.index_code is
  'ksh/eu compounds the prior indexed fee; none carries it unchanged; NULL requires a manual price next year. A missing price always remains missing.';
