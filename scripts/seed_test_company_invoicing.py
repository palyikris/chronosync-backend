"""Prepare the user-requested Test Inc invoicing demo; never issue an invoice.

Only the exact Test Inc tenant and deterministic synthetic records are writable.
Run through the installed Szamlazz staging-key helper. Secrets stay in memory.
Existing accounts and timesheets are preserved. Synthetic employees cannot log in
after the temporary tenant-scoped verification session is revoked.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import secrets
import subprocess
from uuid import UUID, uuid5

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
COMPANY = "e4abcee0-f29b-4e51-bb26-326e3b38f388"
ADMIN = "98ba43b8-d9cc-4feb-abfa-c621670258cd"
SEED = "test-inc-accounting-demo-2026-11-04"
NAMESPACE = UUID("94c64dd8-3c93-4d64-90cb-c27ecc3c7872")
CFG = dotenv_values(ROOT.parent / "chronosync/.env")
BASE = str(CFG["VITE_SUPABASE_URL"]).rstrip("/")
API = str(CFG["VITE_API_BASE_URL"]).rstrip("/")
REPORT = ROOT / "docs/test-inc-invoicing-demo-2026-11-04.json"
assert BASE == "https://kqihfxhewjgrwoepdsau.supabase.co"


def uid(label):
    return str(uuid5(NAMESPACE, SEED + ":" + label))


def quote(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def sql(query):
    env = dict(os.environ, PGPASSWORD=str(CFG["SUPABASE_PASSWORD"]))
    dsn = ("host=db.kqihfxhewjgrwoepdsau.supabase.co port=5432 dbname=postgres "
           "user=postgres sslmode=verify-full "
           "sslrootcert=/tmp/chronosync-supabase-prod-ca-2021.crt connect_timeout=15")
    result = subprocess.run(["psql", dsn, "-XAtq", "-1", "-v", "ON_ERROR_STOP=1"],
                            input=query, env=env, capture_output=True, text=True, timeout=90)
    if result.returncode:
        # SQL and server context can contain credentials. Never print either.
        raise RuntimeError("Database operation failed: " + result.stderr.splitlines()[0][:180])
    return result.stdout.strip()


def guard():
    return f"""DO $$ BEGIN IF NOT EXISTS (
      SELECT 1 FROM public.companies c JOIN public.profiles p ON p.company_id=c.id
      JOIN auth.users u ON u.id=p.id
      WHERE c.id='{COMPANY}' AND c.name='Test Inc' AND c.is_active AND NOT c.is_deleted
        AND p.id='{ADMIN}' AND p.role='company_admin' AND p.is_active
        AND lower(u.email)='krizsamizsa@gmail.com'
    ) THEN RAISE EXCEPTION 'Unexpected target company or administrator'; END IF; END $$;
    """


PEOPLE = [
    ("Kovács Anna", "anna.kovacs", 40, "accounting"),
    ("Nagy Bence", "bence.nagy", 40, "accounting"),
    ("Tóth Eszter", "eszter.toth", 40, "accounting"),
    ("Szabó Dóra", "dora.szabo", 40, "payroll"),
    ("Varga Máté", "mate.varga", 40, "payroll"),
    ("Kiss Réka", "reka.kiss", 40, "advisory"),
    ("Farkas Levente", "levente.farkas", 30, "advisory"),
    ("Molnár Zsófia", "zsofia.molnar", 20, "accounting"),
]

# All names, addresses, tax IDs, rates and purchase orders below are synthetic.
CLIENTS = [
    ("QD1", "DunaPart Digital Kft. (teszt)", "HU", "1138", "Budapest", "Minta fasor 12.", "12345676-2-41", None, 1, "HUF", "27", "hu", 15, 18, [125000, 4200, 24000]),
    ("QD2", "Pannon Műhely Kft. (teszt)", "HU", "9027", "Győr", "Példa ipartelep 8.", "23456787-2-08", None, 1, "HUF", "27", "hu", 30, 42, [225000, 3800, 28000]),
    ("QD3", "ZöldLiget Webshop Kft. (teszt)", "HU", "6724", "Szeged", "Minta park 5.", "34567898-2-06", None, 1, "HUF", "27", "hu", 15, 12, [175000, 4200, 26000]),
    ("QD4", "Körút Gasztro Kft. (teszt)", "HU", "4026", "Debrecen", "Teszt köz 3.", "45678909-2-09", None, 1, "HUF", "27", "hu", 8, 9, [95000, 4000, 22000]),
    ("QD5", "Danube Ventures GmbH (teszt)", "DE", "10115", "Berlin", "Musterstrasse 18", None, "DE123456789", 6, "EUR", "EUFAD37", "en", 30, 26, [850, 18, 85]),
    ("QD6", "Hídpont Consulting Kft. (teszt)", "HU", "1117", "Budapest", "Példa sétány 6.", "56789010-2-43", None, 1, "HUF", "27", "hu", 15, 14, [16500, 1800, 27500]),
]


def fixtures():
    people = [dict(id=uid("employee:" + slug), name=name + " (teszt)",
                   email=slug + "+chronosync-demo@example.com", weekly_hours=hours, department=department)
              for name, slug, hours, department in PEOPLE]
    clients, projects, entries = [], [], []
    for index, data in enumerate(CLIENTS):
        code, name, country, postal, city, address, tax, eu, subject, currency, vat, lang, terms, headcount, rates = data
        customer = dict(id=uid("client:" + code), code=code, name=name, country=country,
                        postal=postal, city=city, address=address, tax=tax, eu=eu,
                        tax_subject=subject, currency=currency, vat=vat, language=lang,
                        terms=terms, headcount=headcount, budget=[38, 65, 48, 29, 42, 36][index])
        clients.append(customer)
        for kind, label, unit, rate in zip(
            ["accounting", "payroll", "advisory"],
            ["Könyvelés és havi zárás", "Bérszámfejtés és TB ügyintézés", "Adó- és üzletviteli tanácsadás"],
            ["óra" if code == "QD6" else "hó", "óra" if code == "QD6" else "fő", "óra"], rates,
        ):
            projects.append(dict(id=uid("project:" + code + ":" + kind), client_id=customer["id"],
                                 code=code, kind=kind, name=label, unit=unit, rate=rate,
                                 currency=currency, headcount=headcount if kind == "payroll" else None,
                                 quantity=None if unit == "óra" else (1 if unit == "hó" else headcount)))
    projects_by_key = {(p["code"], p["kind"]): p for p in projects}
    days = []
    day = date(2026, 10, 1)
    while day <= date(2026, 11, 4):
        if day.weekday() < 5 and day != date(2026, 10, 23):
            days.append(day)
        day += timedelta(days=1)
    descriptions = {
        "accounting": ["Bejövő számlák ellenőrzése és könyvelése", "Bankkivonatok egyeztetése, nyitott tételek rendezése", "Főkönyvi egyeztetés és havi zárási munkalap", "Áfaanalitika és számlaeltérések ellenőrzése", "Vevő- és szállítófolyószámlák egyeztetése"],
        "payroll": ["Jelenléti ívek és távollétek ellenőrzése", "Havi bérszámfejtés előkészítése és ellenőrzése", "Belépő és kilépő dolgozók TB adminisztrációja", "Bérjegyzékek és járulékbevallás egyeztetése", "Cafeteria és költségtérítések feldolgozása"],
        "advisory": ["Adózási kérdések áttekintése és ügyfélegyeztetés", "Havi vezetői riport és cash-flow megbeszélés", "Áfakezelési állásfoglalás előkészítése", "Szerződéses és számviteli kérdések elemzése", "Kontrolling kimutatás és eredményelemzés"],
    }
    for person_index, person in enumerate(people):
        for day_index, day in enumerate(days):
            hours = {40: [3.5, 2.5, 2], 30: [2.5, 2, 1.5], 20: [1.5, 1.5, 1]}[person["weekly_hours"]]
            for slot, logged in enumerate(hours):
                code = CLIENTS[(person_index + day_index // 2 + slot * 2) % len(CLIENTS)][0]
                kind = person["department"]
                # Seniors occasionally review related work; keep each employee's main specialty.
                if person_index in (0, 5) and slot == 2:
                    kind = "advisory" if kind == "accounting" else "accounting"
                project = projects_by_key[(code, kind)]
                status = "approved" if day.month == 10 else ("submitted" if day.day < 4 else "draft")
                # A few October corrections exercise the approval screen without blocking billing.
                if day == date(2026, 10, 30) and slot == 2 and person_index in (2, 7):
                    status = "submitted"
                if day == date(2026, 10, 29) and slot == 2 and person_index == 6:
                    status = "rejected"
                multiplier = 4 if code == "QD6" and kind == "payroll" and slot == 1 else 1
                created = f"{day.isoformat()}T16:30:00+01:00" if day >= date(2026, 10, 25) else f"{day.isoformat()}T16:30:00+02:00"
                entries.append(dict(id=uid(f"entry:{person['id']}:{day}:{slot}"), user_id=person["id"],
                    client_id=project["client_id"], project_id=project["id"], work_date=day.isoformat(),
                    hours_logged=logged, description=descriptions[kind][(day_index + slot) % 5] + " [tesztadat]",
                    status=status, billing_multiplier=multiplier, created_at=created,
                    rejection_reason="A megjegyzés pontosítása szükséges: melyik időszakot érintette az egyeztetés?" if status == "rejected" else None))
    return people, clients, projects, entries


def table_insert(table, columns, rows, *, conflict="id"):
    values = []
    for row in rows:
        values.append("(" + ",".join("TRUE" if row.get(c) is True else "FALSE" if row.get(c) is False else quote(row.get(c)) for c in columns) + ")")
    return f"INSERT INTO public.{table} ({','.join(columns)}) VALUES " + ",\n".join(values) + f" ON CONFLICT ({conflict}) DO NOTHING;\n"


def seed():
    people, clients, projects, entries = fixtures()
    statement = guard()+f"""UPDATE public.companies SET enabled_modules=(
      SELECT ARRAY(SELECT DISTINCT unnest(enabled_modules || ARRAY['leave_attachments','invoicing']::text[]) ORDER BY 1))
      WHERE id='{COMPANY}' AND NOT ('invoicing'=ANY(enabled_modules));\n"""
    # Detect collisions or an edited previous demo; never repurpose an existing person.
    for person in people:
        statement += f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM auth.users WHERE
          (id={quote(person['id'])} OR lower(email)={quote(person['email'])})
          AND (id<>{quote(person['id'])} OR raw_user_meta_data->>'demo_seed' IS DISTINCT FROM {quote(SEED)}))
          THEN RAISE EXCEPTION 'Synthetic identity collision'; END IF; END $$;
        INSERT INTO auth.users(instance_id,id,aud,role,email,encrypted_password,email_confirmed_at,
          confirmation_token,recovery_token,email_change_token_new,email_change,
          raw_app_meta_data,raw_user_meta_data,created_at,updated_at,banned_until)
        VALUES ('00000000-0000-0000-0000-000000000000',{quote(person['id'])},'authenticated','authenticated',
          {quote(person['email'])},'',NULL,'','','','', '{{"provider":"email","providers":["email"]}}'::jsonb,
          {quote(json.dumps({'full_name':person['name'],'demo_seed':SEED},ensure_ascii=False))}::jsonb,
          now(),now(),'infinity') ON CONFLICT(id) DO NOTHING;
        UPDATE public.profiles SET company_id='{COMPANY}',full_name={quote(person['name'])},
          role='regular',is_active=true,weekly_work_hours={person['weekly_hours']}
          WHERE id={quote(person['id'])} AND (company_id IS NULL OR company_id='{COMPANY}');
        """
    for client in clients:
        statement += table_insert("clients", ["id","company_id","name","client_code","invoice_attachment_language","available_hours_per_month"], [dict(
            id=client["id"],company_id=COMPANY,name=client["name"],client_code=client["code"],
            invoice_attachment_language=client["language"],available_hours_per_month=client["budget"])])
        statement += table_insert("client_billing_profiles", ["client_id","company_id","billing_name","billing_country_code","billing_postal_code","billing_city","billing_address","tax_subject","tax_number","eu_vat_number","billing_email","send_invoice_email","invoice_language","payment_method","payment_term_days","vat_code","automatic_invoicing_enabled","attach_timesheet_report"], [dict(
            client_id=client["id"],company_id=COMPANY,billing_name=client["name"],billing_country_code=client["country"],
            billing_postal_code=client["postal"],billing_city=client["city"],billing_address=client["address"],
            tax_subject=client["tax_subject"],tax_number=client["tax"],eu_vat_number=client["eu"],
            billing_email=f"billing.{client['code'].lower()}@example.com",send_invoice_email=False,
            invoice_language=client["language"],payment_method="Átutalás",payment_term_days=client["terms"],
            vat_code=client["vat"],automatic_invoicing_enabled=False,attach_timesheet_report=True)], conflict="client_id")
        # Correct fixture-only tax IDs on a resumed setup; never alter issued data.
        statement += f"""UPDATE public.client_billing_profiles SET tax_number={quote(client['tax'])}
          WHERE client_id={quote(client['id'])} AND company_id='{COMPANY}'
            AND tax_number IS DISTINCT FROM {quote(client['tax'])}
            AND NOT EXISTS (SELECT 1 FROM public.invoice_ledger WHERE client_id={quote(client['id'])});\n"""
    for project in projects:
        statement += table_insert("projects", ["id","company_id","client_id","name","estimated_hours_per_month","is_payroll","payroll_headcount"], [dict(
            id=project["id"],company_id=COMPANY,client_id=project["client_id"],name=project["name"],
            estimated_hours_per_month=18 if project["kind"]=="accounting" else 12,
            is_payroll=project["kind"]=="payroll",payroll_headcount=project["headcount"])])
        statement += table_insert("project_pricing", ["project_id","company_id","pricing_year","original_fee","indexed_fee","original_currency","original_unit","currency","unit","index_code","price_origin","po_number","attachment_note"], [dict(
            project_id=project["id"],company_id=COMPANY,pricing_year=2026,original_fee=project["rate"],indexed_fee=project["rate"],
            original_currency=project["currency"],original_unit=project["unit"],currency=project["currency"],unit=project["unit"],
            index_code="none",price_origin="manual",po_number=f"DEMO-2026-{project['code']}",
            attachment_note="Kizárólag tesztelési célú, fiktív szerződéses díj.")], conflict="project_id,pricing_year")
    for entry in entries:
        entry.update(company_id=COMPANY,approved_by=ADMIN if entry["status"]=="approved" else None,
                     approved_at="2026-11-02T09:00:00+01:00" if entry["status"]=="approved" else None,
                     submitted_at=entry["created_at"] if entry["status"]!="draft" else None)
    statement += table_insert("timesheets", ["id","user_id","company_id","client_id","project_id","work_date","hours_logged","description","status","billing_multiplier","created_at","submitted_at","approved_by","approved_at","rejection_reason"], entries)
    statement += "SELECT 'seed_complete';"
    assert sql(statement).endswith("seed_complete")
    return people, clients, projects, entries


def verify(people, clients, projects, entries):
    if not os.environ.get("SZAMLAZZ_AGENT_KEY"):
        raise RuntimeError("Run with the saved staging-key helper")
    checks = []
    report = dict(company_id=COMPANY,company_name="Test Inc",seed=SEED,checks=checks,
                  added=dict(employees=len(people),clients=len(clients),projects=len(projects),timesheets=len(entries)),
                  invoice_creation_calls=0,email_sends=0,clients=[])

    def check(name, passed, **details):
        row = dict(check=name,passed=bool(passed),**details)
        checks.append(row)
        REPORT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps(row,ensure_ascii=False),flush=True)
        if not passed:
            raise RuntimeError("Verification failed: " + name)

    verifier = people[0]
    password = secrets.token_urlsafe(40)
    session = None
    # Temporary credentials apply only to this newly seeded fictional employee.
    sql(guard()+f"""UPDATE auth.users SET encrypted_password=extensions.crypt({quote(password)},extensions.gen_salt('bf')),
      banned_until=NULL,email_confirmed_at=now() WHERE id={quote(verifier['id'])}
      AND raw_user_meta_data->>'demo_seed'={quote(SEED)};
      UPDATE public.profiles SET role='company_admin' WHERE id={quote(verifier['id'])} AND company_id='{COMPANY}';""")
    try:
        with httpx.Client(timeout=90,headers={"apikey":str(CFG["VITE_SUPABASE_PUBLISHABLE_KEY"])}) as client:
            login = client.post(BASE+"/auth/v1/token?grant_type=password",json={"email":verifier["email"],"password":password})
            check("synthetic_tenant_admin_signin",login.status_code==200,status=login.status_code)
            session=login.json()
            client.headers["Authorization"]="Bearer "+session["access_token"]
            profile=client.get(BASE+"/rest/v1/profiles",params={"id":"eq."+verifier["id"],"select":"id,company_id,role,is_active"})
            check("synthetic_admin_profile_ready",profile.status_code==200 and len(profile.json())==1
                  and profile.json()[0]["role"]=="company_admin",status=profile.status_code,profile=profile.json())
            saved=client.put(API+"/company/invoice-settings",json={"company_id":COMPANY,
                "invoice_provider":"szamlazz_hu","api_key":os.environ["SZAMLAZZ_AGENT_KEY"]})
            check("staging_key_saved_by_deployed_backend",saved.status_code==200 and saved.json().get("api_key_configured") is True,status=saved.status_code,
                  detail=None if saved.status_code==200 else saved.json().get("detail","unknown"))
            company=client.get(BASE+"/rest/v1/companies",params={"select":"id,name","id":"eq."+COMPANY})
            check("authenticated_company_scope",company.status_code==200 and company.json()==[{"id":COMPANY,"name":"Test Inc"}])
            team=client.post(BASE+"/rest/v1/rpc/get_company_members",json={})
            check("synthetic_employees_visible_in_team",team.status_code==200 and {p["id"] for p in people}.issubset({p["id"] for p in team.json()}))
            before=json.loads(sql(f"SELECT json_build_object('invoices',count(*)) FROM invoice_ledger WHERE company_id='{COMPANY}';"))
            for customer in clients:
                own_projects=[p for p in projects if p["client_id"]==customer["id"]]
                quantities={p["id"]:str(p["quantity"]) for p in own_projects if p["quantity"] is not None}
                body=dict(company_id=COMPANY,client_id=customer["id"],period_start="2026-10-01",period_end="2026-10-31",project_quantities=quantities)
                response=client.post(API+"/invoices/client-review",json=body)
                check(customer["code"]+"_hosted_review",response.status_code==200,status=response.status_code,
                      detail=None if response.status_code==200 else response.json().get("detail","unknown")[:220])
                review=response.json()
                expected_entries=[e for e in entries if e["client_id"]==customer["id"] and e["status"]=="approved"]
                check(customer["code"]+"_approved_work_only",{e["id"] for e in review["approved_entries"]}=={e["id"] for e in expected_entries}
                      and review["recipient"]["send_email"] is False,entries=len(review["approved_entries"]))
                expected_net=Decimal(0)
                expected_vat=Decimal(0)
                for p in own_projects:
                    quantity=Decimal(str(p["quantity"])) if p["quantity"] is not None else sum(
                        (Decimal(str(e["hours_logged"]))*e["billing_multiplier"] for e in expected_entries if e["project_id"]==p["id"]),Decimal(0))
                    precision=Decimal("1") if customer["currency"]=="HUF" else Decimal("0.01")
                    net=(quantity*Decimal(str(p["rate"]))).quantize(precision,rounding=ROUND_HALF_UP)
                    expected_net+=net
                    expected_vat+=(net*Decimal("0.27")).quantize(precision,rounding=ROUND_HALF_UP) if customer["vat"]=="27" else Decimal(0)
                check(customer["code"]+"_amounts_match_saved_project_prices",Decimal(review["net_total"])==expected_net
                      and Decimal(review["gross_total"])==expected_net+expected_vat,net=review["net_total"],gross=review["gross_total"],currency=review["currency"])
                pdf=client.post(API+"/invoices/client-preview",json=body)
                check(customer["code"]+"_provider_pdf_preview",pdf.status_code==200 and pdf.content.startswith(b"%PDF"),status=pdf.status_code,
                      pdf_bytes=len(pdf.content) if pdf.status_code==200 else 0,
                      detail=None if pdf.status_code==200 else pdf.json().get("detail","unknown")[:220])
                output=ROOT/"docs/test-inc-invoicing-demo-previews"
                output.mkdir(exist_ok=True)
                (output/(customer["code"]+"-2026-10-preview.pdf")).write_bytes(pdf.content)
                report["clients"].append(dict(code=customer["code"],name=customer["name"],currency=customer["currency"],
                    client_id=customer["id"],net=review["net_total"],vat=review["vat_total"],gross=review["gross_total"],
                    approved_entries=len(review["approved_entries"]),manual_quantities={p["name"]:p["quantity"] for p in own_projects if p["quantity"] is not None}))
            after=json.loads(sql(f"SELECT json_build_object('invoices',count(*)) FROM invoice_ledger WHERE company_id='{COMPANY}';"))
            check("previews_did_not_create_invoices",before==after,invoice_count=after["invoices"])
            # The user intends to issue manually using the dedicated saved test-account key.
            # This is the application preview gate, not the provider's Tesztuzem switch.
            r=client.patch(BASE+"/rest/v1/companies",params={"id":"eq."+COMPANY,"select":"id,szamlazz_test_mode"},json={"szamlazz_test_mode":False},headers={"Prefer":"return=representation"})
            check("manual_test_account_issuance_enabled",r.status_code==200 and len(r.json())==1 and r.json()[0]["szamlazz_test_mode"] is False)
    finally:
        if session:
            with httpx.Client(timeout=30,headers={"apikey":str(CFG["VITE_SUPABASE_PUBLISHABLE_KEY"]),"Authorization":"Bearer "+session["access_token"]}) as client:
                client.post(BASE+"/auth/v1/logout?scope=global")
        sql(guard()+f"""UPDATE public.profiles SET role='regular' WHERE id={quote(verifier['id'])} AND company_id='{COMPANY}';
          UPDATE auth.users SET encrypted_password='',banned_until='infinity',email_confirmed_at=NULL
          WHERE id={quote(verifier['id'])} AND raw_user_meta_data->>'demo_seed'={quote(SEED)};
          DELETE FROM auth.sessions WHERE user_id={quote(verifier['id'])};""")
        check("temporary_admin_access_removed",json.loads(sql(f"""SELECT json_build_object('safe',p.role='regular' AND u.banned_until='infinity' AND u.encrypted_password='' AND NOT EXISTS (SELECT 1 FROM auth.sessions s WHERE s.user_id=p.id)) FROM profiles p JOIN auth.users u ON u.id=p.id WHERE p.id={quote(verifier['id'])} AND p.company_id='{COMPANY}';"""))["safe"])
    REPORT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    people,clients,projects,entries=fixtures()
    if not args.apply:
        print(json.dumps(dict(company_id=COMPANY,employees=len(people),clients=len(clients),projects=len(projects),timesheets=len(entries),hours=str(sum(Decimal(str(e["hours_logged"])) for e in entries)),dates=[min(e["work_date"] for e in entries),max(e["work_date"] for e in entries)])))
        return
    if not os.environ.get("SZAMLAZZ_AGENT_KEY"):
        raise RuntimeError("Run through the saved staging-key helper")
    people,clients,projects,entries=seed()
    print(json.dumps(dict(seed_complete=True,employees=len(people),clients=len(clients),projects=len(projects),timesheets=len(entries))),flush=True)
    verify(people,clients,projects,entries)


if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps(dict(failed=True,error_type=type(exc).__name__,message=str(exc) if isinstance(exc,RuntimeError) else "Operation failed; no credentials logged")),flush=True)
        raise SystemExit(1)
