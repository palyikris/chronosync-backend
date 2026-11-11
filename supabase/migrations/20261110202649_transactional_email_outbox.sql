-- No network I/O in business transactions. Jobs are created atomically with the
-- request/issued document, then claimed by the server using service-only RPCs.
create schema if not exists private;
create table private.transactional_emails (
  id uuid primary key default gen_random_uuid(),
  event_key text not null unique,
  company_id uuid not null references public.companies(id) on delete cascade,
  kind text not null check (kind in ('leave_requested','invoice_issued','invoice_reversed')),
  source_id uuid not null,
  recipient_user_id uuid,
  recipient text not null,
  event_data jsonb not null,
  message jsonb,
  status text not null default 'pending'
    check (status in ('pending','sending','accepted','failed','uncertain','cancelled')),
  attempts integer not null default 0,
  available_at timestamptz not null default now(),
  claim_token uuid,
  leased_until timestamptz,
  first_attempt_at timestamptz,
  provider_id text,
  last_error text,
  created_at timestamptz not null default now(),
  accepted_at timestamptz
);
alter table private.transactional_emails enable row level security;
revoke all on private.transactional_emails from public,anon,authenticated,service_role;
create index transactional_emails_due on private.transactional_emails(available_at,created_at)
  where status in ('pending','sending');
create index transactional_emails_company on private.transactional_emails(company_id,status);

create or replace function private.enqueue_leave_email()
returns trigger language plpgsql security definer set search_path='' as $$
declare event_version text;
begin
  if new.status <> 'PENDING' then return new; end if;
  if tg_op = 'UPDATE' then
    if row(new.start_date,new.end_date,new.weekly_work_hours,new.status)
      is not distinct from row(old.start_date,old.end_date,old.weekly_work_hours,old.status)
      then return new; end if;
  end if;
  event_version := md5(concat_ws(':',new.start_date,new.end_date,new.weekly_work_hours,new.updated_at));
  insert into private.transactional_emails(
    event_key,company_id,kind,source_id,recipient_user_id,recipient,event_data)
  select 'leave/'||new.id||'/'||event_version||'/'||admin.id,
    new.company_id,'leave_requested',new.id,admin.id,lower(u.email),
    jsonb_build_object('company_name',c.name,'employee_name',employee.full_name,
      'start_date',new.start_date,'end_date',new.end_date,'weekly_work_hours',new.weekly_work_hours)
  from public.profiles admin join auth.users u on u.id=admin.id
    join public.companies c on c.id=new.company_id
    join public.profiles employee on employee.id=new.user_id
  where admin.company_id=new.company_id and admin.role in ('company_admin','super_admin')
    and admin.is_active and c.is_active and not c.is_deleted
    and u.email is not null and u.email_confirmed_at is not null
  on conflict(event_key) do nothing;
  return new;
end; $$;
revoke all on function private.enqueue_leave_email() from public,anon,authenticated,service_role;
create trigger enqueue_leave_email after insert or update on public.leave_requests
  for each row execute function private.enqueue_leave_email();

create or replace function private.enqueue_invoice_email()
returns trigger language plpgsql security definer set search_path='' as $$
declare data jsonb; event_kind text; document_pdf bytea; document_number text;
begin
  -- Missing transport means a historical provider-delivered invoice. Never
  -- backfill those records into Resend, including during reconciliation.
  if not new.send_email or new.invoice_snapshot->>'email_transport' is distinct from 'resend'
    or nullif(trim(new.recipient_email),'') is null then return new; end if;
  if new.status='issued' and new.pdf_bytes is not null and new.attachment_bytes is not null then
    event_kind:='invoice_issued'; document_pdf:=new.pdf_bytes; document_number:=new.invoice_number;
  elsif new.status='reversed' and new.reversal_pdf_bytes is not null then
    event_kind:='invoice_reversed'; document_pdf:=new.reversal_pdf_bytes; document_number:=new.reversal_invoice_number;
  else return new; end if;
  select jsonb_build_object('company_name',c.name,'recipient_name',new.recipient_name,
    'invoice_number',document_number,'period_start',new.period_start,'period_end',new.period_end,
    'gross_total',case when event_kind='invoice_issued' then new.gross_total else new.reversal_gross_total end,
    'currency',new.currency,'language',coalesce(new.invoice_snapshot->>'language','hu'),
    'due_date',new.invoice_snapshot->>'due_date',
    'pdf_base64',replace(encode(document_pdf,'base64'),E'\n','')) into data
    from public.companies c where c.id=new.company_id;
  if event_kind='invoice_issued' then
    data:=data||jsonb_build_object('attachment',new.attachment_metadata,
      'attachment_base64',replace(encode(new.attachment_bytes,'base64'),E'\n',''));
  end if;
  insert into private.transactional_emails(event_key,company_id,kind,source_id,recipient,event_data)
    values(event_kind||'/'||new.id,new.company_id,event_kind,new.id,lower(trim(new.recipient_email)),data)
    on conflict(event_key) do nothing;
  return new;
end; $$;
revoke all on function private.enqueue_invoice_email() from public,anon,authenticated,service_role;
create trigger enqueue_invoice_email after insert or update on public.invoice_ledger
  for each row execute function private.enqueue_invoice_email();

create or replace function public.email_worker_ready()
returns boolean language sql security definer set search_path='' as $$
  select exists(select 1 from pg_catalog.pg_class where oid='private.transactional_emails'::regclass);
$$;

create or replace function public.claim_transactional_email()
returns jsonb language plpgsql security definer set search_path='' as $$
declare job private.transactional_emails;
begin
  -- Resend remembers idempotency keys for 24h. Stop uncertain recovery after
  -- 23h; a late retry must never silently create another customer email.
  update private.transactional_emails set status='uncertain',last_error='idempotency_window_expired',
    claim_token=null,leased_until=null
    where status in ('pending','sending') and first_attempt_at < now()-interval '23 hours';
  update private.transactional_emails e set status='cancelled',claim_token=null,leased_until=null
    where (e.status='pending' or (e.status='sending' and e.leased_until<now())) and (
      not exists(select 1 from public.companies c where c.id=e.company_id and c.is_active and not c.is_deleted)
      or (e.kind='leave_requested' and not exists(
        select 1 from public.leave_requests r join public.profiles p on p.id=e.recipient_user_id
          join auth.users u on u.id=p.id
        where r.id=e.source_id and r.company_id=e.company_id and r.status='PENDING'
          and r.start_date::text=e.event_data->>'start_date' and r.end_date::text=e.event_data->>'end_date'
          and r.weekly_work_hours::text=e.event_data->>'weekly_work_hours'
          and p.is_active and p.company_id=e.company_id and p.role in ('company_admin','super_admin')
          and lower(u.email)=e.recipient and u.email_confirmed_at is not null))
      or (e.kind='invoice_issued' and not exists(
        select 1 from public.invoice_ledger i where i.id=e.source_id and i.company_id=e.company_id and i.status='issued'))
      or (e.kind='invoice_reversed' and not exists(
        select 1 from public.invoice_ledger i where i.id=e.source_id and i.company_id=e.company_id and i.status='reversed'))
    );
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

create or replace function public.prepare_transactional_email(p_id uuid,p_token uuid,p_message jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare job private.transactional_emails;
begin
  select * into job from private.transactional_emails where id=p_id for update;
  if job.id is null or job.status<>'sending' or job.claim_token is distinct from p_token or job.leased_until<=now() then
    raise sqlstate 'PT409' using message='Email lease expired'; end if;
  if job.message is null then
    if jsonb_typeof(p_message) is distinct from 'object' or p_message->'to' is distinct from jsonb_build_array(job.recipient)
      then raise sqlstate '22023' using message='Invalid email request'; end if;
    update private.transactional_emails set message=p_message where id=p_id returning * into job;
  end if;
  return job.message;
end; $$;

create or replace function public.finish_transactional_email(
  p_id uuid,p_token uuid,p_provider_id text default null,p_error text default null,p_retry boolean default false
) returns boolean language plpgsql security definer set search_path='' as $$
declare job private.transactional_emails;
begin
  select * into job from private.transactional_emails where id=p_id for update;
  if job.id is null or job.status<>'sending' or job.claim_token is distinct from p_token or job.leased_until<=now() then
    raise sqlstate 'PT409' using message='Email lease expired'; end if;
  if p_provider_id is not null and job.message is null then
    raise sqlstate '22023' using message='Email request was not prepared'; end if;
  update private.transactional_emails set
    status=case when p_provider_id is not null then 'accepted'
      when p_retry and attempts<8 then 'pending'
      when p_retry then 'uncertain' else 'failed' end,
    provider_id=p_provider_id,last_error=left(p_error,100),
    accepted_at=case when p_provider_id is not null then now() else null end,
    available_at=now()+make_interval(secs=>least(3600,30*power(2,attempts)::integer)),
    claim_token=null,leased_until=null where id=p_id;
  return true;
end; $$;

-- RPC execution rights, rather than a browser JWT, protect the sending boundary.
revoke all on function public.email_worker_ready() from public,anon,authenticated;
revoke all on function public.claim_transactional_email() from public,anon,authenticated;
revoke all on function public.prepare_transactional_email(uuid,uuid,jsonb) from public,anon,authenticated;
revoke all on function public.finish_transactional_email(uuid,uuid,text,text,boolean) from public,anon,authenticated;
grant execute on function public.email_worker_ready(),public.claim_transactional_email(),
  public.prepare_transactional_email(uuid,uuid,jsonb),public.finish_transactional_email(uuid,uuid,text,text,boolean)
  to service_role;

create or replace function public.get_company_email_status(p_company_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
  if not private.can_manage_company_invoices(p_company_id) then
    raise sqlstate '42501' using message='Active company administrator access required'; end if;
  return jsonb_build_object(
    'queue',coalesce((select jsonb_object_agg(status,n) from (
      select status,count(*) n from private.transactional_emails where company_id=p_company_id group by status
    ) counts),'{}'::jsonb),
    'recent_failures',coalesce((select jsonb_agg(to_jsonb(f)) from (
      select id,kind,status,last_error,attempts from private.transactional_emails
      where company_id=p_company_id and status in ('failed','uncertain')
      order by created_at desc limit 20) f),'[]'::jsonb));
end; $$;
revoke all on function public.get_company_email_status(uuid) from public,anon;
grant execute on function public.get_company_email_status(uuid) to authenticated;
