-- Until the atomic cancellation RPC is deployed, a regular user's old
-- two-request cancellation must not delete leave after RLS refused its entries.
alter policy leave_delete on public.leave_requests using (
  (select public.is_super_admin()) or (company_id = (select public.get_auth_company_id())
    and ((select public.is_admin()) or (user_id = (select auth.uid()) and status <> 'APPROVED')))
);
