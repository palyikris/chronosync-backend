"""Queue/authorization regressions on an isolated local PostgreSQL database."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

DSN = os.environ.get('EMAIL_TEST_PG_DSN', '')
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not DSN, reason='requires isolated email PostgreSQL')
COMPANY = '20000000-0000-0000-0000-000000000001'
OTHER = '20000000-0000-0000-0000-000000000002'
ADMIN = '10000000-0000-0000-0000-000000000001'
ADMIN2 = '10000000-0000-0000-0000-000000000002'
EMPLOYEE = '10000000-0000-0000-0000-000000000003'
REQUEST = '40000000-0000-0000-0000-000000000001'
INVOICE = '50000000-0000-0000-0000-000000000001'
ENTRY = '60000000-0000-0000-0000-000000000001'
CLIENT = '70000000-0000-0000-0000-000000000001'
PROJECT = '80000000-0000-0000-0000-000000000001'


def sql(query, *, role=None, identity=None, check=True):
    assert 'host=/tmp/chronosync-email-tests' in DSN and 'dbname=email_tests' in DSN
    prefix = f"set request.jwt.claim.sub='{identity}';" if identity else ''
    if role:
        assert role in ('anon', 'authenticated', 'service_role')
        prefix += f'set role {role};'
    result = subprocess.run(['psql', DSN, '-XAtq', '-v', 'ON_ERROR_STOP=1'],
        input=prefix + query, text=True, capture_output=True, timeout=10)
    if check: assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope='module', autouse=True)
def schema():
    if not DSN: return
    sql("""
    drop schema if exists public cascade;
    drop schema if exists private cascade;
    drop schema if exists auth cascade;
    create schema public; grant usage on schema public to public;
    create schema auth; create schema private;
    do $$ begin
      if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if;
      if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
      if not exists(select 1 from pg_roles where rolname='service_role') then create role service_role; end if;
    end $$;
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid;
    $$;
    create table public.companies(id uuid primary key,name text,is_active boolean default true,is_deleted boolean default false);
    create table auth.users(id uuid primary key,email text,email_confirmed_at timestamptz default now());
    create table public.profiles(id uuid primary key,company_id uuid,role text,full_name text,is_active boolean default true);
    create table public.leave_requests(id uuid primary key,user_id uuid,company_id uuid,start_date date,end_date date,
      status text default 'PENDING',weekly_work_hours numeric(4,1) default 40,updated_at timestamptz default now());
    create table public.clients(id uuid primary key,company_id uuid,name text);
    create table public.projects(id uuid primary key,company_id uuid,client_id uuid,name text);
    create type public.timesheet_entry_status as enum ('draft','submitted','approved','rejected','invoiced');
    create table public.timesheets(id uuid primary key,user_id uuid,company_id uuid,client_id uuid,project_id uuid,
      work_date date,hours_logged numeric(5,2),description text,status public.timesheet_entry_status default 'submitted',
      rejection_reason text,approved_at timestamptz,approved_by uuid);
    create table public.invoice_ledger(id uuid primary key,company_id uuid,status text default 'issuing',
      invoice_snapshot jsonb,send_email boolean default true,recipient_email text default 'buyer@example.test',
      recipient_name text default 'Buyer',pdf_bytes bytea,attachment_bytes bytea,attachment_metadata jsonb,
      invoice_number text default 'TEST-1',period_start date default '2026-10-01',period_end date default '2026-10-31',
      gross_total numeric default 31750,currency text default 'HUF',reversal_pdf_bytes bytea,
      reversal_invoice_number text default 'STORNO-1',reversal_gross_total numeric default -31750);
    create function private.can_manage_company_invoices(p_company_id uuid) returns boolean
      language sql stable security definer set search_path='' as $$
      select exists(select 1 from public.profiles p join public.companies c on c.id=p_company_id
        where p.id=auth.uid() and p.is_active and c.is_active and not c.is_deleted
        and (p.role='super_admin' or (p.role='company_admin' and p.company_id=p_company_id)));
    $$;
    """)
    sql((ROOT / 'supabase/migrations/20261110202649_transactional_email_outbox.sql').read_text())
    sql((ROOT / 'supabase/migrations/20261110213624_employee_decision_emails.sql').read_text())
    sql((ROOT / 'supabase/migrations/20261113151433_enqueue_multiple_invoice_recipients.sql').read_text())


@pytest.fixture(autouse=True)
def seed(schema):
    if not DSN: return
    sql(f"""
      truncate private.transactional_emails,public.invoice_ledger,public.leave_requests,public.timesheets,
        public.projects,public.clients,
        public.profiles,auth.users,public.companies cascade;
      insert into public.companies(id,name) values('{COMPANY}','Company'),('{OTHER}','Other');
      insert into public.profiles(id,company_id,role,full_name) values
        ('{ADMIN}','{COMPANY}','company_admin','Admin'),('{ADMIN2}','{OTHER}','company_admin','Other Admin'),
        ('{EMPLOYEE}','{COMPANY}','regular','Employee');
      insert into auth.users(id,email) values('{ADMIN}','admin@example.test'),
        ('{ADMIN2}','other-admin@example.test'),('{EMPLOYEE}','employee@example.test');
      insert into public.clients values('{CLIENT}','{COMPANY}','Client');
      insert into public.projects values('{PROJECT}','{COMPANY}','{CLIENT}','Project');
    """)


def create_leave():
    sql(f"""insert into leave_requests(id,user_id,company_id,start_date,end_date) values('{REQUEST}',
        '{EMPLOYEE}','{COMPANY}','2026-11-11','2026-11-12');""")


def claim():
    return json.loads(sql('select public.claim_transactional_email();', role='service_role').stdout.strip() or 'null')


def create_invoice(*, transport='resend', sending=True, status='issuing'):
    snapshot = json.dumps({'email_transport': transport, 'language': 'en', 'due_date': '2026-11-20'})
    sql(f"""insert into invoice_ledger(id,company_id,status,invoice_snapshot,send_email)
      values('{INVOICE}','{COMPANY}','{status}','{snapshot}',{str(sending).lower()});""")


def finalize_invoice():
    sql(f"""update invoice_ledger set status='issued',pdf_bytes=convert_to('%PDF-issued','UTF8'),
      attachment_bytes=decode('504b0304','hex'),attachment_metadata='{{"filename":"report.xlsx"}}'
      where id='{INVOICE}';""")


def test_leave_notifications_are_atomic_and_tenant_scoped():
    create_leave()
    assert sql('select recipient from private.transactional_emails').stdout.strip() == 'admin@example.test'
    sql(f"update leave_requests set status='APPROVED' where id='{REQUEST}';")
    job = claim()
    assert job['kind'] == 'leave_decision' and job['recipient'] == 'employee@example.test'
    assert sql("select status from private.transactional_emails where kind='leave_requested'").stdout.strip() == 'cancelled'


def test_leave_edits_cancel_stale_notifications_and_notify_current_dates():
    create_leave()
    sql(f"update leave_requests set start_date='2026-11-12',updated_at=now() where id='{REQUEST}';")
    job = claim()
    assert job['event_data']['start_date'] == '2026-11-12'
    assert sql("select count(*) from private.transactional_emails where status='cancelled'").stdout.strip() == '1'


@pytest.mark.parametrize('change', [
    f"delete from leave_requests where id='{REQUEST}'",
    f"update profiles set is_active=false where id='{ADMIN}'",
    f"update profiles set company_id='{OTHER}' where id='{ADMIN}'",
    f"update auth.users set email='changed@example.test' where id='{ADMIN}'",
    f"update companies set is_active=false where id='{COMPANY}'",
])
def test_stale_or_revoked_leave_access_is_rechecked_at_claim(change):
    create_leave()
    sql(change)
    assert claim() is None


def test_business_transaction_rollback_does_not_queue_email():
    sql(f"""begin;insert into leave_requests(id,user_id,company_id,start_date,end_date) values('{REQUEST}',
        '{EMPLOYEE}','{COMPANY}','2026-11-11','2026-11-12');rollback;""")
    assert claim() is None


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
def test_browser_roles_cannot_claim_send_or_read_payloads(role):
    create_leave()
    for query in ('select * from private.transactional_emails', 'select public.claim_transactional_email()',
                  'select public.email_worker_ready()',
                  f"select public.prepare_transactional_email('{uuid4()}','{uuid4()}','{{}}')",
                  f"select public.finish_transactional_email('{uuid4()}','{uuid4()}')"):
        assert sql(query, role=role, identity=ADMIN, check=False).returncode != 0


def test_only_company_admin_can_read_scoped_status_without_payloads():
    create_leave()
    response = json.loads(sql(f"select get_company_email_status('{COMPANY}')",
        role='authenticated', identity=ADMIN).stdout)
    assert response['queue'] == {'pending': 1} and 'recipient' not in str(response)
    for identity in (ADMIN2, EMPLOYEE):
        assert sql(f"select get_company_email_status('{COMPANY}')", role='authenticated',
                   identity=identity, check=False).returncode != 0


def test_invoice_queue_waits_for_confirmed_archived_bytes_and_is_idempotent():
    create_invoice()
    assert claim() is None
    finalize_invoice()
    finalize_invoice()
    job = claim()
    assert job['kind'] == 'invoice_issued' and job['event_data']['pdf_base64'] == 'JVBERi1pc3N1ZWQ='
    assert sql('select count(*) from private.transactional_emails').stdout.strip() == '1'


def test_multi_recipient_invoice_and_reversal_queue_one_job_per_address():
    create_invoice()
    sql("update invoice_ledger set recipient_email='Buyer@example.test, team@example.test,BUYER@example.test'")
    finalize_invoice()
    finalize_invoice()
    assert sql("select recipient from private.transactional_emails order by event_key").stdout.splitlines() == [
        'buyer@example.test', 'team@example.test',
    ]
    for _ in range(2):
        sql("update invoice_ledger set status='reversed',reversal_pdf_bytes=convert_to('%PDF-storno','UTF8')")
    assert claim()['kind'] == 'invoice_reversed'
    assert sql("select count(*) from private.transactional_emails where status='cancelled'").stdout.strip() == '2'
    assert sql("select count(*) from private.transactional_emails where kind='invoice_reversed'").stdout.strip() == '2'


@pytest.mark.parametrize('transport,sending', [('provider', True), ('resend', False)])
def test_disabled_and_provider_email_invoices_are_never_resent(transport, sending):
    create_invoice(transport=transport, sending=sending)
    finalize_invoice()
    assert claim() is None


def test_missing_historical_transport_is_never_backfilled():
    create_invoice()
    sql("update invoice_ledger set invoice_snapshot='{}'")
    finalize_invoice()
    assert claim() is None


def test_reversal_cancels_unsent_invoice_and_queues_confirmed_reversal_once():
    create_invoice()
    finalize_invoice()
    for _ in range(2):
        sql("update invoice_ledger set status='reversed',reversal_pdf_bytes=convert_to('%PDF-storno','UTF8')")
    job = claim()
    assert job['kind'] == 'invoice_reversed' and job['event_data']['invoice_number'] == 'STORNO-1'
    assert sql("select count(*) from private.transactional_emails where status='cancelled'").stdout.strip() == '1'


def test_concurrent_workers_claim_one_job_only_and_freeze_request():
    create_leave()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: claim(), range(2)))
    assert sum(job is not None for job in jobs) == 1
    job = next(job for job in jobs if job)
    prefix = f"'{job['id']}','{job['claim_token']}'"
    message = json.dumps({'to': [job['recipient']], 'subject': 'Frozen', 'from': 'mail@example.test'})
    for changed in (message, message.replace('Frozen', 'Changed')):
        saved = json.loads(sql(f"select prepare_transactional_email({prefix},'{changed}')", role='service_role').stdout)
        assert saved['subject'] == 'Frozen'
    sql(f"select finish_transactional_email({prefix},p_error=>'resend_network_error',p_retry=>true)", role='service_role')
    sql("update private.transactional_emails set available_at=now()-interval '1 second'")
    retry = claim()
    assert retry['id'] == job['id'] and retry['message']['subject'] == 'Frozen'
    assert retry['claim_token'] != job['claim_token']
    assert sql(f"select finish_transactional_email({prefix},p_provider_id=>'late')", role='service_role', check=False).returncode != 0
    sql(f"select finish_transactional_email('{retry['id']}','{retry['claim_token']}',p_provider_id=>'provider-1')", role='service_role')
    assert claim() is None
    assert sql("select status||':'||provider_id from private.transactional_emails").stdout.strip() == 'accepted:provider-1'


def test_uncertain_attempts_stop_before_provider_idempotency_window_expires():
    create_leave()
    job = claim()
    sql("update private.transactional_emails set first_attempt_at=now()-interval '24 hours',leased_until=now()-interval '1 second'")
    assert claim() is None
    assert sql("select status||':'||last_error from private.transactional_emails").stdout.strip() == 'uncertain:idempotency_window_expired'


def create_timesheet():
    sql(f"""insert into timesheets(id,user_id,company_id,client_id,project_id,work_date,hours_logged,description)
      values('{ENTRY}','{EMPLOYEE}','{COMPANY}','{CLIENT}','{PROJECT}','2026-10-01',2.5,'Entry to correct');""")


def reject_timesheet():
    sql(f"""update timesheets set status='rejected',approved_by='{ADMIN}',
      approved_at='2026-11-10T10:00:00Z',rejection_reason='Please explain the extra hour'
      where id='{ENTRY}' and status='submitted';""")


@pytest.mark.parametrize('decision', ['APPROVED', 'REJECTED'])
def test_leave_decision_notifies_only_owner_once_after_status_transition(decision):
    create_leave()
    assert sql("select count(*) from private.transactional_emails where kind='leave_decision'").stdout.strip() == '0'
    for _ in range(2):
        sql(f"update leave_requests set status='{decision}' where id='{REQUEST}';")
    job = claim()
    assert job['kind'] == 'leave_decision' and job['recipient_user_id'] == EMPLOYEE
    assert job['recipient'] == 'employee@example.test' and job['company_id'] == COMPANY
    assert job['event_data']['decision'] == decision
    assert job['event_data']['start_date'] == '2026-11-11'
    assert sql("select count(*) from private.transactional_emails where kind='leave_decision'").stdout.strip() == '1'


@pytest.mark.parametrize('event', ['leave', 'timesheet'])
def test_rolled_back_reviews_do_not_queue_employee_emails(event):
    if event == 'leave':
        create_leave()
        sql("begin;update leave_requests set status='APPROVED';rollback;")
        kind = 'leave_decision'
    else:
        create_timesheet()
        sql("begin;update timesheets set status='rejected',rejection_reason='Reason';rollback;")
        kind = 'timesheet_rejected'
    assert sql(f"select count(*) from private.transactional_emails where kind='{kind}'").stdout.strip() == '0'


def test_timesheet_rejection_contains_reason_and_real_entry_details_without_duplicates():
    create_timesheet()
    assert claim() is None
    reject_timesheet()
    reject_timesheet()
    sql("update timesheets set rejection_reason=rejection_reason")
    job = claim()
    assert job['kind'] == 'timesheet_rejected' and job['recipient'] == 'employee@example.test'
    assert job['recipient_user_id'] == EMPLOYEE and job['company_id'] == COMPANY
    assert job['event_data']['rejection_reason'] == 'Please explain the extra hour'
    assert job['event_data']['hours_logged'] == 2.5
    assert job['event_data']['client_name'] == 'Client' and job['event_data']['project_name'] == 'Project'
    assert job['event_data']['work_date'] == '2026-10-01'
    assert sql("select count(*) from private.transactional_emails where kind='timesheet_rejected'").stdout.strip() == '1'


@pytest.mark.parametrize('event', ['leave', 'timesheet'])
@pytest.mark.parametrize('change', [
    f"update profiles set is_active=false where id='{EMPLOYEE}'",
    f"update profiles set company_id='{OTHER}' where id='{EMPLOYEE}'",
    f"update auth.users set email='changed@example.test' where id='{EMPLOYEE}'",
    f"update auth.users set email_confirmed_at=null where id='{EMPLOYEE}'",
    f"delete from auth.users where id='{EMPLOYEE}'",
])
def test_employee_notifications_recheck_recipient_access_before_claim(event, change):
    if event == 'leave':
        create_leave()
        sql("update leave_requests set status='APPROVED'")
    else:
        create_timesheet()
        reject_timesheet()
    sql(change)
    assert claim() is None


@pytest.mark.parametrize('event', ['leave', 'timesheet'])
def test_cross_company_owner_cannot_receive_another_tenants_data(event):
    sql(f"update profiles set company_id='{OTHER}' where id='{EMPLOYEE}'")
    if event == 'leave':
        create_leave()
        sql("update leave_requests set status='REJECTED'")
    else:
        create_timesheet()
        reject_timesheet()
    assert sql("select count(*) from private.transactional_emails where kind in ('leave_decision','timesheet_rejected')").stdout.strip() == '0'


@pytest.mark.parametrize('change', [
    'delete from timesheets', "update timesheets set status='submitted',rejection_reason=null,approved_at=null",
    "update timesheets set status='approved'", "update timesheets set rejection_reason='New reason'",
    "update timesheets set description='Corrected entry'", "update timesheets set hours_logged=1",
])
def test_resolved_or_changed_timesheet_notifications_are_cancelled(change):
    create_timesheet()
    reject_timesheet()
    sql(change)
    assert claim() is None
    assert sql('select status from private.transactional_emails').stdout.strip() == 'cancelled'


def test_cancelled_leave_does_not_send_stale_decision():
    create_leave()
    sql("update leave_requests set status='APPROVED';delete from leave_requests")
    assert claim() is None


def test_repeated_rejection_after_resubmission_is_a_new_event():
    create_timesheet()
    reject_timesheet()
    sql("update timesheets set status='submitted',rejection_reason=null,approved_at=null")
    # Deliberately reuse the original review timestamp and reason.
    reject_timesheet()
    job = claim()
    assert job['kind'] == 'timesheet_rejected' and job['event_data']['rejection_reason'] == 'Please explain the extra hour'
    assert sql("select count(*) from private.transactional_emails where status='cancelled'").stdout.strip() == '1'
    assert sql("select count(*) from private.transactional_emails where kind='timesheet_rejected'").stdout.strip() == '2'
