"""Read-only email readiness check. Never sends a message or prints credentials."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import settings
from app.services.email_service import (
    EmailWorker, email_configured, email_missing_variables,
    invoice_email_transport, validate_email_configuration,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="store_true", help="Also verify the installed queue migration using a read-only RPC")
    args = parser.parse_args()
    result = {
        "configured": email_configured(), "missing_env": email_missing_variables(),
        "worker_enabled": settings.EMAIL_WORKER_ENABLED, "invoice_transport": invoice_email_transport(),
        "database": "not_checked", "dns_and_smtp": "verify_using_docs/email-setup.md",
    }
    try:
        validate_email_configuration()
    except ValueError as exc:
        result["configuration_error"] = str(exc)
    if args.database:
        if not settings.SUPABASE_SERVICE_ROLE_KEY or not settings.SUPABASE_URL:
            result["database"] = "missing_server_credentials"
        else:
            worker = EmailWorker()
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                worker.client = client
                try:
                    result["database"] = "ready" if await worker.rpc("email_worker_ready", {}) else "not_ready"
                except (RuntimeError, httpx.HTTPError, ValueError):
                    result["database"] = "unavailable_check_migration_and_credentials"
    print(json.dumps(result, indent=2))
    return 0 if (result["configured"] and result["worker_enabled"] and "configuration_error" not in result
                 and (not args.database or result["database"] == "ready")) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
