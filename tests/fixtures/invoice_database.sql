-- Minimal local reproduction of the relevant production tables and trigger.
drop schema if exists public cascade;
drop schema if exists auth cascade;
drop schema if exists private cascade;
create schema public;
grant usage on schema public to public;
do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role; end if;
end; $$;
create schema auth;
create function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
grant usage on schema auth to public;
grant execute on function auth.uid() to public;
create table public.companies (id uuid primary key, is_active boolean default true,
  is_deleted boolean default false, szamlazz_test_mode boolean default false);
create table public.profiles (id uuid primary key, company_id uuid, role text, is_active boolean default true);
create table public.clients (id uuid primary key, company_id uuid references companies,
  name text default 'Test client', client_code text default 'TEST', is_active boolean default true);
create table public.projects (id uuid primary key, client_id uuid references clients,
  name text, is_active boolean default true);
create table public.client_billing_profiles (client_id uuid primary key references clients,
  company_id uuid references companies, updated_at timestamptz default '2026-10-22T00:00:00Z',
  automatic_invoicing_enabled boolean default false);
create type public.timesheet_entry_status as enum ('draft','submitted','approved','rejected','invoiced');
create table public.timesheets (id uuid primary key, company_id uuid references companies,
  client_id uuid references clients, user_id uuid references profiles,
  project_id uuid references projects, work_date date, hours_logged numeric(5,2),
  description text, status public.timesheet_entry_status default 'approved');
grant select,update,delete,insert on public.timesheets to authenticated;
-- The exact unqualified-name behavior present in the hosted trigger.
create function public.check_timesheet_client_active() returns trigger language plpgsql as $$
begin
 if exists (select 1 from clients where id = new.client_id and not is_active) then
   raise exception 'Cannot log time against an inactive client.';
 end if;
 return new;
end;
$$;
create trigger enforce_active_client_on_timesheet before insert or update on public.timesheets
for each row execute function public.check_timesheet_client_active();
