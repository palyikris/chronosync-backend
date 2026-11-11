"""Resend transport and a leased, durable transactional email worker.

Only server-side RPCs can claim jobs. Freeze the complete request before any
network I/O so recovery uses the same payload and Resend idempotency key.
"""
import asyncio
import base64
from contextlib import suppress
from email.utils import parseaddr
import hashlib
from html import escape
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)


def _configured(value: str) -> bool:
    return bool(value.strip()) and not value.strip().lower().startswith(
        ("replace-with-", "your-", "<")
    )


def valid_mailbox(value: str) -> bool:
    if any(char in value for char in "\r\n"):
        return False
    name, address = parseaddr(value)
    return bool(re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", address)) and (
        value.strip() == address or (bool(name) and value.strip().endswith(f"<{address}>"))
    )


def email_missing_variables() -> list[str]:
    return [name for name in ("RESEND_API_KEY", "EMAIL_FROM", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL")
            if not _configured(getattr(settings, name))]


def email_configured() -> bool:
    return not email_missing_variables()


def invoice_email_transport() -> str:
    if settings.INVOICE_EMAIL_TRANSPORT == "auto":
        return "resend" if email_configured() else "provider"
    return settings.INVOICE_EMAIL_TRANSPORT


def validate_email_configuration() -> None:
    enabled = any(_configured(getattr(settings, name)) for name in ("RESEND_API_KEY", "EMAIL_FROM"))
    if not enabled and settings.INVOICE_EMAIL_TRANSPORT != "resend":
        return
    missing = email_missing_variables()
    if missing:
        raise ValueError("Email configuration missing: " + ", ".join(missing))
    if not valid_mailbox(settings.EMAIL_FROM):
        raise ValueError("EMAIL_FROM must contain one valid sender mailbox")
    if settings.EMAIL_REPLY_TO and not valid_mailbox(settings.EMAIL_REPLY_TO):
        raise ValueError("EMAIL_REPLY_TO must contain one valid reply mailbox")
    origin = urlsplit(settings.FRONTEND_URL)
    if (origin.scheme not in ("https", "http") or not origin.hostname or origin.username
            or origin.password or origin.query or origin.fragment or origin.path not in ("", "/")
            or (origin.scheme == "http" and origin.hostname not in ("localhost", "127.0.0.1"))):
        raise ValueError("FRONTEND_URL must be an HTTPS origin (HTTP is allowed for localhost)")


def require_email_ready() -> None:
    try:
        validate_email_configuration()
    except ValueError:
        raise HTTPException(503, "Email sending is not configured. Check the server email settings.") from None
    if not email_configured() or not settings.EMAIL_WORKER_ENABLED:
        raise HTTPException(503, "The email delivery worker must be configured and enabled before issuing emailed invoices.")


def render_email(job: dict[str, Any]) -> dict[str, Any]:
    data = job["event_data"]
    recipient = job["recipient"]
    if not valid_mailbox(recipient) or parseaddr(recipient)[1] != recipient:
        raise ValueError("Invalid recipient")
    company = data["company_name"]
    kind = job["kind"]
    if kind == "leave_requested":
        subject = f"{company} — Szabadságkérelem / Leave request"
        text = (f"{data['employee_name']}\n{data['start_date']} – {data['end_date']}\n\n"
                "Új szabadságkérelem vár jóváhagyásra. / A new leave request needs review.\n"
                f"{settings.FRONTEND_URL.rstrip('/')}/leaves")
    elif kind == "leave_decision":
        decision = data["decision"]
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError("Invalid leave decision")
        label = ("Szabadság jóváhagyva / Leave approved" if decision == "APPROVED"
                 else "Szabadság elutasítva / Leave rejected")
        subject = f"{company} — {label}"
        text = (f"{data['employee_name']}\n{data['start_date']} – {data['end_date']}\n\n"
                + ("A szabadságkérelmedet jóváhagyták. / Your leave request has been approved.\n"
                   if decision == "APPROVED" else
                   "A szabadságkérelmedet elutasították. / Your leave request has been rejected.\n")
                + f"{settings.FRONTEND_URL.rstrip('/')}/leaves")
    elif kind == "timesheet_rejected":
        subject = f"{company} — Munkaidő-bejegyzés elutasítva / Timesheet rejected"
        reason = data.get("rejection_reason") or "Nincs megadott indok. / No reason was provided."
        text = (f"{data['employee_name']}\n{data['work_date']} — {data['hours_logged']} óra / hours\n"
                f"{data.get('client_name') or ''} / {data.get('project_name') or ''}\n"
                f"{data.get('description') or ''}\n\n"
                f"Elutasítás indoka / Rejection reason:\n{reason}\n\n"
                "Javítsd a bejegyzést, majd küldd be újra. / Correct the entry and submit it again.\n"
                f"{settings.FRONTEND_URL.rstrip('/')}/timesheet?view=attention")
    elif kind in ("invoice_issued", "invoice_reversed"):
        hu = data.get("language", "hu") == "hu"
        reversed_invoice = kind == "invoice_reversed"
        label = ("Sztornószámla" if reversed_invoice else "Számla") if hu else (
            "Reversal invoice" if reversed_invoice else "Invoice")
        subject = f"{company} — {label} {data['invoice_number']}"
        text = (f"{data['recipient_name']},\n\n{label}: {data['invoice_number']}\n"
                f"{data['period_start']} – {data['period_end']}\n"
                f"{data['gross_total']} {data['currency']}\n\n")
        if not reversed_invoice:
            text += (f"Fizetési határidő: {data['due_date']}\n" if hu else f"Due date: {data['due_date']}\n")
        text += ("A dokumentumokat csatoltuk.\n" if hu else "The documents are attached.\n") + company
    else:
        raise ValueError("Unsupported email event")
    # Escape every database value; plain text remains available to all clients.
    message: dict[str, Any] = {
        "from": settings.EMAIL_FROM.strip(), "to": [recipient],
        "subject": re.sub(r"[\r\n]+", " ", subject)[:250], "text": text,
        "html": '<div style="font:16px/1.6 sans-serif;white-space:pre-wrap">' + escape(text) + '</div>',
        "tags": [{"name": "event", "value": kind}],
    }
    if settings.EMAIL_REPLY_TO.strip():
        message["reply_to"] = settings.EMAIL_REPLY_TO.strip()
    if kind in ("invoice_issued", "invoice_reversed"):
        pdf = base64.b64decode(data["pdf_base64"], validate=True)
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Invalid invoice PDF")
        filename = re.sub(r"[^A-Za-z0-9_-]", "_", data["invoice_number"])[:100]
        message["attachments"] = [{"filename": f"invoice-{filename}.pdf", "content": data["pdf_base64"]}]
        if kind == "invoice_issued":
            annex = base64.b64decode(data["attachment_base64"], validate=True)
            metadata = data["attachment"]
            if (not annex.startswith(b"PK\x03\x04") or hashlib.sha256(annex).hexdigest() != metadata["sha256"]
                    or len(annex) != metadata["size_bytes"]
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}\.xlsx", metadata["filename"])):
                raise ValueError("Invalid saved invoice attachment")
            message["attachments"].append({"filename": metadata["filename"], "content": data["attachment_base64"]})
        if sum(len(item["content"]) for item in message["attachments"]) > 35_000_000:
            raise ValueError("Invoice email attachments exceed safe message size")
    return message


class EmailSendError(Exception):
    def __init__(self, code: str, *, retryable: bool):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


async def send_resend(client: httpx.AsyncClient, message: dict[str, Any], job_id: str) -> str:
    try:
        response = await client.post("https://api.resend.com/emails", json=message, headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY.strip()}",
            "Idempotency-Key": f"chronosync/{job_id}",
        })
    except httpx.RequestError:
        raise EmailSendError("resend_network_error", retryable=True) from None
    if response.status_code >= 400:
        # Provider bodies can echo addresses or content. Persist only a safe code.
        retryable = response.status_code in (408, 429) or response.status_code >= 500
        if response.status_code == 409:
            # A concurrent request is retryable; payload mismatch needs review.
            try:
                retryable = response.json().get("name") == "concurrent_idempotent_requests"
            except (ValueError, AttributeError):
                pass
        raise EmailSendError(f"resend_http_{response.status_code}", retryable=retryable)
    try:
        provider_id = response.json()["id"]
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 200:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise EmailSendError("resend_response_unconfirmed", retryable=True) from None
    return provider_id


class EmailWorker:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self.task:
            return
        validate_email_configuration()
        if not email_configured() or not settings.EMAIL_WORKER_ENABLED:
            logger.info("Transactional email worker disabled; configured=%s", email_configured())
            return
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
        try:
            # Fail startup on missing migration or invalid server credentials.
            if not await self.rpc("email_worker_ready", {}):
                raise RuntimeError("Email queue migration is not ready")
        except BaseException:
            await self.client.aclose()
            self.client = None
            raise
        self.task = asyncio.create_task(self.run(), name="transactional-email")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.client:
            await self.client.aclose()
            self.client = None

    async def rpc(self, name: str, params: dict[str, Any]) -> Any:
        assert self.client is not None
        response = await self.client.post(f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/rpc/{name}",
            json=params, headers={"apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                                 "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}"})
        if response.status_code >= 400:
            raise RuntimeError(f"Email RPC {name} failed (HTTP {response.status_code})")
        return response.json()

    async def process_one(self) -> bool:
        job = await self.rpc("claim_transactional_email", {})
        if not job:
            return False
        args = {"p_id": job["id"], "p_token": job["claim_token"]}
        try:
            message = job.get("message") or render_email(job)
        except (ValueError, KeyError, TypeError):
            await self.rpc("finish_transactional_email", {**args, "p_error": "invalid_email_payload", "p_retry": False})
            return True
        if not job.get("message"):
            message = await self.rpc("prepare_transactional_email", {**args, "p_message": message})
        assert self.client is not None
        try:
            provider_id = await send_resend(self.client, message, job["id"])
        except EmailSendError as exc:
            await self.rpc("finish_transactional_email", {**args, "p_error": exc.code, "p_retry": exc.retryable})
            logger.warning("Transactional email attempt failed; job_id=%s code=%s", job["id"], exc.code)
        else:
            await self.rpc("finish_transactional_email", {**args, "p_provider_id": provider_id})
            logger.info("Transactional email accepted; job_id=%s", job["id"])
        return True

    async def run(self) -> None:
        while True:
            try:
                processed = await self.process_one()
            except Exception as exc:
                # Do not log exception bodies, addresses, credentials or attachments.
                logger.error("Transactional email worker iteration failed; error_type=%s", type(exc).__name__)
                processed = False
            await asyncio.sleep(1 if processed else settings.EMAIL_POLL_SECONDS)
