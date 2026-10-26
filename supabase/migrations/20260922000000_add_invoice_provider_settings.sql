alter table public.companies
  add column if not exists invoice_provider text not null default 'szamlazz_hu',
  add column if not exists invoice_api_key_encrypted text;

update public.companies
set invoice_provider = 'szamlazz_hu'
where invoice_provider is null;

alter table public.companies
  alter column invoice_provider set default 'szamlazz_hu',
  alter column invoice_provider set not null;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.companies'::regclass
      and conname = 'companies_invoice_provider_check'
  ) then
    alter table public.companies
      add constraint companies_invoice_provider_check
      check (invoice_provider in ('szamlazz_hu', 'billingo'));
  end if;
end;
$$;

create or replace function public.set_company_invoice_credentials(
  p_company_id uuid,
  p_invoice_provider text,
  p_encrypted_key text default null
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare
  caller_profile record;
begin
  if p_invoice_provider not in ('szamlazz_hu', 'billingo') then
    raise exception 'Unsupported invoice provider'
      using errcode = '22023';
  end if;

  if p_encrypted_key is not null and btrim(p_encrypted_key) = '' then
    raise exception 'Encrypted credential must not be blank'
      using errcode = '22023';
  end if;

  select
    profile.role,
    profile.company_id,
    profile.is_active
  into caller_profile
  from public.profiles as profile
  where profile.id = (select auth.uid())
  limit 1;

  if caller_profile.is_active is distinct from true
    or coalesce(caller_profile.role, '') not in ('company_admin', 'super_admin')
    or (
      coalesce(caller_profile.role, '') <> 'super_admin'
      and caller_profile.company_id is distinct from p_company_id
    )
  then
    raise exception 'Not authorized to update these invoice credentials'
      using errcode = '42501';
  end if;

  update public.companies
  set invoice_provider = p_invoice_provider,
      invoice_api_key_encrypted = case
        when p_encrypted_key is null then invoice_api_key_encrypted
        else p_encrypted_key
      end
  where id = p_company_id
    and is_active = true
    and is_deleted = false;

  if not found then
    raise exception 'Company is missing or inactive'
      using errcode = 'P0002';
  end if;
end;
$$;

revoke all on function public.set_company_invoice_credentials(uuid, text, text)
  from public, anon;
grant execute on function public.set_company_invoice_credentials(uuid, text, text)
  to authenticated;
