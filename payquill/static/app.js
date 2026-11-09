"use strict";

/* PayQuill dashboard. Plain JavaScript, no build step. */

const $ = (sel) => document.querySelector(sel);
let STATE = null;
let reviewSignature = "";
let busy = false;
let openDrawer = null; // invoice id whose reminder preview is open

// ---------------------------------------------------------------- helpers
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function money(cents) {
  const v = (cents || 0) / 100;
  return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function moneyShort(cents) {
  const v = Math.abs(cents || 0) / 100;
  const s = v >= 1000 ? `$${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : `$${Math.round(v)}`;
  return (cents < 0 ? "-" : "") + s;
}
function parseISO(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}
function shortDate(iso) {
  return iso ? parseISO(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
}
function longDate(iso) {
  return parseISO(iso).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function dueText(inv) {
  if (inv.status !== "sent") return "";
  const d = inv.days_to_due;
  if (d > 1) return `in ${d} days`;
  if (d === 1) return "tomorrow";
  if (d === 0) return "today";
  return `${plural(-d, "day")} late`;
}
function terms(b) {
  return b.discount_pct ? `${b.discount_pct}/${b.discount_days} net ${b.net_days}` : `Net ${b.net_days}`;
}
function sourceBadge(source) {
  if (source === "Claude") return `<span class="badge claude">Written by Claude</span>`;
  if (source && source.startsWith("offline")) return `<span class="badge offline">Offline ${esc(source.replace("offline ", ""))}</span>`;
  return `<span class="badge">${esc(source || "")}</span>`;
}
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isError ? " err" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = "toast"), isError ? 7000 : 3500);
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function withBusy(button, fn, busyLabel) {
  if (busy) return;
  busy = true;
  const label = button ? button.textContent : "";
  if (button) { button.disabled = true; if (busyLabel) button.textContent = busyLabel; }
  try { await fn(); }
  catch (err) { toast(err.message, true); }
  finally {
    busy = false;
    if (button && button.isConnected) { button.disabled = false; button.textContent = label; }
  }
}

// ---------------------------------------------------------------- render
function render() {
  const s = STATE;
  $("#biz").textContent = `${s.business}, ${s.owner}`;
  $("#today").textContent = longDate(s.today) + (s.clock_offset_days ? ` (demo +${s.clock_offset_days}d)` : "");

  const modes = [];
  modes.push(s.mode.paypal === "sandbox"
    ? `<span class="mode live" title="Real PayPal sandbox API calls">PayPal sandbox</span>`
    : `<span class="mode warn" title="No PayPal keys found; using a local mock. Add sandbox keys to .env.">PayPal mock</span>`);
  modes.push(s.mode.ai === "live"
    ? `<span class="mode live" title="Model: ${esc(s.ai_model || "")}">Claude</span>`
    : `<span class="mode warn" title="No Claude key: invoices, bills and reminders use offline rules and templates. Add ANTHROPIC_API_KEY to .env.">AI offline</span>`);
  modes.push(s.mode.webhooks
    ? `<span class="mode live" title="PayPal webhooks, signature-verified">Webhooks</span>`
    : `<span class="mode" title="Payment status is checked with PayPal every 10 seconds">Polling 10s</span>`);
  $("#modes").innerHTML = modes.join("");

  renderSummary(s);
  renderInvoices(s);
  renderBills(s);
  renderReview(s);
  renderForecast(s);
  renderLog(s);
}

function renderSummary(s) {
  const f = s.forecast, m = s.summary;
  const belowBuffer = f.low_cents < f.buffer_cents;
  const overdue = m.overdue_count
    ? `<span class="warn">${money(m.overdue_cents)} overdue</span>`
    : "none overdue";
  const nextPayout = m.next_payout_date
    ? `next: <b>${money(m.next_payout_cents)}</b> to ${esc(m.next_payout_to)} on ${shortDate(m.next_payout_date)}`
    : "nothing scheduled";
  $("#summary").innerHTML = `
    <div class="metric cash">
      <div class="label">Cash on hand</div>
      <div class="value">${money(s.balance_cents)}</div>
      <div class="context">Ledger balance as of ${shortDate(s.today)}</div>
    </div>
    <div class="metric in">
      <div class="label">Receivables</div>
      <div class="value">${money(s.open_receivables_cents)}</div>
      <div class="context">${plural(m.open_invoice_count, "open invoice")}, ${overdue}</div>
    </div>
    <div class="metric out">
      <div class="label">Payables</div>
      <div class="value">${money(s.open_payables_cents)}</div>
      <div class="context">${plural(m.scheduled_bill_count, "bill")} scheduled; ${nextPayout}</div>
    </div>
    <div class="metric ${belowBuffer ? "bad" : "ok"}">
      <div class="label">30-day low point</div>
      <div class="value">${money(f.low_cents)}</div>
      <div class="context">${belowBuffer ? `<span class="warn">Below</span>` : "Above"} the ${money(f.buffer_cents)} buffer, on ${shortDate(f.low_date)}</div>
    </div>`;
}

function invoiceRow(inv) {
  const name = inv.milestone && !inv.description.toLowerCase().includes(inv.milestone.toLowerCase())
    ? `${inv.milestone}: ${inv.description}` : inv.description;
  const risk = inv.status === "sent"
    ? `<span class="pill ${inv.risk_band}">${inv.risk_band} risk</span><span class="reason">${esc(inv.risk_reason)}</span>`
    : inv.status === "paid" ? `<span class="sub">Paid ${shortDate(inv.paid_date)}</span>` : "";
  const reminded = inv.last_reminder
    ? `<div class="sub">Reminded ${shortDate(inv.last_reminder.date)}${inv.reminder_count > 1 ? ` (${inv.reminder_count}x)` : ""}</div>` : "";
  const status = inv.status === "sent" && inv.days_to_due < 0 ? "overdue" : inv.status;
  const actions = inv.status === "sent"
    ? `${inv.payer_url ? `<a href="${esc(inv.payer_url)}" target="_blank" rel="noopener">Payer page</a>` : ""}
       <button class="link" data-remind="${inv.id}">${openDrawer === inv.id ? "Close" : "Remind"}</button>` : "";
  let html = `<tr class="${openDrawer === inv.id ? "open" : ""}">
    <td><strong>${esc(inv.customer_name)}</strong><div class="sub">${esc(name)}</div></td>
    <td><span class="inv-no">${esc(inv.number)}</span><div class="sub">Issued ${shortDate(inv.issue_date)}</div></td>
    <td class="num">${money(inv.amount_cents)}</td>
    <td class="nowrap">${shortDate(inv.due_date)}<div class="sub nowrap">${dueText(inv)}</div></td>
    <td>${risk}</td>
    <td><span class="pill ${status}">${status}</span>${reminded}</td>
    <td class="actions">${actions}</td></tr>`;
  if (openDrawer === inv.id) html += `<tr class="open"><td colspan="7" class="drawer" id="drawer-${inv.id}"></td></tr>`;
  return html;
}

function renderInvoices(s) {
  // Don't redraw under someone typing in the reminder preview.
  if (openDrawer && $(`#drawer-${openDrawer}`)?.contains(document.activeElement)) return;
  const drawerHTML = openDrawer ? $(`#drawer-${openDrawer}`)?.innerHTML : null;
  const open = s.invoices.filter((i) => i.status === "sent").length;
  $("#inv-count").textContent = open ? `${open} open` : "";
  $("#invoices").innerHTML = `<thead><tr><th>Customer</th><th>Invoice</th><th class="num">Amount</th>
    <th>Due</th><th>Late-pay risk</th><th>Status</th><th></th></tr></thead>
    <tbody>${s.invoices.map(invoiceRow).join("") || `<tr class="empty"><td colspan="7">No invoices yet. Describe a job in New invoice to send your first one.</td></tr>`}</tbody>`;
  if (openDrawer && drawerHTML && $(`#drawer-${openDrawer}`)) $(`#drawer-${openDrawer}`).innerHTML = drawerHTML;
}

function renderBills(s) {
  const rows = s.bills.filter((b) => b.status !== "review").map((b) => {
    const saves = b.pay_amount_cents != null && b.pay_amount_cents < b.amount_cents
      ? `<div class="sub">saves ${money(b.amount_cents - b.pay_amount_cents)}</div>` : "";
    return `<tr>
      <td><strong>${esc(b.supplier_name)}</strong><div class="sub">${esc(b.number || "")}</div></td>
      <td class="num">${money(b.amount_cents)}</td>
      <td>${terms(b)}<div class="sub">due ${shortDate(b.due_date)}</div></td>
      <td class="nowrap">${shortDate(b.pay_date)}</td>
      <td class="num">${money(b.pay_amount_cents ?? b.amount_cents)}${saves}</td>
      <td><span class="pill ${b.status}">${b.status}</span></td></tr>`;
  });
  const scheduled = s.bills.filter((b) => ["scheduled", "processing"].includes(b.status)).length;
  $("#bill-count").textContent = scheduled ? `${scheduled} scheduled` : "";
  $("#bills").innerHTML = `<thead><tr><th>Supplier</th><th class="num">Bill</th><th>Terms</th>
    <th>Pay on</th><th class="num">Pays</th><th>Status</th></tr></thead>
    <tbody>${rows.join("") || `<tr class="empty"><td colspan="6">No scheduled bills yet. Upload one above.</td></tr>`}</tbody>`;
}

function renderReview(s) {
  const reviews = s.bills.filter((b) => b.status === "review");
  const sig = JSON.stringify(reviews.map((b) => [b.id, b.amount_cents, b.recommendation && b.recommendation.headline, b.discount_pct, b.due_date]));
  const box = $("#review");
  if (sig === reviewSignature) return;
  if (box.contains(document.activeElement) && document.activeElement.tagName !== "BUTTON") return;
  reviewSignature = sig;
  box.innerHTML = reviews.map((b) => reviewCard(b, s)).join("");
}

function reviewCard(b, s) {
  const r = b.recommendation || {};
  const ex = b.extraction || {};
  const supplierOptions = s.suppliers.map((sp) =>
    `<option value="${sp.id}" ${sp.id === b.supplier_id ? "selected" : ""}>${esc(sp.name)}</option>`).join("");
  const warn = (ex.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  let bars = "";
  if (b.discount_pct > 0) {
    const apr = r.annualized_return * 100, cash = r.cash_yield_rate * 100;
    const max = Math.max(apr, cash, 1);
    bars = `<div class="compare" aria-label="Return comparison">
      <div class="bar-row"><span>Taking the discount</span><div class="bar"><span class="disc" style="width:${(apr / max) * 100}%"></span></div><b>${apr.toFixed(1)}%</b></div>
      <div class="bar-row"><span>Holding the cash</span><div class="bar"><span class="cash" style="width:${(cash / max) * 100}%"></span></div><b>${cash.toFixed(1)}%</b></div>
    </div>`;
  }
  const by = ex.source === "Claude" ? `<span class="badge claude">Read by Claude</span>` : sourceBadge(ex.source);
  return `<div class="card bill" data-bill="${b.id}">
    <div class="card-head"><strong>${esc(b.supplier_name)} bill ${esc(b.number || "")}</strong>
      <span class="by">${by} ${Math.round((ex.confidence || 0) * 100)}% confident. Check the numbers, then approve.</span></div>
    <div class="review-grid">
      <div class="field"><label>Supplier</label><select data-f="supplier_id">${supplierOptions}</select></div>
      <div class="field"><label>Amount due</label><input data-f="amount" inputmode="decimal" value="${(b.amount_cents / 100).toFixed(2)}"></div>
      <div class="field"><label>Bill date</label><input type="date" data-f="bill_date" value="${b.bill_date}"></div>
      <div class="field"><label>Discount %</label><input data-f="discount_pct" inputmode="decimal" value="${b.discount_pct}"></div>
      <div class="field"><label>Within days</label><input data-f="discount_days" inputmode="numeric" value="${b.discount_days}"></div>
      <div class="field"><label>Net days</label><input data-f="net_days" inputmode="numeric" value="${b.net_days}"></div>
    </div>
    ${warn ? `<ul class="warnings">${warn}</ul>` : ""}
    <div class="verdict">${esc(r.headline || "")}</div>
    <p class="verdict-detail">${esc(r.detail || "")}</p>
    ${bars}
    <div class="actions-row">
      <button class="primary" data-approve="${b.id}" data-choice="recommended">Approve: pay ${money(r.pay_amount_cents)} on ${shortDate(r.pay_date)}</button>
      ${r.take_discount ? `<button class="secondary" data-approve="${b.id}" data-choice="due_date">Pay full amount on the due date</button>` : ""}
      <button class="link" data-recalc="${b.id}">Recalculate with my changes</button>
    </div></div>`;
}

function renderForecast(s) {
  const f = s.forecast;
  const series = f.series;
  const W = 340, H = 180, padL = 8, padR = 8, padT = 22, padB = 26;
  const vals = series.map((p) => p.balance_cents).concat([f.buffer_cents]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, 100000);
  lo -= span * 0.12; hi += span * 0.12;
  const x = (i) => padL + (i / (series.length - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  let path = `M${x(0)},${y(series[0].balance_cents)}`;
  for (let i = 1; i < series.length; i++) path += `H${x(i)}V${y(series[i].balance_cents)}`;
  const area = `${path}V${H - padB}H${x(0)}Z`;
  const by = y(f.buffer_cents);
  const marks = series.map((p, i) => {
    let out = "";
    if (p.in_cents) out += `<circle cx="${x(i)}" cy="${y(p.balance_cents)}" r="3.5" fill="var(--moss)"><title>${shortDate(p.date)}: +${money(p.in_cents)}</title></circle>`;
    if (p.out_cents) out += `<circle cx="${x(i)}" cy="${y(p.balance_cents)}" r="3.5" fill="var(--crimson)"><title>${shortDate(p.date)}: -${money(p.out_cents)}</title></circle>`;
    return out;
  }).join("");
  const last = series[series.length - 1];
  const mid = series[Math.floor(series.length / 2)];
  $("#chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Projected cash balance for the next 30 days">
    <path d="${area}" fill="var(--moss-soft)"/>
    <line x1="${padL}" x2="${W - padR}" y1="${by}" y2="${by}" stroke="var(--amber)" stroke-dasharray="4 4" stroke-width="1.2"/>
    <text x="${W - padR}" y="${by - 5}" text-anchor="end" font-size="11" fill="var(--amber)">Buffer ${moneyShort(f.buffer_cents)}</text>
    <path d="${path}" fill="none" stroke="var(--moss)" stroke-width="2"/>
    ${marks}
    <text x="${padL}" y="14" font-size="11.5" fill="var(--ink-2)">${moneyShort(series[0].balance_cents)} today</text>
    <text x="${W - padR}" y="14" text-anchor="end" font-size="11.5" font-weight="600" fill="var(--ink)">${moneyShort(last.balance_cents)} by ${shortDate(last.date)}</text>
    <text x="${padL}" y="${H - 8}" font-size="11" fill="var(--ink-3)">${shortDate(series[0].date)}</text>
    <text x="${x(Math.floor(series.length / 2))}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--ink-3)">${shortDate(mid.date)}</text>
    <text x="${W - padR}" y="${H - 8}" text-anchor="end" font-size="11" fill="var(--ink-3)">${shortDate(last.date)}</text>
  </svg>`;

  const lowBad = f.low_cents < f.buffer_cents;
  $("#forecast-note").textContent = lowBad
    ? `Cash dips below your ${money(f.buffer_cents)} buffer on ${shortDate(f.low_date)}.`
    : `Stays above your ${money(f.buffer_cents)} buffer. Payments from late-paying customers are shifted by their usual delay.`;

  const items = [];
  for (const p of series) for (const it of p.items) items.push({ date: p.date, ...it });
  $("#flows").innerHTML = items.slice(0, 9).map((it) => `<li><span class="d">${shortDate(it.date)}</span>
      <span>${esc(it.label)}</span><span class="${it.cents > 0 ? "in" : "out"}">${it.cents > 0 ? "+" : "-"}${money(Math.abs(it.cents))}</span></li>`).join("")
    || `<li><span></span><span class="sub">Nothing expected in the next 30 days.</span><span></span></li>`;
}

function renderLog(s) {
  $("#log").innerHTML = s.events.map((e) => {
    const t = new Date(e.ts).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    return `<li class="${e.kind === "error" ? "error" : ""}"><span class="t">${t}</span>${esc(e.message)}</li>`;
  }).join("");
}

// ---------------------------------------------------------------- invoice draft
function renderDraft(d) {
  const box = $("#draft");
  const options = [`<option value="">Choose a customer</option>`].concat(STATE.customers.map((c) =>
    `<option value="${c.id}" ${c.id === d.customer_id ? "selected" : ""}>${esc(c.name)}</option>`)).join("");
  const desc = d.milestone && !d.description.toLowerCase().includes(d.milestone.toLowerCase())
    ? `${d.milestone}: ${d.description}` : d.description;
  const risk = d.risk
    ? `<span class="risk-line"><span class="pill ${d.risk.band}">${d.risk.band} risk</span> ${esc(d.risk.reason)}${d.risk.band === "high" ? ". Reminders will start 3 days before the due date." : ""}</span>`
    : `<span class="risk-line sub">Pick a customer and amount to see the late-pay risk.</span>`;
  const warn = (d.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  box.hidden = false;
  box.innerHTML = `<form class="card" id="slip">
    <div class="card-head"><strong>Invoice draft from ${esc(STATE.business)}</strong>
      <span class="by">${sourceBadge(d.source)} Nothing is sent until you click Send.</span></div>
    <div class="fields">
      <div class="field"><label for="d-customer">Customer</label><select id="d-customer" required>${options}</select></div>
      <div class="field"><label for="d-desc">Line item</label><input id="d-desc" value="${esc(desc)}" maxlength="200"></div>
      <div class="field"><label for="d-amount">Amount (USD)</label><input id="d-amount" inputmode="decimal" value="${d.amount_cents ? (d.amount_cents / 100).toFixed(2) : ""}" required></div>
      <div class="field"><label for="d-due">Due date</label><input id="d-due" type="date" value="${d.due_date || ""}" required></div>
    </div>
    ${warn ? `<ul class="warnings">${warn}</ul>` : ""}
    <div class="card-foot">${risk}
      <span class="actions-row"><button type="button" class="link" id="discard">Discard</button>
      <button type="submit" class="primary" id="send-btn">Send with PayPal</button></span></div>
  </form>`;
  $("#discard").onclick = () => { box.hidden = true; box.innerHTML = ""; };
  $("#slip").onsubmit = (ev) => {
    ev.preventDefault();
    withBusy($("#send-btn"), async () => {
      const inv = await api("/api/invoices", { method: "POST", body: {
        customer_id: Number($("#d-customer").value),
        description: $("#d-desc").value,
        amount: $("#d-amount").value,
        due_date: $("#d-due").value,
      }});
      box.hidden = true; box.innerHTML = ""; $("#sentence").value = "";
      toast(`Sent ${inv.number} to ${inv.customer_name} through PayPal.`);
      await load();
    }, "Sending...");
  };
}

// ---------------------------------------------------------------- reminder preview
async function openReminder(id, button) {
  if (openDrawer === id) { openDrawer = null; renderInvoices(STATE); return; }
  await withBusy(button, async () => {
    const draft = await api(`/api/invoices/${id}/reminder/draft`, { method: "POST" });
    openDrawer = id;
    renderInvoices(STATE);
    const inv = STATE.invoices.find((i) => i.id === id);
    $(`#drawer-${id}`).innerHTML = `<form class="card" id="reminder-form" data-id="${id}">
      <div class="card-head"><strong>Reminder to ${esc(inv.customer_name)}</strong>
        <span class="by">${sourceBadge(draft.source)} <b>${esc(draft.tone)}</b> tone, based on their history and the due date. Edit anything before sending.</span></div>
      <div class="field"><label for="r-subject">Subject</label><input id="r-subject" value="${esc(draft.subject)}" maxlength="120"></div>
      <div class="field" style="margin-top:10px"><label for="r-note">Message (PayPal adds the payment button)</label>
        <textarea id="r-note" rows="8">${esc(draft.note)}</textarea></div>
      <div class="card-foot"><span class="sub">Sent through PayPal's invoice reminder.</span>
        <span class="actions-row"><button type="button" class="link" id="r-cancel">Cancel</button>
        <button type="submit" class="primary" id="r-send">Send reminder with PayPal</button></span></div>
    </form>`;
    $("#r-cancel").onclick = () => { openDrawer = null; renderInvoices(STATE); };
    $("#reminder-form").onsubmit = (ev) => {
      ev.preventDefault();
      const original = { subject: draft.subject, note: draft.note };
      const subject = $("#r-subject").value, note = $("#r-note").value;
      const edited = subject !== original.subject || note !== original.note;
      withBusy($("#r-send"), async () => {
        // Unedited drafts are sent as reviewed; the server records who wrote them.
        await api(`/api/invoices/${id}/remind`, { method: "POST",
          body: edited ? { subject, note } : { subject, note, source: draft.source, tone: draft.tone } });
        openDrawer = null;
        toast(`Reminder sent to ${inv.customer_name} through PayPal.`);
        await load();
      }, "Sending...");
    };
    $("#r-note").focus();
  }, "Writing...");
}

// ---------------------------------------------------------------- data
async function load() {
  STATE = await api("/api/state");
  render();
}

async function poll() {
  if (document.hidden || busy) return;
  try {
    await api("/api/invoices/refresh", { method: "POST" });
    await load();
  } catch (_) { /* transient; next poll retries */ }
}

async function uploadBill(file) {
  const form = new FormData();
  form.append("file", file);
  const bill = await api("/api/bills/upload", { method: "POST", form });
  toast(`Read ${bill.supplier_name}: ${money(bill.amount_cents)}.`);
  reviewSignature = "";
  await load();
  document.querySelector(`[data-bill="${bill.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------------------------------------------------------------- events
function wire() {
  $("#use-example").onclick = () => {
    $("#sentence").value = "Bill Hillside HOA for phase 2 of the courtyard, $4,800, due in 15 days";
    $("#sentence").focus();
  };
  $("#composer").onsubmit = (ev) => {
    ev.preventDefault();
    withBusy($("#draft-btn"), async () => {
      const draft = await api("/api/invoices/draft", { method: "POST", body: { text: $("#sentence").value } });
      renderDraft(draft);
    }, "Reading...");
  };
  $("#sentence").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#composer").requestSubmit(); }
  });

  $("#invoices").addEventListener("click", (ev) => {
    const id = ev.target.dataset.remind;
    if (id) openReminder(Number(id), ev.target);
  });
  $("#sync").onclick = (ev) => withBusy(ev.target, async () => {
    const r = await api("/api/invoices/refresh", { method: "POST" });
    toast(r.updated ? `${plural(r.updated, "invoice")} updated from PayPal.` : "Everything matches PayPal.");
    await load();
  }, "Checking...");

  const fileInput = $("#bill-file"), drop = $("#drop");
  fileInput.onchange = () => fileInput.files[0] && withBusy(null, () => uploadBill(fileInput.files[0]).finally(() => (fileInput.value = "")));
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) withBusy(null, () => uploadBill(f)); });

  document.querySelectorAll("[data-sample]").forEach((btn) => {
    btn.onclick = () => withBusy(btn, async () => {
      const bill = await api(`/api/bills/sample/${btn.dataset.sample}`, { method: "POST" });
      toast(`Read ${bill.supplier_name}: ${money(bill.amount_cents)}.`);
      reviewSignature = "";
      await load();
      document.querySelector(`[data-bill="${bill.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, "Reading the bill...");
  });

  $("#review").addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.dataset.approve) {
      withBusy(t, async () => {
        const bill = await api(`/api/bills/${t.dataset.approve}/approve`, { method: "POST", body: { choice: t.dataset.choice } });
        toast(`Approved. PayPal Payouts sends ${money(bill.pay_amount_cents)} on ${shortDate(bill.pay_date)}.`);
        reviewSignature = "";
        await load();
      });
    } else if (t.dataset.recalc) {
      const card = t.closest(".card");
      const fields = {};
      card.querySelectorAll("[data-f]").forEach((el) => (fields[el.dataset.f] = el.value));
      withBusy(t, async () => {
        await api(`/api/bills/${t.dataset.recalc}`, { method: "PATCH", body: fields });
        reviewSignature = "";
        document.activeElement.blur();
        await load();
        toast("Recalculated with your changes.");
      });
    }
  });

  $("#next-day").onclick = (ev) => withBusy(ev.target, async () => {
    const r = await api("/api/clock/advance", { method: "POST", body: { days: 1 } });
    toast(`Now ${shortDate(r.today)}: ${plural(r.reminders_sent, "reminder")} sent, ${plural(r.payouts_released, "payout")} released.`);
    await load();
  });
  $("#run-job").onclick = (ev) => withBusy(ev.target, async () => {
    const r = await api("/api/scheduler/run", { method: "POST" });
    toast(`${plural(r.reminders_sent, "reminder")} sent, ${plural(r.payouts_released, "payout")} released.`);
    await load();
  });
  $("#reset").onclick = (ev) => {
    if (!confirm("Reset all demo data? This creates fresh demo invoices.")) return;
    withBusy(ev.target, async () => {
      await api("/api/demo/reset", { method: "POST" });
      reviewSignature = ""; openDrawer = null;
      $("#draft").hidden = true;
      await load();
      toast("Demo data reset.");
    }, "Resetting...");
  };

  const params = new URLSearchParams(location.search);
  if (params.get("paid")) {
    const b = $("#banner");
    b.hidden = false;
    b.textContent = `Payment received for ${params.get("paid")}. The invoice is marked paid and your cash forecast is updated.`;
    history.replaceState(null, "", "/");
  }
}

wire();
load().catch((err) => toast(err.message, true));
setInterval(poll, 10000);
