alter function public.set_company_invoice_credentials(uuid, text, text)
  security invoker;

revoke all on function public.set_company_invoice_credentials(uuid, text, text)
  from public, anon;
grant execute on function public.set_company_invoice_credentials(uuid, text, text)
  to authenticated;
