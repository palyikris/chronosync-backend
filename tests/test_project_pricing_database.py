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
        add invoice_item_name text default 'Szolgáltatás', add vat_code text default 'AAM';""")
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
    sql("alter table clients add available_hours_per_month numeric default 100, add hours_from_previous_month numeric default 2;")
    sql((ROOT/'supabase/migrations/20261021000000_harden_invoice_attachment_finalization.sql').read_text())
    sql((FRONTEND/'supabase/migrations/20261029193933_add_project_payroll_headcount.sql').read_text())
    sql((ROOT/'supabase/migrations/20261029193936_multiply_payroll_invoice_hours.sql').read_text())


@pytest.fixture(autouse=True)
def seed(schema):
    if not DSN: return
    sql(f"""truncate invoice_ledger_entries,invoice_ledger,timesheets,project_pricing,projects,
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
    return {'billing_model': 'project', 'billing_profile_updated_at': '2026-10-22T00:00:00Z',
        'project_quantities': {}, 'approved_entries': [{'id': ENTRY, 'project_id': PROJECT,
            'work_date': '2026-10-15', 'hours_logged': '2.5', 'description': 'Work', 'project_name': 'Consulting'}],
        'items': [{'project_id': PROJECT, 'project_name': 'Consulting', 'pricing_year': 2026,
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
        hours_logged='2',description='Work',project_name='Other work'))
    assert reserve(review).returncode != 0
    sql(f"insert into project_pricing(project_id,company_id,pricing_year,indexed_fee,unit,currency) "
        f"values ('{second}','{COMPANY}',2026,50,'óra','HUF')")
    price=json.loads(sql(f"select row_to_json(p) from project_pricing p where project_id='{second}'").stdout)
    review['items'].append(dict(review['items'][0], project_id=second, project_name='Other work',
        pricing_updated_at=price['updated_at'],quantity='2',net_unit_price='50',net_total='100',gross_total='100'))
    result=reserve(review,net='350')
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('headcount', [0, -1])
def test_database_rejects_invalid_payroll_headcount(headcount):
    result = sql(f"update projects set payroll_headcount={headcount}", check=False)
    assert result.returncode != 0 and 'projects_payroll_headcount_positive' in result.stderr


def test_payroll_invoice_reservation_uses_multiplied_quantity_and_freezes_headcount():
    sql('update projects set payroll_headcount=12')
    review = snapshot()
    review['items'][0].update(payroll_headcount=12, quantity='30', net_total='3000', gross_total='3000')
    result = reserve(review, net='3000')
    assert result.returncode == 0, result.stderr
    assert Decimal(sql('select hours_logged from timesheets').stdout.strip()) == Decimal('2.5')
    sql('update projects set payroll_headcount=20')
    saved = json.loads(sql("select invoice_snapshot->'items'->0 from invoice_ledger").stdout)
    assert saved['payroll_headcount'] == 12 and saved['quantity'] == '30'


def test_payroll_invoice_rejects_tampered_quantity_and_changed_headcount():
    review = snapshot()
    sql('update projects set payroll_headcount=1')
    assert reserve(review).returncode != 0  # Same total, but the reviewed payroll flag changed.
    sql('update projects set payroll_headcount=12')
    review['items'][0]['payroll_headcount'] = 12
    assert reserve(review).returncode != 0  # Unmultiplied quantity is forbidden.
    assert sql('select count(*) from invoice_ledger').stdout.strip() == '0'


def test_payroll_attachment_finalization_matches_multiplied_workbook_hours():
    sql('update projects set payroll_headcount=12')
    updates = json.dumps([dict(client_id=CLIENT, starting_hours=2, available_hours=100,
        used_hours=30, remaining_hours=72)])
    query = f"select * from finalize_invoice_attachment_period('{COMPANY}','2026-10-01','2026-10-31','{updates}')"
    assert 't' in sql(query, ADMIN).stdout
    assert Decimal(sql('select hours_from_previous_month from clients').stdout.strip()) == 72
    assert 'f' in sql(query, ADMIN).stdout  # Re-download never applies carry-over twice.
    assert Decimal(sql('select hours_from_previous_month from clients').stdout.strip()) == 72
