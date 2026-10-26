alter function public.update_company_agent_key(uuid, text, text)
  set search_path = '';

revoke all on function public.update_company_agent_key(uuid, text, text)
  from public, anon, authenticated;
