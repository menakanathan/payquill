"""The four demo steps end to end, through the HTTP API (mock PayPal, built-in AI)."""


def test_seeded_state(client):
    s = client.get("/api/state").get_json()
    assert s["owner"] == "Mitchell Movado" and s["business"] == "Verde Studio"
    assert s["mode"] == {"paypal": "mock", "ai": "offline", "webhooks": False}
    assert len([i for i in s["invoices"] if i["status"] == "sent"]) == 5
    assert {b["status"] for b in s["bills"]} == {"scheduled"}
    assert s["model"]["trained_on"] > 20


def test_step1_to_3_invoice_paid(client, svc):
    draft = client.post("/api/invoices/draft",
                        json={"text": "Bill Hillside HOA for phase 2 of the courtyard, $4,800, due in 15 days"}).get_json()
    assert draft["customer_name"] == "Hillside HOA" and draft["risk"]["band"] == "high"
    assert "last 3 invoices" in draft["risk"]["reason"]

    inv = client.post("/api/invoices", json={"customer_id": draft["customer_id"], "description": draft["description"],
                                             "amount": "4800.00", "due_date": draft["due_date"],
                                             "milestone": draft["milestone"]}).get_json()
    assert inv["status"] == "sent" and inv["paypal_id"].startswith("INV2-MOCK")
    assert len(inv["reminders"]) == 3  # high risk: 3 nudges

    before = svc.db.balance()
    res = client.post(f"/mock-pay/{inv['paypal_id']}")
    assert res.status_code == 302
    assert svc.invoice(inv["id"])["status"] == "paid"
    assert svc.db.balance() == before + 480_000


def test_step4_bill_scheduled_and_paid_out(client, svc):
    bill = client.post("/api/bills/sample/bay-area-stone").get_json()
    assert bill["status"] == "review" and bill["discount_pct"] == 2.0
    rec = bill["recommendation"]
    assert rec["take_discount"] and rec["annualized_return"] == 0.3724

    approved = client.post(f"/api/bills/{bill['id']}/approve", json={"choice": "recommended"}).get_json()
    assert approved["status"] == "scheduled" and approved["pay_amount_cents"] == rec["pay_amount_cents"]

    before = svc.db.balance()
    client.post("/api/clock/advance", json={"days": 6})
    paid = svc.bill(bill["id"])
    assert paid["status"] == "paid" and paid["payout_batch_id"]
    # Seeded GreenRoots bill is not due yet; only this payout left the account.
    assert svc.db.balance() == before - rec["pay_amount_cents"]


def test_owner_can_correct_what_the_ai_read(client):
    bill = client.post("/api/bills/sample/bay-area-stone").get_json()
    fixed = client.patch(f"/api/bills/{bill['id']}", json={"discount_pct": 0, "discount_days": 0}).get_json()
    assert fixed["recommendation"]["take_discount"] is False
    bad = client.patch(f"/api/bills/{bill['id']}", json={"discount_pct": 2, "discount_days": 40, "net_days": 30})
    assert bad.status_code == 400


def test_reminders_follow_risk(client, svc):
    client.post("/api/clock/advance", json={"days": 1})  # Linden PTA is due in 4 days and high risk: nudge at due-3
    assert any(e["kind"] == "reminder" and "Linden" in e["message"] for e in svc.db.all("SELECT * FROM events"))


def test_unverified_webhook_is_rejected(client, svc):
    inv = svc.db.one("SELECT * FROM invoices WHERE status = 'sent'")
    res = client.post("/webhooks/paypal", json={"event_type": "INVOICING.INVOICE.PAID",
                                                "resource": {"invoice": {"id": inv["paypal_id"]}}})
    assert res.status_code == 400
    assert svc.invoice(inv["id"])["status"] == "sent"


def test_bad_input_is_explained(client):
    assert client.post("/api/invoices", json={"customer_id": 1, "amount": "0", "due_date": "2030-01-01"}).status_code == 400
    assert "sentence" in client.post("/api/invoices/draft", json={"text": " "}).get_json()["error"]
