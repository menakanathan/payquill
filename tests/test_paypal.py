"""The sandbox client is checked against a fake HTTP session: right endpoints, right bodies."""

from datetime import date

from payquill.config import load_settings
from payquill.paypal import SandboxPayPal, build_invoice_payload, summarize_invoice


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.content = b"x" if body is not None else b""
        self.text = ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):  # oauth
        self.calls.append(("POST", url, kw))
        return FakeResponse(200, {"access_token": "TOKEN", "expires_in": 3600})

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        if url.endswith("/v2/invoicing/invoices"):
            return FakeResponse(201, {"id": "INV2-ABCD"})
        if url.endswith("/send"):
            return FakeResponse(200, {"href": "https://www.sandbox.paypal.com/invoice/p/#INV2-ABCD"})
        if url.endswith("/remind"):
            return FakeResponse(204)
        if url.endswith("/v1/payments/payouts"):
            return FakeResponse(201, {"batch_header": {"payout_batch_id": "BATCH1", "batch_status": "PENDING"}})
        if "/v1/payments/payouts/" in url:
            return FakeResponse(200, {"batch_header": {"batch_status": "SUCCESS"},
                                      "items": [{"transaction_status": "UNCLAIMED"}]})
        if url.endswith("/verify-webhook-signature"):
            return FakeResponse(200, {"verification_status": "SUCCESS"})
        if "/v2/invoicing/invoices/" in url:
            return FakeResponse(200, {"id": "INV2-ABCD", "status": "PAID",
                                      "detail": {"metadata": {"recipient_view_url": "https://pay.example/x"}},
                                      "payments": {"transactions": [{"payment_date": "2026-11-09 10:00:00 PST"}]}})
        return FakeResponse(404, {"message": "not found"})


def client():
    s = load_settings(paypal_client_id="id", paypal_client_secret="secret", paypal_mode="sandbox",
                      paypal_webhook_id="WH-1")
    session = FakeSession()
    return SandboxPayPal(s, session), session


def test_invoice_payload_shape():
    p = build_invoice_payload(number="VS-1", invoice_date=date(2026, 11, 8), due_date=date(2026, 11, 23),
                              business_name="Verde Studio", recipient_name="Hillside HOA",
                              recipient_email="buyer@example.com", description="Phase 2", amount_cents=480_000)
    assert p["detail"]["payment_term"] == {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": "2026-11-23"}
    assert p["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "4800.00"}
    assert p["primary_recipients"][0]["billing_info"]["email_address"] == "buyer@example.com"


def test_invoice_lifecycle_hits_invoicing_v2():
    pp, session = client()
    inv_id = pp.create_invoice({"detail": {}})
    pp.send_invoice(inv_id)
    pp.remind_invoice(inv_id, "Friendly reminder")
    info = pp.get_invoice(inv_id)
    urls = [c[1] for c in session.calls]
    assert urls[0].endswith("/v1/oauth2/token")
    assert any(u.endswith("/v2/invoicing/invoices/INV2-ABCD/send") for u in urls)
    assert any(u.endswith("/v2/invoicing/invoices/INV2-ABCD/remind") for u in urls)
    assert info["paid"] and info["paid_date"] == "2026-11-09" and info["payer_url"] == "https://pay.example/x"


def test_payouts_and_webhook_verification():
    pp, session = client()
    batch = pp.create_payout("PQ-1", [{"email": "s@example.com", "amount_cents": 472_992, "note": "n", "item_id": "b1"}])
    assert batch == "BATCH1"
    body = [c[2] for c in session.calls if c[1].endswith("/v1/payments/payouts")][0]
    assert body["items"][0]["amount"] == {"value": "4729.92", "currency": "USD"}
    assert pp.get_payout("BATCH1") == {"batch_status": "SUCCESS", "item_status": "UNCLAIMED"}
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.paypal.com/cert",
               "PAYPAL-TRANSMISSION-ID": "t1", "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "now"}
    assert pp.verify_webhook(headers, {"event_type": "INVOICING.INVOICE.PAID"}) is True
    assert pp.verify_webhook({}, {"event_type": "X"}) is False  # missing headers are never trusted


def test_summarize_marks_paid_statuses():
    assert summarize_invoice({"status": "MARKED_AS_PAID"})["paid"] is True
    assert summarize_invoice({"status": "SENT"})["paid"] is False
