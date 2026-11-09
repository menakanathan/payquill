"""Sample supplier bills (real PDFs) dated relative to today, for the demo."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

SAMPLE_BILLS = {
    "bay-area-stone": {
        "supplier": "Bay Area Stone Supply",
        "address": ["2210 Quarry Road", "San Jose, CA 95131"],
        "number": "BAS-10482",
        "days_ago": 3,
        "items": [("Arizona flagstone, full pallet", 2, 1450.00),
                  ("Decomposed granite, gold (tons)", 6, 180.00),
                  ("Boulder set, 3-piece", 1, 360.00)],
        "delivery": 85.00,
        "terms": (2, 10, 30),
    },
    "greenroots": {
        "supplier": "GreenRoots Nursery",
        "address": ["88 Orchard Lane", "Gilroy, CA 95020"],
        "number": "GRN-2291",
        "days_ago": 4,
        "items": [("Japanese maple, 15 gal", 4, 265.00),
                  ("Lavender 'Hidcote', 1 gal", 40, 9.50),
                  ("Native grass plugs (flat of 50)", 6, 42.00)],
        "delivery": 60.00,
        "terms": (1.5, 15, 45),
    },
    "coastal-irrigation": {
        "supplier": "Coastal Irrigation Co.",
        "address": ["415 Harbor Blvd, Unit C", "Santa Cruz, CA 95060"],
        "number": "CIC-7731",
        "days_ago": 12,
        "items": [("Drip line kit, 500 ft", 2, 289.00),
                  ("Smart controller, 8-zone", 1, 412.00),
                  ("Valve manifold, 4-valve", 1, 255.00)],
        "delivery": 0.0,
        "terms": (0, 0, 30),
    },
}

TAX_RATE = 0.0925


def _fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def bill_totals(spec: dict) -> tuple[float, float, float]:
    subtotal = sum(q * p for _, q, p in spec["items"])
    tax = round(subtotal * TAX_RATE, 2)
    return subtotal, tax, round(subtotal + tax + spec["delivery"], 2)


def write_bill_pdf(key: str, out_dir: str | Path, today: date, payee_email: str) -> Path:
    spec = SAMPLE_BILLS[key]
    bill_date = today - timedelta(days=spec["days_ago"])
    pct, ddays, net = spec["terms"]
    due = bill_date + timedelta(days=net)
    subtotal, tax, total = bill_totals(spec)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{key}-{spec['number']}.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    w, h = LETTER
    ink = colors.HexColor("#1f2a37")
    muted = colors.HexColor("#5b6675")

    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(inch, h - inch, spec["supplier"])
    c.setFont("Helvetica", 10)
    c.setFillColor(muted)
    for i, line in enumerate(spec["address"]):
        c.drawString(inch, h - inch - 16 - 13 * i, line)

    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - inch, h - inch, "INVOICE")
    c.setFont("Helvetica", 10)
    meta = [("Invoice No:", spec["number"]), ("Invoice Date:", _fmt_date(bill_date)), ("Due Date:", _fmt_date(due))]
    for i, (k, v) in enumerate(meta):
        y = h - inch - 22 - 14 * i
        c.drawRightString(w - inch - 110, y, k)
        c.drawRightString(w - inch, y, v)

    y = h - 2.3 * inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(inch, y, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(inch, y - 14, "Verde Studio")
    c.drawString(inch, y - 27, "Attn: Mitchell Movado")
    c.drawString(inch, y - 40, "1200 Stevens Creek Blvd, Cupertino, CA 95014")

    y -= 80
    c.setFillColor(colors.HexColor("#eef1f4"))
    c.rect(inch, y - 6, w - 2 * inch, 20, stroke=0, fill=1)
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(inch + 6, y, "Description")
    c.drawRightString(w - inch - 170, y, "Qty")
    c.drawRightString(w - inch - 90, y, "Unit price")
    c.drawRightString(w - inch - 6, y, "Line total")
    c.setFont("Helvetica", 10)
    for desc, qty, price in spec["items"]:
        y -= 20
        c.drawString(inch + 6, y, desc)
        c.drawRightString(w - inch - 170, y, f"{qty:g}")
        c.drawRightString(w - inch - 90, y, f"${price:,.2f}")
        c.drawRightString(w - inch - 6, y, f"${qty * price:,.2f}")

    y -= 30
    rows = [("Subtotal:", subtotal), (f"Sales tax ({TAX_RATE * 100:.2f}%):", tax)]
    if spec["delivery"]:
        rows.append(("Delivery:", spec["delivery"]))
    for label, value in rows:
        c.drawRightString(w - inch - 110, y, label)
        c.drawRightString(w - inch - 6, y, f"${value:,.2f}")
        y -= 16
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - inch - 110, y - 4, "Amount Due:")
    c.drawRightString(w - inch - 6, y - 4, f"${total:,.2f}")

    y -= 50
    c.setFont("Helvetica-Bold", 10)
    if pct:
        c.drawString(inch, y, f"Terms: {pct:g}/{ddays} Net {net}")
        c.setFont("Helvetica", 10)
        c.drawString(inch, y - 14, f"Take {pct:g}% off if paid within {ddays} days of the invoice date. "
                                   f"Full amount due in {net} days.")
    else:
        c.drawString(inch, y, f"Terms: Net {net}")
        c.setFont("Helvetica", 10)
        c.drawString(inch, y - 14, f"Full amount due within {net} days of the invoice date.")
    c.setFillColor(muted)
    c.drawString(inch, y - 40, f"We accept PayPal: {payee_email}")
    c.drawString(inch, y - 54, "Sample bill generated for the PayQuill demo. Not a real invoice.")
    c.showPage()
    c.save()
    return path


def write_all(out_dir: str | Path, today: date, payee_email: str) -> dict[str, Path]:
    return {key: write_bill_pdf(key, out_dir, today, payee_email) for key in SAMPLE_BILLS}
