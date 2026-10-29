"""Saved-profile to real provider preview matrix, restricted to the approved tenant."""
import argparse,json
from pathlib import Path
import httpx
from dotenv import dotenv_values
STAGE='d2026102-7000-4000-8000-000000000001'
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--session-file',type=Path,required=True);a=p.parse_args()
s=json.loads(a.session_file.read_text());assert s['company_id']==STAGE
cfg=dotenv_values(Path(__file__).resolve().parents[2]/'chronosync/.env')
h={'apikey':cfg['VITE_SUPABASE_PUBLISHABLE_KEY'],'Authorization':'Bearer '+s['access_token'],'Prefer':'return=representation,resolution=merge-duplicates'}
rest=cfg['VITE_SUPABASE_URL']+'/rest/v1/'; api='http://127.0.0.1:8000/api/v1';out=Path('/tmp/chronosync-staging-e2e');out.mkdir(exist_ok=True)
checks=[]
def record(name,ok,**details):
 r={'check':name,'passed':bool(ok),**details};checks.append(r);print(json.dumps(r),flush=True);Path('docs/staging-matrix-results-2026-10-27.json').write_text(json.dumps(checks,indent=2))
with httpx.Client(headers=h,timeout=60) as c:
 def db(method,table,data=None,**params):
  r=c.request(method,rest+table,json=data,params=params);r.raise_for_status();return r.json() if r.content else None
 company=db('GET','companies',id='eq.'+STAGE,select='name')[0];assert company['name']=='ChronoSync staging E2E 2026-10-27'
 base=db('GET','client_billing_profiles',company_id='eq.'+STAGE,client_id='eq.f9c69a15-d86e-4138-a47f-3516bf796b6b')[0]
 base={k:v for k,v in base.items() if k not in ['created_at','updated_at']}
 cases=[('ZQ2','fixed-eur',{'currency':'EUR','net_unit_price':250},'250.00','317.50'),
 ('ZQ3','private-huf-edge',{'tax_subject':-1,'tax_number':None,'net_unit_price':1.5},'2','2'),
 ('ZQ4','group-huf',{'tax_number':'12345676-4-42','group_tax_number':'11111111-5-42','net_unit_price':100},'100','127'),
 ('ZQ5','eu-business',{'billing_country_code':'DE','tax_subject':6,'tax_number':None,'eu_vat_number':'DE123456789','currency':'EUR','net_unit_price':100},'100.00','127.00'),
 ('ZQ6','non-eu-business',{'billing_country_code':'US','tax_subject':7,'tax_number':'US-TEST-42','currency':'USD','net_unit_price':100},'100.00','127.00'),
 ('ZQ7','aam-huf',{'vat_code':'AAM','net_unit_price':100},'100','100')]
 from decimal import Decimal
 for code,name,overrides,net,gross in cases:
  existing=db('GET','clients',company_id='eq.'+STAGE,client_code='eq.'+code)
  buyer=existing[0] if existing else db('POST','clients',{'company_id':STAGE,'client_code':code,'name':'E2E '+name})[0]
  db('POST','client_billing_profiles',{**base,'client_id':buyer['id'],'billing_name':'E2E '+name,'billing_model':'fixed_monthly','send_invoice_email':False,**overrides},on_conflict='client_id')
  body={'company_id':STAGE,'client_id':buyer['id'],'period_start':'2026-10-01','period_end':'2026-10-31'}
  r=c.post(api+'/invoices/client-review',json=body)
  ok=r.status_code==200 and Decimal(r.json()['net_total'])==Decimal(net) and Decimal(r.json()['gross_total'])==Decimal(gross)
  record(name+'_review',ok,status=r.status_code,net=r.json().get('net_total'),gross=r.json().get('gross_total'))
  preview=c.post(api+'/invoices/client-preview',json=body)
  ok=preview.status_code==200 and preview.content.startswith(b'%PDF')
  record(name+'_provider_preview',ok,status=preview.status_code,detail=None if ok else preview.text[:200])
  if ok:(out/(name+'.pdf')).write_bytes(preview.content)
  if code=='ZQ2':
   bad=c.post(api+'/invoices/client-review',json={**body,'period_start':'2026-10-02'})
   record('partial_fixed_month_rejected',bad.status_code==409,status=bad.status_code)
 # The backend still enforces tenant scope despite the overly permissive RLS.
 r=c.get(cfg['VITE_API_BASE_URL']+'/invoices/history',params={'company_id':'dd840115-c0ea-4956-b53b-5c2d24cd508a'})
 record('backend_cross_company_invoice_access_denied',r.status_code==403,status=r.status_code)
 r=c.post(cfg['VITE_API_BASE_URL']+'/reports/generate-szamlamelleklet',json={'company_id':STAGE,'client_ids':['f9c69a15-d86e-4138-a47f-3516bf796b6b'],'start_date':'2026-10-01','end_date':'2026-10-31','update_remaining_hours':False})
 record('hosted_timesheet_attachment',r.status_code==200 and r.content.startswith(b'PK'),status=r.status_code)
 if r.status_code==200:(out/'timesheet-attachment.xlsx').write_bytes(r.content)
