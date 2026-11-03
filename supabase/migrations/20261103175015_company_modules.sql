begin;

-- Preserve existing companies; future companies start with timesheets and review.
alter table public.companies add column enabled_modules text[] not null
  default array['leave_attachments', 'invoicing']::text[];
alter table public.companies alter column enabled_modules set default '{}'::text[];
alter table public.companies add constraint companies_known_modules check (
  enabled_modules <@ array['leave_attachments', 'invoicing']::text[]
  and array_position(enabled_modules, null) is null
);
alter table public.companies add constraint companies_module_dependencies check (
  not ('invoicing' = any(enabled_modules)) or 'leave_attachments' = any(enabled_modules)
);
grant select (enabled_modules), insert (enabled_modules), update (enabled_modules)
  on public.companies to authenticated;

-- Private lookup avoids RLS recursion. It reveals no other company's entitlement.
create function private.company_module_enabled(p_company_id uuid, p_module text)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.companies c
    where c.id = p_company_id and c.is_active and not c.is_deleted
      and p_module = any(c.enabled_modules)
      and (public.is_super_admin() or p_company_id = public.get_auth_company_id())
  );
$$;
revoke all on function private.company_module_enabled(uuid,text) from public, anon;
grant execute on function private.company_module_enabled(uuid,text) to authenticated;

create function private.guard_company_modules()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if current_user in ('authenticated', 'anon') and not public.is_super_admin()
    and (tg_op = 'INSERT' or new.enabled_modules is distinct from old.enabled_modules) then
    raise sqlstate '42501' using message = 'Only a super administrator can change company modules';
  end if;
  if tg_op = 'UPDATE' and 'invoicing' = any(old.enabled_modules)
    and not ('invoicing' = any(new.enabled_modules)) and exists (
      select 1 from public.invoice_ledger where company_id = old.id and status in ('issuing','uncertain')
    ) then
    raise sqlstate 'PT409' using message = 'Resolve in-progress or uncertain invoices before disabling invoicing';
  end if;
  if tg_op = 'UPDATE' and auth.uid() is not null then
    if (new.leave_client_id, new.leave_project_id) is distinct from (old.leave_client_id, old.leave_project_id)
      and not ('leave_attachments' = any(new.enabled_modules)) then
      raise sqlstate '42501' using message = 'Leave requests and invoice attachments module is disabled';
    end if;
    if new.szamlazz_test_mode is distinct from old.szamlazz_test_mode
      and not ('invoicing' = any(new.enabled_modules)) then
      raise sqlstate '42501' using message = 'Invoicing module is disabled';
    end if;
  end if;
  return new;
end $$;
revoke all on function private.guard_company_modules() from public, anon, authenticated;
create trigger guard_company_modules before insert or update on public.companies
  for each row execute function private.guard_company_modules();

-- Serialize invoice reservations with entitlement changes. A reservation either
-- commits first (so the company guard sees an issuing invoice), or sees the
-- disabled module after waiting for the company update and fails before issuance.
create function private.guard_invoice_module()
returns trigger language plpgsql security invoker set search_path = '' as $$
declare v_enabled boolean;
begin
  if auth.uid() is not null then
    select c.is_active and not c.is_deleted and 'invoicing' = any(c.enabled_modules)
      into v_enabled from public.companies c where c.id = new.company_id for share;
    if v_enabled is distinct from true then
      raise sqlstate '42501' using message = 'Invoicing module is disabled';
    end if;
  end if;
  return new;
end $$;
revoke all on function private.guard_invoice_module() from public, anon, authenticated;
create trigger guard_invoice_module before insert or update on public.invoice_ledger
  for each row execute function private.guard_invoice_module();

-- All manual-invoice RPCs and ledger policies already use this common authority.
create or replace function private.can_manage_company_invoices(p_company_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select private.company_module_enabled(p_company_id, 'invoicing') and exists (
    select 1 from public.profiles p where p.id = auth.uid() and p.is_active
      and (p.role = 'super_admin' or (p.role = 'company_admin' and p.company_id = p_company_id))
  );
$$;

create or replace function private.current_billing_admin_company_id()
returns uuid language sql stable security definer set search_path = '' as $$
  select p.company_id from public.profiles p
  where p.id = auth.uid() and p.role = 'company_admin' and p.is_active
    and private.company_module_enabled(p.company_id, 'invoicing') limit 1;
$$;

-- Approved leave remains visible to the core timesheet date-lock checks.
create policy module_leave_reads on public.leave_requests as restrictive
  for select to authenticated using (
    status = 'APPROVED' or private.company_module_enabled(company_id, 'leave_attachments')
  );
create function private.guard_leave_module()
returns trigger language plpgsql security invoker set search_path = '' as $$
declare v_company uuid;
begin
  if tg_op = 'DELETE' then v_company := old.company_id; else v_company := new.company_id; end if;
  if auth.uid() is not null and not private.company_module_enabled(v_company, 'leave_attachments') then
    raise sqlstate '42501' using message = 'Leave requests and invoice attachments module is disabled';
  end if;
  if tg_op = 'DELETE' then return old; end if;
  return new;
end $$;
revoke all on function private.guard_leave_module() from public, anon, authenticated;
create trigger guard_leave_module before insert or update or delete on public.leave_requests
  for each row execute function private.guard_leave_module();

create policy module_attachment_reads on public.invoice_attachment_periods as restrictive
  for select to authenticated using (private.company_module_enabled(company_id, 'leave_attachments'));

-- Keep the existing accounting transaction intact behind a module-aware facade.
alter function public.finalize_invoice_attachment_period(uuid,date,date,jsonb) set schema private;
alter function private.finalize_invoice_attachment_period(uuid,date,date,jsonb)
  rename to finalize_invoice_attachment_period_impl;
revoke all on function private.finalize_invoice_attachment_period_impl(uuid,date,date,jsonb)
  from public, anon, authenticated;
create function public.finalize_invoice_attachment_period(
  p_company_id uuid, p_period_start date, p_period_end date, p_client_updates jsonb
) returns table (client_id uuid, applied boolean)
language plpgsql security definer set search_path = '' as $$
begin
  if auth.uid() is null or not private.company_module_enabled(p_company_id, 'leave_attachments') then
    raise sqlstate '42501' using message = 'Leave requests and invoice attachments module is disabled';
  end if;
  return query select * from private.finalize_invoice_attachment_period_impl(
    p_company_id, p_period_start, p_period_end, p_client_updates);
end $$;
revoke all on function public.finalize_invoice_attachment_period(uuid,date,date,jsonb) from public, anon;
grant execute on function public.finalize_invoice_attachment_period(uuid,date,date,jsonb) to authenticated;

-- Credential RPCs predate the manual invoice authority helper.
create or replace function public.get_company_invoice_credentials(p_company_id uuid)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
begin
  if not private.can_manage_company_invoices(p_company_id) then
    raise sqlstate '42501' using message = 'Invoicing module and company administrator access required';
  end if;
  return (select jsonb_build_object('invoice_provider',invoice_provider,
    'invoice_api_key_encrypted',invoice_api_key_encrypted)
    from public.companies where id=p_company_id and is_active and not is_deleted);
end $$;

create or replace function public.set_company_invoice_credentials(
  p_company_id uuid, p_invoice_provider text, p_encrypted_key text default null
) returns void language plpgsql security definer set search_path = '' as $$
begin
  if not private.can_manage_company_invoices(p_company_id) then
    raise sqlstate '42501' using message = 'Invoicing module and company administrator access required';
  end if;
  if p_invoice_provider is null or p_invoice_provider not in ('szamlazz_hu','billingo')
    or (p_encrypted_key is not null and (length(p_encrypted_key) > 4096 or trim(p_encrypted_key)='')) then
    raise sqlstate '22023' using message = 'Invalid provider credential';
  end if;
  if p_encrypted_key is null and exists (select 1 from public.companies
    where id=p_company_id and invoice_provider <> p_invoice_provider) then
    raise sqlstate '22023' using message = 'Changing provider requires a new key';
  end if;
  update public.companies set invoice_provider=p_invoice_provider,
    invoice_api_key_encrypted=coalesce(p_encrypted_key,invoice_api_key_encrypted)
    where id=p_company_id and is_active and not is_deleted;
  if not found then raise sqlstate 'PT409' using message = 'Company is missing or inactive'; end if;
end $$;

-- Scheduled pricing catches up only for companies with invoicing enabled.
create or replace function private.rollover_project_pricing() returns integer
language plpgsql security definer set search_path = '' as $$
declare v_company uuid; v_total integer := 0;
begin
  for v_company in select id from public.companies
    where is_active and not is_deleted and 'invoicing' = any(enabled_modules) order by id loop
    v_total := v_total + private.rollover_company_project_pricing(v_company,
      extract(year from now() at time zone 'Europe/Budapest')::int);
  end loop;
  return v_total;
end $$;

comment on column public.companies.enabled_modules is
  'Optional modules. Empty means core timesheets/review. Only super admins can change this list. Invoicing requires leave_attachments.';
commit;
