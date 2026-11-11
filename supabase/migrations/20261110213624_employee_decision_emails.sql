-- Extend the durable queue with employee-facing decisions. No historical
-- decisions are backfilled, and no network call occurs in a review transaction.
alter table private.transactional_emails drop constraint transactional_emails_kind_check;
alter table private.transactional_emails add constraint transactional_emails_kind_check
  check (kind in ('leave_requested','leave_decision','timesheet_rejected','invoice_issued','invoice_reversed'));

create or replace function private.enqueue_leave_decision_email()
returns trigger language plpgsql security definer set search_path='' as $$
begin
  if old.status <> 'PENDING' or new.status not in ('APPROVED','REJECTED') then return new; end if;
  insert into private.transactional_emails(
    event_key,company_id,kind,source_id,recipient_user_id,recipient,event_data)
  select 'leave_decision/'||new.id||'/'||gen_random_uuid(),new.company_id,'leave_decision',
    new.id,employee.id,lower(u.email),jsonb_build_object(
      'company_name',c.name,'employee_name',employee.full_name,'decision',new.status,
      'start_date',new.start_date,'end_date',new.end_date)
  from public.profiles employee join auth.users u on u.id=employee.id
    join public.companies c on c.id=new.company_id
  where employee.id=new.user_id and employee.company_id=new.company_id and employee.is_active
    and c.is_active and not c.is_deleted and u.email is not null and u.email_confirmed_at is not null;
  return new;
end; $$;
revoke all on function private.enqueue_leave_decision_email() from public,anon,authenticated,service_role;
create trigger enqueue_leave_decision_email after update on public.leave_requests
  for each row when (old.status is distinct from new.status)
  execute function private.enqueue_leave_decision_email();

create or replace function private.enqueue_timesheet_rejection_email()
returns trigger language plpgsql security definer set search_path='' as $$
begin
  if old.status <> 'submitted' or new.status <> 'rejected' then return new; end if;
  -- A new rejection after a resubmission is a new event, even when the reviewer
  -- reuses the same reason/timestamp. Supersede any older unsent rejection.
  update private.transactional_emails set status='cancelled',claim_token=null,leased_until=null
    where kind='timesheet_rejected' and source_id=new.id and company_id=new.company_id
      and (status='pending' or (status='sending' and leased_until<now()));
  insert into private.transactional_emails(
    event_key,company_id,kind,source_id,recipient_user_id,recipient,event_data)
  select 'timesheet_rejected/'||new.id||'/'||gen_random_uuid(),new.company_id,'timesheet_rejected',
    new.id,employee.id,lower(u.email),jsonb_build_object(
      'company_name',c.name,'employee_name',employee.full_name,'work_date',new.work_date,
      'hours_logged',new.hours_logged,'description',coalesce(new.description,''),
      'rejection_reason',coalesce(new.rejection_reason,''),'reviewed_at',new.approved_at,
      'client_id',new.client_id,'project_id',new.project_id,
      'client_name',client.name,'project_name',project.name)
  from public.profiles employee join auth.users u on u.id=employee.id
    join public.companies c on c.id=new.company_id
    left join public.clients client on client.id=new.client_id and client.company_id=new.company_id
    left join public.projects project on project.id=new.project_id and project.company_id=new.company_id
  where employee.id=new.user_id and employee.company_id=new.company_id and employee.is_active
    and c.is_active and not c.is_deleted and u.email is not null and u.email_confirmed_at is not null;
  return new;
end; $$;
revoke all on function private.enqueue_timesheet_rejection_email() from public,anon,authenticated,service_role;
create trigger enqueue_timesheet_rejection_email after update on public.timesheets
  for each row when (old.status is distinct from new.status)
  execute function private.enqueue_timesheet_rejection_email();

-- This internal predicate runs as the existing service-only claim function's
-- owner. It grants no new browser access to profiles, Auth emails or snapshots.
create or replace function private.transactional_email_is_current(e private.transactional_emails)
returns boolean language sql stable security invoker set search_path='' as $$
  select exists(select 1 from public.companies c
      where c.id=e.company_id and c.is_active and not c.is_deleted)
    and case e.kind
      when 'leave_requested' then exists(
        select 1 from public.leave_requests r join public.profiles p on p.id=e.recipient_user_id
          join auth.users u on u.id=p.id
        where r.id=e.source_id and r.company_id=e.company_id and r.status='PENDING'
          and r.start_date::text=e.event_data->>'start_date' and r.end_date::text=e.event_data->>'end_date'
          and r.weekly_work_hours::text=e.event_data->>'weekly_work_hours'
          and p.is_active and p.company_id=e.company_id and p.role in ('company_admin','super_admin')
          and lower(u.email)=e.recipient and u.email_confirmed_at is not null)
      when 'leave_decision' then exists(
        select 1 from public.leave_requests r join public.profiles p on p.id=r.user_id
          join auth.users u on u.id=p.id
        where r.id=e.source_id and r.company_id=e.company_id and r.user_id=e.recipient_user_id
          and r.status=e.event_data->>'decision'
          and r.start_date::text=e.event_data->>'start_date' and r.end_date::text=e.event_data->>'end_date'
          and p.is_active and p.company_id=e.company_id
          and lower(u.email)=e.recipient and u.email_confirmed_at is not null)
      when 'timesheet_rejected' then exists(
        select 1 from public.timesheets t join public.profiles p on p.id=t.user_id
          join auth.users u on u.id=p.id
        where t.id=e.source_id and t.company_id=e.company_id and t.user_id=e.recipient_user_id
          and t.status='rejected' and t.work_date::text=e.event_data->>'work_date'
          and t.hours_logged=(e.event_data->>'hours_logged')::numeric
          and coalesce(t.description,'')=e.event_data->>'description'
          and coalesce(t.rejection_reason,'')=e.event_data->>'rejection_reason'
          and t.approved_at is not distinct from (e.event_data->>'reviewed_at')::timestamptz
          and t.client_id::text is not distinct from e.event_data->>'client_id'
          and t.project_id::text is not distinct from e.event_data->>'project_id'
          and p.is_active and p.company_id=e.company_id
          and lower(u.email)=e.recipient and u.email_confirmed_at is not null)
      when 'invoice_issued' then exists(
        select 1 from public.invoice_ledger i where i.id=e.source_id and i.company_id=e.company_id and i.status='issued')
      when 'invoice_reversed' then exists(
        select 1 from public.invoice_ledger i where i.id=e.source_id and i.company_id=e.company_id and i.status='reversed')
      else false end;
$$;
revoke all on function private.transactional_email_is_current(private.transactional_emails)
  from public,anon,authenticated,service_role;

create or replace function public.claim_transactional_email()
returns jsonb language plpgsql security definer set search_path='' as $$
declare job private.transactional_emails;
begin
  update private.transactional_emails set status='uncertain',last_error='idempotency_window_expired',
    claim_token=null,leased_until=null
    where status in ('pending','sending') and first_attempt_at < now()-interval '23 hours';
  update private.transactional_emails e set status='cancelled',claim_token=null,leased_until=null
    where (e.status='pending' or (e.status='sending' and e.leased_until<now()))
      and not private.transactional_email_is_current(e);
  select * into job from private.transactional_emails e
    where (e.status='pending' and e.available_at<=now()) or (e.status='sending' and e.leased_until<now())
    order by e.available_at,e.created_at for update skip locked limit 1;
  if not found then return null; end if;
  if job.attempts>=8 then
    update private.transactional_emails set status='uncertain',last_error='attempt_limit_reached'
      where id=job.id;
    return null;
  end if;
  update private.transactional_emails set status='sending',attempts=attempts+1,
    first_attempt_at=coalesce(first_attempt_at,now()),claim_token=gen_random_uuid(),
    leased_until=now()+interval '5 minutes' where id=job.id returning * into job;
  return to_jsonb(job);
end; $$;
revoke all on function public.claim_transactional_email() from public,anon,authenticated;
grant execute on function public.claim_transactional_email() to service_role;
