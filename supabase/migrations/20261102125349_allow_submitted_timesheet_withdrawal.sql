-- The owner must be able to reach a submitted row to withdraw it. Keep the
-- existing tenant/owner checks and restrict that newly reachable state below.
begin;

create function private.guard_submitted_timesheet_withdrawal()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if current_user in ('authenticated', 'anon') and not public.is_admin()
    and old.status = 'submitted' then
    if new.status <> 'draft' or new.submitted_at is not null
      or (to_jsonb(new) - array['status', 'submitted_at'])
        is distinct from (to_jsonb(old) - array['status', 'submitted_at']) then
      raise sqlstate '42501' using message = 'Submitted entries must be withdrawn before editing';
    end if;
  end if;
  return new;
end $$;
revoke all on function private.guard_submitted_timesheet_withdrawal() from public, anon, authenticated;
create trigger guard_submitted_timesheet_withdrawal before update on public.timesheets
  for each row execute function private.guard_submitted_timesheet_withdrawal();

alter policy timesheets_update on public.timesheets
using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((user_id = (select auth.uid()) and status in ('draft', 'rejected', 'submitted'))
      or (select public.is_admin())))
)
with check (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((user_id = (select auth.uid()) and status in ('draft', 'submitted')) or (select public.is_admin())))
);

commit;
