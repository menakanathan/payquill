# PayQuill

**PayQuill writes your invoices, reads your bills, and tells you the right day to pay, with PayPal moving every dollar.**

PayQuill is a small AI cash assistant for small businesses. The demo business is
**Verde Studio**, a 6-person landscape design firm run by **Mitchell Movado** (a fictional persona).
Mitchell has three problems every small business shares: customers pay late, early-payment
discounts from suppliers get missed, and there's no clear view of the cash coming in and going out.

| What Mitchell does | What PayQuill does | AI | PayPal API |
|---|---|---|---|
| Types one sentence about a job | Drafts the invoice; Mitchell checks it and sends | Claude | Invoicing v2: create, send |
| Looks at open invoices | Flags who will pay late and why | Risk model trained on his own history | |
| Waits for payment | Sends each customer a reminder written for them: warm for reliable payers, clear and earlier for late payers, firm when overdue | Claude | Invoicing v2: remind |
| | Marks the invoice paid the moment PayPal reports it | | Invoicing v2: get; Webhooks |
| Uploads a supplier bill (PDF, scan or phone photo) | Reads amount, dates and terms like "2/10 net 30" from the document itself, then shows the best day to pay | Claude (vision) | |
| Approves | The payment goes out on that day | | Payouts v1 |
| Checks the next 30 days | Cash forecast from open invoices and scheduled payouts | | |

The AI reads, writes and reasons. It never moves money: invoices and bill payments wait for
Mitchell's approval, and every AI output is validated before it is used (see [How the AI is used](#how-the-ai-is-used)).

---

## Judge in 5 minutes

### Option A: run it locally (one command)

You need Python 3.10 or newer.

```bash
git clone <this repo> payquill && cd payquill
make demo            # creates .venv, installs, starts on http://localhost:5000
```

No `make` (for example on Windows)?

```bash
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m payquill
```

Or with Docker: `docker compose up --build`.

Open **http://localhost:5000**. Demo data loads automatically on first start.

> **For the full experience, add two keys to `.env`:** PayPal sandbox keys and an `ANTHROPIC_API_KEY`
> (see [Use the real PayPal sandbox](#use-the-real-paypal-sandbox) and [Turn on Claude](#turn-on-claude)).
> Without them the app still runs end to end, but in a reduced mode that is labelled in the top bar:
> **PayPal mock** (a local stand-in for PayPal) and **AI offline** (rules and templates instead of Claude:
> no scanned bills, no personalised reminders).

### Option B: hosted demo

`<hosted Render URL goes here>`, already connected to the PayPal sandbox.
Sandbox buyer login for paying invoices: `<sandbox buyer email>` / `<sandbox buyer password>`
(sandbox-only test credentials, never a real account).

### The four steps to try

1. **Invoice from a sentence.** In *New invoice*, click **Use the example** (or type your own), then
   **Draft invoice**. Claude fills in customer, line item, amount and due date. Hillside HOA is
   flagged **high risk** because they paid their last 3 invoices 11+ days late. Click **Send with PayPal**.
2. **See who will pay late, and remind them.** *Invoices* shows every open invoice with its late-pay
   risk and the reason. Click **Remind** on a high-risk customer (Linden Elementary PTA) and on a
   reliable one (Oakmont Dental): Claude writes a different message for each, matched to their
   history and the due date. Edit if you like, then **Send reminder with PayPal**. Scheduled reminders
   (3 days before, on, and after the due date for high-risk customers) are written the same way.
3. **Pay the invoice as the customer.** Click **Payer page** on the Hillside invoice.
   - Sandbox: log in as the sandbox buyer and pay. Within 10 seconds (or instantly with webhooks)
     the invoice flips to **paid** and cash goes up.
   - Mock: click **Pay** on the local checkout page.
4. **Best day to pay a bill.** In *Supplier bills*, click **Bay Area Stone (2/10 net 30)**, or upload
   any bill PDF, a scan, or a phone photo of a paper bill. PayQuill reads it and shows: *pay on day 10 and save 2%, which is worth 37.2% a
   year versus 4.5% from holding the cash.* Click **Approve**. Then click **Next day** a few times
   (the demo clock) to watch PayPal Payouts release the payment on the scheduled day.

**Reset demo** in the top bar restores the starting data at any time.

---

## Use the real PayPal sandbox

1. Sign in at [developer.paypal.com](https://developer.paypal.com) and open **Apps & Credentials > Sandbox**.
   Create an app (type: Merchant). Copy its **Client ID** and **Secret**.
2. Under **Testing Tools > Sandbox Accounts** you have a default business account (Verde Studio)
   and a personal account. Note the personal account's email and password: this is the buyer who pays invoices.
   Create a second personal account if you want supplier payouts to land somewhere separate.
3. In your app's settings, make sure **Payouts** is enabled under Features.
4. Copy `.env.example` to `.env` and fill in:

   ```bash
   PAYPAL_CLIENT_ID=...
   PAYPAL_CLIENT_SECRET=...
   SANDBOX_BUYER_EMAIL=sb-xxxx@personal.example.com
   SANDBOX_PAYEE_EMAIL=sb-yyyy@personal.example.com
   ```

5. Delete `data/payquill.db` (or click **Reset demo**) so demo invoices are created in the sandbox,
   then start the app. The top bar now shows **PayPal sandbox**.

**Webhooks (optional, needs a public URL such as the Render deploy).** In your sandbox app, add a webhook
pointing to `https://<your-host>/webhooks/paypal` with the events `Invoicing invoice paid`,
`Invoicing invoice cancelled`, `Payment payouts-batch success` and `Payment payouts-batch denied`.
Put its ID in `PAYPAL_WEBHOOK_ID`. Every webhook is verified with PayPal's
`verify-webhook-signature` API; unverified events are rejected. Without webhooks, PayQuill checks
invoice and payout status with PayPal every 10 seconds while the dashboard is open, so the demo
works the same on a laptop.

## Turn on Claude

1. Create an API key at [platform.claude.com](https://platform.claude.com) (set a monthly spend limit
   there; a full demo run costs a few cents).
2. Add it to `.env`:

   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   AI_MODEL=claude-sonnet-5-5
   ```

3. Restart the app. The top bar shows **Claude** with a green dot.

If a Claude call fails (network, rate limit), PayQuill uses the offline fallback for that one request
and says so in the activity log, so a demo never stalls.

## Deploy to Render

The repo includes `render.yaml`. In Render, choose **New > Blueprint**, select the repo, and fill in
the secret environment variables. One web service runs the app and its daily job (reminders and
scheduled payouts) in-process, because SQLite lives on that service's disk. On the free plan the disk
resets on each deploy; the app reseeds itself automatically.

---

## How it works

```
Mitchell's browser ──REST──> PayQuill (Flask) ──> PayPal sandbox: Invoicing v2, Payouts v1, Webhooks
Judge (sandbox buyer) ──pays──> PayPal ──webhook / status check──> PayQuill
PayQuill ──> Claude (Messages API, forced tool use) for reading, writing and drafting only
PayQuill ──> SQLite ledger (one file)
```

### How the AI is used

| Job | What Claude receives | What comes back | Checks before use |
|---|---|---|---|
| Invoice from a sentence | The owner's sentence, today's date, the customer list | Customer, line item, milestone, amount, due date, confidence | Customer must match a real customer; amount > 0; due date not in the past; owner reviews before sending |
| Read a supplier bill | **The PDF or photo itself** (document or image block), so scans, layout and handwriting are visible | Supplier, number, amount due, dates, discount terms, notes to double-check | Discount window must fit the net terms; dates sane; printed due date cross-checked against terms; owner approves before any payout |
| Write a reminder | Invoice, timing, how this customer usually pays, reminders already sent | Subject, message, tone (warm, friendly, clear, polite, firm) | Must contain the exact invoice number and amount; no links; length capped. Fails a check: the safe template is sent instead and the log says so |

Every call uses **forced tool use** (`tool_choice`), so Claude always answers in a fixed JSON schema
instead of free text. For reminders, Claude is told how the customer usually pays but not their risk
score, and the prompt forbids mentioning payment history to the customer or inventing fees, penalties
or deadlines.

**Offline fallback.** With no key, or when a call fails, rule-based readers and fixed templates keep the
app working. They can't read scans or photos and their reminders are generic; the UI labels every
result with who produced it ("Written by Claude" or "Offline template").

| Part | File | What it does |
|---|---|---|
| PayPal client | `payquill/paypal.py` | OAuth, Invoicing v2 (create, send, get, remind), Payouts v1, webhook verification, idempotency keys. Also the local mock with the same interface. |
| AI adapter | `payquill/ai.py` | Claude Messages API with forced tool use, PDF and image input, per-customer reminders; offline fallback; validation of every value the AI returns. |
| Late-pay risk | `payquill/risk.py` | Logistic regression written in numpy, trained on the business's own payment history. Retrains after every payment. |
| Pay-date optimizer | `payquill/paydate.py` | Discount return = d / (1 - d) x 365 / (net - discount days). Takes the discount only when it beats the cash yield and the 30-day forecast stays above the cash buffer. |
| Cash view | `payquill/forecast.py` | 30-day projection. Expected payments from risky customers are shifted by their usual delay. |
| Workflows | `payquill/services.py` | Invoices, bills, scheduler, webhook handling, polling fallback. |
| Web | `payquill/app.py`, `payquill/static/` | Flask API and a single plain HTML/JavaScript page. No build step. |

### The money math

For terms of 2/10 net 30, paying 20 days early saves 2%:

```
2 / 98 x 365 / 20 = 37.2% a year
```

Idle cash earns about 4.5% a year, so the discount is the better use of the money, as long as paying
early doesn't push the projected balance below the buffer (default $5,000). If it would, PayQuill
recommends paying the full amount the day before it's due and explains why.

### The risk model

Each past invoice becomes a training example labelled "late" if paid more than 3 days after its due
date. Features use only what was known before that invoice: the customer's average days late, the
share of their invoices paid late, this invoice's size against their usual amount, and how much
history there is. The reason shown next to each score comes from the model's largest contributing feature.

### What was built by hand

The PayPal client, the risk model, the pay-date logic, the forecast and the dashboard are all written
for this project. The only libraries are Flask, requests, numpy, pypdf, reportlab and python-dotenv.
Sponsor tools used: **Render** (hosting) and **Postman** (the collection in `postman/` covers every
PayPal call the app makes).

## Tests

```bash
make test       # or: pip install -r requirements-dev.txt && python -m pytest -q
```

33 tests cover the discount math, the risk model, the PayPal client's requests (against a fake HTTP
session), the Claude calls (against a fake Messages API: request shape, PDF and image blocks, reminder
checks and fallbacks), and the four demo steps end to end through the API.

## Postman

Import `postman/PayQuill.postman_collection.json` and `postman/PayQuill.sandbox.postman_environment.json`.
Fill in `client_id`, `client_secret`, `buyer_email` and `payee_email`, run **Get access token**, then
any request. The **PayQuill API** folder drives the app itself.

## Limits and next steps

- Investing features are out of scope; the cash yield is a planning assumption, not an investment.
- Venmo, Zettle and Hyperwallet are natural next steps once their sandbox APIs are available.
- Scanned bills and photos are read by Claude; without a Claude key only text PDFs can be read.
- One business per install; no user accounts.

*Verde Studio, Mitchell Movado, all customers and all suppliers are fictional. Sample bills are
generated for the demo and are not real invoices.*
