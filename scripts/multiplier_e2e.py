"""Repeatable hosted invoice checks, restricted to the retained synthetic staging tenant.

No emails. Creation requires a separate --provider-test-mode-confirmed flag.
Sessions/passwords stay in a private /tmp directory; reports contain no secrets.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.hosted_rollout_check as c
from app.schemas.invoices import InvoiceReviewProjectLine
from app.services.client_invoice_service import ClientInvoiceService
from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path('/tmp/chronosync-multiplier-e2e-20261030')
PRIVATE.mkdir(mode=0o700, exist_ok=True)
c.PRIVATE, c.SESSION = PRIVATE, PRIVATE / 'session.json'
REPORT = ROOT / 'docs/multiplier-e2e-results-2026-10-30.json'
EVIDENCE = ROOT / 'docs/staging-evidence-2026-10-30'
EVIDENCE.mkdir(exist_ok=True)
STATE = PRIVATE / 'fixtures.json'
D = Decimal


def record(name, passed, **details):
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {'checks': []}
    row = dict(check=name, passed=bool(passed), **details)
    report['checks'] = [r for r in report['checks'] if r['check'] != name] + [row]
    report['passed'] = sum(r['passed'] for r in report['checks'])
    report['failed'] = sum(not r['passed'] for r in report['checks'])
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return passed


class Run:
    def __init__(self, label='admin'):
        session = json.loads(c.SESSION.read_text())
        assert session['company_id'] == c.STAGE
        self.client = httpx.Client(timeout=90, headers={
            'apikey': c.CFG['VITE_SUPABASE_PUBLISHABLE_KEY'],
            'Authorization': 'Bearer ' + session[label]['access_token'],
            'Prefer': 'return=representation,resolution=merge-duplicates'})

    def db(self, method, table, body=None, **params):
        # Every fixture request must explicitly scope the known staging tenant.
        assert (body and body.get('company_id') == c.STAGE) or params.get('company_id') == 'eq.' + c.STAGE
        if table == 'companies':
            assert params.get('id') == 'eq.' + c.STAGE
            params.pop('company_id', None)
            params.setdefault('select', 'id,szamlazz_test_mode')
            body = {k: v for k, v in (body or {}).items() if k != 'company_id'} or None
        r = self.client.request(method, c.BASE + '/rest/v1/' + table, json=body, params=params)
        if r.status_code >= 400:
            raise RuntimeError(f'{method} {table}: HTTP {r.status_code}; {r.json().get("message", "unknown")[:250]}')
        return r.json() if r.content else None

    def api(self, method, path, body=None, **params):
        return self.client.request(method, c.API + path, json=body, params=params)


def setup():
    if not c.SESSION.exists():
        c.setup(include_worker=True)
    run = Run()
    r = run.api('GET', '/company/invoice-settings', company_id=c.STAGE)
    assert r.status_code == 200 and r.json()['api_key_configured'], 'Staging credentials unavailable'
    record('hosted_invoice_credentials_available', True)


def seed():
    setup_only_clients = []
    if STATE.exists():
        previous = json.loads(STATE.read_text())
        assert not any(case['entries'] for case in previous['cases']), 'Reuse completed fixtures'
        setup_only_clients = [case['client_id'] for case in previous['cases']]
    run, worker = Run(), Run('worker')
    stamp = uuid4().hex[:5].upper()
    state = {'company_id': c.STAGE, 'cases': [], 'setup_only_clients': setup_only_clients}
    cases = [
        ('mixed-huf', 'hu', 'HUF', 1, '27', [
            ('Payroll', True, 'óra', '100.125', [('1.25', 3), ('2.50', 7)]),
            ('Consulting', False, 'hour', '200', [('0.75', 1)]),
            ('Monthly payroll', True, 'hó', '2500', [('0.50', 9)])]),
        ('mixed-eur', 'en', 'EUR', 6, '27', [
            ('Payroll EUR', True, 'hours', '19.955', [('1.33', 12)]),
            ('Units EUR', True, 'db', '2.55', [('0.67', 5)])]),
        ('private-huf', 'en', 'HUF', -1, '27', [
            ('Private payroll', True, 'hour', '1.5', [('0.25', 3), ('0.25', 1)])]),
        ('exempt-huf', 'hu', 'HUF', 1, 'AAM', [
            ('Exempt payroll', True, 'óra', '11.11', [('0.17', 3), ('0.33', 7)])]),
    ]
    for index, (name, lang, currency, tax, vat, projects) in enumerate(cases):
        code = f'{stamp[:2]}{index}'
        buyer = run.db('POST', 'clients', dict(company_id=c.STAGE, client_code=code,
            name=f'Multiplier E2E {name} 2026-10-30 {stamp}', invoice_attachment_language=lang,
            available_hours_per_month=100, hours_from_previous_month=2))[0]
        profile = dict(company_id=c.STAGE, client_id=buyer['id'], billing_name=f'E2E {name}',
            billing_country_code='DE' if tax == 6 else 'HU', billing_postal_code='10115' if tax == 6 else '1111',
            billing_city='Berlin' if tax == 6 else 'Budapest', billing_address='Synthetic test street 1',
            tax_subject=tax, tax_number='12345676-2-42' if tax == 1 else None,
            eu_vat_number='DE123456789' if tax == 6 else None, group_tax_number=None,
            billing_email=None, send_invoice_email=False, invoice_language=lang, vat_code=vat)
        run.db('POST', 'client_billing_profiles', profile, on_conflict='client_id')
        case = dict(name=name, code=code, client_id=buyer['id'], projects=[], entries=[],
            body=dict(company_id=c.STAGE, client_id=buyer['id'], period_start='2026-10-01',
                period_end='2026-10-31', project_quantities={}), expected_attachment='0', expected_logged='0')
        state['cases'].append(case)
        STATE.write_text(json.dumps(state))
        for j, (project_name, payroll, unit, rate, entries) in enumerate(projects):
            project = run.db('POST', 'projects', dict(company_id=c.STAGE, client_id=buyer['id'],
                name=project_name, is_payroll=payroll))[0]
            run.db('POST', 'project_pricing', dict(company_id=c.STAGE, project_id=project['id'],
                pricing_year=2026, original_fee=rate, original_currency=currency, original_unit=unit,
                indexed_fee=rate, unit=unit, currency=currency), on_conflict='project_id,pricing_year')
            case['projects'].append(dict(id=project['id'], unit=unit, payroll=payroll, rate=rate))
            if unit in ('hó', 'db'):
                case['body']['project_quantities'][project['id']] = '1' if unit == 'hó' else '2'
            for k, (hours, multiplier) in enumerate(entries):
                entry = worker.db('POST', 'timesheets', dict(company_id=c.STAGE, user_id=c.WORKER,
                    client_id=buyer['id'], project_id=project['id'], work_date=f'2026-10-{10+j:02d}',
                    hours_logged=hours, billing_multiplier=multiplier,
                    description=f'{name} work {j+1}.{k+1}', status='draft'))[0]
                worker.db('PATCH', 'timesheets', dict(company_id=c.STAGE, status='submitted'),
                    id='eq.' + entry['id'], company_id='eq.' + c.STAGE)
                run.db('PATCH', 'timesheets', dict(company_id=c.STAGE, status='approved'),
                    id='eq.' + entry['id'], company_id='eq.' + c.STAGE)
                case['entries'].append(dict(id=entry['id'], project_id=project['id'],
                    hours=hours, multiplier=multiplier))
                case['expected_attachment'] = str(D(case['expected_attachment']) + D(hours) * multiplier)
                case['expected_logged'] = str(D(case['expected_logged']) + D(hours))
            STATE.write_text(json.dumps(state))
        record(name + '_fixtures_approved', True, entries=len(case['entries']), client_id=buyer['id'])


def workbook_values(content, case):
    book = load_workbook(BytesIO(content), data_only=False)
    sheet = book[case['code']]
    count = len(case['entries'])
    values = [D(str(sheet.cell(17+i, 4).value)) for i in range(count)]
    expected = sorted(D(e['hours']) * e['multiplier'] for e in case['entries'])
    assert sorted(values) == expected, (values, expected)
    formulas = [cell.value for row in sheet for cell in row if cell.data_type == 'f']
    assert f'=SUM(D17:D{16+count})' in formulas, formulas
    assert sum(values) == D(case['expected_attachment'])
    assert book.calculation.fullCalcOnLoad
    return values, formulas


def checks():
    run, worker = Run(), Run('worker')
    state = json.loads(STATE.read_text())
    for case in state['cases']:
        name, body = case['name'], case['body']
        response = run.api('POST', '/invoices/client-review', body)
        assert response.status_code == 200, (name, response.status_code, response.text[:300])
        review = response.json()
        case['review'] = review
        hourly_quantity = D(0)
        for line in review['items']:
            project = next(p for p in case['projects'] if p['id'] == line['project_id'])
            es = [e for e in case['entries'] if e['project_id'] == project['id']]
            expected = sum((D(e['hours']) * e['multiplier'] for e in es), D(0)) if project['unit'] not in ('hó', 'db') else D(body['project_quantities'][project['id']])
            assert D(line['quantity']) == expected
            if project['unit'] not in ('hó', 'db'):
                hourly_quantity += expected
                assert D(line['logged_hours']) == sum(D(e['hours']) for e in es)
        assert sum(D(e['hours_logged']) for e in review['approved_entries']) == D(case['expected_logged'])
        record(name + '_review_quantities', True, logged_hours=case['expected_logged'],
            attachment_hours=case['expected_attachment'], hourly_invoice_quantity=str(hourly_quantity),
            net=review['net_total'], vat=review['vat_total'], gross=review['gross_total'])
        preview = run.api('POST', '/invoices/client-preview', body)
        record(name + '_provider_preview', preview.status_code == 200 and preview.content.startswith(b'%PDF'), status=preview.status_code)
        if preview.status_code == 200:
            (EVIDENCE / (name + '-preview.pdf')).write_bytes(preview.content)
        attachment_body = dict(company_id=c.STAGE, client_ids=[case['client_id']],
            start_date='2026-10-01', end_date='2026-10-31', update_remaining_hours=False)
        attachment = run.api('POST', '/reports/generate-szamlamelleklet', attachment_body)
        assert attachment.status_code == 200, (attachment.status_code, attachment.text[:300])
        values, formulas = workbook_values(attachment.content, case)
        (EVIDENCE / (name + '-attachment.xlsx')).write_bytes(attachment.content)
        record(name + '_attachment_values_and_formulas', True, values=[str(v) for v in values], formulas=formulas,
            cache_control=attachment.headers.get('cache-control'))
        r = run.api('POST', '/invoices/client-issue', dict(body, review_fingerprint=review['review_fingerprint'], confirm=True))
        record(name + '_preview_gate_blocks_issuance', r.status_code == 409, status=r.status_code)
        r = worker.api('POST', '/invoices/client-review', body)
        record(name + '_worker_review_denied', r.status_code == 403, status=r.status_code)
        r = worker.api('POST', '/reports/generate-szamlamelleklet', attachment_body)
        record(name + '_worker_attachment_denied', r.status_code == 403, status=r.status_code)
        rows = run.db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE,
            client_id='eq.' + case['client_id'], select='id,status')
        record(name + '_previews_create_no_ledger', not rows)
        STATE.write_text(json.dumps(state))
    case = state['cases'][0]
    first = case['entries'][0]
    run.db('PATCH', 'timesheets', dict(company_id=c.STAGE, billing_multiplier=4),
        company_id='eq.' + c.STAGE, id='eq.' + first['id'])
    r = run.api('POST', '/invoices/client-issue', dict(case['body'], review_fingerprint=case['review']['review_fingerprint'], confirm=True))
    record('multiplier_change_invalidates_review', r.status_code == 409 and 'changed' in r.text, status=r.status_code)
    run.db('PATCH', 'timesheets', dict(company_id=c.STAGE, billing_multiplier=3),
        company_id='eq.' + c.STAGE, id='eq.' + first['id'])
    body = case['body']
    for label, modified in [('hourly_override', dict(body, project_quantities={**body['project_quantities'], first['project_id']: '1'})),
            ('partial_month', dict(body, period_start='2026-10-02')),
            ('cross_year', dict(body, period_end='2027-01-31'))]:
        r = run.api('POST', '/invoices/client-review', modified)
        record(label + '_rejected', r.status_code == 409, status=r.status_code)
    # A project flag edit cannot rewrite historical entry factors.
    run.db('PATCH', 'projects', dict(company_id=c.STAGE, is_payroll=False),
        company_id='eq.' + c.STAGE, id='eq.' + first['project_id'])
    r = run.api('POST', '/invoices/client-review', body)
    line = next(x for x in r.json()['items'] if x['project_id'] == first['project_id'])
    record('project_flag_change_preserves_entry_multipliers', r.status_code == 200 and D(line['quantity']) == D('21.25'))
    run.db('PATCH', 'projects', dict(company_id=c.STAGE, is_payroll=True),
        company_id='eq.' + c.STAGE, id='eq.' + first['project_id'])
    all_ids = [x['client_id'] for x in state['cases']]
    r = run.api('POST', '/reports/generate-szamlamelleklet', dict(company_id=c.STAGE, client_ids=all_ids,
        start_date='2026-10-01', end_date='2026-10-31', update_remaining_hours=False))
    book = load_workbook(BytesIO(r.content))
    record('batch_attachment_four_localized_sheets', r.status_code == 200 and set(book.sheetnames) == {x['code'] for x in state['cases']})
    (EVIDENCE / 'batch-attachments.xlsx').write_bytes(r.content)
    STATE.write_text(json.dumps(state))


def issue(confirmed):
    assert confirmed, 'Explicit current provider test-mode confirmation required'
    run = Run()
    state = json.loads(STATE.read_text())
    assert not any(case.get('attempt_started') and not case.get('invoice') for case in state['cases']), 'Reconcile uncertain attempt first'
    try:
        run.db('PATCH', 'companies', dict(company_id=c.STAGE, szamlazz_test_mode=False),
            company_id='eq.' + c.STAGE, id='eq.' + c.STAGE)
        for case in state['cases']:
            if case.get('invoice'):
                continue
            r = run.api('POST', '/invoices/client-review', case['body'])
            assert r.status_code == 200
            review = r.json()
            case['attempt_started'] = True
            STATE.write_text(json.dumps(state))
            r = run.api('POST', '/invoices/client-issue', dict(case['body'],
                review_fingerprint=review['review_fingerprint'], confirm=True))
            record(case['name'] + '_provider_issuance', r.status_code == 200, status=r.status_code)
            assert r.status_code == 200, 'Never replay this attempt; reconcile provider external ID'
            invoice = case['invoice'] = r.json()
            STATE.write_text(json.dumps(state))
            assert invoice['status'] == 'issued' and invoice['invoice_number'].startswith('TESZT-')
            record(case['name'] + '_issued_totals_match_review',
                all(D(invoice[key]) == D(review[key]) for key in ('net_total', 'vat_total', 'gross_total')),
                invoice_number=invoice['invoice_number'], invoice_id=invoice['id'],
                external_id=invoice['external_id'], net=invoice['net_total'], gross=invoice['gross_total'])
            pdf = run.api('GET', '/invoices/' + invoice['id'] + '/pdf', company_id=c.STAGE)
            assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')
            (EVIDENCE / (case['name'] + '-issued.pdf')).write_bytes(pdf.content)
            record(case['name'] + '_stored_pdf', True, bytes=len(pdf.content), sha256=hashlib.sha256(pdf.content).hexdigest())
            entries = run.db('GET', 'timesheets', company_id='eq.' + c.STAGE,
                client_id='eq.' + case['client_id'], select='id,status,hours_logged,billing_multiplier')
            record(case['name'] + '_work_invoiced_and_raw_hours_preserved',
                all(e['status'] == 'invoiced' for e in entries) and
                sum(D(str(e['hours_logged'])) for e in entries) == D(case['expected_logged']))
            r = run.api('POST', '/invoices/client-issue', dict(case['body'], review_fingerprint=review['review_fingerprint'], confirm=True))
            record(case['name'] + '_duplicate_stopped', r.status_code in (404, 409), status=r.status_code)
            ledger = run.db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE,
                client_id='eq.' + case['client_id'], select='id,status')
            record(case['name'] + '_single_issued_ledger', len(ledger) == 1 and ledger[0]['status'] == 'issued')
    finally:
        c.sql(c.stage_guard() + f"update public.companies set szamlazz_test_mode=true where id='{c.STAGE}';")
        record('staging_preview_gate_restored', True)


def postchecks():
    run, worker = Run(), Run('worker')
    state = json.loads(STATE.read_text())
    for case in state['cases']:
        invoice = case['invoice']
        r = worker.api('GET', '/invoices/' + invoice['id'] + '/pdf', company_id=c.STAGE)
        record(case['name'] + '_worker_pdf_denied', r.status_code == 403, status=r.status_code)
        r = run.api('GET', '/invoices/' + invoice['id'] + '/pdf', company_id='00000000-0000-4000-8000-000000000001')
        record(case['name'] + '_foreign_company_pdf_denied', r.status_code == 403, status=r.status_code)
        first = case['entries'][0]
        r = run.client.patch(c.BASE + '/rest/v1/timesheets', params={
            'id': 'eq.' + first['id'], 'company_id': 'eq.' + c.STAGE},
            json={'billing_multiplier': first['multiplier'] + 1})
        saved = run.db('GET', 'timesheets', company_id='eq.' + c.STAGE,
            id='eq.' + first['id'], select='billing_multiplier')[0]
        locked = r.status_code in (403, 409) and saved['billing_multiplier'] == first['multiplier']
        record(case['name'] + '_issued_multiplier_locked', locked, status=r.status_code)
        assert locked, 'An issued multiplier unexpectedly changed'
        profile = run.db('GET', 'client_billing_profiles', company_id='eq.' + c.STAGE,
            client_id='eq.' + case['client_id'])[0]
        ledger = run.db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE,
            id='eq.' + invoice['id'], select='invoice_snapshot')[0]['invoice_snapshot']
        lines = [InvoiceReviewProjectLine.model_validate(x) for x in ledger['items']]
        request, _ = ClientInvoiceService._build_invoice_request(company_id=UUID(c.STAGE),
            client_id=UUID(case['client_id']),
            period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
            client={'client_code': case['code']}, profile=profile, lines=lines, preview=False)
        xml = SzamlazzHuProvider('redacted-staging-key').build_invoice_xml(request)
        result = subprocess.run(['xmllint', '--noout', '--nonet', '--schema',
            str(ROOT / 'tests/fixtures/szamlazz/xmlszamla.xsd'), '-'], input=xml, capture_output=True)
        record(case['name'] + '_ledger_lines_xml_schema', result.returncode == 0)
        assert b'<sendEmail>false</sendEmail>' in xml
        (EVIDENCE / (case['name'] + '-request-redacted.xml')).write_bytes(xml)
        # Read-only query using the saved staging Agent key, never a create replay.
        result = asyncio.run(SzamlazzHuProvider(os.environ['SZAMLAZZ_AGENT_KEY']).get_invoice_pdf(external_id=invoice['external_id']))
        record(case['name'] + '_provider_pdf_lookup_matches_ledger',
            result['invoice_number'] == invoice['invoice_number'] and
            D(result['net_total']) == D(invoice['net_total']) and D(result['gross_total']) == D(invoice['gross_total']) and
            result['pdf'].startswith(b'%PDF'), invoice_number=result['invoice_number'])
    case = state['cases'][0]
    original = dict(company_id=c.STAGE, client_ids=[case['client_id']],
        start_date='2026-10-01', end_date='2026-10-31', update_remaining_hours=True)
    for i in range(2):
        r = run.api('POST', '/reports/generate-szamlamelleklet', original)
        assert r.status_code == 200, (r.status_code, r.text[:200])
        workbook_values(r.content, case)
        client = run.db('GET', 'clients', company_id='eq.' + c.STAGE, id='eq.' + case['client_id'])[0]
        record('october_carryover_' + ('finalized' if i == 0 else 'idempotent'), D(str(client['hours_from_previous_month'])) == D('75.50'))
    project = case['projects'][0]
    entry = worker.db('POST', 'timesheets', dict(company_id=c.STAGE, user_id=c.WORKER,
        client_id=case['client_id'], project_id=project['id'], work_date='2026-11-10',
        hours_logged='0.25', billing_multiplier=4, description='Later month carry-over test', status='draft'))[0]
    worker.db('PATCH', 'timesheets', dict(company_id=c.STAGE, status='submitted'),
        company_id='eq.' + c.STAGE, id='eq.' + entry['id'])
    run.db('PATCH', 'timesheets', dict(company_id=c.STAGE, status='approved'),
        company_id='eq.' + c.STAGE, id='eq.' + entry['id'])
    r = run.api('POST', '/reports/generate-szamlamelleklet', dict(original,
        start_date='2026-11-01', end_date='2026-11-30'))
    assert r.status_code == 200, r.text[:200]
    (EVIDENCE / 'november-carryover.xlsx').write_bytes(r.content)
    client = run.db('GET', 'clients', company_id='eq.' + c.STAGE, id='eq.' + case['client_id'])[0]
    record('november_carryover_uses_october_snapshot', D(str(client['hours_from_previous_month'])) == D('174.50'))
    r = run.api('POST', '/reports/generate-szamlamelleklet', original)
    assert r.status_code == 200, r.text[:200]
    sheet = load_workbook(BytesIO(r.content))[case['code']]
    client = run.db('GET', 'clients', company_id='eq.' + c.STAGE, id='eq.' + case['client_id'])[0]
    record('historical_redownload_keeps_original_carryover',
        sheet['D23'].value == 2 and D(str(client['hours_from_previous_month'])) == D('174.50'))
    # Both new conflict branches must return promptly through actual PostgREST.
    for finalized, start, end in [(True, '2026-10-01', '2026-10-31'), (False, '2026-12-01', '2026-12-31')]:
        r = run.client.post(c.BASE + '/rest/v1/rpc/finalize_invoice_attachment_period',
            json={'p_company_id': c.STAGE, 'p_period_start': start, 'p_period_end': end,
                'p_client_updates': [dict(client_id=case['client_id'], starting_hours=2,
                    available_hours=100, used_hours=25, remaining_hours=77)]})
        record('hosted_stale_attachment_' + ('snapshot' if finalized else 'new_period') + '_returns_409',
            r.status_code == 409 and r.json().get('code') == 'PT409', status=r.status_code,
            elapsed_seconds=round(r.elapsed.total_seconds(), 3))
    r = run.api('GET', '/invoices/history', company_id=c.STAGE)
    rows = r.json()
    record('no_uncertain_or_issuing_staging_invoices', r.status_code == 200 and
        not any(x['status'] in ('uncertain', 'issuing') for x in rows))


def recovery():
    run = Run()
    state = json.loads(STATE.read_text())
    invoice = state['cases'][1]['invoice']
    assert invoice['invoice_number'] == 'TESZT-2026-8'
    original = run.db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE,
        id='eq.' + invoice['id'], select='issued_at,attempt_count,pdf_available')[0]
    original_pdf = (EVIDENCE / 'mixed-eur-issued.pdf').read_bytes()
    assert original_pdf.startswith(b'%PDF')
    predicate = f"id='{invoice['id']}' and company_id='{c.STAGE}' and invoice_number='{invoice['invoice_number']}' and status='issued'"
    try:
        # Only the synthetic invoice's cached copy is cleared; retain a local
        # backup, and use the existing provider external ID to recover it.
        c.sql(c.stage_guard() + f'update public.invoice_ledger set pdf_bytes=null,pdf_available=false where {predicate};')
        r = run.api('GET', '/invoices/' + invoice['id'] + '/pdf', company_id=c.STAGE)
        record('hosted_missing_pdf_recovers_from_provider', r.status_code == 200 and r.content.startswith(b'%PDF'))
        assert r.status_code == 200
        restored = run.db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE,
            id='eq.' + invoice['id'], select='issued_at,attempt_count,pdf_available,status')[0]
        record('hosted_pdf_recovery_preserves_issuance', restored['status'] == 'issued' and restored['pdf_available'] and
            restored['issued_at'] == original['issued_at'] and restored['attempt_count'] == original['attempt_count'])
        again = run.api('GET', '/invoices/' + invoice['id'] + '/pdf', company_id=c.STAGE)
        record('hosted_recovered_pdf_redownload_identical', again.status_code == 200 and again.content == r.content)
        ledger = run.db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE,
            client_id='eq.' + state['cases'][1]['client_id'], select='id')
        record('hosted_pdf_recovery_creates_no_new_invoice', len(ledger) == 1)
    finally:
        # If the recovery request fails, restore the exact known local PDF.
        c.sql(c.stage_guard() + f"update public.invoice_ledger set pdf_bytes=decode('{original_pdf.hex()}','hex'),pdf_available=true where {predicate} and pdf_bytes is null;")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['setup', 'seed', 'checks', 'issue', 'postchecks', 'recovery', 'cleanup'])
    p.add_argument('--provider-test-mode-confirmed', action='store_true')
    args = p.parse_args()
    if args.action == 'setup': setup()
    elif args.action == 'seed': seed()
    elif args.action == 'checks': checks()
    elif args.action == 'issue': issue(args.provider_test_mode_confirmed)
    elif args.action == 'postchecks': postchecks()
    elif args.action == 'recovery': recovery()
    else:
        saved_session = json.loads(c.SESSION.read_text()) if c.SESSION.exists() else None
        c.disarm()
        record('staging_disarmed_private_session_removed', not c.SESSION.exists())
        if saved_session:
            for label in ('admin', 'worker'):
                response = httpx.post(c.BASE + '/auth/v1/token?grant_type=password',
                    headers={'apikey': c.CFG['VITE_SUPABASE_PUBLISHABLE_KEY']},
                    json={'email': saved_session[label]['user']['email'],
                        'password': saved_session[label + '_password']}, timeout=15)
                record('cleanup_' + label + '_signin_blocked', response.status_code == 400 and
                    response.json().get('error_code') == 'user_banned')


if __name__ == '__main__':
    main()
