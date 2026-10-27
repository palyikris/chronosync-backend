-- Disposable local reproduction of the hosted membership schema and permissive rules.
drop schema if exists public cascade;
drop schema if exists auth cascade;
drop schema if exists private cascade;
create schema public;
create schema auth;
grant usage on schema public, auth to public;
do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role; end if;
end $$;
create function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
create table auth.users(id uuid primary key, email text unique, email_confirmed_at timestamptz,
  raw_user_meta_data jsonb default '{}');
create table public.companies(id uuid primary key, name text not null default 'Company',
  billing_email text, szamlazz_test_mode boolean default false, invoice_provider text default 'szamlazz_hu',
  invoice_api_key_encrypted text, szamlazz_agent_key_encrypted text, szamlazz_token text, szamla_agent_key text,
  is_active boolean not null default true, is_deleted boolean not null default false,
  deleted_at timestamptz, created_at timestamptz not null default now(), leave_client_id uuid, leave_project_id uuid);
create table public.profiles(id uuid primary key references auth.users on delete cascade,
  company_id uuid references companies, full_name text not null default 'Member',
  role text not null default 'regular' check (role in ('regular','company_admin','super_admin')),
  is_active boolean not null default true, created_at timestamptz not null default now(),
  weekly_work_hours numeric, show_timesheet_timer boolean not null default false);
create table public.clients(id uuid primary key, company_id uuid references companies,
  name text default 'Test client', client_code text default 'TEST', is_active boolean default true);
create table public.projects(id uuid primary key, company_id uuid references companies, client_id uuid references clients,
  name text default 'General Task', is_active boolean default true);
create table public.client_billing_profiles(client_id uuid primary key references clients, company_id uuid references companies,
  updated_at timestamptz default '2026-10-22T00:00:00Z', automatic_invoicing_enabled boolean default false);
create type public.timesheet_entry_status as enum ('draft','submitted','approved','rejected','invoiced');
create table public.timesheets(id uuid primary key default gen_random_uuid(), user_id uuid references profiles,
  company_id uuid references companies, client_id uuid references clients, project_id uuid references projects,
  work_date date not null, hours_logged numeric not null, description text,
  status timesheet_entry_status not null default 'draft', approved_by uuid references auth.users,
  approved_at timestamptz, created_at timestamptz not null default now());
create table public.leave_requests(id uuid primary key default gen_random_uuid(), user_id uuid references profiles,
  company_id uuid references companies, start_date date not null, end_date date not null,
  status text not null default 'PENDING' check (status in ('PENDING','APPROVED','REJECTED')),
  weekly_work_hours numeric, hours_taken numeric, created_at timestamptz not null default now());
grant all on all tables in schema public to authenticated, anon;
alter table profiles enable row level security;
create policy all_temp on profiles for all to authenticated using (true) with check (true);
alter table companies enable row level security;
create policy legacy_admin on companies for all to authenticated using (exists (
  select 1 from profiles where id=auth.uid() and role in ('company_admin','super_admin')));
create function public.get_auth_company_id() returns uuid language sql stable security definer set search_path='' as $$
  select company_id from public.profiles where id=auth.uid();
$$;
create function public.is_admin() returns boolean language sql stable security definer set search_path='' as $$
  select exists(select 1 from public.profiles where id=auth.uid() and role in ('company_admin','super_admin'));
$$;
alter table clients enable row level security;
create policy clients_read on clients for select to authenticated using(company_id=public.get_auth_company_id());
create policy clients_manage on clients for all to authenticated using(public.is_admin() and company_id=public.get_auth_company_id())
  with check(public.is_admin() and company_id=public.get_auth_company_id());
alter table projects enable row level security;
create policy projects_read on projects for select to authenticated using(company_id=public.get_auth_company_id());
create policy projects_manage on projects for all to authenticated using(public.is_admin() and company_id=public.get_auth_company_id())
  with check(public.is_admin() and company_id=public.get_auth_company_id());
create function public.handle_new_user() returns trigger language plpgsql security definer set search_path='' as $$
begin
  insert into public.profiles(id,company_id,full_name,role,is_active) values (
    new.id,(new.raw_user_meta_data->>'company_id')::uuid,'Member',coalesce(new.raw_user_meta_data->>'role','regular'),true);
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();
