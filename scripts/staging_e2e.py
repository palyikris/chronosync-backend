"""Explicitly scoped hosted staging checks; never reads or changes other tenants.

Run with venv/bin/python scripts/staging_e2e.py --session-file /private/session.json.
The session file contains access_token, company_id and user.id. Provider issuance
requires --issue-test-invoices after verifying Teszt cég is visibly in Tesztüzem.
No credentials or PDFs are included in the JSON results.
"""
import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import dotenv_values

STAGING_COMPANY = "d2026102-7000-4000-8000-000000000001"
STAGING_NAME = "ChronoSync staging E2E 2026-10-27"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--issue-test-invoices", action="store_true")
    parser.add_argument("--api-base", help="Override the backend for an explicitly scoped local source run")
    parser.add_argument("--output", type=Path, default=Path("docs/staging-e2e-results-2026-10-27.json"))
    args = parser.parse_args()
    session = json.loads(args.session_file.read_text())
    assert session["company_id"] == STAGING_COMPANY
    cfg = dotenv_values(Path(__file__).resolve().parents[2] / "chronosync/.env")
    rest = cfg["VITE_SUPABASE_URL"] + "/rest/v1/"
    api = (args.api_base or cfg["VITE_API_BASE_URL"]).rstrip("/")
    headers = {"apikey": cfg["VITE_SUPABASE_PUBLISHABLE_KEY"],
               "Authorization": "Bearer " + session["access_token"],
               "Prefer": "return=representation"}
    results = []
    artifacts = Path("/tmp/chronosync-staging-e2e")
    artifacts.mkdir(mode=0o700, exist_ok=True)

    def record(name, ok, **details):
        result = {"check": name, "passed": bool(ok), **details}
        results.append(result)
        args.output.write_text(json.dumps({"company_id": STAGING_COMPANY, "checks": results}, indent=2))
        print(json.dumps(result), flush=True)
        return ok

    with httpx.Client(headers=headers, timeout=60) as client:
        def db(method, table, *, data=None, **params):
            response = client.request(method, rest + table, params=params, json=data)
            response.raise_for_status()
            return response.json() if response.content else None

        company = db("GET", "companies", id="eq." + STAGING_COMPANY,
                     select="id,name,szamlazz_test_mode")[0]
        assert company["name"] == STAGING_NAME, "Refusing an unexpected company"
        existing = db("GET", "clients", company_id="eq." + STAGING_COMPANY)
        hourly = next(row for row in existing if row["client_code"] == "ZQ1")
        profile = db("GET", "client_billing_profiles", company_id="eq." + STAGING_COMPANY,
                     client_id="eq." + hourly["id"])[0]
        body = {"company_id": STAGING_COMPANY, "client_id": hourly["id"],
                "period_start": "2026-10-01", "period_end": "2026-10-31"}

        # Confirm the application-built review includes only approved work.
        review_response = client.post(api + "/invoices/client-review", json=body)
        if not record("hosted_hourly_review", review_response.status_code == 200,
                      status=review_response.status_code,
                      detail=None if review_response.status_code == 200 else review_response.text[:300]):
            return
        review = review_response.json()
        record("review_totals", Decimal(review["quantity"]) == Decimal("2.5")
               and Decimal(review["net_total"]) == Decimal("25000")
               and Decimal(review["gross_total"]) == Decimal("31750"),
               quantity=review["quantity"], net=review["net_total"], gross=review["gross_total"],
               approved_entries=len(review["approved_entries"]),
               billing_profile_timestamp_present=bool(review.get("billing_profile_updated_at")))
        no_confirmation = client.post(api + "/invoices/client-issue",
                                     json={**body, "review_fingerprint": review["review_fingerprint"], "confirm": False})
        record("explicit_confirmation_required", no_confirmation.status_code == 422,
               status=no_confirmation.status_code)
        gate = client.post(api + "/invoices/client-issue",
                           json={**body, "review_fingerprint": review["review_fingerprint"], "confirm": True})
        record("preview_gate_blocks_issuance", gate.status_code == 409,
               status=gate.status_code, detail=gate.text[:220])
        preview = client.post(api + "/invoices/client-preview", json=body)
        preview_ok = preview.status_code == 200 and preview.content.startswith(b"%PDF")
        record("hosted_hourly_provider_preview", preview_ok, status=preview.status_code,
               detail=None if preview_ok else preview.text[:300])
        if preview_ok:
            (artifacts / "hourly-preview.pdf").write_bytes(preview.content)

        # Keep an unchanged approved snapshot stale by editing only our profile.
        db("PATCH", "client_billing_profiles", client_id="eq." + hourly["id"],
           company_id="eq." + STAGING_COMPANY, data={"net_unit_price": "10001"})
        stale = client.post(api + "/invoices/client-issue",
                            json={**body, "review_fingerprint": review["review_fingerprint"], "confirm": True})
        record("stale_review_blocked", stale.status_code == 409, status=stale.status_code)
        db("PATCH", "client_billing_profiles", client_id="eq." + hourly["id"],
           company_id="eq." + STAGING_COMPANY, data={"net_unit_price": "10000"})

        if not args.issue_test_invoices or not preview_ok:
            record("hosted_issuance", False, blocked="Explicit issuance flag absent or provider preview failed; no creation sent")
            return
        db("PATCH", "companies", id="eq." + STAGING_COMPANY, data={"szamlazz_test_mode": False})
        try:
            fresh = client.post(api + "/invoices/client-review", json=body)
            fresh.raise_for_status()
            issue = client.post(api + "/invoices/client-issue", json={**body,
                                "review_fingerprint": fresh.json()["review_fingerprint"], "confirm": True})
            if not record("hosted_issue", issue.status_code == 200, status=issue.status_code,
                          detail=None if issue.status_code == 200 else issue.text[:500]):
                # Never retry an unknown creation. Fetch history for reconciliation.
                history = client.get(api + "/invoices/history", params={"company_id": STAGING_COMPANY})
                if history.status_code == 200:
                    record("history_after_failed_issuance", True,
                           attempts=[{"id": i["id"], "status": i["status"], "external_id": i["external_id"]}
                                     for i in history.json()])
                return
            invoice = issue.json()
            record("confirmed_invoice", invoice["status"] == "issued" and invoice["pdf_available"],
                   invoice_id=invoice["id"], invoice_number=invoice["invoice_number"])
            pdf = client.get(api + "/invoices/" + invoice["id"] + "/pdf", params={"company_id": STAGING_COMPANY})
            record("stored_pdf_download", pdf.status_code == 200 and pdf.content.startswith(b"%PDF"),
                   status=pdf.status_code, sha256=hashlib.sha256(pdf.content).hexdigest())
            if pdf.status_code == 200:
                (artifacts / "hourly-issued.pdf").write_bytes(pdf.content)
            entries = db("GET", "timesheets", company_id="eq." + STAGING_COMPANY,
                         client_id="eq." + hourly["id"], select="id,status,hours_logged")
            record("approved_work_finalized", all(e["status"] == "invoiced" for e in entries), entries=entries)
            repeat = client.post(api + "/invoices/client-issue", json={**body,
                                 "review_fingerprint": fresh.json()["review_fingerprint"], "confirm": True})
            # An hourly client has no approved work after finalization (404).
            # A fixed fee or active reservation is rejected as a conflict (409).
            record("duplicate_issue_blocked", repeat.status_code in {404, 409}, status=repeat.status_code)
        finally:
            db("PATCH", "companies", id="eq." + STAGING_COMPANY, data={"szamlazz_test_mode": True})


if __name__ == "__main__":
    main()
