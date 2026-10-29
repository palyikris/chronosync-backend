"""Exercise current invoice service with real staging PostgreSQL and a mocked provider.

Only the dedicated 2026-10-27 staging tenant is permitted. No provider creation
calls are made. SIMULATED ledger rows remain as explicit recovery-test evidence.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.client_invoice_service import ClientInvoiceService as Service
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuProvider, SzamlazzHuRejectedError, SzamlazzHuRequestError,
)
from fastapi import HTTPException

STAGE = "d2026102-7000-4000-8000-000000000001"
COMPANY = UUID(STAGE)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-file", type=Path, required=True)
    args = parser.parse_args()
    session = json.loads(args.session_file.read_text())
    assert session["company_id"] == STAGE
    token = session["access_token"]
    cfg = dotenv_values(Path(__file__).resolve().parents[2] / "chronosync/.env")
    rest = cfg["VITE_SUPABASE_URL"] + "/rest/v1/"
    headers = {"apikey": cfg["VITE_SUPABASE_PUBLISHABLE_KEY"],
               "Authorization": "Bearer " + token, "Prefer": "return=representation"}
    results_path = Path("docs/staging-recovery-results-2026-10-27.json")
    checks = json.loads(results_path.read_text()) if results_path.exists() else []

    def check(name, passed, **details):
        row = {"check": name, "passed": bool(passed), **details}
        checks.append(row)
        Path("docs/staging-recovery-results-2026-10-27.json").write_text(json.dumps(checks, indent=2))
        print(json.dumps(row), flush=True)
        assert passed, row

    @asynccontextmanager
    async def synthetic_key(_jwt, company_id):
        assert company_id == COMPANY
        yield "synthetic-mocked-provider-never-transmitted"

    async with httpx.AsyncClient(headers=headers, timeout=45) as client:
        async def db(method, table, data=None, **params):
            response = await client.request(method, rest + table, json=data, params=params)
            response.raise_for_status()
            return response.json() if response.content else None

        company = (await db("GET", "companies", id="eq." + STAGE, select="name,szamlazz_test_mode"))[0]
        assert company["name"] == "ChronoSync staging E2E 2026-10-27"
        # Read one profile as the schema-complete baseline, omitting generated fields.
        profiles = await db("GET", "client_billing_profiles", company_id="eq." + STAGE)
        baseline = {key: value for key, value in profiles[0].items() if key not in {"created_at", "updated_at"}}
        await db("PATCH", "companies", {"szamlazz_test_mode": False}, id="eq." + STAGE)
        try:
            for code, mode in [("ZR1", "rejection"), ("ZR2", "response_loss"), ("ZR3", "finalization_loss")]:
                existing = await db("GET", "clients", company_id="eq." + STAGE, client_code="eq." + code)
                if existing:
                    # Resume verification/reconciliation only; never replay creation.
                    history = await Service.list_invoice_history(user_jwt=token, company_id=COMPANY)
                    attempt = next(row for row in history if str(row.client_id) == existing[0]["id"])
                    if attempt.status in {"failed", "issued"}:
                        continue
                    assert attempt.status == "uncertain" and existing[0]["name"].startswith("E2E SIMULATED ")
                    entry = (await db("GET", "timesheets", company_id="eq." + STAGE,
                                     client_id="eq." + existing[0]["id"]))[0]
                    edit = await client.patch(rest + "timesheets", params={"id": "eq." + entry["id"], "company_id": "eq." + STAGE},
                                              json={"description": "SIMULATED edit after failure"})
                    check(mode + "_reservation", edit.status_code >= 400, edit_status=edit.status_code)
                    kwargs = dict(user_jwt=token, company_id=COMPANY, client_id=UUID(existing[0]["id"]),
                                  period_start=date(2026, 10, 1), period_end=date(2026, 10, 31))
                    review = await Service.build_invoice_review(**kwargs)
                    with patch.object(SzamlazzHuProvider, "create_invoice", AsyncMock()) as replay:
                        try:
                            await Service.issue_client_invoice(**kwargs, review_fingerprint=review.review_fingerprint)
                        except HTTPException as exc:
                            check(mode + "_replay_blocked", exc.status_code == 409 and replay.await_count == 0,
                                  status=exc.status_code, provider_calls=replay.await_count)
                    if mode == "response_loss":
                        resolved = await Service.reconcile_invoice(user_jwt=token, company_id=COMPANY,
                                        invoice_id=attempt.id, resolution="not_issued", invoice_number=None,
                                        net_total=None, gross_total=None)
                        check(mode + "_reconciled", resolved.status == "failed")
                        changed = await db("PATCH", "timesheets", {"description": "SIMULATED lock released"},
                                           id="eq." + entry["id"], company_id="eq." + STAGE)
                        check(mode + "_released", bool(changed))
                    else:
                        raise AssertionError("Resume issued reconciliation explicitly from the saved evidence")
                    continue
                customer = (await db("POST", "clients", {"company_id": STAGE,
                                  "name": "E2E SIMULATED " + mode, "client_code": code}))[0]
                project = (await db("POST", "projects", {"company_id": STAGE,
                                 "client_id": customer["id"], "name": "SIMULATED recovery work"}))[0]
                await db("POST", "client_billing_profiles", {**baseline, "client_id": customer["id"],
                         "billing_name": "E2E SIMULATED " + mode, "net_unit_price": 100,
                         "send_invoice_email": False})
                entry = (await db("POST", "timesheets", {"company_id": STAGE,
                                "client_id": customer["id"], "project_id": project["id"],
                                "user_id": session["user"]["id"], "work_date": "2026-10-26",
                                "hours_logged": 1, "description": "SIMULATED recovery test", "status": "approved"}))[0]
                kwargs = dict(user_jwt=token, company_id=COMPANY, client_id=UUID(customer["id"]),
                              period_start=date(2026, 10, 1), period_end=date(2026, 10, 31))
                review = await Service.build_invoice_review(**kwargs)
                provider = AsyncMock(side_effect=(SzamlazzHuRejectedError("Synthetic rejection", code="137")
                             if mode == "rejection" else SzamlazzHuRequestError("Synthetic response loss")))
                if mode == "finalization_loss":
                    provider = AsyncMock(return_value={"invoice_number": "SIMULATED-2026-1",
                                         "net_total": Decimal("100"), "gross_total": Decimal("127"),
                                         "pdf": b"%PDF-synthetic-staging-fixture"})
                with patch.object(CompanyCredentialService, "decrypted_szamlazz_agent_key", synthetic_key), \
                     patch.object(SzamlazzHuProvider, "create_invoice", provider):
                    try:
                        if mode == "finalization_loss":
                            with patch.object(Service, "_finalize_invoice", AsyncMock(side_effect=RuntimeError("Synthetic database failure"))):
                                await Service.issue_client_invoice(**kwargs, review_fingerprint=review.review_fingerprint)
                        else:
                            await Service.issue_client_invoice(**kwargs, review_fingerprint=review.review_fingerprint)
                    except HTTPException as exc:
                        check(mode + "_http_classification", exc.status_code == (422 if mode == "rejection" else 502), status=exc.status_code)
                    else:
                        raise AssertionError("Expected injected failure")
                history = await Service.list_invoice_history(user_jwt=token, company_id=COMPANY)
                attempt = next(row for row in history if str(row.client_id) == customer["id"])
                check(mode + "_ledger", attempt.status == ("failed" if mode == "rejection" else "uncertain"),
                      invoice_id=str(attempt.id), status=attempt.status, provider_calls=provider.await_count)
                edit = await client.patch(rest + "timesheets", params={"id": "eq." + entry["id"], "company_id": "eq." + STAGE},
                                          json={"description": "SIMULATED edit after failure"})
                check(mode + "_reservation", edit.status_code == 200 if mode == "rejection" else edit.status_code >= 400,
                      edit_status=edit.status_code)
                if mode != "rejection":
                    with patch.object(SzamlazzHuProvider, "create_invoice", AsyncMock()) as replay:
                        try:
                            await Service.issue_client_invoice(**kwargs, review_fingerprint=review.review_fingerprint)
                        except HTTPException as exc:
                            check(mode + "_replay_blocked", exc.status_code == 409 and replay.await_count == 0,
                                  status=exc.status_code, provider_calls=replay.await_count)
                    if mode == "response_loss":
                        resolved = await Service.reconcile_invoice(user_jwt=token, company_id=COMPANY,
                                        invoice_id=attempt.id, resolution="not_issued", invoice_number=None,
                                        net_total=None, gross_total=None)
                        check(mode + "_reconciled", resolved.status == "failed")
                        changed = await db("PATCH", "timesheets", {"description": "SIMULATED lock released"},
                                           id="eq." + entry["id"], company_id="eq." + STAGE)
                        check(mode + "_released", bool(changed))
                    else:
                        resolved = await Service.reconcile_invoice(user_jwt=token, company_id=COMPANY,
                                        invoice_id=attempt.id, resolution="issued", invoice_number="SIMULATED-2026-1",
                                        net_total=Decimal("100"), gross_total=Decimal("127"))
                        changed = await db("GET", "timesheets", id="eq." + entry["id"], company_id="eq." + STAGE)
                        check(mode + "_reconciled", resolved.status == "issued" and changed[0]["status"] == "invoiced",
                              pdf_available=resolved.pdf_available)
        finally:
            await db("PATCH", "companies", {"szamlazz_test_mode": True}, id="eq." + STAGE)


if __name__ == "__main__":
    asyncio.run(main())
