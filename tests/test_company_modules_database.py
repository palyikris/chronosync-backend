"""Module boundaries on a disposable local PostgreSQL database."""
import json
import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT.parent / 'chronosync'
DSN = os.environ.get('MODULES_TEST_PG_DSN', '')
pytestmark = pytest.mark.skipif(not DSN, reason='requires isolated local PostgreSQL')
A, B = [f'20000000-0000-0000-0000-{i:012}' for i in (1, 2)]
ADMIN, EMPLOYEE, SUPER = [f'10000000-0000-0000-0000-{i:012}' for i in (1, 2, 3)]
CLIENT = '30000000-0000-0000-0000-000000000001'
PROJECT = '40000000-0000-0000-0000-000000000001'


def sql(query, identity=None, check=True):
    assert all(value in DSN for value in ('host=127.0.0.1', 'port=55440', 'dbname=modules_audit'))
    prefix = f"set request.jwt.claim.sub='{identity}'; set role authenticated;" if identity else ''
    result = subprocess.run(['psql', DSN, '-XAtq', '-v', 'ON_ERROR_STOP=1'],
        input=prefix + query, capture_output=True, text=True, encoding='utf-8', timeout=20)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def denied(query, identity=ADMIN):
    result = sql(query, identity, check=False)
    assert result.returncode != 0, result.stdout


@pytest.fixture(scope='module', autouse=True)
def schema():
    if not DSN:
        return
    sql((ROOT / 'tests/fixtures/security_database.sql').read_text(encoding='utf-8'))
    sql("drop table client_billing_profiles; alter table clients add invoice_attachment_language text default 'en',"
        "add available_hours_per_month numeric default 40, add hours_from_previous_month numeric default 0;")
    sql((FRONTEND / 'supabase/migrations/20261022090000_create_client_billing_profiles.sql').read_text(encoding='utf-8'))
    for name in [
        '20261022140000_create_manual_invoice_ledger.sql',
        '20261026083353_harden_manual_invoice_consistency.sql',
        '20261027091353_avoid_retryable_invoice_business_conflicts.sql',
        '20261027133954_secure_tenant_boundaries.sql',
        '20261027141923_protect_approved_leave_cancellation.sql',
        '20261027133957_trusted_invitations_and_atomic_leave.sql',
        '20261021000000_harden_invoice_attachment_finalization.sql',
    ]:
        sql((ROOT / 'supabase/migrations' / name).read_text(encoding='utf-8'))
    for name in ['20261028093502_create_project_pricing.sql', '20261028094448_preserve_original_project_price_units.sql']:
        sql((FRONTEND / 'supabase/migrations' / name).read_text(encoding='utf-8'))
    sql((ROOT / 'supabase/migrations/20261028101844_annual_project_price_indexing.sql').read_text(encoding='utf-8'))
    sql(f"insert into companies(id) values ('{A}');")
    sql((ROOT / 'supabase/migrations/20261103175015_company_modules.sql').read_text(encoding='utf-8'))
    assert json.loads(sql(f"select to_json(enabled_modules) from companies where id='{A}'").stdout) == ['leave_attachments', 'invoicing']


@pytest.fixture(autouse=True)
def seed(schema):
    if not DSN:
        return
    sql('truncate public.companies, auth.users cascade;')
    sql(f"insert into companies(id) values ('{A}'),('{B}');")
    for identity, role in [(ADMIN, 'company_admin'), (EMPLOYEE, 'regular'), (SUPER, 'super_admin')]:
        sql(f"insert into auth.users(id,email) values ('{identity}','{identity}@test.invalid');"
            f"update profiles set company_id='{A}',role='{role}',is_active=true where id='{identity}';")
    sql(f"insert into clients(id,company_id) values ('{CLIENT}','{A}');"
        f"insert into projects(id,company_id,client_id) values ('{PROJECT}','{A}','{CLIENT}');")


def enable(modules):
    sql(f"update companies set enabled_modules=array{modules!r}::text[] where id='{A}'", SUPER)


def test_new_company_is_base_only_and_only_super_admin_can_manage_modules():
    assert sql(f"select cardinality(enabled_modules) from companies where id='{A}'", ADMIN).stdout.strip() == '0'
    denied(f"update companies set enabled_modules=array['leave_attachments'] where id='{A}'")
    assert sql(f"update companies set enabled_modules=array['leave_attachments'] where id='{A}' returning id", EMPLOYEE).stdout.strip() == ''
    denied(f"update companies set enabled_modules=array['invoicing'] where id='{A}'", SUPER)
    denied(f"update companies set enabled_modules=array['unknown'] where id='{A}'", SUPER)
    enable(['leave_attachments'])
    assert sql(f"select private.company_module_enabled('{A}','leave_attachments')", ADMIN).stdout.strip() == 't'
    assert sql(f"select private.company_module_enabled('{B}','leave_attachments')", ADMIN).stdout.strip() == 'f'


def test_base_can_log_submit_and_review_but_cannot_use_optional_operations():
    sql(f"insert into timesheets(user_id,company_id,client_id,project_id,work_date,hours_logged)"
        f"values ('{EMPLOYEE}','{A}','{CLIENT}','{PROJECT}','2026-11-02',2);"
        "update timesheets set status='submitted';", EMPLOYEE)
    sql("update timesheets set status='approved';", ADMIN)
    assert sql('select status from timesheets', EMPLOYEE).stdout.strip() == 'approved'
    denied(f"insert into leave_requests(user_id,company_id,start_date,end_date) values ('{EMPLOYEE}','{A}','2026-12-01','2026-12-01')", EMPLOYEE)
    denied(f"select get_company_invoice_credentials('{A}')")
    denied(f"select set_company_invoice_credentials('{A}','szamlazz_hu','encrypted')")
    denied(f"select ensure_company_project_pricing('{A}')")
    denied(f"select finalize_invoice_attachment_period('{A}','2026-11-01','2026-11-30','[]')")
    assert sql(f"select private.can_manage_company_invoices('{A}')", ADMIN).stdout.strip() == 'f'
    denied(f"update companies set leave_client_id='{CLIENT}',leave_project_id='{PROJECT}' where id='{A}'")
    denied(f"update companies set szamlazz_test_mode=true where id='{A}'")


def test_first_module_supports_leave_but_cannot_invoice():
    enable(['leave_attachments'])
    sql(f"insert into leave_requests(user_id,company_id,start_date,end_date) values ('{EMPLOYEE}','{A}','2026-12-01','2026-12-01')", EMPLOYEE)
    request = sql('select id from leave_requests', ADMIN).stdout.strip()
    sql(f"select review_leave_request('{request}','REJECTED')", ADMIN)
    denied(f"select get_company_invoice_credentials('{A}')")
    denied(f"insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency)"
        f"values ('{PROJECT}','{A}',2026,100,'hour','HUF')")
    # The attachment RPC reaches its business validation with the first module.
    result = sql(f"select finalize_invoice_attachment_period('{A}','2026-11-01','2026-11-30','[]')", ADMIN, check=False)
    assert 'Between 1 and 100' in result.stderr


def test_full_modules_enable_pricing_and_credentials_and_disable_preserves_data():
    enable(['leave_attachments', 'invoicing'])
    sql(f"select set_company_invoice_credentials('{A}','szamlazz_hu','encrypted')", ADMIN)
    assert json.loads(sql(f"select get_company_invoice_credentials('{A}')", ADMIN).stdout)['invoice_api_key_encrypted'] == 'encrypted'
    sql(f"insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency)"
        f"values ('{PROJECT}','{A}',2026,100,'hour','HUF')", ADMIN)
    assert sql('select count(*) from project_pricing', ADMIN).stdout.strip() == '1'
    enable(['leave_attachments'])
    assert sql('select count(*) from project_pricing', ADMIN).stdout.strip() == '0'
    assert sql('select count(*) from project_pricing').stdout.strip() == '1'
    denied(f"select get_company_invoice_credentials('{A}')")
    enable(['leave_attachments', 'invoicing'])
    assert sql('select count(*) from project_pricing', ADMIN).stdout.strip() == '1'


def test_disabling_first_module_blocks_leave_rpc_and_keeps_existing_approved_date_locks():
    enable(['leave_attachments'])
    sql(f"insert into leave_requests(user_id,company_id,start_date,end_date,status) values"
        f"('{EMPLOYEE}','{A}','2026-12-01','2026-12-01','PENDING'),"
        f"('{EMPLOYEE}','{A}','2026-12-02','2026-12-02','APPROVED');")
    pending = sql("select id from leave_requests where status='PENDING'", ADMIN).stdout.strip()
    approved = sql("select id from leave_requests where status='APPROVED'", ADMIN).stdout.strip()
    enable([])
    assert sql('select count(*) from leave_requests', EMPLOYEE).stdout.strip() == '1'
    denied(f"select review_leave_request('{pending}','REJECTED')")
    denied(f"select cancel_leave_request('{approved}')", EMPLOYEE)
    denied(f"insert into timesheets(user_id,company_id,client_id,project_id,work_date,hours_logged)"
        f"values ('{EMPLOYEE}','{A}','{CLIENT}','{PROJECT}','2026-12-02',2)", EMPLOYEE)


def test_invoicing_cannot_be_disabled_during_issuance_or_reconciliation():
    enable(['leave_attachments', 'invoicing'])
    sql(f"set request.jwt.claim.sub='{ADMIN}';" + invoice_insert())
    for status in ['issuing', 'uncertain']:
        sql(f"update invoice_ledger set status='{status}'")
        denied(f"update companies set enabled_modules='{{}}' where id='{A}'", SUPER)
    sql("update invoice_ledger set status='failed'")
    enable([])
    assert sql('select count(*) from invoice_ledger', ADMIN).stdout.strip() == '0'
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '1'


def invoice_insert():
    return f"""insert into invoice_ledger(company_id,client_id,period_start,period_end,external_id,
        review_fingerprint,invoice_snapshot,recipient_name,billing_model,quantity,unit,net_unit_price,
        vat_code,currency,net_total,vat_total,gross_total,created_by)
      values ('{A}','{CLIENT}','2026-11-01','2026-11-30','module-test','fingerprint','{{}}','Buyer',
        'hourly',1,'hour',100,'AAM','HUF',100,0,100,'{ADMIN}');"""


def test_invoice_reservation_waits_for_module_change_and_then_rechecks_access():
    enable(['leave_attachments', 'invoicing'])
    blocker = subprocess.Popen(['psql', DSN, '-XAtq', '-v', 'ON_ERROR_STOP=1'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8')
    try:
        blocker.stdin.write(f"begin; update companies set enabled_modules='{{}}' where id='{A}';\n\\echo ready\n")
        blocker.stdin.flush()
        assert blocker.stdout.readline().strip() == 'ready'
        with ThreadPoolExecutor(max_workers=1) as executor:
            reservation = executor.submit(sql, "set application_name='module-reservation-test';"
                f"set request.jwt.claim.sub='{ADMIN}';" + invoice_insert(), None, False)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = sql("select count(*) from pg_stat_activity where application_name='module-reservation-test' and wait_event_type='Lock'").stdout.strip()
                    if waiting == '1':
                        break
                else:
                    pytest.fail('Invoice reservation did not wait for the company transaction')
            finally:
                _, stderr = blocker.communicate('commit;\n', timeout=5)
                assert blocker.returncode == 0, stderr
            result = reservation.result(timeout=5)
            assert result.returncode != 0 and 'Invoicing module is disabled' in result.stderr
        assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'
    finally:
        if blocker.poll() is None:
            blocker.communicate('rollback;\n', timeout=5)


def test_scheduled_rollover_skips_disabled_companies_and_catches_up_when_enabled():
    enable(['leave_attachments', 'invoicing'])
    sql(f"insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency,index_code)"
        f"values ('{PROJECT}','{A}',2025,100,'hour','HUF','none')", ADMIN)
    enable([])
    assert sql('select private.rollover_project_pricing()').stdout.strip() == '0'
    assert sql('select count(*) from project_pricing').stdout.strip() == '1'
    enable(['leave_attachments', 'invoicing'])
    assert int(sql('select private.rollover_project_pricing()').stdout.strip()) >= 1
