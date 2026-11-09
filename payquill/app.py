"""Flask app: JSON API, PayPal webhook, the dashboard page and the local mock checkout."""

from __future__ import annotations

import os
import threading
import time

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, send_file

from .ai import AIAdapter, parse_money
from .config import Settings, load_settings
from .db import Database, parse_date
from .paydate import money
from .paypal import MockPayPal, PayPalError, make_paypal
from .samples import SAMPLE_BILLS, write_bill_pdf
from .services import PayQuill, UserError


def build_service(settings: Settings | None = None) -> PayQuill:
    s = settings or load_settings()
    db = Database(s.db_path)
    paypal = make_paypal(s, db)
    ai = AIAdapter(s, on_fallback=lambda msg: db.log("ai", msg))
    return PayQuill(s, db, paypal, ai)


def create_app(service: PayQuill | None = None) -> Flask:
    svc = service or build_service()
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    app.config["PAYQUILL"] = svc

    if not svc.customers():
        from .seed import seed
        seed(svc)

    # In-process daily scheduler. Render cron jobs run on separate machines and
    # can't see this SQLite file, so the web process runs the job itself.
    interval = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "3600"))
    if interval > 0 and os.getenv("PAYQUILL_TESTING") != "1":
        def loop():
            while True:
                time.sleep(interval)
                try:
                    svc.run_scheduler()
                except Exception as exc:  # keep the loop alive
                    svc.db.log("error", f"Scheduler run failed: {exc}")

        threading.Thread(target=loop, daemon=True, name="payquill-scheduler").start()

    @app.errorhandler(UserError)
    def user_error(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(PayPalError)
    def paypal_error(exc):
        return jsonify(error=f"PayPal: {exc}"), 502

    def body() -> dict:
        return request.get_json(silent=True) or {}

    # ---- pages -------------------------------------------------------------
    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "paypal": svc.paypal.mode, "ai": svc.ai.mode}

    # ---- state -------------------------------------------------------------
    @app.get("/api/state")
    def state():
        return jsonify(svc.state())

    # ---- invoices ----------------------------------------------------------
    @app.post("/api/invoices/draft")
    def draft_invoice():
        return jsonify(svc.draft_invoice(body().get("text", "")))

    @app.post("/api/invoices")
    def create_invoice():
        b = body()
        amount = b.get("amount_cents")
        if amount is None and b.get("amount") is not None:
            amount = parse_money(str(b["amount"]))
        due = parse_date(b.get("due_date"))
        if not due:
            raise UserError("Pick a due date.")
        try:
            customer_id = int(b.get("customer_id") or 0)
        except (TypeError, ValueError):
            customer_id = 0
        inv = svc.create_invoice(customer_id, b.get("description", ""), int(amount or 0), due, b.get("milestone"))
        return jsonify(inv), 201

    @app.post("/api/invoices/refresh")
    def refresh():
        return jsonify(svc.refresh())

    @app.post("/api/invoices/<int:invoice_id>/reminder/draft")
    def draft_reminder(invoice_id: int):
        return jsonify(svc.draft_reminder(invoice_id))

    @app.post("/api/invoices/<int:invoice_id>/remind")
    def remind(invoice_id: int):
        b = body()
        return jsonify(svc.remind(invoice_id, subject=b.get("subject"), note=b.get("note"),
                                  source=b.get("source"), tone=b.get("tone")))

    # ---- bills -------------------------------------------------------------
    @app.post("/api/bills/upload")
    def upload_bill():
        f = request.files.get("file")
        if not f:
            raise UserError("Choose a bill to upload.")
        return jsonify(svc.upload_bill(f.filename, f.read())), 201

    @app.post("/api/bills/sample/<key>")
    def sample_bill(key: str):
        if key not in SAMPLE_BILLS:
            abort(404)
        path = write_bill_pdf(key, svc.s.samples_dir, svc.db.today(), svc.s.payee_email)
        return jsonify(svc.upload_bill(path.name, path.read_bytes())), 201

    @app.get("/samples/<key>.pdf")
    def download_sample(key: str):
        if key not in SAMPLE_BILLS:
            abort(404)
        path = write_bill_pdf(key, svc.s.samples_dir, svc.db.today(), svc.s.payee_email)
        return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=path.name)

    @app.patch("/api/bills/<int:bill_id>")
    def update_bill(bill_id: int):
        b = body()
        if "amount" in b and "amount_cents" not in b:
            b["amount_cents"] = parse_money(str(b["amount"]))
        return jsonify(svc.update_bill(bill_id, b))

    @app.post("/api/bills/<int:bill_id>/approve")
    def approve_bill(bill_id: int):
        choice = body().get("choice", "recommended")
        if choice not in ("recommended", "due_date"):
            raise UserError("choice must be 'recommended' or 'due_date'.")
        return jsonify(svc.approve_bill(bill_id, choice))

    # ---- scheduler and demo clock -------------------------------------------
    @app.post("/api/scheduler/run")
    def run_scheduler():
        return jsonify(svc.run_scheduler())

    @app.post("/api/clock/advance")
    def advance_clock():
        try:
            days = int(body().get("days", 1))
        except (TypeError, ValueError):
            days = 1
        if not 1 <= days <= 60:
            raise UserError("Advance between 1 and 60 days.")
        today = svc.db.advance_clock(days)
        svc.db.log("clock", f"Demo clock moved forward {days} day{'s' if days != 1 else ''} to {today:%b} {today.day}.")
        return jsonify(svc.run_scheduler())

    @app.post("/api/demo/reset")
    def reset():
        from .seed import seed
        seed(svc)
        return jsonify(ok=True)

    # ---- PayPal webhook -----------------------------------------------------
    @app.post("/webhooks/paypal")
    def paypal_webhook():
        event = request.get_json(silent=True)
        if not event:
            return jsonify(error="expected a JSON event"), 400
        try:
            verified = svc.paypal.verify_webhook(dict(request.headers), event)
        except PayPalError as exc:
            svc.db.log("error", f"Webhook verification call failed: {exc}")
            return jsonify(error="verification failed"), 400
        if not verified:
            # Unverified events are never trusted. The status poll still catches every payment.
            svc.db.log("webhook", f"Rejected unverified webhook {event.get('event_type', '?')}.")
            return jsonify(error="signature not verified"), 400
        result = svc.handle_webhook(event)
        svc.db.log("webhook", f"{event.get('event_type')}: {result}.")
        return jsonify(result=result)

    # ---- local mock checkout (only when there are no sandbox keys) ----------
    @app.route("/mock-pay/<paypal_id>", methods=["GET", "POST"])
    def mock_pay(paypal_id: str):
        if not isinstance(svc.paypal, MockPayPal):
            abort(404)
        inv = svc.db.one("SELECT i.*, c.name AS customer_name FROM invoices i "
                         "JOIN customers c ON c.id = i.customer_id WHERE i.paypal_id = ?", (paypal_id,))
        if not inv:
            abort(404)
        if request.method == "POST":
            svc.paypal.pay_invoice(paypal_id)
            # Same code path a verified INVOICING.INVOICE.PAID webhook takes.
            svc.handle_webhook({"event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": paypal_id}}})
            return redirect("/?paid=" + inv["number"])
        return render_template_string(MOCK_CHECKOUT, inv=inv, amount=money(inv["amount_cents"]),
                                      business=svc.s.business_name)

    return app


MOCK_CHECKOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mock checkout · PayQuill</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#fff;color:#14213d;margin:0;display:grid;place-items:center;min-height:100vh}
 .card{background:#fff;border:1px solid #e2e7ee;border-radius:10px;padding:28px 32px;width:min(420px,90vw)}
 .banner{background:#fff4d6;color:#7a5600;border-radius:8px;padding:8px 12px;font-size:13px;margin-bottom:18px}
 h1{font-size:20px;margin:0 0 4px} .muted{color:#5b6675;font-size:14px}
 .amt{font-size:34px;font-weight:700;margin:18px 0}
 button{width:100%;padding:14px;border:0;border-radius:24px;background:#ffc439;color:#111;font-weight:700;font-size:16px;cursor:pointer}
 a{color:#5b6675;font-size:13px;display:block;text-align:center;margin-top:14px}
</style></head><body><div class="card">
 <div class="banner">Local mock checkout. Add PayPal sandbox keys to use the real sandbox checkout.</div>
 <h1>{{ business }}</h1>
 <div class="muted">Invoice {{ inv.number }} for {{ inv.customer_name }}</div>
 <div class="muted">{{ inv.description }}</div>
 <div class="amt">{{ amount }}</div>
 {% if inv.status == 'paid' %}<p>This invoice is already paid.</p>
 {% else %}<form method="post"><button type="submit">Pay {{ amount }}</button></form>{% endif %}
 <a href="/">Back to PayQuill</a>
</div></body></html>"""


def main() -> None:
    app = create_app()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
