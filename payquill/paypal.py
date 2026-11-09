"""PayPal REST client (sandbox) plus a local mock with the same interface.

Every dollar in PayQuill moves through this module:
  * Invoicing v2: create, send, get and remind customer invoices
  * Payouts v1:   pay suppliers on the scheduled day
  * Webhooks:     verify INVOICING.INVOICE.PAID and payout events

The mock exists only so the app (and its tests) run with no credentials.
It is clearly labelled in the UI and never pretends to be the sandbox.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from typing import Any, Protocol

import requests

from .config import Settings
from .db import Database

TIMEOUT = 20

PAID_STATUSES = {"PAID", "MARKED_AS_PAID"}
CLOSED_STATUSES = {"CANCELLED", "REFUNDED", "MARKED_AS_REFUNDED"}


class PayPalError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id


def cents_to_value(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def build_invoice_payload(
    *,
    number: str,
    invoice_date: date,
    due_date: date,
    business_name: str,
    recipient_name: str,
    recipient_email: str,
    description: str,
    amount_cents: int,
    note: str = "",
) -> dict[str, Any]:
    """Body for POST /v2/invoicing/invoices."""
    return {
        "detail": {
            "invoice_number": number,
            "invoice_date": invoice_date.isoformat(),
            "currency_code": "USD",
            "note": note or f"Thank you for your business. - {business_name}",
            "payment_term": {
                "term_type": "DUE_ON_DATE_SPECIFIED",
                "due_date": due_date.isoformat(),
            },
        },
        "invoicer": {"business_name": business_name},
        "primary_recipients": [
            {
                "billing_info": {
                    "business_name": recipient_name,
                    "email_address": recipient_email,
                }
            }
        ],
        "items": [
            {
                "name": description[:200],
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": cents_to_value(amount_cents)},
                "unit_of_measure": "AMOUNT",
            }
        ],
        "configuration": {"allow_tip": False},
    }


def build_payout_payload(batch_id: str, items: list[dict]) -> dict[str, Any]:
    """Body for POST /v1/payments/payouts. items: {email, amount_cents, note, item_id}."""
    return {
        "sender_batch_header": {
            "sender_batch_id": batch_id,
            "email_subject": "You have a payment from Verde Studio",
            "email_message": "Paid through PayQuill. Thank you!",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": cents_to_value(i["amount_cents"]), "currency": "USD"},
                "receiver": i["email"],
                "note": i.get("note", "")[:4000],
                "sender_item_id": i["item_id"],
            }
            for i in items
        ],
    }


class PayPal(Protocol):
    mode: str

    def create_invoice(self, payload: dict) -> str: ...
    def send_invoice(self, invoice_id: str) -> None: ...
    def get_invoice(self, invoice_id: str) -> dict: ...
    def remind_invoice(self, invoice_id: str, note: str, subject: str = ...) -> None: ...
    def create_payout(self, batch_id: str, items: list[dict]) -> str: ...
    def get_payout(self, payout_batch_id: str) -> dict: ...
    def verify_webhook(self, headers: dict, event: dict) -> bool: ...


def summarize_invoice(raw: dict) -> dict:
    """Reduce a PayPal invoice to what PayQuill needs."""
    status = raw.get("status", "")
    metadata = raw.get("detail", {}).get("metadata", {})
    paid_date = None
    for txn in raw.get("payments", {}).get("transactions", []) or []:
        paid_date = (txn.get("payment_date") or "")[:10] or paid_date
    return {
        "id": raw.get("id"),
        "status": status,
        "paid": status in PAID_STATUSES,
        "closed": status in CLOSED_STATUSES,
        "payer_url": metadata.get("recipient_view_url"),
        "paid_date": paid_date,
    }


# --------------------------------------------------------------------------
# Real sandbox client
# --------------------------------------------------------------------------
class SandboxPayPal:
    mode = "sandbox"

    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.base = settings.paypal_base_url
        self.http = session or requests.Session()
        self._token: str | None = None
        self._token_expiry = 0.0

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Send with one retry on network errors.

        PayPal closes idle keep-alive connections, so the first request after a quiet spell
        can fail with 'Connection aborted'. Retrying is safe: POSTs carry the same
        PayPal-Request-Id, so PayPal never applies the same request twice.
        """
        for attempt in (1, 2):
            try:
                return self.http.request(method, url, timeout=TIMEOUT, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == 2:
                    raise PayPalError(
                        "Couldn't reach PayPal's sandbox (network error). Check your internet "
                        "connection or VPN and try again. "
                        f"Details: {exc.__class__.__name__}: {str(exc)[:160]}"
                    ) from exc
                time.sleep(1.0)
        raise AssertionError("unreachable")

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self._send(
            "POST",
            f"{self.base}/v1/oauth2/token",
            auth=(self.s.paypal_client_id, self.s.paypal_client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise PayPalError(f"PayPal auth failed ({resp.status_code}). Check client id and secret.", resp.status_code)
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        hdrs = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),  # idempotency for retries
        }
        hdrs.update(headers or {})
        resp = self._send(method, f"{self.base}{path}", json=body, headers=hdrs)
        if resp.status_code == 401:  # token revoked or expired early: refresh once
            self._token = None
            hdrs["Authorization"] = f"Bearer {self._access_token()}"
            resp = self._send(method, f"{self.base}{path}", json=body, headers=hdrs)
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"message": resp.text[:300]}
            detail = err.get("message") or err.get("error_description") or "PayPal error"
            issues = "; ".join(d.get("issue", "") for d in err.get("details", []) if isinstance(d, dict))
            raise PayPalError(
                f"{method} {path} failed ({resp.status_code}): {detail}{' - ' + issues if issues else ''}",
                resp.status_code,
                err.get("debug_id"),
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # Invoicing v2 ----------------------------------------------------------
    def create_invoice(self, payload: dict) -> str:
        body = self._request("POST", "/v2/invoicing/invoices", payload, {"Prefer": "return=representation"})
        invoice_id = body.get("id")
        if not invoice_id and body.get("href"):
            invoice_id = body["href"].rstrip("/").split("/")[-1]
        if not invoice_id:
            raise PayPalError("PayPal did not return an invoice id")
        return invoice_id

    def send_invoice(self, invoice_id: str) -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            {"send_to_invoicer": False, "send_to_recipient": True},
        )

    def get_invoice(self, invoice_id: str) -> dict:
        return summarize_invoice(self._request("GET", f"/v2/invoicing/invoices/{invoice_id}"))

    def remind_invoice(self, invoice_id: str, note: str, subject: str = "Payment reminder") -> None:
        self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/remind",
            {"subject": subject[:120], "note": note[:4000],
             "send_to_invoicer": False, "send_to_recipient": True},
        )

    # Payouts v1 ------------------------------------------------------------
    def create_payout(self, batch_id: str, items: list[dict]) -> str:
        body = self._request("POST", "/v1/payments/payouts", build_payout_payload(batch_id, items))
        return body["batch_header"]["payout_batch_id"]

    def get_payout(self, payout_batch_id: str) -> dict:
        body = self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")
        header = body.get("batch_header", {})
        items = body.get("items", [])
        return {
            "batch_status": header.get("batch_status", ""),
            "item_status": items[0].get("transaction_status", "") if items else "",
        }

    # Webhooks --------------------------------------------------------------
    def verify_webhook(self, headers: dict, event: dict) -> bool:
        if not self.s.paypal_webhook_id:
            return False
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": self.s.paypal_webhook_id,
            "webhook_event": event,
        }
        if not all(body.values()):
            return False
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", body)
        return result.get("verification_status") == "SUCCESS"


# --------------------------------------------------------------------------
# Local mock (no credentials). Stored in SQLite so it survives restarts.
# --------------------------------------------------------------------------
class MockPayPal:
    mode = "mock"

    def __init__(self, db: Database, public_url: str):
        self.db = db
        self.public_url = public_url

    def _save(self, obj_id: str, kind: str, body: dict) -> None:
        self.db.execute(
            "INSERT INTO mock_paypal(id, kind, body) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET body = excluded.body",
            (obj_id, kind, json.dumps(body)),
        )

    def _load(self, obj_id: str) -> dict:
        row = self.db.one("SELECT body FROM mock_paypal WHERE id = ?", (obj_id,))
        if not row:
            raise PayPalError(f"Mock PayPal: {obj_id} not found", 404)
        return json.loads(row["body"])

    def create_invoice(self, payload: dict) -> str:
        invoice_id = "INV2-MOCK-" + uuid.uuid4().hex[:12].upper()
        self._save(invoice_id, "invoice", {"id": invoice_id, "status": "DRAFT", "payload": payload})
        return invoice_id

    def send_invoice(self, invoice_id: str) -> None:
        inv = self._load(invoice_id)
        inv["status"] = "SENT"
        self._save(invoice_id, "invoice", inv)

    def get_invoice(self, invoice_id: str) -> dict:
        inv = self._load(invoice_id)
        return {
            "id": invoice_id,
            "status": inv["status"],
            "paid": inv["status"] in PAID_STATUSES,
            "closed": inv["status"] in CLOSED_STATUSES,
            "payer_url": f"{self.public_url}/mock-pay/{invoice_id}",
            "paid_date": inv.get("paid_date"),
        }

    def raw_invoice(self, invoice_id: str) -> dict:
        return self._load(invoice_id)

    def pay_invoice(self, invoice_id: str) -> None:
        """Simulates the buyer paying on the PayPal checkout page."""
        inv = self._load(invoice_id)
        inv["status"] = "PAID"
        inv["paid_date"] = self.db.today().isoformat()
        self._save(invoice_id, "invoice", inv)

    def remind_invoice(self, invoice_id: str, note: str, subject: str = "Payment reminder") -> None:
        inv = self._load(invoice_id)
        inv.setdefault("reminders", []).append({"subject": subject, "note": note})
        self._save(invoice_id, "invoice", inv)

    def create_payout(self, batch_id: str, items: list[dict]) -> str:
        payout_id = "MOCKPAYOUT" + uuid.uuid4().hex[:10].upper()
        self._save(payout_id, "payout", {"batch_status": "SUCCESS", "item_status": "SUCCESS",
                                         "payload": build_payout_payload(batch_id, items)})
        return payout_id

    def get_payout(self, payout_batch_id: str) -> dict:
        body = self._load(payout_batch_id)
        return {"batch_status": body["batch_status"], "item_status": body["item_status"]}

    def verify_webhook(self, headers: dict, event: dict) -> bool:
        # Mock events are generated inside this process and never come from outside.
        return False


def make_paypal(settings: Settings, db: Database) -> PayPal:
    if settings.is_sandbox:
        return SandboxPayPal(settings)
    return MockPayPal(db, settings.public_url)
