"""Demo data for Verde Studio, owned by Mitchell Movado (a fictional persona).

Run directly:  python -m payquill.seed
In sandbox mode this creates real sandbox invoices, so it needs PayPal keys.
"""

from __future__ import annotations

import random
from datetime import timedelta

from .db import iso
from .paydate import money
from .samples import write_all

CUSTOMERS = [
    # name, contact, mail domain, history profile: (invoices, late probability, mean days late, typical $)
    ("Hillside HOA", "Dana Whitfield", "hillsidehoa", (9, 0.85, 13, 4200)),
    ("Oakmont Dental Group", "Dr. Priya Raman", "oakmontdental", (10, 0.05, 2, 3100)),
    ("Ridgeview Apartments", "Marco Ellis", "ridgeviewapts", (8, 0.45, 8, 5800)),
    ("Carver Family Residence", "Jen Carver", "carverfamily", (6, 0.10, 3, 2400)),
    ("Linden Elementary PTA", "Tom Okafor", "lindenpta", (7, 0.80, 19, 1600)),
    ("Bayview Medical Plaza", "Alicia Moreno", "bayviewmedical", (1, 0.0, 0, 9000)),
]

SUPPLIERS = ["Bay Area Stone Supply", "GreenRoots Nursery", "Coastal Irrigation Co.", "Redwood Mulch & Soil"]

# Open invoices already out with customers: name, description, amount $, due in N days
OPEN_INVOICES = [
    ("Oakmont Dental Group", "Courtyard planting refresh", 3250, 6),
    ("Ridgeview Apartments", "Phase 1: irrigation redesign", 6400, 12),
    ("Carver Family Residence", "Backyard design consultation", 2180, 20),
    ("Linden Elementary PTA", "School garden beds", 1750, 4),
    ("Bayview Medical Plaza", "Entry plaza landscape plan", 8900, 25),
]


def seed(app) -> None:
    db, s = app.db, app.s
    db.reset()
    db.set_setting("clock_offset_days", 0)
    db.set_setting("cash_balance_cents", s.opening_balance_cents)
    today = db.today()
    rng = random.Random(42)

    for name, contact, domain, (count, p_late, mean_late, typical) in CUSTOMERS:
        email = s.buyer_email if s.is_sandbox else f"ap@{domain}.example"
        cid = db.execute("INSERT INTO customers(name, email, contact) VALUES(?, ?, ?)", (name, email, contact))
        for i in range(count):
            due = today - timedelta(days=30 + 45 * (count - i) + rng.randint(-6, 6))
            if name == "Hillside HOA" and i >= count - 3:
                late = [11, 14, 12][i - (count - 3)]  # a clear recent pattern for the demo
            elif rng.random() < p_late:
                late = max(4, int(rng.gauss(mean_late, 4)))
            else:
                late = rng.choice([-3, -1, 0, 0, 1, 2])
            amount = int(typical * rng.uniform(0.6, 1.4)) * 100
            db.execute("INSERT INTO payment_history(customer_id, amount_cents, due_date, paid_date) "
                       "VALUES(?, ?, ?, ?)", (cid, amount, iso(due), iso(due + timedelta(days=late))))

    for name in SUPPLIERS:
        db.execute("INSERT INTO suppliers(name, email) VALUES(?, ?)", (name, s.payee_email))

    db.log("setup", f"Demo data loaded for {s.business_name} ({s.owner_name}). "
                    f"Starting cash {money(s.opening_balance_cents)}.")
    app.retrain()

    ids = {c["name"]: c["id"] for c in app.customers()}
    for name, desc, dollars, days in OPEN_INVOICES:
        try:
            app.create_invoice(ids[name], desc, dollars * 100, today + timedelta(days=days))
        except Exception as exc:  # keep seeding even if one PayPal call fails
            db.log("error", f"Seed invoice for {name} failed: {exc}")

    samples = write_all(s.samples_dir, today, s.payee_email)
    for key, choice in (("greenroots", "recommended"), ("coastal-irrigation", "due_date")):
        bill = app.upload_bill(samples[key].name, samples[key].read_bytes())
        app.approve_bill(bill["id"], choice)


if __name__ == "__main__":
    from .app import build_service

    service = build_service()
    seed(service)
    print(f"Seeded {service.s.business_name}: {len(service.customers())} customers, "
          f"{len(service.db.all('SELECT id FROM invoices'))} open invoices, "
          f"PayPal mode = {service.paypal.mode}.")
