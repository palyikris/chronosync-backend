-- Approved leave and its generated time entries must change in one transaction.
alter table public.timesheets add column leave_request_id uuid references public.leave_requests(id);
create index timesheets_leave_request_id on public.timesheets(leave_request_id) where leave_request_id is not null;
revoke delete on public.leave_requests from authenticated;

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
  if current_user in ('authenticated', 'anon') and tg_op = 'UPDATE' and (
    new.status is distinct from old.status or new.hours_taken is distinct from old.hours_taken
  ) then raise sqlstate '42501' using message = 'Use the leave review operation'; end if;
  if new.status in ('PENDING', 'APPROVED') and exists (
    select 1 from public.leave_requests r where r.id <> new.id and r.user_id = new.user_id
      and r.status in ('PENDING', 'APPROVED') and r.start_date <= new.end_date and r.end_date >= new.start_date
  ) then raise sqlstate 'PT409' using message = 'Leave dates overlap an existing request'; end if;
  return new;
end $$;
revoke all on function private.guard_leave_membership() from public, anon, authenticated;
drop trigger if exists guard_leave_membership on public.leave_requests;
create trigger guard_leave_membership before insert or update on public.leave_requests
  for each row execute function private.guard_leave_membership();

create or replace function private.guard_approved_leave_timesheet()
returns trigger language plpgsql security invoker set search_path = '' as $$
declare v_user uuid; v_changed boolean := true;
begin
  if current_user not in ('authenticated', 'anon') then
    if tg_op = 'DELETE' then return old; end if;
    return new;
  end if;
  if tg_op = 'DELETE' then v_user := old.user_id; else v_user := new.user_id; end if;
  perform pg_advisory_xact_lock(hashtextextended('chronosync-leave:' || v_user::text, 0));
  if tg_op = 'DELETE' then
    if old.leave_request_id is not null then
      raise sqlstate 'PT409' using message = 'Cancel the leave request to remove its time entries';
    end if;
    return old;
  end if;
  if tg_op = 'UPDATE' then
    v_changed := (new.work_date, new.hours_logged, new.description, new.client_id, new.project_id)
      is distinct from (old.work_date, old.hours_logged, old.description, old.client_id, old.project_id);
    if new.leave_request_id is distinct from old.leave_request_id or (old.leave_request_id is not null and v_changed) then
      raise sqlstate '42501' using message = 'Leave entries can only change through the leave operation';
    end if;
  elsif new.leave_request_id is not null then
    raise sqlstate '42501' using message = 'Only leave approval can create linked entries';
  end if;
  if v_changed and exists (select 1 from public.leave_requests where user_id = new.user_id
    and status = 'APPROVED' and new.work_date between start_date and end_date) then
    raise sqlstate 'PT409' using message = 'Date is locked by approved leave';
  end if;
  return new;
end $$;
revoke all on function private.guard_approved_leave_timesheet() from public, anon, authenticated;
create trigger guard_approved_leave_timesheet before insert or update or delete on public.timesheets
  for each row execute function private.guard_approved_leave_timesheet();

create or replace function public.review_leave_request(p_id uuid, p_status text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare r public.leave_requests; c public.companies; v_day date; v_daily numeric; v_hours numeric := 0;
  v_conflicts jsonb;
begin
  select * into r from public.leave_requests where id = p_id for update;
  if r.id is null or not public.is_admin() or (not public.is_super_admin()
    and r.company_id is distinct from public.get_auth_company_id()) then
    raise sqlstate '42501' using message = 'Company administrator access required';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('chronosync-leave:' || r.user_id::text, 0));
  if r.status <> 'PENDING' then raise sqlstate 'PT409' using message = 'Leave request was already reviewed'; end if;
  if p_status is null or p_status not in ('APPROVED', 'REJECTED') then
    raise sqlstate '22023' using message = 'Invalid leave decision';
  end if;
  if p_status = 'APPROVED' then
    select jsonb_agg(distinct work_date order by work_date) into v_conflicts from public.timesheets
      where user_id = r.user_id and work_date between r.start_date and r.end_date
        and extract(isodow from work_date) <= 5 and hours_logged > 0;
    if v_conflicts is not null then
      raise sqlstate 'PT409' using message = 'LOGGED_HOURS_EXIST', detail = v_conflicts::text;
    end if;
    select * into c from public.companies where id = r.company_id;
    if c.leave_client_id is null or c.leave_project_id is null then
      raise sqlstate '22023' using message = 'Configure the leave client and project first';
    end if;
    select round(coalesce(r.weekly_work_hours, p.weekly_work_hours, 40) / 5, 2) into v_daily
      from public.profiles p where p.id = r.user_id;
    if v_daily is null or v_daily <= 0 or v_daily > 16 then
      raise sqlstate '22023' using message = 'Invalid weekly working hours';
    end if;
    for v_day in select d::date from generate_series(r.start_date::timestamp, r.end_date::timestamp, interval '1 day') d
      where extract(isodow from d) <= 5 loop
      insert into public.timesheets(user_id,company_id,client_id,project_id,work_date,hours_logged,
        description,status,approved_by,approved_at,leave_request_id)
        values(r.user_id,r.company_id,c.leave_client_id,c.leave_project_id,v_day,v_daily,
          'Approved leave','approved',auth.uid(),now(),r.id);
      v_hours := v_hours + v_daily;
    end loop;
  end if;
  update public.leave_requests set status = p_status,
    hours_taken = case when p_status = 'APPROVED' then v_hours else null end
    where id = r.id returning * into r;
  return to_jsonb(r);
end $$;
revoke all on function public.review_leave_request(uuid, text) from public, anon;
grant execute on function public.review_leave_request(uuid, text) to authenticated;

create or replace function public.cancel_leave_request(p_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
declare r public.leave_requests; c public.companies;
begin
  select * into r from public.leave_requests where id = p_id for update;
  if r.id is null or not public.is_caller_active() or not (
    public.is_super_admin() or (r.company_id = public.get_auth_company_id()
      and (r.user_id = auth.uid() or public.is_admin()))
  ) then raise sqlstate '42501' using message = 'Leave access required'; end if;
  perform pg_advisory_xact_lock(hashtextextended('chronosync-leave:' || r.user_id::text, 0));
  select * into c from public.companies where id = r.company_id;
  -- The second branch supports approved entries created before linked leave IDs.
  -- Existing invoice guards still run and roll back the whole cancellation.
  if r.status = 'APPROVED' then
    delete from public.timesheets where leave_request_id = r.id or (
      leave_request_id is null and user_id = r.user_id and company_id = r.company_id
      and project_id = c.leave_project_id and description = 'Approved leave'
      and work_date between r.start_date and r.end_date);
  end if;
  delete from public.leave_requests where id = r.id;
end $$;
revoke all on function public.cancel_leave_request(uuid) from public, anon;
grant execute on function public.cancel_leave_request(uuid) to authenticated;

create table private.company_invitations (
  id uuid primary key default gen_random_uuid(),
  email text not null check (email = lower(trim(email))),
  company_id uuid not null references public.companies(id),
  full_name text not null check (length(full_name) between 1 and 120),
  role text not null check (role in ('regular', 'company_admin')),
  invited_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '7 days',
  accepted_at timestamptz,
  accepted_by uuid references auth.users(id) on delete set null,
  revoked_at timestamptz
);
create unique index company_invitations_pending_email on private.company_invitations(email)
  where accepted_at is null and revoked_at is null;
create index company_invitations_company_id on private.company_invitations(company_id);
create index company_invitations_invited_by on private.company_invitations(invited_by);
create index company_invitations_accepted_by on private.company_invitations(accepted_by);
alter table private.company_invitations enable row level security;
revoke all on private.company_invitations from public, anon, authenticated;

create or replace function public.prepare_company_invitation(
  p_email text, p_company_id uuid, p_full_name text, p_role text
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_email text := lower(trim(p_email)); v_user uuid; v_profile public.profiles;
  v_invite private.company_invitations; v_id uuid;
begin
  if auth.uid() is null or not public.is_admin() or (
    not public.is_super_admin() and p_company_id is distinct from public.get_auth_company_id()
  ) then raise sqlstate '42501' using message = 'Company administrator access required'; end if;
  if not exists (select 1 from public.companies where id = p_company_id and is_active and not is_deleted)
    or p_role not in ('regular', 'company_admin') or p_role is null
    or length(trim(p_full_name)) not between 1 and 120 or p_full_name is null
    or v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' or length(v_email) > 254 or v_email is null then
    raise sqlstate '22023' using message = 'Invalid invitation';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('chronosync-invite:' || v_email, 0));
  select id into v_user from auth.users where lower(email) = v_email;
  select * into v_profile from public.profiles where id = v_user;
  if v_profile.company_id is not null and v_profile.company_id <> p_company_id or v_profile.role = 'super_admin' then
    raise sqlstate 'PT409' using message = 'This identity already belongs to another membership';
  end if;
  if v_profile.company_id = p_company_id and not v_profile.is_active then
    raise sqlstate 'PT409' using message = 'Reactivate this member before inviting them';
  end if;
  select * into v_invite from private.company_invitations
    where email = v_email and accepted_at is null and revoked_at is null for update;
  if found then
    if v_invite.expires_at > now() and v_invite.company_id <> p_company_id then
      raise sqlstate 'PT409' using message = 'An invitation is already pending for this identity';
    end if;
    if v_invite.created_at > now() - interval '60 seconds' then
      raise sqlstate 'PT429' using message = 'Wait before resending an invitation';
    end if;
    update private.company_invitations set revoked_at = now() where id = v_invite.id;
  end if;
  insert into private.company_invitations(email, company_id, full_name, role, invited_by)
    values (v_email, p_company_id, trim(p_full_name), p_role, auth.uid()) returning id into v_id;
  return jsonb_build_object('id', v_id, 'email', v_email, 'existing_user', v_user is not null);
end $$;
revoke all on function public.prepare_company_invitation(text, uuid, text, text) from public, anon;
grant execute on function public.prepare_company_invitation(text, uuid, text, text) to authenticated;

create or replace function public.claim_company_invitation()
returns boolean language plpgsql security definer set search_path = '' as $$
declare v_user auth.users; v_invite private.company_invitations; v_profile public.profiles;
begin
  if auth.uid() is null then raise sqlstate '42501' using message = 'Authentication required'; end if;
  select * into v_user from auth.users where id = auth.uid();
  if v_user.email_confirmed_at is null then return false; end if;
  perform pg_advisory_xact_lock(hashtextextended('chronosync-invite:' || lower(v_user.email), 0));
  select * into v_invite from private.company_invitations
    where email = lower(v_user.email) and accepted_at is null and revoked_at is null and expires_at > now()
    order by created_at desc limit 1 for update;
  if not found then return false; end if;
  if not exists (select 1 from public.companies where id = v_invite.company_id and is_active and not is_deleted)
    or not exists (select 1 from public.profiles p where p.id = v_invite.invited_by and p.is_active
      and (p.role = 'super_admin' or (p.role = 'company_admin' and p.company_id = v_invite.company_id))) then
    raise sqlstate '42501' using message = 'Invitation is no longer authorized';
  end if;
  select * into v_profile from public.profiles where id = auth.uid() for update;
  if v_profile.role = 'super_admin' or (v_profile.company_id is not null and v_profile.company_id <> v_invite.company_id)
    or (v_profile.company_id is not null and not v_profile.is_active) then
    raise sqlstate 'PT409' using message = 'Existing membership cannot be replaced by an invitation';
  end if;
  insert into public.profiles(id, company_id, full_name, role, is_active)
    values (auth.uid(), v_invite.company_id, v_invite.full_name, v_invite.role, true)
    on conflict (id) do update set company_id = excluded.company_id, full_name = excluded.full_name,
      role = case when profiles.role = 'company_admin' then profiles.role else excluded.role end, is_active = true;
  update private.company_invitations set accepted_at = now(), accepted_by = auth.uid() where id = v_invite.id;
  return true;
end $$;
revoke all on function public.claim_company_invitation() from public, anon;
grant execute on function public.claim_company_invitation() to authenticated;

-- Auth emails are exposed only to authorized team administrators, never through
-- the general profile read policy or a caller-selected auth-user lookup.
create or replace function public.get_company_members()
returns jsonb language plpgsql stable security definer set search_path = '' as $$
begin
  if not public.is_admin() then raise sqlstate '42501' using message = 'Administrator access required'; end if;
  return (select coalesce(jsonb_agg(to_jsonb(p) || jsonb_build_object(
    'email', u.email, 'companies', jsonb_build_object('name', c.name)
  ) order by p.full_name), '[]'::jsonb)
  from public.profiles p join auth.users u on u.id = p.id
  left join public.companies c on c.id = p.company_id
  where public.is_super_admin() or (p.company_id = public.get_auth_company_id() and p.role <> 'super_admin'));
end $$;
revoke all on function public.get_company_members() from public, anon;
grant execute on function public.get_company_members() to authenticated;

-- Column grants keep all legacy plaintext and encrypted credentials out of
-- ordinary company reads. Admin-only RPCs preserve the backend's key lifecycle.
revoke all on public.companies from authenticated;
grant delete on public.companies to authenticated;
grant select (id,name,billing_email,is_active,is_deleted,deleted_at,created_at,
  leave_client_id,leave_project_id,szamlazz_test_mode,invoice_provider),
  insert (id,name,billing_email,is_active,is_deleted,deleted_at,created_at,
  leave_client_id,leave_project_id,szamlazz_test_mode,invoice_provider),
  update (name,billing_email,is_active,is_deleted,deleted_at,
  leave_client_id,leave_project_id,szamlazz_test_mode) on public.companies to authenticated;

create or replace function public.get_company_invoice_credentials(p_company_id uuid)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
begin
  if not public.is_admin() or (not public.is_super_admin()
    and p_company_id is distinct from public.get_auth_company_id()) then
    raise sqlstate '42501' using message = 'Company administrator access required';
  end if;
  return (select jsonb_build_object('invoice_provider',invoice_provider,
    'invoice_api_key_encrypted',invoice_api_key_encrypted)
    from public.companies where id=p_company_id and is_active and not is_deleted);
end $$;
revoke all on function public.get_company_invoice_credentials(uuid) from public, anon;
grant execute on function public.get_company_invoice_credentials(uuid) to authenticated;

create or replace function public.set_company_invoice_credentials(
  p_company_id uuid, p_invoice_provider text, p_encrypted_key text default null
) returns void language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_admin() or (not public.is_super_admin()
    and p_company_id is distinct from public.get_auth_company_id()) then
    raise sqlstate '42501' using message = 'Company administrator access required';
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
revoke all on function public.set_company_invoice_credentials(uuid,text,text) from public, anon;
grant execute on function public.set_company_invoice_credentials(uuid,text,text) to authenticated;
