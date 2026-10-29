"""Checks restricted to the approved ChronoSync staging tenant. Never sends email.

The temporary session file is private and must be removed with the disarm action.
Only blocked issuance checks are sent; no provider invoice creation is permitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
CFG = dotenv_values(ROOT.parent / "chronosync/.env")
STAGE = "d2026102-7000-4000-8000-000000000001"
NAME = "ChronoSync staging E2E 2026-10-27"
ADMIN = "d2026102-7000-4000-8000-000000000010"
WORKER = "683d5d37-18d4-4ddc-86fa-206033552ce1"
INVOICE = "b997cfb2-9595-43db-8e7f-5fed27e4dfc7"
BASE = str(CFG["VITE_SUPABASE_URL"]).rstrip("/")
API = str(CFG["VITE_API_BASE_URL"]).rstrip("/")
assert BASE == "https://kqihfxhewjgrwoepdsau.supabase.co"
PRIVATE = Path("/tmp/chronosync-hosted-rollout-20261028")
PRIVATE.mkdir(mode=0o700, exist_ok=True)
SESSION = PRIVATE / "session.json"
RESULT = ROOT / "docs/hosted-rollout-results-2026-10-28.json"
REDACT = [str(CFG["SUPABASE_PASSWORD"])]


def sql(query: str) -> str:
    env = os.environ.copy()
    env["PGPASSWORD"] = str(CFG["SUPABASE_PASSWORD"])
    dsn = ("host=db.kqihfxhewjgrwoepdsau.supabase.co port=5432 dbname=postgres "
           "user=postgres sslmode=verify-full "
           "sslrootcert=/tmp/chronosync-supabase-prod-ca-2021.crt connect_timeout=10")
    r = subprocess.run(["psql", dsn, "-XAtq", "-1", "-v", "ON_ERROR_STOP=1"],
                       input=query, env=env, text=True, capture_output=True, timeout=45)
    if r.returncode:
        message = r.stderr
        for value in REDACT:
            message = message.replace(value, "[redacted]")
        raise RuntimeError(message[:800])
    return r.stdout.strip()


def stage_guard() -> str:
    return f"""DO $$ BEGIN IF NOT EXISTS (
      SELECT 1 FROM public.companies WHERE id='{STAGE}' AND name='{NAME}' AND NOT is_deleted
    ) THEN RAISE EXCEPTION 'Unexpected staging company'; END IF; END $$;\n"""


def setup(include_worker=True):
    if SESSION.exists():
        raise RuntimeError("Existing staging session must be reused or disarmed first")
    people = json.loads(sql(f"""select json_agg(json_build_object('id',p.id,'email',u.email,
      'role',p.role,'active',p.is_active,'email_confirmed',u.email_confirmed_at is not null)) from public.profiles p join auth.users u on u.id=p.id
      where p.company_id='{STAGE}' and p.id in ('{ADMIN}','{WORKER}');"""))
    assert {p["id"] for p in people} == {ADMIN, WORKER}
    assert next(p for p in people if p["id"] == ADMIN)["role"] == "company_admin"
    selected = people if include_worker else [p for p in people if p["id"] == ADMIN]
    passwords = {p["id"]: secrets.token_urlsafe(32) for p in selected}
    REDACT.extend(passwords.values())
    changes = stage_guard() + f"update public.companies set is_active=true where id='{STAGE}';\n"
    if include_worker:
        changes += f"update public.profiles set is_active=true where id='{WORKER}' and company_id='{STAGE}';\n"
        # Requires explicit user approval for this temporary fixture confirmation.
        changes += f"update auth.users set email_confirmed_at=coalesce(email_confirmed_at,now()) where id='{WORKER}';\n"
    for person in selected:
        changes += ("update auth.users set banned_until=null,encrypted_password=extensions.crypt("
                    f"'{passwords[person['id']]}',extensions.gen_salt('bf')) where id='{person['id']}';\n")
    sql(changes)
    state = {"company_id": STAGE, "original_worker_active": next(p for p in people if p["id"] == WORKER)["active"],
             "original_worker_confirmed": next(p for p in people if p["id"] == WORKER)["email_confirmed"],
             "temporary_worker_confirmation": include_worker}
    fd = os.open(SESSION, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    try:
        with httpx.Client(timeout=30, headers={"apikey": str(CFG["VITE_SUPABASE_PUBLISHABLE_KEY"])}) as client:
            for label, identity in [("admin", ADMIN), ("worker", WORKER)]:
                if identity not in passwords:
                    continue
                person = next(p for p in people if p["id"] == identity)
                response = client.post(BASE + "/auth/v1/token?grant_type=password",
                    json={"email": person["email"], "password": passwords[identity]})
                if response.status_code != 200:
                    raise RuntimeError(f"Staging {label} sign-in failed: HTTP {response.status_code}, code={response.json().get('error_code','unknown')}")
                state[label] = response.json()
                state[label + "_password"] = passwords[identity]
                SESSION.write_text(json.dumps(state))
        print(json.dumps({"staging_logins_ready": True, "company_id": STAGE}), flush=True)
    except Exception:
        disarm()
        raise


def disarm():
    state = json.loads(SESSION.read_text()) if SESSION.exists() else {}
    assert not state or state["company_id"] == STAGE
    for label in ("admin", "worker"):
        if state.get(label):
            try:
                httpx.post(BASE + "/auth/v1/logout", headers={
                    "apikey": str(CFG["VITE_SUPABASE_PUBLISHABLE_KEY"]),
                    "Authorization": "Bearer " + state[label]["access_token"]}, timeout=15)
            except httpx.HTTPError:
                pass
    restore_confirmation = (f"update auth.users set email_confirmed_at=null where id='{WORKER}';\n"
                            if state.get('temporary_worker_confirmation') and state.get('original_worker_confirmed') is False else '')
    sql(stage_guard() + restore_confirmation + f"""update public.companies set is_active=false where id='{STAGE}';
      update public.profiles set is_active=false where id='{WORKER}' and company_id='{STAGE}';
      update auth.users set banned_until='2126-10-27T00:00:00Z' where id in ('{ADMIN}','{WORKER}');
      select json_build_object('company_active',(select is_active from public.companies where id='{STAGE}'),
        'banned_logins',(select count(*) from auth.users where id in ('{ADMIN}','{WORKER}') and banned_until>now()));""")
    SESSION.unlink(missing_ok=True)
    print(json.dumps({"staging_disarmed": True, "temporary_session_removed": True}), flush=True)


def checks():
    state = json.loads(SESSION.read_text())
    assert state["company_id"] == STAGE
    results = json.loads(RESULT.read_text())["checks"] if state.get("new_entry_id") else []

    def check(name, passed, **details):
        row = {"check": name, "passed": bool(passed), **details}
        results.append(row)
        RESULT.write_text(json.dumps({"company_id": STAGE, "checks": results}, indent=2))
        print(json.dumps(row), flush=True)
        if not passed:
            raise AssertionError(name)

    def client(label):
        return httpx.Client(headers={"apikey": str(CFG["VITE_SUPABASE_PUBLISHABLE_KEY"]),
            "Authorization": "Bearer " + state[label]["access_token"],
            "Prefer": "return=representation"}, timeout=60)

    has_worker = "worker" in state
    owner = WORKER if has_worker else ADMIN
    with client("admin") as admin, client("worker" if has_worker else "admin") as worker:
        def db(who, method, table, data=None, **params):
            r = who.request(method, BASE + "/rest/v1/" + table, json=data, params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {table}: HTTP {r.status_code}: {r.text[:400]}")
            return r.json() if r.content else None

        for label, who in [("admin", admin), ("worker", worker)]:
            if label == "worker" and not has_worker:
                continue
            r = who.post(BASE + "/rest/v1/rpc/claim_company_invitation", json={})
            check(label + "_membership_claim", r.status_code == 200, status=r.status_code)
            people = db(who, "GET", "profiles", select="id,company_id,role,is_active")
            check(label + "_profile_isolation", all(p["company_id"] == STAGE for p in people), visible=len(people))
            companies = db(who, "GET", "companies", select="id,name,is_active")
            check(label + "_company_isolation", len(companies) == 1 and companies[0]["id"] == STAGE)
            r = who.get(BASE + "/rest/v1/companies", params={"select": "invoice_api_key_encrypted"})
            check(label + "_credential_columns_blocked", r.status_code == 403, status=r.status_code)
        members = admin.post(BASE + "/rest/v1/rpc/get_company_members", json={})
        check("admin_team_listing", members.status_code == 200 and len(members.json()) == 2)
        if has_worker:
            members = worker.post(BASE + "/rest/v1/rpc/get_company_members", json={})
            check("worker_team_emails_blocked", members.status_code == 403, status=members.status_code)
        r = admin.get(API + "/company/invoice-settings", params={"company_id": STAGE})
        check("hosted_encrypted_key_lookup", r.status_code == 200 and r.json().get("api_key_configured") is True,
              status=r.status_code)
        if has_worker:
            r = worker.get(API + "/company/invoice-settings", params={"company_id": STAGE})
            check("worker_invoice_settings_blocked", r.status_code == 403, status=r.status_code)
        r = admin.get(API + "/company/invoice-settings", params={"company_id": "00000000-0000-4000-8000-000000000001"})
        check("cross_tenant_backend_blocked", r.status_code == 403, status=r.status_code)

        if not state.get("new_entry_id"):
            code = "Z" + uuid4().hex[:2].upper()
            customer = db(admin, "POST", "clients", {"company_id": STAGE, "name": "E2E deployed rollout " + code, "client_code": code})[0]
            project = db(admin, "POST", "projects", {"company_id": STAGE, "client_id": customer["id"], "name": "Deployed rollout work"})[0]
            check("client_project_create", project["client_id"] == customer["id"])
            for table, identity in [("clients", customer["id"]), ("projects", project["id"])]:
                changed = db(admin, "PATCH", table, {"name": "E2E deployed rollout verified", "is_active": False}, id="eq." + identity, company_id="eq." + STAGE)
                check(table + "_edit_deactivate", len(changed) == 1 and changed[0]["is_active"] is False)
                db(admin, "PATCH", table, {"is_active": True}, id="eq." + identity, company_id="eq." + STAGE)
            entry = db(worker, "POST", "timesheets", {"company_id": STAGE, "user_id": owner,
                "client_id": customer["id"], "project_id": project["id"], "work_date": "2026-10-28",
                "hours_logged": 1.25, "description": "Hosted rollout entry", "status": "draft"})[0]
            updated = db(worker, "PATCH", "timesheets", {"hours_logged": 2.5, "description": "Hosted rollout entry edited", "status": "submitted"}, id="eq." + entry["id"], company_id="eq." + STAGE)[0]
            check(("worker" if has_worker else "admin") + "_entry_edit_submit", updated["status"] == "submitted" and float(updated["hours_logged"]) == 2.5)
            db(admin, "PATCH", "timesheets", {"status": "approved", "approved_by": ADMIN,
                "approved_at": "2026-10-28T09:00:00Z"}, id="eq." + entry["id"], company_id="eq." + STAGE)
            baseline = db(admin, "GET", "client_billing_profiles", company_id="eq." + STAGE, limit="1")[0]
            profile = {k: v for k, v in baseline.items() if k not in {"created_at", "updated_at"}}
            profile.update(client_id=customer["id"], billing_name="E2E deployed rollout customer", net_unit_price=10000,
                           billing_model="hourly", currency="HUF", vat_code="27", send_invoice_email=False)
            db(admin, "POST", "client_billing_profiles", profile)
            body = {"company_id": STAGE, "client_id": customer["id"], "period_start": "2026-10-01", "period_end": "2026-10-31"}
            r = admin.post(API + "/invoices/client-review", json=body)
            check("hosted_invoice_review", r.status_code == 200, status=r.status_code)
            review = r.json()
            check("hosted_review_totals", float(review["quantity"]) == 2.5 and float(review["net_total"]) == 25000 and float(review["gross_total"]) == 31750)
            r = admin.post(API + "/invoices/client-preview", json=body)
            check("hosted_provider_preview", r.status_code == 200 and r.content.startswith(b"%PDF"), status=r.status_code, bytes=len(r.content))
            (PRIVATE / "deployed-preview.pdf").write_bytes(r.content)
            assert sql(f"select szamlazz_test_mode from public.companies where id='{STAGE}';") == "t", "Blocked-issuance check requires preview mode"
            r = admin.post(API + "/invoices/client-issue", json={**body, "review_fingerprint": review["review_fingerprint"], "confirm": True})
            check("preview_mode_blocks_issuance", r.status_code == 409, status=r.status_code)
            db(admin, "PATCH", "client_billing_profiles", {"net_unit_price": 10001}, client_id="eq." + customer["id"], company_id="eq." + STAGE)
            r = admin.post(API + "/invoices/client-issue", json={**body, "review_fingerprint": review["review_fingerprint"], "confirm": True})
            check("stale_review_blocked", r.status_code == 409, status=r.status_code)
            db(admin, "PATCH", "client_billing_profiles", {"net_unit_price": 10000}, client_id="eq." + customer["id"], company_id="eq." + STAGE)
            state["new_client_id"] = customer["id"]
            state["new_project_id"] = project["id"]
            state["new_entry_id"] = entry["id"]
            SESSION.write_text(json.dumps(state))

        # Configure the test tenant's missing leave mapping through the same Data API as the UI.
        mapping = db(admin, "PATCH", "companies", {"leave_client_id": state["new_client_id"], "leave_project_id": state["new_project_id"]}, id="eq." + STAGE, select="id,leave_client_id,leave_project_id")
        check("staging_leave_configuration", len(mapping) == 1 and mapping[0]["leave_project_id"] == state["new_project_id"])
        pending = db(worker, "GET", "leave_requests", company_id="eq." + STAGE, user_id="eq." + owner, start_date="eq.2026-11-02", end_date="eq.2026-11-03", status="eq.PENDING")
        leave = pending[0] if pending else db(worker, "POST", "leave_requests", {"company_id": STAGE, "user_id": owner,
            "start_date": "2026-11-02", "end_date": "2026-11-03", "weekly_work_hours": 40, "status": "PENDING"})[0]
        if has_worker:
            r = worker.post(BASE + "/rest/v1/rpc/review_leave_request", json={"p_id": leave["id"], "p_status": "APPROVED"})
            check("worker_cannot_self_approve_leave", r.status_code == 403, status=r.status_code)
        approved = db(admin, "POST", "rpc/review_leave_request", {"p_id": leave["id"], "p_status": "APPROVED"})
        linked = db(worker, "GET", "timesheets", company_id="eq." + STAGE, leave_request_id="eq." + leave["id"])
        check("leave_approval_atomic_entries", approved["status"] == "APPROVED" and len(linked) == 2 and sum(float(t["hours_logged"]) for t in linked) == 16)
        r = worker.delete(BASE + "/rest/v1/leave_requests", params={"id": "eq." + leave["id"], "company_id": "eq." + STAGE})
        check("direct_approved_leave_delete_blocked", r.status_code == 403, status=r.status_code)
        db(worker, "POST", "rpc/cancel_leave_request", {"p_id": leave["id"]})
        after_leave = db(worker, "GET", "leave_requests", id="eq." + leave["id"], company_id="eq." + STAGE)
        after_entries = db(worker, "GET", "timesheets", company_id="eq." + STAGE, leave_request_id="eq." + leave["id"])
        check("owner_leave_cancellation_atomic", not after_leave and not after_entries)

        r = admin.get(API + "/invoices/" + INVOICE + "/pdf", params={"company_id": STAGE})
        original_hash = "5565cafe056b6872d7566408a0008b791cb5fe99b84a3497146ad8c06f94fa67"
        check("existing_issued_pdf", r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == original_hash,
              status=r.status_code, bytes=len(r.content))
        # Exercise the deployed PDF-only recovery path, preserving a private backup.
        backup = sql(f"select encode(pdf_bytes, 'base64') from public.invoice_ledger where id='{INVOICE}' and company_id='{STAGE}' and invoice_number='TESZT-2026-5' and status='issued';")
        assert backup
        (PRIVATE / "original-invoice-pdf.base64").write_text(backup)
        sql(stage_guard() + f"update public.invoice_ledger set pdf_bytes=null,pdf_available=false where id='{INVOICE}' and company_id='{STAGE}' and invoice_number='TESZT-2026-5' and status='issued';")
        try:
            r = admin.get(API + "/invoices/" + INVOICE + "/pdf", params={"company_id": STAGE})
            check("hosted_existing_pdf_recovery", r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == original_hash,
                  status=r.status_code, bytes=len(r.content))
            second = admin.get(API + "/invoices/" + INVOICE + "/pdf", params={"company_id": STAGE})
            check("pdf_recovery_idempotent", second.status_code == 200 and second.content == r.content)
        finally:
            if sql(f"select pdf_bytes is null from public.invoice_ledger where id='{INVOICE}' and company_id='{STAGE}';") == "t":
                sql(stage_guard() + f"update public.invoice_ledger set pdf_bytes=decode('{backup}','base64'),pdf_available=true where id='{INVOICE}' and company_id='{STAGE}' and status='issued';")
            (PRIVATE / "original-invoice-pdf.base64").unlink(missing_ok=True)
        print(json.dumps({"checks_passed": len(results), "invoice_creation_calls": 0, "emails_sent": 0}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "setup-admin", "checks", "disarm"])
    args = parser.parse_args()
    {"setup": setup, "setup-admin": lambda: setup(False), "checks": checks, "disarm": disarm}[args.action]()
