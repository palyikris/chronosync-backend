"""Read-only provider recovery check; run with the staging keyring helper."""
import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--external-id', required=True)
    parser.add_argument('--invoice-number', required=True)
    parser.add_argument('--net-total', type=Decimal, required=True)
    parser.add_argument('--gross-total', type=Decimal, required=True)
    args = parser.parse_args()
    result = await SzamlazzHuProvider(os.environ['SZAMLAZZ_AGENT_KEY']).get_invoice_pdf(external_id=args.external_id)
    pdf = result.get('pdf')
    passed = (result['invoice_number'] == args.invoice_number and result['net_total'] == args.net_total
        and result['gross_total'] == args.gross_total and isinstance(pdf, bytes) and pdf.startswith(b'%PDF'))
    print(json.dumps({'passed':passed,'operation':'query_existing_pdf','creation_calls':0,
        'invoice_number':result['invoice_number'],'net_total':str(result['net_total']),
        'gross_total':str(result['gross_total']),'pdf_bytes':len(pdf) if pdf else 0,
        'pdf_sha256':hashlib.sha256(pdf).hexdigest() if pdf else None}))
    return 0 if passed else 1


if __name__ == '__main__':
    try:
        exit_code = asyncio.run(main())
    except Exception as exc:
        # Never print provider bodies, credentials, or request XML.
        print(json.dumps({'passed':False,'error_type':type(exc).__name__}))
        exit_code = 1
    raise SystemExit(exit_code)
