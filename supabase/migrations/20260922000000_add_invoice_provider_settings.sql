alter table public.companies
  add column if not exists invoice_provider text not null default 'szamlazz_hu',
  add column if not exists invoice_api_key_encrypted text;

alter table public.companies
  add constraint companies_invoice_provider_check
  check (invoice_provider in ('szamlazz_hu', 'billingo'));

update public.companies
set invoice_api_key_encrypted = szamlazz_agent_key_encrypted
where invoice_api_key_encrypted is null
  and szamlazz_agent_key_encrypted is not null;

create or replace function public.set_company_invoice_credentials(
  p_company_id uuid,
  p_invoice_provider text,
  p_encrypted_key text default null
)
returns void
language plpgsql
as $$
begin
  if p_invoice_provider not in ('szamlazz_hu', 'billingo') then
    raise exception 'Unsupported invoice provider';
  end if;

  update public.companies
  set invoice_provider = p_invoice_provider,
      invoice_api_key_encrypted = case
        when p_encrypted_key is null then invoice_api_key_encrypted
        else p_encrypted_key
      end
  where id = p_company_id;
end;
$$;
