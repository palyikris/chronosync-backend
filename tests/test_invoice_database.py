"""Real PostgreSQL regressions. Opt in with INVOICE_TEST_PG_DSN (local /tmp only)."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = '10000000-0000-0000-0000-000000000001'
OTHER_ADMIN = '10000000-0000-0000-0000-000000000002'
EMPLOYEE = '10000000-0000-0000-0000-000000000003'
COMPANY = '20000000-0000-0000-0000-000000000001'
OTHER_COMPANY = '20000000-0000-0000-0000-000000000002'
CLIENT = '30000000-0000-0000-0000-000000000001'
ENTRY = '40000000-0000-0000-0000-000000000001'
DSN = os.environ.get('INVOICE_TEST_PG_DSN', '')
pytestmark = pytest.mark.skipif(not DSN, reason='requires isolated local PostgreSQL')


def sql(query, *, identity=None, role=None, check=True):
    # This suite resets its fixture tables: reject any non-local database.
    assert 'host=/tmp/chronosync-invoice-audit' in DSN and 'dbname=invoice_audit' in DSN
    prefix = f"set request.jwt.claim.sub = '{identity}';" if identity else ''
    if role:
        assert role in {'authenticated','anon'}
        prefix += f'set role {role};'
    result = subprocess.run(['psql', DSN, '-XAtq', '-v', 'ON_ERROR_STOP=1'],
                            input=prefix+query, text=True, capture_output=True, timeout=15)
    if check:
        assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope='module', autouse=True)
def schema():
    if not DSN:
        return
    sql((ROOT/'tests/fixtures/invoice_database.sql').read_text())
    sql((ROOT/'supabase/migrations/20261022140000_create_manual_invoice_ledger.sql').read_text())
    # Reproduce the production failure before applying the fix.
    seed()
    invoice_id = reserve()
    result = sql(finalize_sql(invoice_id), identity=ADMIN, role='authenticated', check=False)
    assert result.returncode != 0 and 'relation "clients" does not exist' in result.stderr
    sql((ROOT/'supabase/migrations/20261026083353_harden_manual_invoice_consistency.sql').read_text())


def seed():
    sql(f"""truncate public.invoice_ledger_entries, public.invoice_ledger, public.timesheets,
    public.projects, public.client_billing_profiles, public.clients, public.profiles, public.companies cascade;
    insert into companies(id) values ('{COMPANY}'),('{OTHER_COMPANY}');
    insert into profiles(id,company_id,role) values ('{ADMIN}','{COMPANY}','company_admin'),
      ('{OTHER_ADMIN}','{OTHER_COMPANY}','company_admin'),('{EMPLOYEE}','{COMPANY}','employee');
    insert into clients(id,company_id) values ('{CLIENT}','{COMPANY}');
    insert into client_billing_profiles(client_id,company_id) values ('{CLIENT}','{COMPANY}');
    insert into timesheets(id,company_id,client_id,user_id,work_date,hours_logged,description)
      values ('{ENTRY}','{COMPANY}','{CLIENT}','{EMPLOYEE}','2026-09-15',2.5,'Work');""")


@pytest.fixture(autouse=True)
def reset(schema):
    if DSN:
        seed()


def reserve_sql(invoice_id, *, start='2026-09-01', end='2026-09-30', model='hourly', quantity='2.5', entries=None, snapshot=None):
    entries = [ENTRY] if entries is None else entries
    snapshot = snapshot or {'billing_profile_updated_at':'2026-10-22T00:00:00Z','approved_entries':[
        {'id':ENTRY,'work_date':'2026-09-15','hours_logged':'2.5','description':'Work','project_name':'General Task'}]}
    def quote(value):
        return "'"+str(value).replace("'", "''")+"'"
    values=[invoice_id,COMPANY,CLIENT,start,end,'a'*64,'Test buyer',None,False,model,quantity,
            'hour','10000','27','HUF','25000','6750','31750',json.dumps(snapshot),json.dumps(entries)]
    return 'select * from public.reserve_manual_invoice('+','.join(
        'null' if v is None else 'false' if v is False else quote(v) for v in values)+');'


def reserve(**kwargs):
    invoice_id = str(uuid4())
    result=sql(reserve_sql(invoice_id,**kwargs), identity=ADMIN, role='authenticated')
    return result.stdout.strip()


def finalize_sql(invoice_id, net='25000', gross='31750', number='TEST-2026-1', pdf="'JVBERi1saXZl'"):
    return f"select * from public.finalize_manual_invoice('{invoice_id}','{number}',{net},{gross},{pdf});"


def test_success_is_atomic_and_replay_is_idempotent():
    invoice_id=reserve()
    for _ in range(2):
        sql(finalize_sql(invoice_id),identity=ADMIN,role='authenticated')
    assert sql('select status from timesheets').stdout.strip()=='invoiced'
    assert sql('select status || pdf_available::text from invoice_ledger').stdout.strip()=='issuedtrue'


@pytest.mark.parametrize('identity',[OTHER_ADMIN,EMPLOYEE])
def test_other_tenant_and_employee_cannot_reserve_or_read(identity):
    assert sql(reserve_sql(str(uuid4())),identity=identity,role='authenticated',check=False).returncode
    reserve()
    assert sql('select count(*) from invoice_ledger',identity=identity,role='authenticated').stdout.strip()=='0'


def test_anon_cannot_use_mutation_rpc():
    assert sql(reserve_sql(str(uuid4())),role='anon',check=False).returncode


@pytest.mark.parametrize('change',[
    "hours_logged=3", "description='Changed'", "work_date='2026-09-16'", "status='rejected'"
])
def test_reserved_timesheets_cannot_change(change):
    reserve()
    assert sql(f'update timesheets set {change}',identity=ADMIN,role='authenticated',check=False).returncode


def test_reserved_timesheet_cannot_be_deleted():
    reserve()
    assert sql('delete from timesheets',identity=ADMIN,role='authenticated',check=False).returncode


@pytest.mark.parametrize('change',["description='Changed'",'hours_logged=3',"work_date='2026-09-16'"])
def test_snapshot_changes_block_reservation(change):
    sql(f'update timesheets set {change}')
    assert sql(reserve_sql(str(uuid4())),identity=ADMIN,role='authenticated',check=False).returncode
    assert sql('select count(*) from invoice_ledger').stdout.strip()=='0'


def test_changed_billing_profile_blocks_reservation():
    sql("update client_billing_profiles set updated_at=now()")
    assert sql(reserve_sql(str(uuid4())),identity=ADMIN,role='authenticated',check=False).returncode


@pytest.mark.parametrize('net,gross,pdf', [('null','31750',"null"),('25000','null',"null"),('24999','31750',"null"),('25000','31750',"'bm90LWEtcGRm'")])
def test_invalid_confirmation_rolls_back(net,gross,pdf):
    invoice_id=reserve()
    assert sql(finalize_sql(invoice_id,net=net,gross=gross,pdf=pdf),identity=ADMIN,role='authenticated',check=False).returncode
    assert sql('select status from timesheets').stdout.strip()=='approved'
    assert sql('select status from invoice_ledger').stdout.strip()=='issuing'


def test_concurrent_reservations_only_allow_one():
    def attempt(_):
        return sql(reserve_sql(str(uuid4())),identity=ADMIN,role='authenticated',check=False).returncode
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(attempt,range(8)))
    assert results.count(0)==1
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip()=='1'


def test_overlapping_period_is_blocked_even_without_entries():
    invoice_id=reserve(model='fixed_monthly',quantity='1')
    sql(finalize_sql(invoice_id),identity=ADMIN,role='authenticated')
    assert sql(reserve_sql(str(uuid4()),start='2026-09-15',end='2026-10-15',model='fixed_monthly',quantity='1',entries=[]),identity=ADMIN,role='authenticated',check=False).returncode


def test_failed_attempt_releases_entries_but_uncertain_does_not():
    invoice_id=reserve()
    sql(f"select public.mark_manual_invoice_attempt('{invoice_id}','uncertain','timeout','Unknown')",identity=ADMIN,role='authenticated')
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip()=='1'
    assert sql(reserve_sql(str(uuid4())),identity=ADMIN,role='authenticated',check=False).returncode
    sql(f"select public.resolve_manual_invoice_not_issued('{invoice_id}')",identity=ADMIN,role='authenticated')
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip()=='0'
    assert reserve()==invoice_id


def test_stale_attempt_becomes_uncertain_without_releasing_entries():
    reserve()
    # Disable only the timestamp trigger to simulate an abandoned old worker.
    sql("alter table invoice_ledger disable trigger set_invoice_ledger_updated_at; update invoice_ledger set updated_at=now()-interval '11 minutes'; alter table invoice_ledger enable trigger set_invoice_ledger_updated_at;")
    sql(f"select public.expire_stale_manual_invoice_attempts('{COMPANY}')",identity=ADMIN,role='authenticated')
    assert sql('select status from invoice_ledger').stdout.strip()=='uncertain'
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip()=='1'


def test_preview_mode_blocks_direct_reservation_rpc():
    sql('update companies set szamlazz_test_mode=true')
    assert sql(reserve_sql(str(uuid4())),identity=ADMIN,role='authenticated',check=False).returncode


def test_finalization_still_works_after_client_is_archived():
    invoice_id=reserve()
    sql('update clients set is_active=false')
    sql(finalize_sql(invoice_id),identity=ADMIN,role='authenticated')
    assert sql('select status from timesheets').stdout.strip()=='invoiced'


def test_attempt_limit_is_enforced():
    invoice_id=reserve()
    for _ in range(4):
        sql(f"select public.mark_manual_invoice_attempt('{invoice_id}','failed','57','Rejected')",identity=ADMIN,role='authenticated')
        assert reserve()==invoice_id
    sql(f"select public.mark_manual_invoice_attempt('{invoice_id}','failed','57','Rejected')",identity=ADMIN,role='authenticated')
    assert sql(reserve_sql(str(uuid4())),identity=ADMIN,role='authenticated',check=False).returncode


def test_reservation_lock_blocks_a_concurrent_edit_until_guard_is_visible():
    invoice_id=str(uuid4())
    command=f"set request.jwt.claim.sub='{ADMIN}'; set role authenticated; begin; {reserve_sql(invoice_id)} select pg_sleep(0.4); commit;"
    process=subprocess.Popen(['psql',DSN,'-XAtq','-v','ON_ERROR_STOP=1'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        process.stdin.write(command)
        process.stdin.close()
        assert process.stdout.readline().strip()==invoice_id
        result=sql("update timesheets set description='Concurrent edit'",identity=ADMIN,role='authenticated',check=False)
        assert result.returncode != 0
        assert process.wait(timeout=5)==0,process.stderr.read()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    assert sql('select description from timesheets').stdout.strip()=='Work'


def test_missing_entry_and_duplicate_ids_cannot_reserve():
    for ids in [[],[ENTRY,ENTRY],[str(uuid4())]]:
        assert sql(reserve_sql(str(uuid4()),entries=ids),identity=ADMIN,role='authenticated',check=False).returncode
    assert sql('select count(*) from invoice_ledger').stdout.strip()=='0'


def test_unrelated_period_can_be_reserved_after_success():
    invoice_id=reserve()
    sql(finalize_sql(invoice_id),identity=ADMIN,role='authenticated')
    reserve(start='2026-10-01',end='2026-10-31',model='fixed_monthly',quantity='1',entries=[])
    assert sql('select count(*) from invoice_ledger').stdout.strip()=='2'


def test_issued_timesheets_are_immutable():
    invoice_id=reserve()
    sql(finalize_sql(invoice_id),identity=ADMIN,role='authenticated')
    assert sql("update timesheets set status='approved'",identity=ADMIN,role='authenticated',check=False).returncode


@pytest.mark.parametrize('identity',[OTHER_ADMIN,EMPLOYEE])
@pytest.mark.parametrize('operation',['finalize','mark','release','expire'])
def test_invoice_mutation_rpcs_reject_unauthorized_users(identity,operation):
    invoice_id=reserve()
    queries={
        'finalize':finalize_sql(invoice_id),
        'mark':f"select public.mark_manual_invoice_attempt('{invoice_id}','failed','57','Rejected')",
        'release':f"select public.resolve_manual_invoice_not_issued('{invoice_id}')",
        'expire':f"select public.expire_stale_manual_invoice_attempts('{COMPANY}')",
    }
    assert sql(queries[operation],identity=identity,role='authenticated',check=False).returncode
    assert sql('select status from invoice_ledger').stdout.strip()=='issuing'


def test_reservation_handles_2500_reviewed_entries():
    sql('delete from timesheets')
    sql(f"""insert into timesheets(id,company_id,client_id,user_id,work_date,hours_logged,description)
      select md5(number::text)::uuid,'{COMPANY}','{CLIENT}','{EMPLOYEE}','2026-09-15',0.01,'Work'
      from generate_series(1,2500) as number;""")
    invoice_id=str(uuid4())
    sql(f"""select * from reserve_manual_invoice('{invoice_id}','{COMPANY}','{CLIENT}',
      '2026-09-01','2026-09-30',repeat('a',64),'Test buyer',null,false,'hourly',25,
      'hour',10000,'27','HUF',250000,67500,317500,
      (select jsonb_build_object('billing_profile_updated_at','2026-10-22T00:00:00Z',
        'approved_entries',jsonb_agg(jsonb_build_object('id',id,'work_date',work_date,
          'hours_logged',hours_logged,'description',description,'project_name','General Task')))
       from timesheets), (select jsonb_agg(id) from timesheets));""",identity=ADMIN,role='authenticated')
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip()=='2500'
    sql(finalize_sql(invoice_id,net='250000',gross='317500'),identity=ADMIN,role='authenticated')
    assert sql("select count(*) from timesheets where status='invoiced'").stdout.strip()=='2500'
