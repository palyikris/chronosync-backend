"""Opt-in regressions on an explicitly isolated, disposable local database."""
import json
import os
from pathlib import Path
import subprocess
from decimal import Decimal
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT.parent/'chronosync'
DSN = os.environ.get('PROJECT_PRICING_TEST_PG_DSN', '')
pytestmark = pytest.mark.skipif(not DSN, reason='requires isolated local PostgreSQL')
COMPANY, OTHER = [f'10000000-0000-4000-8000-{i:012}' for i in (1, 2)]
ADMIN, OTHER_ADMIN, WORKER = [f'20000000-0000-4000-8000-{i:012}' for i in (1, 2, 3)]
CLIENT = '30000000-0000-4000-8000-000000000001'
PROJECT = '40000000-0000-4000-8000-000000000001'
ENTRY = '50000000-0000-4000-8000-000000000001'


def sql(query, identity=None, check=True):
    local_host = 'host=/tmp/chronosync-invoice-audit' in DSN or (
        'host=127.0.0.1' in DSN and 'port=55439' in DSN)
    assert local_host and 'dbname=project_pricing_audit' in DSN
    prefix = f"set request.jwt.claim.sub='{identity}'; set role authenticated;" if identity else ''
    result = subprocess.run(['psql', DSN, '-XAtq', '-v', 'ON_ERROR_STOP=1'],
        input=prefix+query, text=True, capture_output=True, timeout=15)
    if check: assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope='module', autouse=True)
def schema():
    if not DSN: return
    sql((ROOT/'tests/fixtures/invoice_database.sql').read_text())
    sql("""alter table projects add company_id uuid references companies;
      alter table client_billing_profiles
        add billing_name text default 'Buyer', add billing_postal_code text default '1111',
        add billing_city text default 'Budapest', add billing_address text default 'Test street',
        add tax_subject smallint default 1, add tax_number text default '12345678-2-41',
        add eu_vat_number text, add send_invoice_email boolean default false, add billing_email text,
        add net_unit_price numeric, add billing_model text default 'hourly',
        add currency text default 'HUF', add invoice_unit text default 'óra',
        add invoice_item_name text default 'Szolgáltatás', add vat_code text default 'AAM',
        add payment_term_days integer default 8, add invoice_language text default 'hu',
        add payment_method text default 'Átutalás';""")
    for name in ['20261022140000_create_manual_invoice_ledger.sql',
                 '20261026083353_harden_manual_invoice_consistency.sql',
                 '20261027091353_avoid_retryable_invoice_business_conflicts.sql']:
        sql((ROOT/'supabase/migrations'/name).read_text())
    sql("""create function private.current_billing_admin_company_id() returns uuid
      language sql stable security definer set search_path='' as $$
        select p.company_id from public.profiles p where p.id=(select auth.uid())
          and private.can_manage_company_invoices(p.company_id) limit 1;
      $$;
      revoke all on function private.current_billing_admin_company_id() from public;
      grant usage on schema private to authenticated;
      grant execute on function private.current_billing_admin_company_id() to authenticated;""")
    for name in ['20261028093502_create_project_pricing.sql', '20261028094448_preserve_original_project_price_units.sql']:
        sql((FRONTEND/'supabase/migrations'/name).read_text())
    sql((ROOT/'supabase/migrations/20261028100422_invoice_from_project_pricing.sql').read_text())
    sql((ROOT/'supabase/migrations/20261028101844_annual_project_price_indexing.sql').read_text())
    sql("alter table clients add available_hours_per_month numeric default 100, add hours_from_previous_month numeric default 2, add invoice_attachment_language text default 'hu';")
    sql((ROOT/'supabase/migrations/20261021000000_harden_invoice_attachment_finalization.sql').read_text())
    sql((FRONTEND/'supabase/migrations/20261029193933_add_project_payroll_headcount.sql').read_text())
    sql((ROOT/'supabase/migrations/20261029193936_multiply_payroll_invoice_hours.sql').read_text())
    # Migration must also backfill archived entries without triggering edit guards.
    sql(f"""insert into companies(id) values ('{COMPANY}');
      insert into profiles(id,company_id,role) values ('{WORKER}','{COMPANY}','employee');
      insert into clients(id,company_id) values ('{CLIENT}','{COMPANY}');
      insert into projects(id,company_id,client_id,name,payroll_headcount)
        values ('{PROJECT}','{COMPANY}','{CLIENT}','Legacy payroll',3);
      insert into timesheets(id,company_id,client_id,user_id,project_id,work_date,hours_logged)
        values ('{ENTRY}','{COMPANY}','{CLIENT}','{WORKER}','{PROJECT}','2026-10-15',2.5);
      update projects set is_active=false;
      update clients set is_active=false;""")
    sql((FRONTEND/'supabase/migrations/20261030130954_move_payroll_multiplier_to_entries.sql').read_text())
    assert sql('select is_payroll from projects').stdout.strip() == 't'
    assert sql('select billing_multiplier from timesheets').stdout.strip() == '3'
    assert Decimal(sql('select hours_logged from timesheets').stdout.strip()) == Decimal('2.5')
    assert sql("select tgenabled from pg_trigger where tgname='enforce_active_client_on_timesheet'").stdout.strip() == 'O'
    sql((ROOT/'supabase/migrations/20261030130958_invoice_entry_multipliers.sql').read_text())
    sql((ROOT/'supabase/migrations/20261030142034_fix_attachment_conflict_retry.sql').read_text())
    sql((ROOT/'supabase/migrations/20261104194919_recurring_invoice_configuration.sql').read_text())
    sql((ROOT/'supabase/migrations/20261104201736_invoice_storno.sql').read_text())
    sql("alter table client_billing_profiles add attach_timesheet_report boolean not null default false;")
    sql((ROOT/'supabase/migrations/20261104204746_invoice_annex_delivery.sql').read_text())
    sql((ROOT/'supabase/migrations/20261110115432_client_invoice_terms.sql').read_text())
    sql("""alter table timesheets add leave_request_id uuid;
      create table public.leave_requests(user_id uuid, status text, start_date date, end_date date);
      grant select on public.leave_requests to authenticated;
      create trigger guard_approved_leave_timesheet before insert or update or delete on timesheets
        for each row execute function private.guard_approved_leave_timesheet();""")


@pytest.fixture(autouse=True)
def seed(schema):
    if not DSN: return
    sql(f"""truncate leave_requests,invoice_ledger_entries,invoice_ledger,timesheets,project_pricing,projects,
      client_billing_profiles,clients,profiles,companies cascade;
      insert into companies(id) values ('{COMPANY}'),('{OTHER}');
      insert into profiles(id,company_id,role) values ('{ADMIN}','{COMPANY}','company_admin'),
        ('{OTHER_ADMIN}','{OTHER}','company_admin'),('{WORKER}','{COMPANY}','employee');
      insert into clients(id,company_id) values ('{CLIENT}','{COMPANY}');
      insert into projects(id,company_id,client_id,name) values ('{PROJECT}','{COMPANY}','{CLIENT}','Consulting');
      insert into client_billing_profiles(client_id,company_id) values ('{CLIENT}','{COMPANY}');
      insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency,
        original_fee,original_currency,original_unit)
        values ('{PROJECT}','{COMPANY}',2026,100,'óra','HUF',90,'HUF','óra');
      insert into timesheets(id,company_id,client_id,user_id,project_id,work_date,hours_logged,description)
        values ('{ENTRY}','{COMPANY}','{CLIENT}','{WORKER}','{PROJECT}','2026-10-15',2.5,'Work');""")


def snapshot():
    price = json.loads(sql(f"select row_to_json(p) from project_pricing p where project_id='{PROJECT}'").stdout)
    dates = json.loads(sql("select json_build_object('issue', (now() at time zone 'Europe/Budapest')::date, 'due', (now() at time zone 'Europe/Budapest')::date + 8)").stdout)
    return {'billing_model': 'project', 'billing_profile_updated_at': '2026-10-22T00:00:00Z',
        'issue_date': dates['issue'], 'due_date': dates['due'], 'fulfillment_date': '2026-10-31',
        'e_invoice': False, 'invoice_comment': 'Elszámolási időszak: 2026-10-01 - 2026-10-31',
        'project_quantities': {}, 'approved_entries': [{'id': ENTRY, 'project_id': PROJECT,
            'work_date': '2026-10-15', 'hours_logged': '2.5', 'billing_multiplier': 1, 'description': 'Work', 'project_name': 'Consulting'}],
        'items': [{'is_payroll': False, 'project_id': PROJECT, 'project_name': 'Consulting', 'pricing_year': 2026,
            'pricing_updated_at': price['updated_at'], 'quantity': '2.5', 'unit': price['unit'],
            'net_unit_price': str(price['indexed_fee']), 'vat_code': 'AAM', 'currency': price['currency'],
            'net_total': '250', 'vat_total': '0', 'gross_total': '250'}]}


def reserve(review=None, *, model='project', quantity='1', net='250', vat='0', gross=None,
            currency='HUF', vat_code='AAM', start='2026-10-01', identity=ADMIN):
    review = snapshot() if review is None else review
    values = [str(uuid4()), COMPANY, CLIENT, start, '2026-10-31', 'a'*64, 'Buyer', None, False,
        model, quantity, 'számla', net, vat_code, currency, net, vat, gross or net, json.dumps(review),
        json.dumps([e['id'] for e in review['approved_entries']])]
    def quote(v): return 'null' if v is None else 'false' if v is False else "'"+str(v).replace("'", "''")+"'"
    return sql('select * from reserve_manual_invoice('+','.join(map(quote, values))+');', identity, check=False)


def test_invoice_terms_migration_preserves_defaults_and_private_permissions():
    assert sql("select payment_due_policy || ':' || payment_due_reference || ':' || payment_due_month_offset from client_billing_profiles").stdout.strip() == 'days_after_issue:issue_date:1'
    assert reserve().returncode == 0
    assert sql("select has_function_privilege('authenticated','private.validate_project_invoice_prices(uuid,uuid,date,date,jsonb,text,text,numeric,numeric,numeric)','execute')").stdout.strip() == 'f'


@pytest.mark.parametrize('reference,offset', [('issue_date', 0), ('issue_date', 1), ('issue_date', 2), ('period_end', 1), ('period_end', 2)])
def test_month_end_deadlines_are_revalidated_at_reservation(reference, offset):
    sql(f"update client_billing_profiles set payment_due_policy='month_end', payment_due_reference='{reference}', payment_due_month_offset={offset}, fulfillment_date_policy='due_date'")
    anchor = "(now() at time zone 'Europe/Budapest')::date" if reference == 'issue_date' else "date '2026-10-31'"
    due = sql(f"select (date_trunc('month',{anchor}) + make_interval(months=>{offset+1}) - interval '1 day')::date").stdout.strip()
    review = snapshot()
    review.update(due_date=due, fulfillment_date=due)
    assert reserve(review).returncode == 0
    stored = json.loads(sql('select invoice_snapshot from invoice_ledger').stdout)
    assert stored['due_date'] == stored['fulfillment_date'] == due


def test_invoice_note_is_checked_against_saved_profile_and_archived():
    sql("update client_billing_profiles set invoice_note='Approved services & annex'")
    review = snapshot()
    assert reserve(review).returncode != 0
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'
    review['invoice_comment'] += '\nApproved services & annex'
    assert reserve(review).returncode == 0
    assert json.loads(sql("select invoice_snapshot->'invoice_comment' from invoice_ledger").stdout) == review['invoice_comment']


def test_manual_dates_are_archived_and_do_not_change_approved_time():
    dates = json.loads(sql("select json_build_object('due',(now() at time zone 'Europe/Budapest')::date+20,'fulfillment',(now() at time zone 'Europe/Budapest')::date-2)").stdout)
    review = snapshot()
    review.update(due_date=dates['due'], fulfillment_date=dates['fulfillment'],
        date_overrides={'due_date': dates['due'], 'fulfillment_date': dates['fulfillment']})
    assert reserve(review).returncode == 0
    stored = json.loads(sql('select invoice_snapshot from invoice_ledger').stdout)
    assert stored['date_overrides'] == review['date_overrides']
    assert Decimal(sql('select hours_logged from timesheets').stdout.strip()) == Decimal('2.5')
    assert sql('select status from timesheets').stdout.strip() == 'approved'


@pytest.mark.parametrize('policy', ['days_after_issue', 'month_end'])
def test_backend_generated_dates_and_note_match_database_reservation(policy):
    from datetime import date
    from uuid import UUID
    from app.schemas.invoices import InvoiceDateOverrides, InvoiceReviewProjectLine
    from app.services.client_invoice_service import ClientInvoiceService

    sql(f"update client_billing_profiles set invoice_note='Approved services & annex', payment_due_policy='{policy}', payment_due_reference='period_end', payment_due_month_offset=2")
    profile = json.loads(sql('select row_to_json(p) from client_billing_profiles p').stdout)
    profile['billing_country_code'] = 'HU'
    review = snapshot()
    invoice, _ = ClientInvoiceService._build_invoice_request(company_id=UUID(COMPANY), client_id=UUID(CLIENT),
        period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        profile=profile, client={'client_code': 'TEST'},
        lines=[InvoiceReviewProjectLine.model_validate(review['items'][0])], preview=False,
        date_overrides=InvoiceDateOverrides(fulfillment_date=review['issue_date']))
    review.update(invoice_comment=invoice.comment, due_date=invoice.due_date.isoformat(),
        fulfillment_date=invoice.fulfillment_date.isoformat(),
        date_overrides={'fulfillment_date': review['issue_date']})
    assert reserve(review).returncode == 0


@pytest.mark.parametrize('expression', ["repeat('x',451)", "chr(1)", "chr(65534)", "'   '"])
def test_invalid_invoice_note_cannot_be_saved(expression):
    assert sql(f'update client_billing_profiles set invoice_note={expression}', check=False).returncode != 0


@pytest.mark.parametrize('change', ['past_due', 'far_due', 'far_fulfillment', 'mismatched_due', 'unknown_override', 'invalid_override'])
def test_date_tampering_never_reserves_work(change):
    today = sql("select (now() at time zone 'Europe/Budapest')::date").stdout.strip()
    past = sql("select (now() at time zone 'Europe/Budapest')::date-1").stdout.strip()
    far = sql("select (now() at time zone 'Europe/Budapest')::date+367").stdout.strip()
    review = snapshot()
    if change == 'past_due': review.update(due_date=past, date_overrides={'due_date': past})
    if change == 'far_due': review.update(due_date=far, date_overrides={'due_date': far})
    if change == 'far_fulfillment': review.update(fulfillment_date=far, date_overrides={'fulfillment_date': far})
    if change == 'mismatched_due': review['date_overrides'] = {'due_date': today}
    if change == 'unknown_override': review['date_overrides'] = {'issue_date': today}
    if change == 'invalid_override': review['date_overrides'] = {'due_date': 1795132800}
    assert reserve(review).returncode != 0
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip() == '0'


@pytest.mark.parametrize('identity', [OTHER_ADMIN, WORKER])
def test_new_terms_do_not_bypass_invoice_admin_or_tenant_access(identity):
    review = snapshot()
    review['date_overrides'] = {'fulfillment_date': review['issue_date']}
    review['fulfillment_date'] = review['issue_date']
    assert reserve(review, identity=identity).returncode != 0
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'


def test_new_reservation_uses_project_rate_and_preserves_reviewed_lines():
    result = reserve()
    assert result.returncode == 0, result.stderr
    assert sql("select billing_model || ':' || net_total::text from invoice_ledger").stdout.strip() == 'project:250.0000'
    sql(f"update project_pricing set indexed_fee=200 where project_id='{PROJECT}'")
    assert Decimal(sql("select invoice_snapshot->'items'->0->>'net_unit_price' from invoice_ledger").stdout.strip()) == 100
    invoice_id = result.stdout.strip()
    sql(f"select * from finalize_manual_invoice('{invoice_id}','TEST-PROJECT-1',250,250,'JVBERi1saXZl')", ADMIN)
    assert sql('select status from timesheets').stdout.strip() == 'invoiced'


@pytest.mark.parametrize('change', ['indexed_fee=101', 'indexed_fee=null', "unit='db'", "currency='EUR'"])
def test_changes_between_review_and_reservation_abort_before_any_reservation(change):
    review = snapshot()
    sql(f"update project_pricing set {change} where project_id='{PROJECT}'")
    result = reserve(review)
    assert result.returncode != 0 and 'Project prices changed' in result.stderr
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'


@pytest.mark.parametrize('field,value', [('net_unit_price','1'), ('quantity','99'), ('net_total','1')])
def test_rpc_cannot_override_project_prices_hours_or_totals(field, value):
    review = snapshot()
    review['items'][0][field] = value
    assert reserve(review).returncode != 0
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'


def test_legacy_pricing_cannot_be_written_or_used_for_new_invoices():
    changed = sql('update client_billing_profiles set net_unit_price=1', check=False)
    assert changed.returncode != 0 and 'project pricing' in changed.stderr
    assert reserve(model='hourly').returncode != 0


def test_missing_project_and_other_tenant_or_employee_cannot_reserve():
    review = snapshot()
    review['items'][0]['project_id'] = str(uuid4())
    assert reserve(review).returncode != 0
    for identity in [OTHER_ADMIN, WORKER]:
        result = reserve(identity=identity)
        assert result.returncode != 0 and 'Not authorized' in result.stderr


def test_monthly_price_requires_explicit_quantity_and_whole_month():
    sql(f"update project_pricing set unit='hó' where project_id='{PROJECT}'; delete from timesheets;")
    review = snapshot()
    review['approved_entries'] = []
    review['items'][0].update(quantity='1', unit='hó', net_total='100', gross_total='100')
    assert reserve(review, net='100').returncode != 0
    review['project_quantities'] = {PROJECT: '1'}
    assert reserve(review, net='100', start='2026-10-02').returncode != 0
    result = reserve(review, net='100')
    assert result.returncode == 0, result.stderr


def test_database_rejects_omitted_monthly_service_without_timesheets():
    monthly = '40000000-0000-4000-8000-000000000002'
    sql(f"""insert into projects(id,company_id,client_id,name)
      values ('{monthly}','{COMPANY}','{CLIENT}','Monthly service');
      insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency,recurring_quantity)
      values ('{monthly}','{COMPANY}',2026,500,'hó','HUF',1);""")
    result = reserve()
    assert result.returncode != 0 and 'recurring project is missing' in result.stderr
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'


@pytest.mark.parametrize('field,value', [
    ('e_invoice', True), ('fulfillment_date', '2026-10-30'),
    ('invoice_prefix', 'FORGED'), ('seller_bank_account', 'FORGED'),
])
def test_database_rejects_changed_document_settings(field, value):
    review = snapshot()
    review[field] = value
    result = reserve(review)
    assert result.returncode != 0 and 'document or date settings changed' in result.stderr


def test_database_accepts_configured_electronic_invoice_and_due_date_fulfillment():
    sql("""update client_billing_profiles set invoice_electronic=true,
      fulfillment_date_policy='due_date',invoice_prefix='E-ECS',
      seller_bank_name='Test bank',seller_bank_account='TEST-IBAN'""")
    review = snapshot()
    review.update(e_invoice=True, fulfillment_date=review['due_date'], invoice_prefix='E-ECS',
                  seller_bank_name='Test bank', seller_bank_account='TEST-IBAN')
    result = reserve(review)
    assert result.returncode == 0, result.stderr


def test_recurring_invoice_can_reserve_without_any_time_entries():
    sql("delete from timesheets; update project_pricing set unit='hó',recurring_quantity=1")
    review = snapshot()
    review['approved_entries'] = []
    review['project_quantities'] = {PROJECT: '1'}
    review['items'][0].update(unit='hó', quantity='1', net_total='100', gross_total='100')
    result = reserve(review, net='100')
    assert result.returncode == 0, result.stderr
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip() == '0'
    assert sql('select net_total from invoice_ledger').stdout.strip() == '100.0000'


def test_recurring_quantity_rolls_over_and_cannot_be_saved_on_hourly_price():
    rejected = sql('update project_pricing set recurring_quantity=1', check=False)
    assert rejected.returncode != 0
    sql("update project_pricing set unit='/fő/hó', recurring_quantity=26, invoice_name='Payroll', index_code='none'")
    sql(f"select private.rollover_company_project_pricing('{COMPANY}',2027)")
    data = json.loads(sql("select json_build_object('quantity',recurring_quantity,'name',invoice_name) from project_pricing where pricing_year=2027").stdout)
    assert data == {'quantity': 26, 'name': 'Payroll'}


def annual_prices():
    return json.loads(sql(f"select json_agg(p order by pricing_year) from project_pricing p where project_id='{PROJECT}'").stdout)


def test_annual_rollover_compounds_is_idempotent_and_keeps_original_price():
    sql(f"update project_pricing set index_code='ksh' where project_id='{PROJECT}';"
        f"insert into company_price_indexes values ('{COMPANY}','ksh',2026,0.044,null,now(),now());")
    sql(f"select private.rollover_company_project_pricing('{COMPANY}',2028)")
    prices = annual_prices()
    assert [Decimal(str(p['indexed_fee'])) for p in prices] == [Decimal('100'),Decimal('104.4'),Decimal('108.9936')]
    assert all(p['original_fee'] == 90 and p['original_currency'] == 'HUF' and p['original_unit'] == 'óra' for p in prices)
    assert prices[-1]['price_origin'] == 'rollover' and prices[-1]['generated_from_year'] == 2027
    assert Decimal(str(prices[-1]['applied_index_rate'])) == Decimal('.044')
    assert sql(f"select private.rollover_company_project_pricing('{COMPANY}',2028)").stdout.strip() == '0'
    assert annual_prices() == prices


@pytest.mark.parametrize('rule,fee,rate,expected', [('ksh',None,'.044',None), ('ksh',0,'.044',0),
    ('eu',100,'.024',102.4), ('none',100,None,100), (None,100,None,None), ('eu',100,None,None)])
def test_annual_rollover_preserves_missing_and_zero_and_needs_configured_rule(rule,fee,rate,expected):
    rule_sql = "null" if rule is None else "'"+rule+"'"
    fee_sql = 'null' if fee is None else str(fee)
    sql(f"update project_pricing set index_code={rule_sql},indexed_fee={fee_sql}")
    if rate is not None:
        sql(f"insert into company_price_indexes(company_id,index_code,effective_year,rate) values ('{COMPANY}','{rule}',2026,{rate})")
    sql(f"select private.rollover_company_project_pricing('{COMPANY}',2027)")
    assert annual_prices()[-1]['indexed_fee'] == expected


def test_next_year_index_changes_never_reprice_current_year_or_overwrite_explicit_next_year_price():
    year = int(sql("select extract(year from now() at time zone 'Europe/Budapest')::int").stdout.strip())
    sql(f"update project_pricing set pricing_year={year},index_code='ksh';"
        f"insert into company_price_indexes(company_id,index_code,effective_year,rate) values ('{COMPANY}','ksh',{year},.044);")
    before = annual_prices()
    sql(f"insert into company_price_indexes(company_id,index_code,effective_year,rate) values ('{COMPANY}','ksh',{year+1},.05)", ADMIN)
    assert annual_prices() == before
    sql(f"select private.rollover_company_project_pricing('{COMPANY}',{year+1})")
    assert annual_prices()[-1]['indexed_fee'] == 105
    sql(f"update project_pricing set indexed_fee=200 where pricing_year={year+1}", ADMIN)
    sql(f"update company_price_indexes set rate=.06 where effective_year={year+1}", ADMIN)
    sql(f"select private.rollover_company_project_pricing('{COMPANY}',{year+1})")
    assert annual_prices()[-1]['indexed_fee'] == 200
    assert annual_prices()[0] == before[0]


def test_index_settings_and_rollover_are_tenant_admin_only_and_past_rates_are_immutable():
    year = int(sql("select extract(year from now() at time zone 'Europe/Budapest')::int").stdout.strip())
    for identity in [OTHER_ADMIN, WORKER]:
        assert sql(f"select ensure_company_project_pricing('{COMPANY}')", identity, check=False).returncode != 0
        assert sql(f"insert into company_price_indexes(company_id,index_code,effective_year,rate) values ('{COMPANY}','ksh',{year+1},.04)", identity, check=False).returncode != 0
    assert sql(f"insert into company_price_indexes(company_id,index_code,effective_year,rate) values ('{COMPANY}','ksh',{year},.04)", ADMIN, check=False).returncode != 0
    assert sql(f"select private.rollover_company_project_pricing('{COMPANY}',{year+1})", ADMIN, check=False).returncode != 0
    assert sql(f"select ensure_company_project_pricing('{COMPANY}')", ADMIN).returncode == 0


def test_year_boundary_uses_budapest_and_annual_history_retains_effective_rates():
    assert sql("select extract(year from timestamptz '2026-12-31 23:00:00+00' at time zone 'Europe/Budapest')::int").stdout.strip() == '2027'
    sql(f"update project_pricing set index_code='ksh';insert into company_price_indexes(company_id,index_code,effective_year,rate) values ('{COMPANY}','ksh',2026,.044),('{COMPANY}','ksh',2028,.05)")
    sql(f"select private.rollover_company_project_pricing('{COMPANY}',2028)")
    assert [Decimal(str(p['indexed_fee'])) for p in annual_prices()] == [Decimal('100'),Decimal('104.4'),Decimal('109.62')]
    assert sql("select count(*) from company_price_indexes where effective_year=2027 and rate=.044").stdout.strip() == '1'


@pytest.mark.parametrize('currency,tax_subject,fee,net,vat,gross', [
    ('HUF',1,'100.6','252','68','320'),('HUF',-1,'100.6','251','68','319'),
    ('EUR',1,'100.6','251.50','67.91','319.41')])
def test_database_price_validation_matches_provider_rounding(currency,tax_subject,fee,net,vat,gross):
    sql(f"update project_pricing set indexed_fee={fee},currency='{currency}';"
        f"update client_billing_profiles set vat_code='27',tax_subject={tax_subject}")
    review = snapshot()
    review['items'][0].update(net_unit_price=fee, currency=currency, vat_code='27',
        net_total=net, vat_total=vat, gross_total=gross)
    result = reserve(review,net=net,vat=vat,gross=gross,currency=currency,vat_code='27')
    assert result.returncode == 0, result.stderr


def test_every_approved_project_is_required_and_multiline_prices_are_validated():
    second='40000000-0000-4000-8000-000000000002'
    entry='50000000-0000-4000-8000-000000000002'
    sql(f"insert into projects(id,company_id,client_id,name) values ('{second}','{COMPANY}','{CLIENT}','Other work');"
        f"insert into timesheets(id,company_id,client_id,user_id,project_id,work_date,hours_logged,description) "
        f"values ('{entry}','{COMPANY}','{CLIENT}','{WORKER}','{second}','2026-10-15',2,'Work')")
    review=snapshot()
    review['approved_entries'].append(dict(id=entry, project_id=second, work_date='2026-10-15',
        hours_logged='2',billing_multiplier=1,description='Work',project_name='Other work'))
    assert reserve(review).returncode != 0
    sql(f"insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency) "
        f"values ('{second}','{COMPANY}',2026,50,'óra','HUF')")
    price=json.loads(sql(f"select row_to_json(p) from project_pricing p where project_id='{second}'").stdout)
    review['items'].append(dict(review['items'][0], project_id=second, project_name='Other work',
        pricing_updated_at=price['updated_at'],quantity='2',net_unit_price='50',net_total='100',gross_total='100'))
    result=reserve(review,net='350')
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('multiplier', [0, -1, 'null', 2147483648])
def test_database_rejects_invalid_entry_multiplier(multiplier):
    sql('update projects set is_payroll=true')
    result = sql(f"update timesheets set billing_multiplier={multiplier}", check=False)
    assert result.returncode != 0


def test_ordinary_projects_cannot_receive_a_multiplier_above_one():
    result = sql('update timesheets set billing_multiplier=3', WORKER, check=False)
    assert result.returncode != 0 and 'only available for payroll projects' in result.stderr


def test_payroll_invoice_reservation_freezes_the_entry_multiplier():
    sql('update projects set is_payroll=true; update timesheets set billing_multiplier=12')
    review = snapshot()
    review['approved_entries'][0]['billing_multiplier'] = 12
    review['items'][0].update(is_payroll=True, quantity='30', net_total='3000', gross_total='3000')
    result = reserve(review, net='3000')
    assert result.returncode == 0, result.stderr
    assert Decimal(sql('select hours_logged from timesheets').stdout.strip()) == Decimal('2.5')
    changed = sql('update timesheets set billing_multiplier=20', check=False)
    assert changed.returncode != 0 and 'reserved for an invoice' in changed.stderr
    # Preserve the hosted non-retryable conflict code when freezing a factor.
    sql("""do $$ begin
      update timesheets set billing_multiplier=20;
      raise exception 'A reserved multiplier unexpectedly changed';
    exception when sqlstate 'PT409' then null;
    end $$;""")
    sql('update projects set is_payroll=false, payroll_headcount=999')
    saved = json.loads(sql("select invoice_snapshot from invoice_ledger").stdout)
    assert saved['approved_entries'][0]['billing_multiplier'] == 12
    assert saved['items'][0]['quantity'] == '30'
    invoice_id = result.stdout.strip()
    sql(f"select * from finalize_manual_invoice('{invoice_id}','TEST-FACTOR-1',3000,3000,'JVBERi1saXZl')", ADMIN)
    assert sql('select status from timesheets').stdout.strip() == 'invoiced'
    assert sql('update timesheets set billing_multiplier=1', check=False).returncode != 0


def test_payroll_invoice_rejects_tampered_quantity_and_changed_entry_factor():
    sql('update projects set is_payroll=true')
    review = snapshot()
    review['items'][0]['is_payroll'] = True
    sql('update timesheets set billing_multiplier=12')
    assert reserve(review).returncode != 0  # Factor changed after review.
    review['approved_entries'][0]['billing_multiplier'] = 12
    assert reserve(review).returncode != 0  # Unmultiplied quantity is forbidden.
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'


def test_distinct_entry_factors_are_summed_and_swapping_them_requires_review():
    second = '50000000-0000-4000-8000-000000000002'
    sql(f"""update projects set is_payroll=true;
      update timesheets set billing_multiplier=3;
      insert into timesheets(id,company_id,client_id,user_id,project_id,work_date,hours_logged,description,billing_multiplier)
        values ('{second}','{COMPANY}','{CLIENT}','{WORKER}','{PROJECT}','2026-10-15',2.5,'Work',7);""")
    review = snapshot()
    review['approved_entries'][0]['billing_multiplier'] = 3
    review['approved_entries'].append(dict(review['approved_entries'][0], id=second, billing_multiplier=7))
    review['items'][0].update(is_payroll=True, quantity='25', net_total='2500', gross_total='2500')
    # Keep the same aggregate total, but change the distribution across entries.
    sql('update timesheets set billing_multiplier=10-billing_multiplier')
    assert reserve(review, net='2500').returncode != 0
    sql('update timesheets set billing_multiplier=10-billing_multiplier')
    result = reserve(review, net='2500')
    assert result.returncode == 0, result.stderr


def test_payroll_attachment_finalization_matches_entry_factors_and_project_changes():
    sql('update projects set is_payroll=true; update timesheets set billing_multiplier=12; update projects set is_payroll=false')
    updates = json.dumps([dict(client_id=CLIENT, starting_hours=2, available_hours=100,
        used_hours=30, remaining_hours=72)])
    query = f"select * from finalize_invoice_attachment_period('{COMPANY}','2026-10-01','2026-10-31','{updates}')"
    assert 't' in sql(query, ADMIN).stdout
    assert Decimal(sql('select hours_from_previous_month from clients').stdout.strip()) == 72
    assert 'f' in sql(query, ADMIN).stdout  # Re-download never applies carry-over twice.
    assert Decimal(sql('select hours_from_previous_month from clients').stdout.strip()) == 72


@pytest.mark.parametrize('already_finalized', [False, True])
def test_stale_multiplier_attachment_returns_nonretryable_conflict(already_finalized):
    sql('update projects set is_payroll=true; update timesheets set billing_multiplier=12')
    updates = [dict(client_id=CLIENT, starting_hours=2, available_hours=100,
        used_hours=30, remaining_hours=72)]
    def command(values):
        return f"public.finalize_invoice_attachment_period('{COMPANY}','2026-10-01','2026-10-31','{json.dumps(values)}')"
    if already_finalized:
        sql('select * from ' + command(updates), ADMIN)
    # Simulate an attachment built before an entry's multiplier changed.
    updates[0].update(used_hours=25, remaining_hours=77)
    sql(f"""do $$ begin
      perform * from {command(updates)};
      raise exception 'A stale multiplier attachment unexpectedly finalized';
    exception when sqlstate 'PT409' then null;
    end $$;""", ADMIN)
    expected = 72 if already_finalized else 2
    assert Decimal(sql('select hours_from_previous_month from clients').stdout.strip()) == expected
    assert sql('select count(*) from invoice_attachment_periods').stdout.strip() == str(int(already_finalized))


def test_multiplier_only_edits_respect_leave_locks():
    sql(f"""update projects set is_payroll=true;
      insert into leave_requests values ('{WORKER}','APPROVED','2026-10-15','2026-10-15');""")
    result = sql('update timesheets set billing_multiplier=3', WORKER, check=False)
    assert result.returncode != 0 and 'locked by approved leave' in result.stderr


def issued_storno_source():
    result = reserve()
    assert result.returncode == 0, result.stderr
    invoice_id = result.stdout.strip()
    sql(f"select * from finalize_manual_invoice('{invoice_id}','TEST-ORIGINAL',250,250,'JVBERi1saXZl')", ADMIN)
    return invoice_id


def reserve_storno(invoice_id, request_id=None, identity=ADMIN):
    request_id = request_id or str(uuid4())
    result = sql(f"select * from reserve_invoice_storno('{invoice_id}','{request_id}',"
        "'TEST-ORIGINAL','Correction',(now() at time zone 'Europe/Budapest')::date,false)", identity, check=False)
    return request_id, result


def finalize_storno(invoice_id, request_id, *, net='-250', gross='-250', pdf='JVBERi1zdG9ybm8=', identity=ADMIN):
    request = f"'{request_id}'" if request_id else 'null'
    return sql(f"select * from finalize_invoice_storno('{invoice_id}',{request},'TEST-STORNO',"
        f"{net},{gross},'{pdf}','Correction',(now() at time zone 'Europe/Budapest')::date,false)", identity, check=False)


def test_storno_retains_both_documents_and_snapshots_and_allows_reviewed_replacement():
    invoice_id = issued_storno_source()
    sql('update client_billing_profiles set automatic_invoicing_enabled=true')
    request_id, result = reserve_storno(invoice_id)
    assert result.returncode == 0, result.stderr
    # The original and all billed entries stay locked during cancellation.
    assert reserve().returncode != 0
    assert sql("update timesheets set status='approved'", ADMIN, check=False).returncode != 0
    result = finalize_storno(invoice_id, request_id)
    assert result.returncode == 0, result.stderr
    assert sql('select status from timesheets').stdout.strip() == 'approved'
    assert sql("select invoice_number||':'||reversal_invoice_number||':'||status from invoice_ledger").stdout.strip() == 'TEST-ORIGINAL:TEST-STORNO:reversed'
    assert sql('select count(*) from invoice_ledger_entries where released_at is not null').stdout.strip() == '1'
    assert sql('select pdf_bytes is not null and reversal_pdf_bytes is not null from invoice_ledger').stdout.strip() == 't'
    assert sql('select automatic_invoicing_enabled from client_billing_profiles').stdout.strip() == 'f'
    replacement = reserve()
    assert replacement.returncode == 0, replacement.stderr
    assert replacement.stdout.strip() != invoice_id
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '2'
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip() == '2'
    # A late duplicate finalization cannot release the replacement's reservation.
    assert finalize_storno(invoice_id, request_id).returncode == 0
    assert sql('select count(*) from invoice_ledger_entries where released_at is null').stdout.strip() == '1'
    assert sql("update timesheets set description='changed'", ADMIN, check=False).returncode != 0


@pytest.mark.parametrize('state', ['reversing','uncertain'])
def test_storno_duplicate_request_and_rebilling_stay_blocked(state):
    invoice_id = issued_storno_source()
    request_id, result = reserve_storno(invoice_id)
    assert result.returncode == 0
    if state == 'uncertain':
        sql(f"select mark_invoice_storno_attempt('{invoice_id}','{request_id}','uncertain','timeout')", ADMIN)
    assert reserve_storno(invoice_id)[1].returncode != 0
    assert reserve().returncode != 0
    assert sql('select status from timesheets').stdout.strip() == 'invoiced'
    assert sql('select count(*) from invoice_ledger_entries where released_at is null').stdout.strip() == '1'


@pytest.mark.parametrize('override', [{'net':'250'},{'gross':'-249'},{'pdf':'YmFk'}])
def test_invalid_storno_confirmation_rolls_back_without_releasing_work(override):
    invoice_id = issued_storno_source()
    request_id, result = reserve_storno(invoice_id)
    assert result.returncode == 0
    assert finalize_storno(invoice_id, request_id, **override).returncode != 0
    assert sql('select status from invoice_ledger').stdout.strip() == 'issued'
    assert sql('select status from timesheets').stdout.strip() == 'invoiced'
    assert sql('select count(*) from invoice_ledger_entries where released_at is null').stdout.strip() == '1'


@pytest.mark.parametrize('identity', [OTHER_ADMIN, WORKER])
def test_storno_tenant_and_employee_authorization(identity):
    invoice_id = issued_storno_source()
    assert reserve_storno(invoice_id, identity=identity)[1].returncode != 0
    request_id, result = reserve_storno(invoice_id)
    assert result.returncode == 0
    assert finalize_storno(invoice_id, request_id, identity=identity).returncode != 0
    assert sql(f"select mark_invoice_storno_attempt('{invoice_id}','{request_id}','failed','3')", identity, check=False).returncode != 0
    assert sql(f"select resolve_invoice_storno_not_reversed('{invoice_id}','{request_id}')", identity, check=False).returncode != 0


def test_storno_stale_worker_cannot_finalize_a_new_attempt():
    invoice_id = issued_storno_source()
    previous_id, _ = reserve_storno(invoice_id)
    sql(f"select mark_invoice_storno_attempt('{invoice_id}','{previous_id}','uncertain','timeout')", ADMIN)
    sql(f"select resolve_invoice_storno_not_reversed('{invoice_id}','{previous_id}')", ADMIN)
    current_id, result = reserve_storno(invoice_id)
    assert result.returncode == 0
    assert finalize_storno(invoice_id, previous_id).returncode != 0
    assert finalize_storno(invoice_id, current_id).returncode == 0


def test_storno_expiry_retains_reservations_and_allows_verified_recovery():
    invoice_id = issued_storno_source()
    request_id, _ = reserve_storno(invoice_id)
    sql("update invoice_ledger set reversal_started_at=now()-interval '11 minutes'")
    sql(f"select expire_stale_manual_invoice_attempts('{COMPANY}')", ADMIN)
    assert sql('select reversal_status from invoice_ledger').stdout.strip() == 'uncertain'
    assert reserve_storno(invoice_id)[1].returncode != 0
    assert finalize_storno(invoice_id, request_id).returncode == 0


def test_storno_can_be_recorded_from_provider_without_a_creation_attempt():
    invoice_id = issued_storno_source()
    result = finalize_storno(invoice_id, None)
    assert result.returncode == 0, result.stderr
    assert sql('select status from timesheets').stdout.strip() == 'approved'


def test_storno_releases_work_even_for_archived_client_and_project():
    invoice_id = issued_storno_source()
    request_id, _ = reserve_storno(invoice_id)
    sql('update clients set is_active=false; update projects set is_active=false;')
    result = finalize_storno(invoice_id, request_id)
    assert result.returncode == 0, result.stderr
    assert sql('select status from timesheets').stdout.strip() == 'approved'


def test_storno_preview_mode_blocks_provider_mutation_but_permits_recording_existing():
    invoice_id = issued_storno_source()
    sql('update companies set szamlazz_test_mode=true')
    assert reserve_storno(invoice_id)[1].returncode != 0
    assert finalize_storno(invoice_id, None).returncode == 0


def test_storno_five_attempt_limit_never_unlocks_original():
    invoice_id = issued_storno_source()
    for _ in range(5):
        request_id, result = reserve_storno(invoice_id)
        assert result.returncode == 0
        sql(f"select mark_invoice_storno_attempt('{invoice_id}','{request_id}','failed','3')", ADMIN)
    assert reserve_storno(invoice_id)[1].returncode != 0
    assert reserve().returncode != 0
    assert sql('select status from timesheets').stdout.strip() == 'invoiced'


def test_storno_original_pdf_can_still_be_recovered_after_cancellation():
    invoice_id = issued_storno_source()
    request_id, _ = reserve_storno(invoice_id)
    assert finalize_storno(invoice_id, request_id).returncode == 0
    sql('update invoice_ledger set pdf_bytes=null,pdf_available=false')
    sql(f"select store_manual_invoice_pdf('{invoice_id}','TEST-ORIGINAL',250,250,'JVBERi1vcmlnaW5hbA==')", ADMIN)
    assert sql('select pdf_available and reversal_pdf_available from invoice_ledger').stdout.strip() == 't'


def test_only_one_concurrent_storno_reservation_succeeds():
    from concurrent.futures import ThreadPoolExecutor
    invoice_id = issued_storno_source()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve_storno(invoice_id)[1].returncode, range(2)))
    assert sorted(results) == [0, 3]
    assert sql('select reversal_attempt_count from invoice_ledger').stdout.strip() == '1'


def test_storno_does_not_grant_direct_ledger_writes_or_anonymous_rpcs():
    invoice_id = issued_storno_source()
    assert sql(f"update invoice_ledger set status='reversed' where id='{invoice_id}'", ADMIN, check=False).returncode != 0
    assert sql("select has_function_privilege('anon','public.reserve_invoice_storno(uuid,uuid,text,text,date,boolean)','execute')").stdout.strip() == 'f'
    assert sql("select has_function_privilege('anon','public.finalize_invoice_storno(uuid,uuid,text,numeric,numeric,text,text,date,boolean)','execute')").stdout.strip() == 'f'


def test_storno_of_monthly_invoice_without_time_entries_releases_period():
    sql("delete from timesheets; update project_pricing set unit='hó',recurring_quantity=1;")
    review = snapshot()
    review['approved_entries'] = []
    review['project_quantities'] = {PROJECT:'1'}
    review['items'][0].update(quantity='1',unit='hó',net_total='100',gross_total='100')
    result = reserve(review,net='100')
    assert result.returncode == 0, result.stderr
    invoice_id = result.stdout.strip()
    sql(f"select * from finalize_manual_invoice('{invoice_id}','TEST-ORIGINAL',100,100,'JVBERi1saXZl')", ADMIN)
    request_id, result = reserve_storno(invoice_id)
    assert result.returncode == 0
    result = finalize_storno(invoice_id,request_id,net='-100',gross='-100')
    assert result.returncode == 0, result.stderr
    assert sql('select count(*) from invoice_ledger_entries').stdout.strip() == '0'
    assert reserve(review,net='100').returncode == 0


def annex_reservation(*, identity=ADMIN, language='hu'):
    import base64,hashlib
    # The storage contract verifies identity/bytes; workbook semantics are covered
    # with real XLSX files in test_invoice_annex.py.
    content=b'PK\x03\x04reviewed-report-contents'
    metadata={'filename':'invoice-annex-TEST-2026-10-01-2026-10-31.xlsx',
        'media_type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'size_bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),'language':language}
    sql('update client_billing_profiles set attach_timesheet_report=true;')
    review=snapshot()
    review.update(attach_timesheet_report=True,attachment=metadata)
    result=reserve(review,identity=identity)
    return result,review,base64.b64encode(content).decode()


@pytest.fixture
def mandatory_attachment():
    sql((ROOT/'supabase/migrations/20261110151118_require_invoice_attachment.sql').read_text())
    try:
        yield
    finally:
        original = (ROOT/'supabase/migrations/20261104204746_invoice_annex_delivery.sql').read_text()
        sql(original[original.index('create or replace function private.guard_invoice_attachment()'):
                     original.index('create trigger guard_invoice_attachment')])
        sql('alter table client_billing_profiles alter column attach_timesheet_report set default false')


@pytest.mark.parametrize('saved_preference', [True, False])
def test_standard_attachment_is_required_even_when_old_preference_was_disabled(mandatory_attachment, saved_preference):
    result, review, encoded = annex_reservation()
    invoice_id = result.stdout.strip()
    sql(f"select mark_manual_invoice_attempt('{invoice_id}','failed','test','Preparation stopped')", ADMIN)
    sql(f'update client_billing_profiles set attach_timesheet_report={str(saved_preference).lower()}')
    missing = {**review, 'attach_timesheet_report': False, 'attachment': None}
    assert reserve(missing).returncode != 0
    assert reserve({**review, 'attachment': None}).returncode != 0
    assert reserve(review).returncode == 0
    final = f"select * from finalize_manual_invoice('{invoice_id}','TEST-STANDARD',250,250,'JVBERi1saXZl')"
    assert sql(final, ADMIN, check=False).returncode != 0
    sql(f"select * from store_invoice_attachment('{invoice_id}','{encoded}')", ADMIN)
    sql(final, ADMIN)
    assert sql('select attachment_available from invoice_ledger').stdout.strip() == 't'
    assert sql("select has_function_privilege('anon','private.guard_invoice_attachment()','execute')").stdout.strip() == 'f'


def test_required_annex_must_be_stored_before_invoice_can_finalize():
    result,review,encoded=annex_reservation()
    assert result.returncode==0,result.stderr
    invoice_id=result.stdout.strip()
    final=f"select * from finalize_manual_invoice('{invoice_id}','TEST-ORIGINAL',250,250,'JVBERi1saXZl')"
    assert sql(final,ADMIN,check=False).returncode!=0
    assert sql('select status from timesheets').stdout.strip()=='approved'
    stored=sql(f"select * from store_invoice_attachment('{invoice_id}','{encoded}')",ADMIN)
    assert review['attachment']['sha256'] in stored.stdout
    sql(final,ADMIN)
    assert sql('select attachment_available from invoice_ledger').stdout.strip()=='t'
    request_id,_=reserve_storno(invoice_id)
    assert finalize_storno(invoice_id,request_id).returncode==0
    assert sql('select attachment_available from invoice_ledger').stdout.strip()=='t'
    assert json.loads(sql('select attachment_metadata from invoice_ledger').stdout)==review['attachment']
    assert sql(f"select * from store_invoice_attachment('{invoice_id}','{encoded}')",ADMIN,check=False).returncode!=0


def test_report_preference_and_language_changes_reject_old_review():
    sql('update client_billing_profiles set attach_timesheet_report=true;')
    assert reserve().returncode!=0
    result,review,encoded=annex_reservation(language='en')
    assert result.returncode!=0 and 'report' in result.stderr.lower()
    sql("update clients set invoice_attachment_language='en'")
    assert reserve(review).returncode==0


@pytest.mark.parametrize('change',[{'sha256':'x'*64},{'size_bytes':2000001},
    {'filename':'../other-client.xlsx'},{'language':'de'},{'media_type':'text/plain'}])
def test_invalid_or_oversized_attachment_metadata_never_reserves_work(change):
    result,review,encoded=annex_reservation()
    invoice_id=result.stdout.strip()
    sql(f"select mark_manual_invoice_attempt('{invoice_id}','failed','test','safe test rejection')",ADMIN)
    review['attachment'].update(change)
    assert reserve(review).returncode!=0


def test_unexpected_attachment_when_preference_is_off_is_rejected():
    result,review,_=annex_reservation()
    invoice_id=result.stdout.strip()
    sql(f"select mark_manual_invoice_attempt('{invoice_id}','failed','test','safe test rejection')",ADMIN)
    sql('update client_billing_profiles set attach_timesheet_report=false')
    review['attach_timesheet_report']=False
    assert reserve(review).returncode!=0


@pytest.mark.parametrize('identity',[OTHER_ADMIN,WORKER])
def test_other_tenant_or_employee_cannot_store_annex(identity):
    result,review,encoded=annex_reservation()
    invoice_id=result.stdout.strip()
    assert sql(f"select * from store_invoice_attachment('{invoice_id}','{encoded}')",identity,check=False).returncode!=0
    assert sql('select attachment_available from invoice_ledger').stdout.strip()=='f'


def test_attachment_store_is_idempotent_and_rejects_wrong_bytes():
    result,review,encoded=annex_reservation()
    invoice_id=result.stdout.strip()
    assert sql(f"select * from store_invoice_attachment('{invoice_id}','UEsDBHdyb25n')",ADMIN,check=False).returncode!=0
    for _ in range(2):
        sql(f"select * from store_invoice_attachment('{invoice_id}','{encoded}')",ADMIN)
    sql(f"select mark_manual_invoice_attempt('{invoice_id}','uncertain','timeout','Provider uncertain')",ADMIN)
    assert sql('select attachment_available from invoice_ledger').stdout.strip()=='t'
    assert reserve(review).returncode!=0
    assert sql("select has_function_privilege('anon','public.store_invoice_attachment(uuid,text)','execute')").stdout.strip()=='f'


def test_safe_failed_attempt_replaces_annex_only_after_a_fresh_review():
    result,review,encoded=annex_reservation()
    invoice_id=result.stdout.strip()
    sql(f"select * from store_invoice_attachment('{invoice_id}','{encoded}')",ADMIN)
    sql(f"select mark_manual_invoice_attempt('{invoice_id}','failed','3','Rejected before issuance')",ADMIN)
    retry=reserve(review)
    assert retry.returncode==0
    assert retry.stdout.strip()==invoice_id
    assert sql('select attachment_available from invoice_ledger').stdout.strip()=='f'
