"""One authorized hosted test-account issuance; never replay an uncertain request."""
import hashlib
import json

import httpx

import hosted_rollout_check as c

state = json.loads(c.SESSION.read_text())
assert state['company_id'] == c.STAGE
assert not state.get('hosted_invoice_attempt_started'), 'Reconcile the prior attempt; do not replay'
results = json.loads(c.RESULT.read_text())


def record(name, passed, **details):
    row = {'check': name, 'passed': bool(passed), **details}
    results['checks'] = [r for r in results['checks'] if r['check'] != name] + [row]
    results['unique_checks_passed'] = sum(r['passed'] for r in results['checks'])
    c.RESULT.write_text(json.dumps(results, indent=2))
    print(json.dumps(row), flush=True)
    assert passed, name


with httpx.Client(headers={'apikey': str(c.CFG['VITE_SUPABASE_PUBLISHABLE_KEY']),
        'Authorization': 'Bearer ' + state['admin']['access_token']}, timeout=120) as admin:
    def db(method, table, data=None, **params):
        r = admin.request(method, c.BASE + '/rest/v1/' + table, json=data, params=params)
        assert r.status_code < 400, (method, table, r.status_code)
        return r.json() if r.content else None

    body = {'company_id': c.STAGE, 'client_id': state['new_client_id'],
            'period_start': '2026-10-01', 'period_end': '2026-10-31'}
    profile = db('GET', 'client_billing_profiles', client_id='eq.' + state['new_client_id'], company_id='eq.' + c.STAGE)[0]
    assert profile['send_invoice_email'] is False
    existing = db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE, client_id='eq.' + state['new_client_id'], select='id,status')
    assert not existing, 'Existing attempt must be reconciled, not replayed'
    entry = db('GET', 'timesheets', id='eq.' + state['new_entry_id'], company_id='eq.' + c.STAGE)[0]
    assert entry['status'] == 'approved'
    review_response = admin.post(c.API + '/invoices/client-review', json=body)
    assert review_response.status_code == 200
    review = review_response.json()
    assert float(review['net_total']) == 25000 and float(review['gross_total']) == 31750
    assert len(review['approved_entries']) == 1
    assert c.sql(f"select szamlazz_test_mode from companies where id='{c.STAGE}';") == 't'
    results['provider_mode_evidence'] = 'User reconfirmed dedicated Teszt ceg is in Tesztuzem on 2026-10-28; visible verification was recorded on 2026-10-27.'
    try:
        db('PATCH', 'companies', {'szamlazz_test_mode': False}, id='eq.' + c.STAGE)
        state['hosted_invoice_attempt_started'] = True
        c.SESSION.write_text(json.dumps(state))
        results['provider_creation_requests'] = 1
        c.RESULT.write_text(json.dumps(results, indent=2))
        issued = admin.post(c.API + '/invoices/client-issue', json={**body,
            'review_fingerprint': review['review_fingerprint'], 'confirm': True})
        if issued.status_code != 200:
            print(json.dumps({'issuance_status': issued.status_code, 'replay_permitted': False}), flush=True)
            raise RuntimeError('Inspect the staging ledger; never automatically retry invoice creation')
        invoice = issued.json()
        state['hosted_invoice_id'] = invoice['id']
        state['hosted_invoice_number'] = invoice['invoice_number']
        c.SESSION.write_text(json.dumps(state))
        record('hosted_test_invoice_issued', invoice['status'] == 'issued' and invoice['invoice_number'].startswith('TESZT-'),
               status=issued.status_code, invoice_id=invoice['id'], invoice_number=invoice['invoice_number'])
    finally:
        # Restore only the staging company's pre-test preview gate, including unknown outcomes.
        c.sql(c.stage_guard() + f"update public.companies set szamlazz_test_mode=true where id='{c.STAGE}';")

    entry = db('GET', 'timesheets', id='eq.' + state['new_entry_id'], company_id='eq.' + c.STAGE)[0]
    record('hosted_issuance_marks_entry_invoiced', entry['status'] == 'invoiced')
    pdf = admin.get(c.API + '/invoices/' + invoice['id'] + '/pdf', params={'company_id': c.STAGE})
    record('hosted_new_invoice_pdf', pdf.status_code == 200 and pdf.content.startswith(b'%PDF'),
           bytes=len(pdf.content), sha256=hashlib.sha256(pdf.content).hexdigest())
    (c.PRIVATE / 'hosted-issued.pdf').write_bytes(pdf.content)
    # There is no approved work left; a duplicate must stop before provider invocation.
    duplicate = admin.post(c.API + '/invoices/client-issue', json={**body,
        'review_fingerprint': review['review_fingerprint'], 'confirm': True})
    record('hosted_duplicate_stopped', duplicate.status_code in (404,409), status=duplicate.status_code)
    rows = db('GET', 'invoice_ledger', company_id='eq.' + c.STAGE, client_id='eq.' + state['new_client_id'],
              select='id,status,invoice_number,net_total,gross_total,pdf_available')
    record('hosted_single_confirmed_invoice', len(rows) == 1 and rows[0]['status'] == 'issued'
        and float(rows[0]['net_total']) == 25000 and float(rows[0]['gross_total']) == 31750 and rows[0]['pdf_available'] is True)
    record('staging_preview_gate_restored', c.sql(f"select szamlazz_test_mode from companies where id='{c.STAGE}';") == 't')
