from datetime import date, timedelta

from payquill.ai import DemoAI, parse_any_date, validate_bill
from payquill.risk import LateRiskModel, Record

TODAY = date(2026, 11, 8)
CUSTOMERS = [{"id": 1, "name": "Hillside HOA"}, {"id": 2, "name": "Oakmont Dental Group"}]


def test_reads_the_demo_sentence():
    d = DemoAI().draft_invoice("Bill Hillside HOA for phase 2 of the courtyard, $4,800, due in 15 days", CUSTOMERS, TODAY)
    assert d["customer_id"] == 1
    assert d["amount_cents"] == 480_000
    assert d["due_date"] == (TODAY + timedelta(days=15)).isoformat()
    assert d["milestone"] == "Phase 2"
    assert d["description"].startswith("Phase 2 of the courtyard")


def test_reads_alternate_phrasings():
    d = DemoAI().draft_invoice("invoice oakmont $1,250.50 for spring cleanup net 30", CUSTOMERS, TODAY)
    assert d["customer_id"] == 2 and d["amount_cents"] == 125_050
    assert d["due_date"] == (TODAY + timedelta(days=30)).isoformat()
    d = DemoAI().draft_invoice("Bill Hillside HOA $900 for tree work due Dec 1", CUSTOMERS, TODAY)
    assert d["due_date"] == "2026-12-01" and d["amount_cents"] == 90_000


def test_date_formats():
    assert parse_any_date("Nov 5, 2026", TODAY) == date(2026, 11, 5)
    assert parse_any_date("11/05/2026", TODAY) == date(2026, 11, 5)
    assert parse_any_date("2026-11-05", TODAY) == date(2026, 11, 5)
    assert parse_any_date("Jan 3", TODAY) == date(2027, 1, 3)


def test_bill_text_with_terms():
    text = ("Bay Area Stone Supply\nInvoice No: BAS-10482\nInvoice Date: Nov 5, 2026\nDue Date: Dec 5, 2026\n"
            "Amount Due: $4,826.45\nTerms: 2/10 Net 30")
    out = DemoAI().read_bill(text, [{"id": 7, "name": "Bay Area Stone Supply"}], TODAY)
    assert out["supplier_id"] == 7 and out["number"] == "BAS-10482"
    assert out["amount_cents"] == 482_645
    assert (out["discount_pct"], out["discount_days"], out["net_days"]) == (2.0, 10, 30)
    assert out["due_date"] == "2026-12-05"


def test_validation_rejects_impossible_terms():
    reading = validate_bill({"amount_cents": 1000, "bill_date": "2026-11-01", "discount_pct": 2,
                             "discount_days": 40, "net_days": 30}, "test", TODAY)
    assert reading.discount_pct == 0
    assert any("Discount window" in w for w in reading.warnings)


def test_late_payers_score_higher():
    history = []
    for i in range(10):
        due = TODAY - timedelta(days=400 - 40 * i)
        history.append(Record(1, 400_000, due, due + timedelta(days=14)))  # always late
        history.append(Record(2, 300_000, due, due))                        # always on time
    model = LateRiskModel().fit(history)
    late = model.predict([r for r in history if r.customer_id == 1], 400_000)
    good = model.predict([r for r in history if r.customer_id == 2], 300_000)
    assert late["score"] > 0.6 > good["score"]
    assert late["band"] == "high" and good["band"] == "low"
    assert "late" in late["reason"] and "on time" in good["reason"]
