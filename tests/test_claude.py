"""The live Claude path, checked against a fake Messages API: request shape, tool use,
PDF input, reminder validation and fallbacks. (No network, no API key needed.)"""

import base64
from datetime import date

import pytest

from payquill.ai import AIAdapter, OFFLINE_TEMPLATE, document_block
from payquill.config import load_settings

TODAY = date(2026, 11, 9)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeClaude:
    """Answers each forced tool with a canned tool_use block and records the requests."""

    def __init__(self, answers=None, status=200):
        self.answers = answers or {}
        self.status = status
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        if self.status != 200:
            return Resp(self.status, {"error": {"message": "overloaded"}})
        tool = json["tool_choice"]["name"]
        return Resp(200, {"content": [{"type": "tool_use", "name": tool, "id": "t1", "input": self.answers[tool]}]})


def adapter(fake, log=None):
    s = load_settings(anthropic_api_key="sk-test", ai_provider="anthropic", ai_model="claude-sonnet-5-5")
    return AIAdapter(s, on_fallback=(log.append if log is not None else None), session=fake)


CTX = {
    "today_text": "Monday, November 9, 2026", "business": "Verde Studio", "owner": "Mitchell Movado",
    "customer": "Hillside HOA", "contact": "Dana Whitfield", "contact_first_name": "Dana",
    "invoice_number": "VS-261109-006-8FA2", "description": "Phase 2 of the courtyard", "amount": "$4,800.00",
    "due_date_text": "November 6", "days_to_due": -3, "timing_text": "3 days past due",
    "history_text": "paid 9 of 9 past invoices late, on average 13 days late", "risk_band": "high",
    "reminders_sent": 1,
}


def test_invoice_draft_uses_forced_tool():
    fake = FakeClaude({"invoice_draft": {"customer_name": "Hillside", "description": "Phase 2: courtyard",
                                         "milestone": "Phase 2", "amount_usd": 4800, "due_date": "2026-11-24",
                                         "confidence": 0.95}})
    draft = adapter(fake).draft_invoice("Bill Hillside $4,800 for phase 2, due in 15 days",
                                        [{"id": 1, "name": "Hillside HOA"}], TODAY)
    req = fake.requests[0]
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["json"]["tool_choice"] == {"type": "tool", "name": "invoice_draft"}
    assert req["json"]["model"] == "claude-sonnet-5-5"
    assert draft.source == "Claude" and draft.customer_id == 1 and draft.amount_cents == 480_000


def test_bill_pdf_is_sent_to_claude_as_a_document():
    fake = FakeClaude({"bill_reading": {"supplier_name": "Bay Area Stone Supply", "number": "BAS-10482",
                                        "amount_usd": 4826.45, "bill_date": "2026-11-06", "due_date": "2026-12-06",
                                        "discount_pct": 2, "discount_days": 10, "net_days": 30,
                                        "confidence": 0.9, "notes": "Delivery fee handwritten"}})
    pdf = b"%PDF-1.4 fake bytes"
    reading = adapter(fake).read_bill("", [{"id": 3, "name": "Bay Area Stone Supply"}], TODAY,
                                      document_block("bill.pdf", pdf))
    block = fake.requests[0]["json"]["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(block["source"]["data"]) == pdf
    assert reading.amount_cents == 482_645 and reading.discount_days == 10 and reading.supplier_id == 3
    assert any("Delivery fee handwritten" in w for w in reading.warnings)


def test_photo_becomes_an_image_block():
    block = document_block("receipt.JPG", b"\xff\xd8\xff fake")
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"
    assert document_block("notes.docx", b"x") is None


def test_reminder_written_by_claude():
    note = ("Hi Dana,\n\nInvoice VS-261109-006-8FA2 for $4,800.00 was due on November 6 and is now 3 days "
            "past due. Please pay today, or reply with the date we can expect payment.\n\n"
            "Thank you,\nMitchell Movado\nVerde Studio")
    fake = FakeClaude({"payment_reminder": {"subject": "Invoice VS-261109-006-8FA2 is 3 days past due",
                                            "note": note, "tone": "firm"}})
    r = adapter(fake).write_reminder(CTX)
    assert r.source == "Claude" and r.tone == "firm" and r.note == note
    prompt = fake.requests[0]["json"]["messages"][0]["content"][0]["text"]
    assert "3 days past due" in prompt and "Reminders already sent for this invoice: 1" in prompt


@pytest.mark.parametrize("bad", [
    {"subject": "Reminder", "note": "Hi Dana, please pay your invoice soon.", "tone": "polite"},       # no number/amount
    {"subject": "Reminder", "note": "Invoice VS-261109-006-8FA2 for $4,800.00: pay at https://evil.example",
     "tone": "firm"},                                                                                     # a link
])
def test_unsafe_reminder_falls_back_to_template(bad):
    log = []
    r = adapter(FakeClaude({"payment_reminder": bad}), log).write_reminder(CTX)
    assert r.source == OFFLINE_TEMPLATE
    assert "VS-261109-006-8FA2" in r.note and "$4,800.00" in r.note
    assert log and "offline template" in log[0]


def test_api_error_falls_back_and_is_logged():
    log = []
    r = adapter(FakeClaude(status=529), log).write_reminder(CTX)
    assert r.source == OFFLINE_TEMPLATE and r.tone == "firm"
    assert "Claude was unavailable" in log[0]


def test_offline_reminder_tone_follows_risk_and_timing():
    from payquill.ai import OfflineAI

    early_good = OfflineAI().write_reminder({**CTX, "days_to_due": 5, "risk_band": "low"})
    early_risky = OfflineAI().write_reminder({**CTX, "days_to_due": 5, "risk_band": "high"})
    assert early_good["tone"] == "friendly" and "confirm" not in early_good["note"]
    assert early_risky["tone"] == "clear" and "confirm the date" in early_risky["note"]


def test_reminder_endpoints(client, svc):
    inv = svc.db.one("SELECT * FROM invoices WHERE status = 'sent'")
    draft = client.post(f"/api/invoices/{inv['id']}/reminder/draft").get_json()
    assert inv["number"] in draft["note"] and draft["source"] == OFFLINE_TEMPLATE
    sent = client.post(f"/api/invoices/{inv['id']}/remind",
                       json={"subject": draft["subject"], "note": draft["note"] + "\nP.S. Thanks!"}).get_json()
    assert sent["source"] == "you (edited)" and sent["note"].endswith("P.S. Thanks!")
    row = svc.invoice(inv["id"])
    assert row["reminder_count"] == 1 and row["last_reminder"]["subject"] == draft["subject"]
    raw = svc.paypal.raw_invoice(inv["paypal_id"])
    assert raw["reminders"][-1]["note"].endswith("P.S. Thanks!")
    # An unedited preview is sent exactly as shown and credited to its writer.
    again = client.post(f"/api/invoices/{inv['id']}/remind",
                        json={**{k: draft[k] for k in ("subject", "note", "source", "tone")}}).get_json()
    assert again["note"] == draft["note"] and again["source"] == "offline template, approved by you"


def test_scanned_pdf_without_claude_explains_what_to_do(client):
    from io import BytesIO

    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 200, 200, fill=1)  # an image-only page, no text
    c.save()
    res = client.post("/api/bills/upload", data={"file": (BytesIO(buf.getvalue()), "scan.pdf")},
                      content_type="multipart/form-data")
    assert res.status_code == 400 and "ANTHROPIC_API_KEY" in res.get_json()["error"]
