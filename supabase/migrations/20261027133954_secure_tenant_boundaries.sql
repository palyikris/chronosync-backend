-- Authorization comes from stored membership, never user-editable Auth metadata.
create schema if not exists private;
grant usage on schema private to authenticated;

create or replace function private.actor_role()
returns text language sql stable security definer set search_path = '' as $$
  select p.role from public.profiles p
  left join public.companies c on c.id = p.company_id
  where p.id = auth.uid() and p.is_active
    and (p.role = 'super_admin' or (c.is_active and not c.is_deleted));
$$;
revoke all on function private.actor_role() from public, anon;
grant execute on function private.actor_role() to authenticated;

create or replace function public.is_super_admin()
returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce(private.actor_role() = 'super_admin', false);
$$;
create or replace function public.is_admin()
returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce(private.actor_role() in ('company_admin', 'super_admin'), false);
$$;
create or replace function public.is_caller_active()
returns boolean language sql stable security definer set search_path = '' as $$
  select private.actor_role() is not null;
$$;
create or replace function public.get_auth_company_id()
returns uuid language sql stable security definer set search_path = '' as $$
  select company_id from public.profiles where id = auth.uid()
    and private.actor_role() is not null;
$$;
-- This lookup deliberately includes inactive membership, so the login screen can
-- explain disabled access without granting access to any tenant data.
create or replace function private.member_company_id()
returns uuid language sql stable security definer set search_path = '' as $$
  select company_id from public.profiles where id = auth.uid();
$$;
revoke all on function private.member_company_id() from public, anon;
grant execute on function private.member_company_id() to authenticated;
revoke all on function public.is_super_admin(), public.is_admin(),
  public.is_caller_active(), public.get_auth_company_id() from public, anon;
grant execute on function public.is_super_admin(), public.is_admin(),
  public.is_caller_active(), public.get_auth_company_id() to authenticated;

-- Remove every legacy permissive policy on these tables. Policies combine with OR.
do $$ declare p record; begin
  for p in select tablename, policyname from pg_policies where schemaname = 'public'
    and tablename in ('profiles', 'companies', 'timesheets', 'leave_requests') loop
    execute format('drop policy %I on public.%I', p.policyname, p.tablename);
  end loop;
end $$;
alter table public.profiles enable row level security;
alter table public.companies enable row level security;
alter table public.timesheets enable row level security;
alter table public.leave_requests enable row level security;
revoke all on public.profiles, public.companies, public.timesheets,
  public.leave_requests, public.clients, public.projects from anon;
revoke insert on public.profiles from authenticated;
grant select, update, delete on public.profiles to authenticated;
grant select, insert, update, delete on public.leave_requests to authenticated;

create policy profiles_read on public.profiles for select to authenticated using (
  id = (select auth.uid()) or (select public.is_super_admin())
  or company_id = (select public.get_auth_company_id())
);
create policy profiles_update on public.profiles for update to authenticated using (
  (select public.is_super_admin()) or (
    company_id = (select public.get_auth_company_id())
    and (id = (select auth.uid()) or ((select public.is_admin()) and role <> 'super_admin'))
  )
) with check (
  (select public.is_super_admin()) or (
    company_id = (select public.get_auth_company_id())
    and (id = (select auth.uid()) or ((select public.is_admin()) and role <> 'super_admin'))
  )
);
create policy profiles_delete on public.profiles for delete to authenticated using (
  (select public.is_super_admin()) or ((select public.is_admin())
    and company_id = (select public.get_auth_company_id()) and role <> 'super_admin')
);

create or replace function private.guard_profile_membership()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if current_user not in ('authenticated', 'anon') then return new; end if;
  if new.id is distinct from old.id or new.created_at is distinct from old.created_at then
    raise sqlstate '42501' using message = 'Profile identity cannot be changed';
  end if;
  if public.is_super_admin() then return new; end if;
  if new.company_id is distinct from old.company_id
    or old.role = 'super_admin' or new.role = 'super_admin'
    or (not public.is_admin() and (
      new.role is distinct from old.role or new.is_active is distinct from old.is_active
    )) then
    raise sqlstate '42501' using message = 'Only an authorized administrator can change membership';
  end if;
  return new;
end $$;
revoke all on function private.guard_profile_membership() from public, anon, authenticated;
create trigger guard_profile_membership before update on public.profiles
  for each row execute function private.guard_profile_membership();

drop trigger if exists ensure_active_admin_exists on public.profiles;
drop trigger if exists ensure_admin_exists on public.profiles;
create or replace function private.guard_last_active_admin()
returns trigger language plpgsql security definer set search_path = '' as $$
declare removing boolean; begin
  removing := tg_op = 'DELETE';
  if tg_op = 'UPDATE' then
    removing := not new.is_active or new.role is distinct from old.role
      or new.company_id is distinct from old.company_id;
  end if;
  if removing and old.is_active and old.role in ('company_admin', 'super_admin') then
    perform pg_advisory_xact_lock(hashtextextended(
      case when old.role = 'super_admin' then 'chronosync-super-admin'
        else 'chronosync-company-admin:' || old.company_id::text end, 0));
    if not exists (select 1 from public.profiles p where p.id <> old.id and p.is_active
      and (case when old.role = 'super_admin' then p.role = 'super_admin'
        else p.company_id = old.company_id and p.role in ('company_admin', 'super_admin') end)) then
      raise sqlstate 'PT409' using message = 'Cannot remove the last active administrator';
    end if;
  end if;
  if tg_op = 'DELETE' then return old; end if;
  return new;
end $$;
revoke all on function private.guard_last_active_admin() from public, anon, authenticated;
create trigger guard_last_active_admin before update or delete on public.profiles
  for each row execute function private.guard_last_active_admin();

create policy companies_read on public.companies for select to authenticated using (
  (select public.is_super_admin()) or (id = (select private.member_company_id()) and not is_deleted)
);
create policy companies_create on public.companies for insert to authenticated
  with check ((select public.is_super_admin()));
create policy companies_update on public.companies for update to authenticated using (
  (select public.is_super_admin()) or ((select public.is_admin()) and id = (select public.get_auth_company_id()))
) with check (
  (select public.is_super_admin()) or ((select public.is_admin()) and id = (select public.get_auth_company_id()))
);
create policy companies_delete on public.companies for delete to authenticated
  using ((select public.is_super_admin()));

create or replace function private.guard_company_administration()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if current_user in ('authenticated', 'anon') and not public.is_super_admin() and (
    new.id is distinct from old.id or new.is_active is distinct from old.is_active
    or new.is_deleted is distinct from old.is_deleted or new.deleted_at is distinct from old.deleted_at
    or new.created_at is distinct from old.created_at
  ) then
    raise sqlstate '42501' using message = 'Only a super administrator can change company access';
  end if;
  if new.leave_client_id is not null and not exists (
    select 1 from public.clients where id = new.leave_client_id and company_id = new.id
  ) then raise sqlstate '23514' using message = 'Leave client must belong to the company'; end if;
  if new.leave_project_id is not null and not exists (
    select 1 from public.projects where id = new.leave_project_id and company_id = new.id
      and client_id = new.leave_client_id
  ) then raise sqlstate '23514' using message = 'Leave project must belong to the leave client'; end if;
  return new;
end $$;
revoke all on function private.guard_company_administration() from public, anon, authenticated;
create trigger guard_company_administration before insert or update on public.companies
  for each row execute function private.guard_company_administration();

create policy timesheets_read on public.timesheets for select to authenticated using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and (user_id = (select auth.uid()) or (select public.is_admin())))
);
create policy timesheets_insert on public.timesheets for insert to authenticated with check (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((user_id = (select auth.uid()) and status in ('draft', 'submitted')) or (select public.is_admin())))
);
create policy timesheets_update on public.timesheets for update to authenticated using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((user_id = (select auth.uid()) and status in ('draft', 'rejected')) or (select public.is_admin())))
) with check (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((user_id = (select auth.uid()) and status in ('draft', 'submitted')) or (select public.is_admin())))
);
create policy timesheets_delete on public.timesheets for delete to authenticated using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((user_id = (select auth.uid()) and status in ('draft', 'rejected')) or (select public.is_admin())))
);
create or replace function private.guard_timesheet_membership()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if not exists (select 1 from public.profiles where id = new.user_id and company_id = new.company_id)
    or (new.client_id is not null and not exists (
      select 1 from public.clients where id = new.client_id and company_id = new.company_id))
    or (new.project_id is not null and not exists (
      select 1 from public.projects where id = new.project_id and company_id = new.company_id and client_id = new.client_id)) then
    raise sqlstate '23514' using message = 'Timesheet references must belong to one company';
  end if;
  if current_user in ('authenticated', 'anon') then
    if tg_op = 'UPDATE' and (new.id is distinct from old.id or new.user_id is distinct from old.user_id
      or new.company_id is distinct from old.company_id or new.created_at is distinct from old.created_at) then
      raise sqlstate '42501' using message = 'Timesheet identity cannot be changed';
    end if;
    if new.status = 'invoiced' then
      raise sqlstate '42501' using message = 'Only invoice finalization can mark entries invoiced';
    end if;
    if not public.is_admin() and (new.approved_by is not null or new.approved_at is not null) then
      raise sqlstate '42501' using message = 'Only administrators can approve entries';
    end if;
  end if;
  return new;
end $$;
revoke all on function private.guard_timesheet_membership() from public, anon, authenticated;
create trigger guard_timesheet_membership before insert or update on public.timesheets
  for each row execute function private.guard_timesheet_membership();

create policy leave_read on public.leave_requests for select to authenticated using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and (user_id = (select auth.uid()) or status = 'APPROVED' or (select public.is_admin())))
);
create policy leave_insert on public.leave_requests for insert to authenticated with check (
  company_id = (select public.get_auth_company_id()) and user_id = (select auth.uid())
  and status = 'PENDING' and hours_taken is null
);
create policy leave_update on public.leave_requests for update to authenticated using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((select public.is_admin()) or (user_id = (select auth.uid()) and status = 'PENDING')))
) with check (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((select public.is_admin()) or (user_id = (select auth.uid()) and status = 'PENDING' and hours_taken is null)))
);
create policy leave_delete on public.leave_requests for delete to authenticated using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((select public.is_admin()) or user_id = (select auth.uid())))
);
create or replace function private.guard_leave_membership()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('chronosync-leave:' || new.user_id::text, 0));
  if not exists (select 1 from public.profiles where id = new.user_id and company_id = new.company_id) then
    raise sqlstate '23514' using message = 'Leave owner must belong to the company';
  end if;
  if tg_op = 'UPDATE' and (new.id is distinct from old.id or new.user_id is distinct from old.user_id
    or new.company_id is distinct from old.company_id or new.created_at is distinct from old.created_at) then
    raise sqlstate '42501' using message = 'Leave identity cannot be changed';
  end if;
  if new.start_date > new.end_date or new.weekly_work_hours <= 0 or new.weekly_work_hours > 80 then
    raise sqlstate '23514' using message = 'Invalid leave dates or working hours';
  end if;
  if new.status in ('PENDING', 'APPROVED') and exists (
    select 1 from public.leave_requests r where r.id <> new.id and r.user_id = new.user_id
      and r.status in ('PENDING', 'APPROVED') and r.start_date <= new.end_date and r.end_date >= new.start_date
  ) then raise sqlstate 'PT409' using message = 'Leave dates overlap an existing request'; end if;
  return new;
end $$;
revoke all on function private.guard_leave_membership() from public, anon, authenticated;
create trigger guard_leave_membership before insert or update on public.leave_requests
  for each row execute function private.guard_leave_membership();

-- Uninvited signups are inert. Even forged company/role metadata grants no access.
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles(id, company_id, full_name, role, is_active)
    values (new.id, null, left(coalesce(nullif(trim(new.raw_user_meta_data->>'full_name'), ''), 'New Member'), 120), 'regular', false)
    on conflict (id) do nothing;
  return new;
end $$;
revoke all on function public.handle_new_user() from public, anon, authenticated;

create or replace function private.guard_project_company()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if not exists (select 1 from public.clients where id = new.client_id and company_id = new.company_id) then
    raise sqlstate '23514' using message = 'Project client must belong to the same company';
  end if;
  return new;
end $$;
revoke all on function private.guard_project_company() from public, anon, authenticated;
create trigger guard_project_company before insert or update on public.projects
  for each row execute function private.guard_project_company();
