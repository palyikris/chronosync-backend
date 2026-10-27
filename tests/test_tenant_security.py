"""Attack regressions against real, disposable PostgreSQL; never a hosted database."""
import json
import concurrent.futures
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get('SECURITY_TEST_PG_DSN', '')
pytestmark = pytest.mark.skipif(not DSN, reason='requires isolated local PostgreSQL')
A, B = '20000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000002'
ADMIN, OTHER, EMPLOYEE, INACTIVE, SUPER = [f'10000000-0000-0000-0000-{i:012}' for i in range(1, 6)]
NEW = '10000000-0000-0000-0000-000000000006'


def sql(query, identity=None, role=None, check=True):
    assert 'host=/tmp/chronosync-invoice-audit' in DSN and 'dbname=security_audit' in DSN
    prefix = f"set request.jwt.claim.sub='{identity}';" if identity else ''
    if role:
        assert role in ('authenticated', 'anon')
        prefix += f'set role {role};'
    result = subprocess.run(['psql', DSN, '-XAtq', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose'],
                            input=prefix+query, capture_output=True, text=True, timeout=15)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def denied(query, identity=EMPLOYEE, role='authenticated'):
    result = sql(query, identity, role, check=False)
    assert result.returncode != 0, result.stdout
    assert any(code in result.stderr for code in ('42501', '23514', 'PT409', 'PT429')), result.stderr


@pytest.fixture(scope='module', autouse=True)
def schema():
    if not DSN:
        return
    sql((ROOT/'tests/fixtures/security_database.sql').read_text())
    # Demonstrate the existing vulnerability before installing the fix.
    sql(f"insert into companies(id) values ('{A}'),('{B}'); insert into auth.users(id,email,raw_user_meta_data) values "
        f"('{ADMIN}','admin@test.invalid','{{\"company_id\":\"{A}\",\"role\":\"company_admin\"}}'),"
        f"('{OTHER}','other@test.invalid','{{\"company_id\":\"{B}\",\"role\":\"company_admin\"}}');")
    assert sql('select count(*) from profiles', ADMIN, 'authenticated').stdout.strip() == '2'
    assert sql('select count(*) from companies', ADMIN, 'authenticated').stdout.strip() == '2'
    for name in ['20261022140000_create_manual_invoice_ledger.sql',
        '20261026083353_harden_manual_invoice_consistency.sql',
        '20261027091353_avoid_retryable_invoice_business_conflicts.sql',
        '20261027133954_secure_tenant_boundaries.sql',
        '20261027141923_protect_approved_leave_cancellation.sql',
        '20261027133957_trusted_invitations_and_atomic_leave.sql',
        '20261027132449_recover_existing_invoice_pdf.sql']:
        sql((ROOT/'supabase/migrations'/name).read_text())
        if name.endswith('_protect_approved_leave_cancellation.sql'):
            # Core isolation can ship before the new backend/UI. Existing
            # credential reads and the legacy admin leave sequence still work.
            assert sql('select count(*) from profiles',ADMIN,'authenticated').stdout.strip() == '1'
            assert sql('select count(*) from companies',ADMIN,'authenticated').stdout.strip() == '1'
            sql(f"select invoice_api_key_encrypted from companies where id='{A}'",ADMIN,'authenticated')
            denied('select * from leave_requests',None,'anon')
            sql(f"insert into leave_requests(user_id,company_id,start_date,end_date) values "
                f"('{ADMIN}','{A}','2026-12-01','2026-12-01')",ADMIN,'authenticated')
            sql(f"insert into timesheets(user_id,company_id,work_date,hours_logged,status) values "
                f"('{ADMIN}','{A}','2026-12-01',8,'approved')",ADMIN,'authenticated')
            sql("update leave_requests set status='APPROVED',hours_taken=8",ADMIN,'authenticated')
            sql(f"insert into auth.users(id,email,email_confirmed_at) values ('{EMPLOYEE}','core-worker@test.invalid',now()); "
                f"update profiles set company_id='{A}',is_active=true where id='{EMPLOYEE}';")
            sql(f"insert into leave_requests(user_id,company_id,start_date,end_date,status) values "
                f"('{EMPLOYEE}','{A}','2026-12-02','2026-12-02','APPROVED');")
            sql(f"delete from leave_requests where user_id='{EMPLOYEE}'",EMPLOYEE,'authenticated')
            assert sql(f"select count(*) from leave_requests where user_id='{EMPLOYEE}'").stdout.strip() == '1'



@pytest.fixture(autouse=True)
def seed(schema):
    if not DSN:
        return
    sql('truncate private.company_invitations,public.timesheets,public.leave_requests,public.projects,'
        'public.clients,public.profiles,public.companies,auth.users cascade;')
    sql(f"insert into companies(id) values ('{A}'),('{B}');")
    for identity, company, role, active in [(ADMIN,A,'company_admin',True),(OTHER,B,'company_admin',True),
            (EMPLOYEE,A,'regular',True),(INACTIVE,A,'regular',False),(SUPER,None,'super_admin',True)]:
        sql(f"insert into auth.users(id,email,email_confirmed_at) values ('{identity}','{identity}@test.invalid',now());"
            f"update profiles set company_id={'null' if company is None else chr(39)+company+chr(39)}, "
            f"role='{role}',is_active={str(active).lower()} where id='{identity}';")
    sql(f"insert into leave_requests(user_id,company_id,start_date,end_date,status) values "
        f"('{EMPLOYEE}','{A}','2026-11-02','2026-11-03','PENDING'),"
        f"('{ADMIN}','{A}','2026-11-04','2026-11-04','APPROVED'),"
        f"('{OTHER}','{B}','2026-11-02','2026-11-03','PENDING');")


@pytest.mark.parametrize('identity,count', [(ADMIN,'3'),(OTHER,'1'),(EMPLOYEE,'3'),(INACTIVE,'1'),(SUPER,'5')])
def test_profiles_are_tenant_scoped(identity, count):
    assert sql('select count(*) from profiles',identity,'authenticated').stdout.strip() == count


def test_company_admin_cannot_read_or_mutate_another_company():
    assert sql('select count(*) from companies',ADMIN,'authenticated').stdout.strip() == '1'
    sql(f"update companies set name='ATTACK' where id='{B}'", ADMIN, 'authenticated')
    assert sql(f"select name from companies where id='{B}'").stdout.strip() == 'Company'
    denied(f"update companies set is_active=false where id='{A}'", ADMIN)
    sql(f"update companies set name='Allowed' where id='{A}'", ADMIN, 'authenticated')


def test_credentials_are_hidden_from_company_reads_and_checked_by_admin_rpc():
    for actor in [EMPLOYEE,ADMIN,SUPER]:
        denied('select invoice_api_key_encrypted from companies',actor)
        denied('select szamla_agent_key from companies',actor)
    denied(f"select get_company_invoice_credentials('{A}')",EMPLOYEE)
    denied(f"select get_company_invoice_credentials('{B}')",ADMIN)
    denied(f"select set_company_invoice_credentials('{B}','szamlazz_hu','encrypted')",ADMIN)
    sql(f"select set_company_invoice_credentials('{A}','szamlazz_hu','encrypted')",ADMIN,'authenticated')
    assert json.loads(sql(f"select get_company_invoice_credentials('{A}')",ADMIN,'authenticated').stdout)['invoice_api_key_encrypted'] == 'encrypted'


@pytest.mark.parametrize('change', ["role='super_admin'",f"company_id='{B}'",'is_active=false'])
def test_employee_cannot_change_membership(change):
    denied(f"update profiles set {change} where id='{EMPLOYEE}'")


def test_company_admin_cannot_grant_super_admin_or_move_members():
    denied(f"update profiles set role='super_admin' where id='{EMPLOYEE}'", ADMIN)
    denied(f"update profiles set company_id='{B}' where id='{EMPLOYEE}'", ADMIN)
    sql(f"update profiles set role='company_admin' where id='{EMPLOYEE}'",ADMIN,'authenticated')
    sql(f"update profiles set role='regular' where id='{EMPLOYEE}'",ADMIN,'authenticated')
    denied(f"update profiles set role='regular' where id='{ADMIN}'",ADMIN)
    denied(f"delete from profiles where id='{ADMIN}'",ADMIN)
    denied(f"delete from profiles where id='{SUPER}'",SUPER)


def test_employee_can_edit_preferences_and_admin_can_manage_team():
    sql(f"update profiles set full_name='Name',show_timesheet_timer=true,weekly_work_hours=32 where id='{EMPLOYEE}'", EMPLOYEE,'authenticated')
    sql(f"update profiles set is_active=false where id='{EMPLOYEE}'", ADMIN,'authenticated')
    assert sql('select count(*) from leave_requests',EMPLOYEE,'authenticated').stdout.strip() == '0'


@pytest.mark.parametrize('table',['profiles','companies','timesheets','leave_requests','clients','projects'])
def test_anonymous_access_is_revoked(table):
    denied(f'select * from {table}',None,'anon')


def test_leave_reads_and_changes_respect_tenant_and_owner():
    assert sql('select count(*) from leave_requests', EMPLOYEE,'authenticated').stdout.strip() == '2'
    assert sql('select count(*) from leave_requests', OTHER,'authenticated').stdout.strip() == '1'
    denied(f"update leave_requests set status='APPROVED' where user_id='{EMPLOYEE}'")
    denied(f"insert into leave_requests(user_id,company_id,start_date,end_date) values ('{EMPLOYEE}','{B}','2026-12-01','2026-12-02')")
    denied(f"update leave_requests set user_id='{ADMIN}' where user_id='{EMPLOYEE}'")
    request_id = sql(f"select id from leave_requests where user_id='{EMPLOYEE}'").stdout.strip()
    sql(f"select review_leave_request('{request_id}','REJECTED')",ADMIN,'authenticated')


def test_forged_signup_metadata_is_inert():
    sql(f"insert into auth.users(id,email,raw_user_meta_data) values ('{NEW}','new@test.invalid',"
        f"'{{\"company_id\":\"{B}\",\"role\":\"super_admin\"}}');")
    assert sql(f"select role||':'||is_active::text||':'||(company_id is null)::text from profiles where id='{NEW}'").stdout.strip() == 'regular:false:true'
    assert sql('select count(*) from companies',NEW,'authenticated').stdout.strip() == '0'
    denied(f"insert into profiles(id,full_name,role) values ('{NEW}','Attacker','super_admin')",NEW)


def test_employees_cannot_self_approve_or_move_timesheets():
    insert = f"insert into timesheets(user_id,company_id,work_date,hours_logged,status) values ('{EMPLOYEE}','{A}','2026-11-10',2,%s)"
    denied(insert % "'approved'")
    denied(insert.replace(A,B) % "'draft'")
    sql(insert % "'draft'",EMPLOYEE,'authenticated')
    denied("update timesheets set status='approved'")
    denied("update timesheets set approved_by=user_id")
    denied(f"update timesheets set company_id='{B}'")
    sql("update timesheets set status='submitted'", EMPLOYEE, 'authenticated')
    sql("update timesheets set status='approved'", ADMIN, 'authenticated')
    denied("update timesheets set status='invoiced'", ADMIN)


def prepare(email='new@test.invalid', company=A, role='regular', actor=ADMIN, check=True):
    result = sql(f"select public.prepare_company_invitation('{email}','{company}','New Member','{role}')",actor,'authenticated',check)
    return json.loads(result.stdout) if check else result


def test_only_authorized_admin_can_prepare_invites():
    for actor, company, role in [(EMPLOYEE,A,'regular'),(ADMIN,B,'regular'),(ADMIN,A,'super_admin'),(INACTIVE,A,'regular')]:
        assert prepare(company=company,role=role,actor=actor,check=False).returncode != 0
    denied('select * from private.company_invitations',ADMIN)
    denied('select public.claim_company_invitation()',None,'anon')


def test_invitation_requires_verified_email_then_claims_once():
    result = prepare('NEW@Test.Invalid',role='company_admin')
    assert result['email'] == 'new@test.invalid' and result['existing_user'] is False
    sql(f"insert into auth.users(id,email) values ('{NEW}','new@test.invalid');")
    assert sql('select claim_company_invitation()',NEW,'authenticated').stdout.strip() == 'f'
    sql(f"update auth.users set email_confirmed_at=now() where id='{NEW}'")
    assert sql('select claim_company_invitation()',NEW,'authenticated').stdout.strip() == 't'
    assert sql('select claim_company_invitation()',NEW,'authenticated').stdout.strip() == 'f'
    assert sql(f"select role||':'||company_id::text||':'||is_active::text from profiles where id='{NEW}'").stdout.strip() == f'company_admin:{A}:true'


@pytest.mark.parametrize('invalidate',["expires_at=now()-interval '1 second'",'revoked_at=now()'])
def test_expired_or_revoked_invitation_cannot_be_claimed(invalidate):
    prepare()
    sql(f"insert into auth.users(id,email,email_confirmed_at) values ('{NEW}','new@test.invalid',now()); "
        f"update private.company_invitations set {invalidate};")
    assert sql('select claim_company_invitation()',NEW,'authenticated').stdout.strip() == 'f'


def test_existing_membership_cannot_be_transferred_or_demoted():
    assert prepare(email=f'{OTHER}@test.invalid',check=False).returncode != 0
    assert prepare(email=f'{INACTIVE}@test.invalid',check=False).returncode != 0
    assert prepare(email=f'{ADMIN}@test.invalid')['existing_user'] is True
    assert sql('select claim_company_invitation()',ADMIN,'authenticated').stdout.strip() == 't'
    assert sql(f"select role from profiles where id='{ADMIN}'").stdout.strip() == 'company_admin'


def test_invitation_resend_is_rate_limited():
    prepare()
    assert 'PT429' in prepare(check=False).stderr


def test_team_email_lookup_is_admin_only_and_tenant_scoped():
    denied('select get_company_members()')
    members = json.loads(sql('select get_company_members()',ADMIN,'authenticated').stdout)
    assert len(members) == 3 and all(member['company_id'] == A for member in members)
    assert all(member['email'] for member in members)


def setup_leave():
    sql(f"insert into clients(id,company_id) values ('{A}','{A}'); "
        f"insert into projects(id,company_id,client_id) values ('{A}','{A}','{A}'); "
        f"update companies set leave_client_id='{A}',leave_project_id='{A}' where id='{A}';")
    return sql(f"select id from leave_requests where user_id='{EMPLOYEE}'").stdout.strip()


def test_leave_approval_and_owner_cancellation_are_atomic():
    request_id = setup_leave()
    denied(f"select review_leave_request('{request_id}','APPROVED')",OTHER)
    denied(f"select review_leave_request('{request_id}','APPROVED')",EMPLOYEE)
    sql(f"select review_leave_request('{request_id}','APPROVED')",ADMIN,'authenticated')
    assert sql('select count(*)||\':\'||sum(hours_logged)::text from timesheets').stdout.strip() == '2:16.00'
    assert sql(f"select hours_taken from leave_requests where id='{request_id}'").stdout.strip() == '16.00'
    denied("update timesheets set hours_logged=1",ADMIN)
    denied("delete from timesheets",ADMIN)
    denied(f"select review_leave_request('{request_id}','APPROVED')",ADMIN)
    sql(f"select cancel_leave_request('{request_id}')",EMPLOYEE,'authenticated')
    assert sql('select count(*) from timesheets').stdout.strip() == '0'
    assert sql(f"select count(*) from leave_requests where id='{request_id}'").stdout.strip() == '0'


def test_existing_work_blocks_leave_without_creating_partial_entries():
    request_id = setup_leave()
    sql(f"insert into timesheets(user_id,company_id,work_date,hours_logged) values ('{EMPLOYEE}','{A}','2026-11-03',2)",EMPLOYEE,'authenticated')
    denied(f"select review_leave_request('{request_id}','APPROVED')",ADMIN)
    assert sql('select count(*) from timesheets').stdout.strip() == '1'
    assert sql(f"select status from leave_requests where id='{request_id}'").stdout.strip() == 'PENDING'


def test_leave_overlap_is_enforced_even_without_frontend_validation():
    denied(f"insert into leave_requests(user_id,company_id,start_date,end_date) values ('{EMPLOYEE}','{A}','2026-11-03','2026-11-05')")


def test_invoice_guard_failure_rolls_back_leave_cancellation():
    request_id = setup_leave()
    sql(f"select review_leave_request('{request_id}','APPROVED')",ADMIN,'authenticated')
    sql(f"insert into client_billing_profiles(client_id,company_id) values ('{A}','{A}');")
    invoice_id = '90000000-0000-0000-0000-000000000001'
    sql(f"""select * from reserve_manual_invoice('{invoice_id}','{A}','{A}','2026-11-01','2026-11-30',
      'fingerprint','Buyer',null,false,'hourly',16,'hour',100,'27','HUF',1600,432,2032,
      jsonb_build_object('billing_profile_updated_at','2026-10-22T00:00:00Z','approved_entries',
        (select jsonb_agg(jsonb_build_object('id',id,'work_date',work_date,'hours_logged',hours_logged,
          'description',description,'project_name','General Task')) from public.timesheets)),
      (select jsonb_agg(id::text) from public.timesheets));""",ADMIN,'authenticated')
    denied(f"select cancel_leave_request('{request_id}')",EMPLOYEE)
    # Invoice finalization remains compatible with all new tenant/leave guards.
    sql(f"select * from finalize_manual_invoice('{invoice_id}','TEST-LEAVE',1600,2032,'JVBERi1saXZl')",ADMIN,'authenticated')
    denied(f"select cancel_leave_request('{request_id}')",EMPLOYEE)
    assert sql('select count(*) from timesheets where status=\'invoiced\'').stdout.strip() == '2'
    assert sql(f"select status from leave_requests where id='{request_id}'").stdout.strip() == 'APPROVED'


def test_concurrent_admin_demotions_leave_one_active_admin():
    sql(f"update profiles set role='company_admin' where id='{EMPLOYEE}'")
    def demote(identity):
        return sql(f"update profiles set role='regular' where id='{identity}'",SUPER,'authenticated',check=False).returncode
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(demote,[ADMIN,EMPLOYEE]))
    assert results.count(0) == 1
    assert sql(f"select count(*) from profiles where company_id='{A}' and role='company_admin' and is_active").stdout.strip() == '1'
