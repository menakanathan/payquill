"""The AI reads, writes and reasons. It never moves money.

Claude does three jobs in PayQuill:
  1. Turns one sentence ("Bill Hillside HOA $4,800 for phase 2, due in 15 days")
     into an invoice draft.
  2. Reads a supplier bill: the PDF itself (including scans) or a phone photo,
     not just extracted text, and returns amount, dates and early-pay terms.
  3. Writes each payment reminder for the customer it goes to: tone set by
     their late-pay risk, their history and how close the due date is.

Every call offers one strict tool (tool_choice "auto" + strict: true, since
Claude Sonnet 5.5 and Opus 5.5 reject forced tool choice), so Claude answers
in a fixed schema.
Whatever comes back is validated before anyone sees it, and nothing is sent
or paid until the owner approves it (scheduled reminders are the exception:
the owner turns those on by sending the invoice, and each one is checked).

With no API key, an offline fallback (rules and templates) keeps the app
usable. It is labelled as offline everywhere it appears.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

import requests

from .config import Settings

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

LIVE_SOURCE = "Claude"
OFFLINE_SOURCE = "offline reader"
OFFLINE_TEMPLATE = "offline template"

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class NeedsLiveAI(ValueError):
    """The request can only be done by the model (e.g. reading a scanned bill)."""


class ClaudeAPIError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Claude API {status}: {detail}".strip(": "))
        self.status = status
        self.detail = detail or ""


# --------------------------------------------------------------------------
# Small parsing helpers (shared by the offline reader and validation)
# --------------------------------------------------------------------------
def parse_money(text: Any) -> int | None:
    """'$4,800' / '4800.00' / 4800 -> cents."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(round(float(text) * 100))
    cleaned = re.sub(r"[^\d.]", "", str(text))
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return None


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def parse_any_date(text: str, today: date) -> date | None:
    text = text.strip().rstrip(".,")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if m:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        return _safe_date(year, int(m[1]), int(m[2]))
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", text)
    if m and m[1][:3].lower() in MONTHS:
        month, day = MONTHS[m[1][:3].lower()], int(m[2])
        if m[3]:
            return _safe_date(int(m[3]), month, day)
        candidate = _safe_date(today.year, month, day)
        if candidate and candidate < today - timedelta(days=60):
            candidate = _safe_date(today.year + 1, month, day)
        return candidate
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


DATE_PATTERN = (
    r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)"
)


def best_match(name: str, candidates: list[dict]) -> tuple[dict | None, float]:
    """Fuzzy match a typed name to a known customer or supplier."""
    if not name or not name.strip():
        return None, 0.0
    needle = name.lower().strip()
    best, best_score = None, 0.0
    for c in candidates:
        hay = c["name"].lower()
        if needle in hay or hay in needle:
            score = 0.95
        else:
            score = SequenceMatcher(None, needle, hay).ratio()
            if needle.split()[0] == hay.split()[0]:  # "Hillside" -> "Hillside HOA"
                score = max(score, 0.85)
        if score > best_score:
            best, best_score = c, score
    return (best, best_score) if best_score >= 0.6 else (None, best_score)


def find_in_text(text: str, candidates: list[dict]) -> dict | None:
    lowered = text.lower()
    for c in candidates:
        if c["name"].lower() in lowered:
            return c
    for c in candidates:
        first = c["name"].lower().split()[0]
        if len(first) > 3 and re.search(rf"\b{re.escape(first)}\b", lowered):
            return c
    return None


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass
class InvoiceDraft:
    customer_name: str
    customer_id: int | None
    description: str
    milestone: str | None
    amount_cents: int | None
    due_date: date | None
    confidence: float
    source: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "customer_name": self.customer_name,
            "customer_id": self.customer_id,
            "description": self.description,
            "milestone": self.milestone,
            "amount_cents": self.amount_cents,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "warnings": self.warnings,
        }


@dataclass
class BillReading:
    supplier_name: str
    supplier_id: int | None
    number: str | None
    amount_cents: int | None
    bill_date: date | None
    due_date: date | None
    discount_pct: float
    discount_days: int
    net_days: int
    confidence: float
    source: str
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "supplier_name": self.supplier_name,
            "supplier_id": self.supplier_id,
            "number": self.number,
            "amount_cents": self.amount_cents,
            "bill_date": self.bill_date.isoformat() if self.bill_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "discount_pct": self.discount_pct,
            "discount_days": self.discount_days,
            "net_days": self.net_days,
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "notes": self.notes,
            "warnings": self.warnings,
        }


@dataclass
class Reminder:
    subject: str
    note: str
    tone: str
    source: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"subject": self.subject, "note": self.note, "tone": self.tone,
                "source": self.source, "warnings": self.warnings}


# --------------------------------------------------------------------------
# Offline fallback: rules and templates, no API key needed
# --------------------------------------------------------------------------
class OfflineAI:
    name = "offline"

    def draft_invoice(self, sentence: str, customers: list[dict], today: date) -> dict:
        s = " ".join(sentence.split())
        found = 0

        amount = None
        m = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", s) or re.search(
            r"\b([\d,]+(?:\.\d{1,2})?)\s*(?:dollars|usd)\b", s, re.I)
        if m:
            amount = parse_money(m[1])
            found += 1

        due = None
        m = re.search(r"\bdue\s+in\s+(\d{1,3})\s+days?\b", s, re.I) or re.search(r"\bnet\s*(\d{1,3})\b", s, re.I)
        if m:
            due = today + timedelta(days=int(m[1]))
        else:
            m = re.search(r"\bdue\s+(?:on\s+|by\s+)?" + DATE_PATTERN, s, re.I)
            if m:
                due = parse_any_date(m[1], today)
        if due:
            found += 1

        customer, cust_name = None, ""
        m = re.match(r"^(?:please\s+)?(?:bill|invoice|charge)\s+(.+?)\s+(?:for|\$)", s, re.I)
        typed = m[1].strip(" ,") if m else ""
        if typed:
            customer, _ = best_match(typed, customers)
        if not customer:
            customer = find_in_text(s, customers)
        if customer:
            cust_name = customer["name"]
            found += 1
        elif typed:
            cust_name = typed

        desc = ""
        m = re.search(r"\bfor\s+(.+?)(?:,|\s+\$|\s+due\b|\s+net\b|$)", s, re.I)
        if m:
            desc = m[1].strip(" .")
        milestone = None
        mm = re.search(r"\b(phase|milestone|stage)\s*(\d+)\b", s, re.I)
        if mm:
            milestone = f"{mm[1].title()} {mm[2]}"
        if desc:
            desc = desc[0].upper() + desc[1:]
            found += 1

        return {
            "customer_name": cust_name,
            "customer_id": customer["id"] if customer else None,
            "description": desc,
            "milestone": milestone,
            "amount_cents": amount,
            "due_date": due.isoformat() if due else None,
            "confidence": found / 4,
        }

    def read_bill(self, text: str, suppliers: list[dict], today: date, document: dict | None = None) -> dict:
        if len((text or "").strip()) < 20:
            raise NeedsLiveAI("This bill has no readable text (it may be a scan or a photo). "
                              "Reading scans needs Claude: add ANTHROPIC_API_KEY to .env.")
        t = text.replace(" ", " ")
        flat = " ".join(t.split())
        found = 0

        supplier = find_in_text(flat, suppliers)
        supplier_name = supplier["name"] if supplier else (t.strip().splitlines() or [""])[0].strip()
        if supplier:
            found += 1

        number = None
        m = re.search(r"(?:invoice|bill)\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{2,})", flat, re.I)
        if m:
            number = m[1]

        amount = None
        for label in ("amount due", "balance due", "total due", "please pay", "total"):
            hits = re.findall(rf"{label}\s*(?:\(usd\))?\s*[:\-]?\s*\$?\s*([\d,]+\.\d{{2}})", flat, re.I)
            if hits:
                amount = parse_money(hits[-1])
                break
        if amount:
            found += 1

        bill_date = None
        m = re.search(r"(?:invoice|bill)\s+date\s*[:\-]?\s*" + DATE_PATTERN, flat, re.I) or re.search(
            r"\bdate\s*[:\-]\s*" + DATE_PATTERN, flat, re.I)
        if m:
            bill_date = parse_any_date(m[1], today)
        if bill_date:
            found += 1

        discount_pct, discount_days, net_days = 0.0, 0, 30
        m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d{1,3})\s*,?\s*net\s*(\d{1,3})", flat, re.I)
        if m:
            discount_pct, discount_days, net_days = float(m[1]), int(m[2]), int(m[3])
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:discount|off)?[^.]{0,40}?within\s+(\d{1,3})\s+days", flat, re.I)
            if m:
                discount_pct, discount_days = float(m[1]), int(m[2])
            n = re.search(r"\bnet\s*(\d{1,3})\b", flat, re.I)
            if n:
                net_days = int(n[1])
        found += 1  # terms default to net 30 when none are printed

        due = None
        m = re.search(r"due\s+date\s*[:\-]?\s*" + DATE_PATTERN, flat, re.I)
        if m:
            due = parse_any_date(m[1], today)
        if not due and bill_date:
            due = bill_date + timedelta(days=net_days)

        return {
            "supplier_name": supplier_name,
            "supplier_id": supplier["id"] if supplier else None,
            "number": number,
            "amount_cents": amount,
            "bill_date": bill_date.isoformat() if bill_date else None,
            "due_date": due.isoformat() if due else None,
            "discount_pct": discount_pct,
            "discount_days": discount_days,
            "net_days": net_days,
            "confidence": found / 4,
            "notes": "",
        }

    def write_reminder(self, ctx: dict) -> dict:
        """Fixed templates, chosen by risk and timing. Correct, but not personal."""
        first = ctx["contact_first_name"] or "there"
        days = ctx["days_to_due"]
        num, amt, due = ctx["invoice_number"], ctx["amount"], ctx["due_date_text"]
        sign = f"Thank you,\n{ctx['owner']}\n{ctx['business']}"
        if days > 0:
            if ctx["risk_band"] == "high":
                tone = "clear"
                subject = f"Invoice {num} is due {due}"
                body = (f"Hi {first},\n\nInvoice {num} for {amt} ({ctx['description']}) is due on {due}. "
                        f"Could you confirm the date you plan to pay? You can pay through the PayPal link "
                        f"in this email.")
            else:
                tone = "friendly"
                subject = f"Friendly reminder: invoice {num}"
                body = (f"Hi {first},\n\nA quick reminder that invoice {num} for {amt} "
                        f"({ctx['description']}) is due on {due}. You can pay through the PayPal link "
                        f"in this email.")
        elif days == 0:
            tone = "clear"
            subject = f"Invoice {num} is due today"
            body = (f"Hi {first},\n\nInvoice {num} for {amt} is due today. "
                    f"You can pay through the PayPal link in this email.")
        else:
            late = -days
            tone = "firm" if ctx["risk_band"] == "high" else "polite"
            subject = f"Invoice {num} is {late} day{'s' if late != 1 else ''} past due"
            body = (f"Hi {first},\n\nInvoice {num} for {amt} was due on {due} and is now {late} "
                    f"day{'s' if late != 1 else ''} past due. Please pay through the PayPal link in this "
                    f"email, or reply with the date you expect to pay.")
        return {"subject": subject, "note": f"{body}\n\n{sign}", "tone": tone}


DemoAI = OfflineAI  # older name, kept for compatibility


# --------------------------------------------------------------------------
# Claude (live)
# --------------------------------------------------------------------------
INVOICE_SYSTEM = """You turn one sentence from a small-business owner into an invoice draft.
Pick customer_name from the known customers when one clearly matches.
Resolve relative dates ("due in 15 days", "net 30", "next Friday") against today's date.
Write description as a short, professional invoice line item.
Never invent an amount or a customer. If something is missing, leave it null and lower the confidence."""

INVOICE_TOOL = {
    "name": "invoice_draft",
    "description": "Record the invoice draft read from the owner's sentence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_name": {"type": ["string", "null"]},
            "description": {"type": "string", "description": "Short line item, e.g. 'Phase 2: courtyard landscaping'"},
            "milestone": {"type": ["string", "null"], "description": "e.g. 'Phase 2', or null"},
            "amount_usd": {"type": ["number", "null"]},
            "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "confidence": {"type": "number", "description": "0 to 1"},
        },
        "required": ["customer_name", "description", "milestone", "amount_usd", "due_date", "confidence"],
        "additionalProperties": False,
    },
}

BILL_SYSTEM = """You read supplier bills for a small business, including scanned pages and phone photos.
Report only what is printed on the bill. Never guess a number you cannot see.
amount_usd is the final amount due (after tax and delivery), not the subtotal.
Payment terms like "2/10 net 30" mean discount_pct 2, discount_days 10, net_days 30.
If the bill states a discount in words ("2% off if paid within 10 days"), record it the same way.
If there is no early-payment discount, use discount_pct 0 and discount_days 0.
Pick supplier_name from the known suppliers when one clearly matches.
Use notes for anything the owner should double-check (handwriting, a crossed-out figure, a past-due balance)."""

BILL_TOOL = {
    "name": "bill_reading",
    "description": "Record what is printed on the supplier bill.",
    "input_schema": {
        "type": "object",
        "properties": {
            "supplier_name": {"type": "string"},
            "number": {"type": ["string", "null"]},
            "amount_usd": {"type": ["number", "null"]},
            "bill_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "discount_pct": {"type": "number"},
            "discount_days": {"type": "integer"},
            "net_days": {"type": "integer"},
            "confidence": {"type": "number", "description": "0 to 1"},
            "notes": {"type": "string", "description": "Anything to double-check; empty string if none"},
        },
        "required": ["supplier_name", "number", "amount_usd", "bill_date", "due_date", "discount_pct",
                     "discount_days", "net_days", "confidence", "notes"],
        "additionalProperties": False,
    },
}

REMINDER_SYSTEM = """You write payment reminders that a small-business owner sends to a customer through PayPal.
The customer receives your note with PayPal's own payment button, so never include links or bank details.

Match the tone to the situation:
- Reliable customer, not yet due: warm and brief. Assume they will pay.
- Customer who often pays late, not yet due: friendly but clear; ask them to confirm a payment date.
- Overdue: polite, direct, and specific about how many days late. Ask them to pay or reply with a date.
- Overdue, repeatedly late, and already reminded: firm and professional. Still courteous.

Rules:
- Use the exact invoice number, amount and dates given. Never invent fees, penalties, deadlines or facts.
- Never mention risk scores, the customer's payment history, or that software wrote the note.
- Address the contact by first name. Sign off with the owner's name and the business name.
- Subject under 80 characters. Note under 700 characters, plain text, short paragraphs."""

REMINDER_TOOL = {
    "name": "payment_reminder",
    "description": "The reminder to send.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "note": {"type": "string"},
            "tone": {"type": "string", "enum": ["warm", "friendly", "clear", "polite", "firm"]},
        },
        "required": ["subject", "note", "tone"],
        "additionalProperties": False,
    },
}


class AnthropicAI:
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"

    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.key = settings.anthropic_api_key
        self.model = settings.ai_model
        self.http = session or requests.Session()

    def _post(self, payload: dict) -> dict:
        resp = self.http.post(
            self.url,
            headers={
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise ClaudeAPIError(resp.status_code, detail)
        return resp.json()

    def _call(self, system: str, content: list[dict], tool: dict, max_tokens: int = 800) -> dict:
        """One Messages API call that returns the tool's input as a dict.

        Claude Sonnet 5.5 and Opus 5.5 reject forced tool_choice ("tool"/"any"), so the
        tool is offered with tool_choice "auto" and strict: true, which guarantees the
        input matches the schema when Claude calls it, and the prompt says to call it.
        """
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": f"{system}\n\nAlways answer by calling the {tool['name']} tool exactly once. Do not reply with text.",
            "tools": [{**tool, "strict": True}],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": content}],
        }
        try:
            body = self._post(payload)
        except ClaudeAPIError as exc:
            # A model or account without strict tool use: retry once with a plain tool.
            if exc.status == 400 and "strict" in exc.detail.lower():
                payload["tools"] = [tool]
                body = self._post(payload)
            else:
                raise
        text = ""
        for block in body.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
                return block.get("input") or {}
            if block.get("type") == "text":
                text += block.get("text", "")
        # Claude answered in text instead of calling the tool: accept a JSON object if there is one.
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
        raise ValueError("Claude replied without calling the tool")

    def draft_invoice(self, sentence: str, customers: list[dict], today: date) -> dict:
        names = "\n".join(f"- {c['name']}" for c in customers)
        out = self._call(INVOICE_SYSTEM, [{"type": "text", "text":
                         f"Today is {today:%A}, {today.isoformat()}.\nKnown customers:\n{names}\n\n"
                         f"Owner's sentence: {sentence}"}], INVOICE_TOOL)
        customer, _ = best_match(out.get("customer_name") or "", customers)
        return {
            "customer_name": customer["name"] if customer else (out.get("customer_name") or ""),
            "customer_id": customer["id"] if customer else None,
            "description": (out.get("description") or "").strip(),
            "milestone": out.get("milestone"),
            "amount_cents": parse_money(out.get("amount_usd")),
            "due_date": out.get("due_date"),
            "confidence": float(out.get("confidence") or 0.5),
        }

    def read_bill(self, text: str, suppliers: list[dict], today: date, document: dict | None = None) -> dict:
        names = "\n".join(f"- {s['name']}" for s in suppliers)
        prompt = f"Today is {today.isoformat()}.\nKnown suppliers:\n{names}\n\nRead this bill."
        if document:  # the PDF or photo itself, so layout, scans and handwriting are all visible
            content = [document, {"type": "text", "text": prompt}]
        else:
            content = [{"type": "text", "text": f"{prompt}\n\nBill text:\n{text[:12000]}"}]
        out = self._call(BILL_SYSTEM, content, BILL_TOOL, max_tokens=1000)
        supplier, _ = best_match(out.get("supplier_name") or "", suppliers)
        return {
            "supplier_name": supplier["name"] if supplier else (out.get("supplier_name") or ""),
            "supplier_id": supplier["id"] if supplier else None,
            "number": out.get("number"),
            "amount_cents": parse_money(out.get("amount_usd")),
            "bill_date": out.get("bill_date"),
            "due_date": out.get("due_date"),
            "discount_pct": float(out.get("discount_pct") or 0),
            "discount_days": int(out.get("discount_days") or 0),
            "net_days": int(out.get("net_days") or 30),
            "confidence": float(out.get("confidence") or 0.5),
            "notes": (out.get("notes") or "").strip(),
        }

    def write_reminder(self, ctx: dict) -> dict:
        lines = [
            f"Today: {ctx['today_text']}",
            f"Business: {ctx['business']} (owner {ctx['owner']})",
            f"Customer: {ctx['customer']}, contact {ctx['contact'] or 'unknown'}",
            f"Invoice {ctx['invoice_number']}: {ctx['description']}, {ctx['amount']}",
            f"Due: {ctx['due_date_text']} ({ctx['timing_text']})",
            f"How this customer usually pays: {ctx['history_text']}",
            f"Reminders already sent for this invoice: {ctx['reminders_sent']}",
        ]
        return self._call(REMINDER_SYSTEM, [{"type": "text", "text": "\n".join(lines)}], REMINDER_TOOL)


def document_block(filename: str, data: bytes) -> dict | None:
    """Content block that lets Claude see the bill itself."""
    name = filename.lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        media = "application/pdf"
        kind = "document"
    else:
        media = next((t for ext, t in IMAGE_TYPES.items() if name.endswith(ext)), None)
        kind = "image"
        if not media:
            return None
    return {"type": kind, "source": {"type": "base64", "media_type": media,
                                     "data": base64.standard_b64encode(data).decode("ascii")}}


# --------------------------------------------------------------------------
# Adapter: picks a provider, falls back, validates
# --------------------------------------------------------------------------
class AIAdapter:
    def __init__(self, settings: Settings, on_fallback=None, session: requests.Session | None = None):
        self.offline = OfflineAI()
        self.live = (AnthropicAI(settings, session)
                     if settings.ai_provider == "anthropic" and settings.anthropic_api_key else None)
        self.on_fallback = on_fallback or (lambda msg: None)

    @property
    def mode(self) -> str:
        return "live" if self.live else "offline"

    def _run(self, method: str, *args, offline_source: str = OFFLINE_SOURCE) -> tuple[dict, str]:
        if self.live:
            try:
                return getattr(self.live, method)(*args), LIVE_SOURCE
            except Exception as exc:  # network, quota, bad output: fall back, never crash
                self.on_fallback(f"Claude was unavailable ({exc.__class__.__name__}: {str(exc)[:120]}); "
                                 f"used the {offline_source} instead.")
        return getattr(self.offline, method)(*args), offline_source

    def draft_invoice(self, sentence: str, customers: list[dict], today: date) -> InvoiceDraft:
        raw, source = self._run("draft_invoice", sentence, customers, today)
        return validate_invoice(raw, source, today)

    def read_bill(self, text: str, suppliers: list[dict], today: date, document: dict | None = None) -> BillReading:
        raw, source = self._run("read_bill", text, suppliers, today, document)
        return validate_bill(raw, source, today)

    def write_reminder(self, ctx: dict) -> Reminder:
        raw, source = self._run("write_reminder", ctx, offline_source=OFFLINE_TEMPLATE)
        reminder = validate_reminder(raw, source, ctx)
        if reminder is None:  # the model's note failed a check: send the safe template instead
            self.on_fallback("Claude's reminder left out the invoice number or amount, "
                             "so the offline template was used.")
            reminder = validate_reminder(self.offline.write_reminder(ctx), OFFLINE_TEMPLATE, ctx)
        return reminder


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def validate_invoice(raw: dict, source: str, today: date) -> InvoiceDraft:
    warnings = []
    amount = raw.get("amount_cents")
    if amount is not None and (amount <= 0 or amount > 100_000_000):
        warnings.append("Amount looks wrong; please check it.")
        amount = None
    if amount is None:
        warnings.append("No amount found. Add one before sending.")
    due = _to_date(raw.get("due_date"))
    if due is None:
        due = today + timedelta(days=30)
        warnings.append("No due date found; set to 30 days from today.")
    elif due < today:
        warnings.append("Due date is in the past.")
    if not raw.get("customer_id"):
        warnings.append("Customer not recognised. Pick one from the list.")
    desc = (raw.get("description") or "").strip() or "Landscape design services"
    return InvoiceDraft(
        customer_name=raw.get("customer_name") or "",
        customer_id=raw.get("customer_id"),
        description=desc[:200],
        milestone=raw.get("milestone"),
        amount_cents=amount,
        due_date=due,
        confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0))),
        source=source,
        warnings=warnings,
    )


def validate_bill(raw: dict, source: str, today: date) -> BillReading:
    warnings = []
    amount = raw.get("amount_cents")
    if amount is not None and (amount <= 0 or amount > 100_000_000):
        warnings.append("Amount looks wrong; please check it.")
        amount = None
    if amount is None:
        warnings.append("Could not read the amount due.")

    bill_date = _to_date(raw.get("bill_date"))
    if bill_date is None:
        bill_date = today
        warnings.append("No bill date found; assumed today.")
    elif bill_date > today + timedelta(days=7):
        warnings.append("Bill date is in the future; please check it.")

    pct = float(raw.get("discount_pct") or 0)
    ddays = int(raw.get("discount_days") or 0)
    net = int(raw.get("net_days") or 30)
    if not (0 <= pct <= 10):
        warnings.append("Discount percent looks wrong; ignored.")
        pct, ddays = 0.0, 0
    if net <= 0 or net > 180:
        warnings.append("Net terms look wrong; assumed net 30.")
        net = 30
    if pct and (ddays <= 0 or ddays >= net):
        warnings.append("Discount window does not fit the net terms; ignored.")
        pct, ddays = 0.0, 0

    due = _to_date(raw.get("due_date")) or bill_date + timedelta(days=net)
    expected_due = bill_date + timedelta(days=net)
    if abs((due - expected_due).days) > 3:
        warnings.append(f"Printed due date differs from net {net} terms; using the printed date.")
    if not raw.get("supplier_id"):
        warnings.append("Supplier not recognised. Pick one from the list.")
    notes = (raw.get("notes") or "").strip()
    if notes:
        warnings.append(f"Claude noted: {notes[:240]}")

    return BillReading(
        supplier_name=raw.get("supplier_name") or "",
        supplier_id=raw.get("supplier_id"),
        number=raw.get("number"),
        amount_cents=amount,
        bill_date=bill_date,
        due_date=due,
        discount_pct=pct,
        discount_days=ddays,
        net_days=net,
        confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0))),
        source=source,
        notes=notes,
        warnings=warnings,
    )


URL_PATTERN = re.compile(r"https?://|www\.", re.I)


def validate_reminder(raw: dict, source: str, ctx: dict) -> Reminder | None:
    """Reject anything that could mislead the customer. Returns None to trigger the template."""
    subject = " ".join(str(raw.get("subject") or "").split())[:120]
    note = str(raw.get("note") or "").strip()
    if not subject or not note:
        return None
    amount_variants = {ctx["amount"], ctx["amount"].replace(".00", "")}
    if ctx["invoice_number"] not in note + subject or not any(a in note for a in amount_variants):
        return None
    if URL_PATTERN.search(note + subject):
        return None
    warnings = []
    if len(note) > 900:
        note = note[:900].rsplit("\n", 1)[0]
        warnings.append("Shortened to fit.")
    tone = str(raw.get("tone") or "").strip().lower()  # strict mode doesn't guarantee enum casing
    tone = tone if tone in {"warm", "friendly", "clear", "polite", "firm"} else "polite"
    return Reminder(subject=subject, note=note, tone=tone, source=source, warnings=warnings)
