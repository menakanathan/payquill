"""PayQuill workflows. The Flask routes, the scheduler job and the tests all call these."""

from __future__ import annotations

import io
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

from .ai import AIAdapter, NeedsLiveAI, document_block
from .config import Settings
from .db import Database, iso, parse_date
from .forecast import Flow, project
from .paydate import money, recommend
from .paypal import PayPal, PayPalError, build_invoice_payload
from .risk import LateRiskModel, Record, reminder_plan

OPEN_BILL_STATUSES = ("review", "scheduled", "processing")
FAILED_PAYOUT_STATUSES = {"DENIED", "CANCELED", "FAILED", "BLOCKED", "RETURNED", "REVERSED"}


class UserError(ValueError):
    """A problem the owner can fix (bad input), shown as-is in the UI."""


class PayQuill:
    def __init__(self, settings: Settings, db: Database, paypal: PayPal, ai: AIAdapter | None = None):
        self.s = settings
        self.db = db
        self.paypal = paypal
        self.ai = ai or AIAdapter(settings, on_fallback=lambda msg: self.db.log("ai", msg))
        self.model = LateRiskModel()
        self.retrain()

    # ------------------------------------------------------------------ data
    def customers(self) -> list[dict]:
        return self.db.all("SELECT * FROM customers ORDER BY name")

    def suppliers(self) -> list[dict]:
        return self.db.all("SELECT * FROM suppliers ORDER BY name")

    def history(self, customer_id: int | None = None) -> list[Record]:
        sql = "SELECT * FROM payment_history"
        rows = self.db.all(sql + (" WHERE customer_id = ?" if customer_id else "") + " ORDER BY due_date",
                           (customer_id,) if customer_id else ())
        return [Record(r["customer_id"], r["amount_cents"], parse_date(r["due_date"]), parse_date(r["paid_date"]))
                for r in rows]

    def retrain(self) -> None:
        self.model = LateRiskModel().fit(self.history())

    def score(self, customer_id: int, amount_cents: int) -> dict:
        return self.model.predict(self.history(customer_id), amount_cents)

    # ------------------------------------------------------------- invoices
    def draft_invoice(self, sentence: str) -> dict:
        if not sentence or not sentence.strip():
            raise UserError("Describe the job in a sentence first.")
        today = self.db.today()
        draft = self.ai.draft_invoice(sentence.strip(), self.customers(), today).to_dict()
        if draft["customer_id"] and draft["amount_cents"]:
            draft["risk"] = self.score(draft["customer_id"], draft["amount_cents"])
        return draft

    def create_invoice(self, customer_id: int, description: str, amount_cents: int,
                       due_date: date, milestone: str | None = None) -> dict:
        today = self.db.today()
        customer = self.db.one("SELECT * FROM customers WHERE id = ?", (customer_id,))
        if not customer:
            raise UserError("Pick a customer for this invoice.")
        if not amount_cents or amount_cents <= 0:
            raise UserError("Enter an amount greater than zero.")
        if due_date < today:
            raise UserError("The due date can't be in the past.")
        description = (description or "").strip() or "Landscape design services"

        risk = self.score(customer_id, amount_cents)
        plan = reminder_plan(risk["band"], due_date)
        invoice_id = self.db.execute(
            "INSERT INTO invoices(customer_id, number, description, milestone, amount_cents, issue_date, "
            "due_date, status, risk_score, risk_band, risk_reason, reminders, created_at) "
            "VALUES(?, '', ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
            (customer_id, description, milestone, amount_cents, iso(today), iso(due_date),
             risk["score"], risk["band"], risk["reason"], json.dumps(plan), iso(today)),
        )
        number = f"VS-{today:%y%m%d}-{invoice_id:03d}-{uuid.uuid4().hex[:4].upper()}"
        self.db.execute("UPDATE invoices SET number = ? WHERE id = ?", (number, invoice_id))

        recipient = self.s.buyer_email if self.s.is_sandbox else customer["email"]
        line = f"{milestone}: {description}" if milestone and milestone.lower() not in description.lower() else description
        payload = build_invoice_payload(
            number=number, invoice_date=today, due_date=due_date, business_name=self.s.business_name,
            recipient_name=customer["name"], recipient_email=recipient, description=line,
            amount_cents=amount_cents,
        )
        try:
            paypal_id = self.paypal.create_invoice(payload)
            self.paypal.send_invoice(paypal_id)
            info = self.paypal.get_invoice(paypal_id)
        except PayPalError as exc:
            self.db.execute("UPDATE invoices SET status = 'error' WHERE id = ?", (invoice_id,))
            self.db.log("error", f"Invoice {number} not sent: {exc}")
            raise UserError(f"PayPal didn't accept the invoice: {exc}") from exc

        self.db.execute(
            "UPDATE invoices SET status = 'sent', paypal_id = ?, payer_url = ? WHERE id = ?",
            (paypal_id, info.get("payer_url"), invoice_id),
        )
        self.db.log("invoice", f"Sent {number} to {customer['name']} for {money(amount_cents)}, "
                               f"due {due_date:%b} {due_date.day}. Late-pay risk: {risk['band']}.")
        return self.invoice(invoice_id)

    def invoice(self, invoice_id: int) -> dict:
        row = self.db.one(
            "SELECT i.*, c.name AS customer_name FROM invoices i JOIN customers c ON c.id = i.customer_id "
            "WHERE i.id = ?", (invoice_id,))
        if not row:
            raise UserError("Invoice not found.")
        row["reminders"] = json.loads(row["reminders"] or "[]")
        row["last_reminder"] = json.loads(row["last_reminder"]) if row.get("last_reminder") else None
        return row

    def mark_invoice_paid(self, invoice_id: int, paid_on: date | None, via: str) -> bool:
        inv = self.invoice(invoice_id)
        if inv["status"] == "paid":
            return False
        paid = paid_on or self.db.today()
        self.db.execute("UPDATE invoices SET status = 'paid', paid_date = ? WHERE id = ?", (iso(paid), invoice_id))
        self.db.adjust_balance(inv["amount_cents"])
        # Every payment becomes training data, so the risk model learns as the business runs.
        self.db.execute(
            "INSERT INTO payment_history(customer_id, amount_cents, due_date, paid_date) VALUES(?, ?, ?, ?)",
            (inv["customer_id"], inv["amount_cents"], inv["due_date"], iso(paid)),
        )
        self.retrain()
        self.db.log("paid", f"{inv['customer_name']} paid {inv['number']} ({money(inv['amount_cents'])}) via {via}.")
        return True

    def reminder_context(self, inv: dict) -> dict:
        """Everything the AI needs to write a reminder that fits this customer and this moment."""
        today = self.db.today()
        due = parse_date(inv["due_date"])
        days = (due - today).days
        customer = self.db.one("SELECT * FROM customers WHERE id = ?", (inv["customer_id"],)) or {}
        history = self.history(inv["customer_id"])
        n = len(history)
        late = [r.days_late for r in history if r.days_late > 3]
        if n == 0:
            history_text = "new customer, no past invoices"
        elif not late:
            history_text = f"paid all {n} past invoices on time"
        else:
            history_text = (f"paid {len(late)} of {n} past invoices late, "
                            f"on average {round(sum(late) / len(late))} days late")
        if days > 1:
            timing = f"due in {days} days"
        elif days == 1:
            timing = "due tomorrow"
        elif days == 0:
            timing = "due today"
        else:
            timing = f"{-days} day{'s' if days != -1 else ''} past due"
        contact = customer.get("contact") or ""
        first = contact.replace("Dr.", "").split()[0] if contact.strip() else ""
        if contact.startswith("Dr."):
            first = contact.split()[0] + " " + contact.split()[-1]  # "Dr. Raman"
        return {
            "today_text": f"{today:%A}, {today:%B} {today.day}, {today.year}",
            "business": self.s.business_name,
            "owner": self.s.owner_name,
            "customer": inv["customer_name"],
            "contact": contact,
            "contact_first_name": first,
            "invoice_number": inv["number"],
            "description": inv["description"],
            "amount": money(inv["amount_cents"]),
            "due_date_text": f"{due:%B} {due.day}",
            "days_to_due": days,
            "timing_text": timing,
            "history_text": history_text,
            "risk_band": inv["risk_band"] or "low",
            "reminders_sent": inv.get("reminder_count") or 0,
        }

    def draft_reminder(self, invoice_id: int) -> dict:
        inv = self.invoice(invoice_id)
        if inv["status"] != "sent" or not inv["paypal_id"]:
            raise UserError("Only open invoices can be reminded.")
        return self.ai.write_reminder(self.reminder_context(inv)).to_dict()

    def remind(self, invoice_id: int, automatic: bool = False, subject: str | None = None,
               note: str | None = None, source: str | None = None, tone: str | None = None) -> dict:
        """Send a reminder. With a note, the owner previewed it: sent exactly as shown.
        Without one (the scheduler), the AI writes it now."""
        inv = self.invoice(invoice_id)
        if inv["status"] != "sent" or not inv["paypal_id"]:
            raise UserError("Only open invoices can be reminded.")
        if note and note.strip():
            unedited = source in ("Claude", "offline template")
            reminder = {"subject": (subject or "Payment reminder").strip()[:120], "note": note.strip()[:4000],
                        "tone": tone if unedited and tone else "edited",
                        "source": f"{source}, approved by you" if unedited else "you (edited)"}
        else:
            reminder = self.ai.write_reminder(self.reminder_context(inv)).to_dict()
        try:
            self.paypal.remind_invoice(inv["paypal_id"], reminder["note"], reminder["subject"])
        except PayPalError as exc:
            self.db.log("error", f"Reminder for {inv['number']} failed: {exc}")
            raise UserError(f"PayPal didn't send the reminder: {exc}") from exc
        today = self.db.today()
        record = {"date": iso(today), "subject": reminder["subject"], "note": reminder["note"],
                  "tone": reminder["tone"], "source": reminder["source"], "automatic": automatic}
        self.db.execute("UPDATE invoices SET last_reminder = ?, reminder_count = reminder_count + 1 WHERE id = ?",
                        (json.dumps(record), invoice_id))
        self.db.log("reminder", f"{'Scheduled' if automatic else 'Manual'} reminder to {inv['customer_name']} "
                                f"for {inv['number']} ({reminder['tone']} tone, written by {reminder['source']}): "
                                f"\"{reminder['subject']}\"")
        return record

    # ---------------------------------------------------------------- bills
    def _pdf_text(self, data: bytes) -> str:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)

    def upload_bill(self, filename: str, data: bytes) -> dict:
        if not data:
            raise UserError("The file is empty.")
        if len(data) > 10 * 1024 * 1024:
            raise UserError("Bills must be under 10 MB.")
        name = Path(filename or "bill").name
        lower = name.lower()
        document = None
        if lower.endswith(".pdf") or data[:4] == b"%PDF":
            try:
                text = self._pdf_text(data)
            except Exception:
                text = ""  # a damaged or image-only PDF; Claude may still read it
            document = document_block(name, data)
        elif lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
            text = ""
            document = document_block(name, data)
            if len(data) > 5 * 1024 * 1024:
                raise UserError("Photos must be under 5 MB.")
        elif lower.endswith(".txt"):
            text = data.decode("utf-8", errors="replace")
        else:
            raise UserError("Upload the bill as a PDF or a photo (PNG or JPG).")

        today = self.db.today()
        try:
            reading = self.ai.read_bill(text, self.suppliers(), today, document)
        except NeedsLiveAI as exc:
            raise UserError(str(exc)) from exc
        if reading.amount_cents is None:
            raise UserError("Couldn't find the amount due on this bill.")

        supplier_id = reading.supplier_id
        if not supplier_id:
            supplier_name = reading.supplier_name or "Unknown supplier"
            existing = self.db.one("SELECT id FROM suppliers WHERE name = ?", (supplier_name,))
            supplier_id = existing["id"] if existing else self.db.execute(
                "INSERT INTO suppliers(name, email) VALUES(?, ?)", (supplier_name, self.s.payee_email))

        uploads = Path(self.s.db_path).parent / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        stored = uploads / f"{uuid.uuid4().hex[:8]}-{name}"
        stored.write_bytes(data)

        bill_id = self.db.execute(
            "INSERT INTO bills(supplier_id, number, amount_cents, bill_date, due_date, discount_pct, "
            "discount_days, net_days, status, source_file, extraction, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'review', ?, ?, ?)",
            (supplier_id, reading.number, reading.amount_cents, iso(reading.bill_date), iso(reading.due_date),
             reading.discount_pct, reading.discount_days, reading.net_days, stored.name,
             json.dumps(reading.to_dict()), iso(today)),
        )
        self._recommend(bill_id)
        bill = self.bill(bill_id)
        self.db.log("bill", f"Read {bill['supplier_name']} bill for {money(bill['amount_cents'])} "
                            f"({reading.source}, confidence {reading.confidence:.0%}).")
        return bill

    def bill(self, bill_id: int) -> dict:
        row = self.db.one(
            "SELECT b.*, s.name AS supplier_name, s.email AS supplier_email FROM bills b "
            "JOIN suppliers s ON s.id = b.supplier_id WHERE b.id = ?", (bill_id,))
        if not row:
            raise UserError("Bill not found.")
        row["recommendation"] = json.loads(row["recommendation"]) if row["recommendation"] else None
        row["extraction"] = json.loads(row["extraction"]) if row["extraction"] else None
        return row

    def update_bill(self, bill_id: int, fields: dict) -> dict:
        """Owner corrects what the AI read; the recommendation is recalculated."""
        bill = self.bill(bill_id)
        if bill["status"] != "review":
            raise UserError("Only bills still in review can be edited.")
        allowed = {"amount_cents": int, "discount_pct": float, "discount_days": int, "net_days": int,
                   "bill_date": parse_date, "due_date": parse_date, "supplier_id": int}
        updates = {}
        for key, cast in allowed.items():
            if key in fields and fields[key] not in (None, ""):
                try:
                    updates[key] = cast(fields[key])
                except (TypeError, ValueError) as exc:
                    raise UserError(f"{key.replace('_', ' ')} is not valid.") from exc
        if "amount_cents" in updates and updates["amount_cents"] <= 0:
            raise UserError("Amount must be greater than zero.")
        pct = updates.get("discount_pct", bill["discount_pct"])
        ddays = updates.get("discount_days", bill["discount_days"])
        net = updates.get("net_days", bill["net_days"])
        if pct and not (0 < ddays < net):
            raise UserError("The discount window must be shorter than the net terms.")
        for key, value in updates.items():
            self.db.execute(f"UPDATE bills SET {key} = ? WHERE id = ?",
                            (iso(value) if isinstance(value, date) else value, bill_id))
        self._recommend(bill_id)
        return self.bill(bill_id)

    def _recommend(self, bill_id: int) -> dict:
        bill = self.bill(bill_id)
        today = self.db.today()
        buffer = self.s.cash_buffer_cents

        def stays_above_buffer(pay_day: date, amount: int):
            flows = self.flows(exclude_bill=bill_id) + [Flow(pay_day, -amount, "this bill")]
            result = project(self.db.balance(), today, flows)
            low = result["low_cents"]
            return low >= buffer, parse_date(result["low_date"]), low

        rec = recommend(
            amount_cents=bill["amount_cents"], bill_date=parse_date(bill["bill_date"]),
            due_date=parse_date(bill["due_date"]), discount_pct=bill["discount_pct"],
            discount_days=bill["discount_days"], net_days=bill["net_days"], today=today,
            cash_yield_rate=self.s.cash_yield_rate, stays_above_buffer=stays_above_buffer,
        )
        self.db.execute("UPDATE bills SET recommendation = ? WHERE id = ?", (json.dumps(rec), bill_id))
        return rec

    def approve_bill(self, bill_id: int, choice: str = "recommended") -> dict:
        bill = self.bill(bill_id)
        if bill["status"] != "review":
            raise UserError("This bill is already scheduled.")
        rec = bill["recommendation"] or self._recommend(bill_id)
        if choice == "due_date":
            today = self.db.today()
            pay_day = max(today, parse_date(bill["due_date"]) - timedelta(days=1))
            amount = bill["amount_cents"]
        else:
            pay_day, amount = parse_date(rec["pay_date"]), rec["pay_amount_cents"]
        self.db.execute("UPDATE bills SET status = 'scheduled', pay_date = ?, pay_amount_cents = ? WHERE id = ?",
                        (iso(pay_day), amount, bill_id))
        self.db.log("approved", f"Approved {bill['supplier_name']} bill: {money(amount)} "
                                f"will go out on {pay_day:%b} {pay_day.day} through PayPal Payouts.")
        return self.bill(bill_id)

    def _release_payout(self, bill: dict, today: date) -> None:
        amount = bill["pay_amount_cents"]
        rec = bill["recommendation"] or {}
        deadline = parse_date(bill["bill_date"]) + timedelta(days=bill["discount_days"])
        if rec.get("take_discount") and amount < bill["amount_cents"] and today > deadline:
            amount = bill["amount_cents"]
            self.db.log("warning", f"Discount window for {bill['supplier_name']} closed; paying the full amount.")
        batch_id = f"PQ-BILL-{bill['id']}-{uuid.uuid4().hex[:6].upper()}"
        try:
            payout_id = self.paypal.create_payout(batch_id, [{
                "email": bill["supplier_email"], "amount_cents": amount,
                "note": f"Payment for bill {bill['number'] or bill['id']} from {self.s.business_name}",
                "item_id": f"bill-{bill['id']}",
            }])
        except PayPalError as exc:
            self.db.execute("UPDATE bills SET status = 'failed' WHERE id = ?", (bill["id"],))
            self.db.log("error", f"Payout to {bill['supplier_name']} failed: {exc}")
            return
        self.db.execute("UPDATE bills SET status = 'processing', payout_batch_id = ?, pay_amount_cents = ? "
                        "WHERE id = ?", (payout_id, amount, bill["id"]))
        self.db.log("payout", f"Released {money(amount)} to {bill['supplier_name']} (PayPal batch {payout_id}).")

    def _settle_payout(self, bill: dict) -> None:
        try:
            status = self.paypal.get_payout(bill["payout_batch_id"])
        except PayPalError as exc:
            self.db.log("error", f"Couldn't check payout {bill['payout_batch_id']}: {exc}")
            return
        batch = status["batch_status"].upper()
        if batch == "SUCCESS":
            self.db.execute("UPDATE bills SET status = 'paid' WHERE id = ?", (bill["id"],))
            self.db.adjust_balance(-bill["pay_amount_cents"])
            extra = " (supplier hasn't claimed it yet)" if status["item_status"].upper() == "UNCLAIMED" else ""
            self.db.log("paid", f"PayPal confirmed {money(bill['pay_amount_cents'])} paid to "
                                f"{bill['supplier_name']}{extra}.")
        elif batch in FAILED_PAYOUT_STATUSES:
            self.db.execute("UPDATE bills SET status = 'failed' WHERE id = ?", (bill["id"],))
            self.db.log("error", f"PayPal reported the payout to {bill['supplier_name']} as {batch.lower()}.")

    # ------------------------------------------------------------ scheduler
    def run_scheduler(self) -> dict:
        """Daily job (Render cron) and the demo's 'Run scheduler' button."""
        today = self.db.today()
        sent, released = 0, 0

        for inv in self.db.all("SELECT id, reminders FROM invoices WHERE status = 'sent'"):
            plan = json.loads(inv["reminders"] or "[]")
            due_now = [r for r in plan if not r["sent"] and parse_date(r["date"]) <= today]
            if not due_now:
                continue
            try:
                self.remind(inv["id"], automatic=True)
                sent += 1
            except UserError:
                continue
            for r in due_now:  # one nudge per run, even after a fast-forward
                r["sent"] = True
            self.db.execute("UPDATE invoices SET reminders = ? WHERE id = ?", (json.dumps(plan), inv["id"]))

        for row in self.db.all("SELECT id FROM bills WHERE status = 'scheduled' AND pay_date <= ?", (iso(today),)):
            self._release_payout(self.bill(row["id"]), today)
            released += 1

        self.refresh()
        return {"reminders_sent": sent, "payouts_released": released, "today": iso(today)}

    def refresh(self) -> dict:
        """Polling fallback for when webhooks can't reach this machine."""
        updated = 0
        for inv in self.db.all("SELECT id, paypal_id FROM invoices WHERE status = 'sent' AND paypal_id IS NOT NULL"):
            try:
                info = self.paypal.get_invoice(inv["paypal_id"])
            except PayPalError:
                continue
            if info["paid"]:
                updated += self.mark_invoice_paid(inv["id"], parse_date(info.get("paid_date")), "PayPal (status check)")
            elif info["closed"]:
                self.db.execute("UPDATE invoices SET status = 'cancelled' WHERE id = ?", (inv["id"],))
                updated += 1
        for row in self.db.all("SELECT id FROM bills WHERE status = 'processing'"):
            self._settle_payout(self.bill(row["id"]))
        return {"updated": updated}

    # ------------------------------------------------------------- webhooks
    def handle_webhook(self, event: dict) -> str:
        kind = event.get("event_type", "")
        resource = event.get("resource") or {}
        if kind.startswith("INVOICING.INVOICE."):
            paypal_id = (resource.get("invoice") or {}).get("id") or resource.get("id")
            inv = self.db.one("SELECT id FROM invoices WHERE paypal_id = ?", (paypal_id,))
            if not inv:
                return "ignored: unknown invoice"
            if kind == "INVOICING.INVOICE.PAID":
                self.mark_invoice_paid(inv["id"], None, "PayPal webhook")
                return "invoice marked paid"
            if kind == "INVOICING.INVOICE.CANCELLED":
                self.db.execute("UPDATE invoices SET status = 'cancelled' WHERE id = ?", (inv["id"],))
                return "invoice cancelled"
            return "ignored: invoice event"
        if kind.startswith("PAYMENT.PAYOUTSBATCH.") or kind.startswith("PAYMENT.PAYOUTS-ITEM."):
            batch = (resource.get("batch_header") or {}).get("payout_batch_id") or resource.get("payout_batch_id")
            bill = self.db.one("SELECT id FROM bills WHERE payout_batch_id = ? AND status = 'processing'", (batch,))
            if bill:
                self._settle_payout(self.bill(bill["id"]))
                return "payout checked"
            return "ignored: unknown payout"
        return "ignored"

    # ------------------------------------------------------------- forecast
    def expected_delay(self, customer_id: int, band: str | None) -> int:
        if band not in ("high", "medium"):
            return 0
        recs = self.history(customer_id)[-5:]
        return round(sum(r.days_late for r in recs) / len(recs)) if recs else 0

    def flows(self, exclude_bill: int | None = None) -> list[Flow]:
        today = self.db.today()
        flows = []
        for inv in self.db.all(
                "SELECT i.*, c.name AS customer_name FROM invoices i JOIN customers c ON c.id = i.customer_id "
                "WHERE i.status = 'sent'"):
            expected = parse_date(inv["due_date"]) + timedelta(days=self.expected_delay(inv["customer_id"], inv["risk_band"]))
            if expected < today:
                expected = today + timedelta(days=3)  # overdue: assume a few more days
            flows.append(Flow(expected, inv["amount_cents"], inv["customer_name"]))
        for bill in self.db.all(
                "SELECT b.*, s.name AS supplier_name FROM bills b JOIN suppliers s ON s.id = b.supplier_id "
                f"WHERE b.status IN {OPEN_BILL_STATUSES}"):
            if bill["id"] == exclude_bill:
                continue
            if bill["status"] == "review":
                rec = json.loads(bill["recommendation"]) if bill["recommendation"] else None
                if not rec:
                    continue
                day, amount = parse_date(rec["pay_date"]), rec["pay_amount_cents"]
            else:
                day, amount = parse_date(bill["pay_date"]) or today, bill["pay_amount_cents"]
            flows.append(Flow(day, -amount, bill["supplier_name"]))
        return flows

    def forecast(self) -> dict:
        result = project(self.db.balance(), self.db.today(), self.flows())
        result["buffer_cents"] = self.s.cash_buffer_cents
        return result

    # ---------------------------------------------------------------- state
    def state(self) -> dict:
        today = self.db.today()
        invoices = self.db.all(
            "SELECT i.*, c.name AS customer_name FROM invoices i JOIN customers c ON c.id = i.customer_id "
            "ORDER BY CASE i.status WHEN 'sent' THEN 0 WHEN 'error' THEN 1 ELSE 2 END, i.due_date")
        for inv in invoices:
            inv["reminders"] = json.loads(inv["reminders"] or "[]")
            inv["last_reminder"] = json.loads(inv["last_reminder"]) if inv.get("last_reminder") else None
            inv["days_to_due"] = (parse_date(inv["due_date"]) - today).days
        bills = [self.bill(b["id"]) for b in self.db.all(
            "SELECT id FROM bills ORDER BY CASE status WHEN 'review' THEN 0 WHEN 'scheduled' THEN 1 "
            "WHEN 'processing' THEN 2 ELSE 3 END, COALESCE(pay_date, due_date)")]
        open_invoices = [i for i in invoices if i["status"] == "sent"]
        overdue = [i for i in open_invoices if i["days_to_due"] < 0]
        upcoming_bills = sorted((b for b in bills if b["status"] in ("scheduled", "processing")),
                                key=lambda b: b["pay_date"] or "")
        summary = {
            "open_invoice_count": len(open_invoices),
            "overdue_count": len(overdue),
            "overdue_cents": sum(i["amount_cents"] for i in overdue),
            "high_risk_count": sum(1 for i in open_invoices if i["risk_band"] == "high"),
            "scheduled_bill_count": len(upcoming_bills),
            "review_bill_count": sum(1 for b in bills if b["status"] == "review"),
            "next_payout_date": upcoming_bills[0]["pay_date"] if upcoming_bills else None,
            "next_payout_cents": upcoming_bills[0]["pay_amount_cents"] if upcoming_bills else None,
            "next_payout_to": upcoming_bills[0]["supplier_name"] if upcoming_bills else None,
        }
        open_ar = sum(i["amount_cents"] for i in invoices if i["status"] == "sent")
        open_ap = sum((b["pay_amount_cents"] or b["amount_cents"]) for b in bills if b["status"] in OPEN_BILL_STATUSES)
        saved = sum((b["recommendation"] or {}).get("savings_cents", 0) for b in bills
                    if b["status"] in ("scheduled", "processing", "paid")
                    and b["pay_amount_cents"] is not None and b["pay_amount_cents"] < b["amount_cents"])
        return {
            "business": self.s.business_name,
            "owner": self.s.owner_name,
            "today": iso(today),
            "clock_offset_days": int(self.db.get_setting("clock_offset_days", 0)),
            "mode": {"paypal": self.paypal.mode, "ai": self.ai.mode, "webhooks": bool(self.s.paypal_webhook_id)},
            "balance_cents": self.db.balance(),
            "open_receivables_cents": open_ar,
            "open_payables_cents": open_ap,
            "discounts_captured_cents": saved,
            "summary": summary,
            "ai_model": self.s.ai_model if self.ai.mode == "live" else None,
            "model": {"trained_on": self.model.trained_on, "accuracy": self.model.train_accuracy},
            "customers": self.customers(),
            "suppliers": self.suppliers(),
            "invoices": invoices,
            "bills": bills,
            "forecast": self.forecast(),
            "events": self.db.all("SELECT * FROM events ORDER BY id DESC LIMIT 40"),
        }
