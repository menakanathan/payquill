"""Best day to pay a supplier bill.

Terms like "2/10 net 30" mean: pay within 10 days and take 2% off, otherwise
the full amount is due in 30 days. Taking the discount means paying 20 days
early. The return on that is

    discount / (1 - discount) * 365 / (net_days - discount_days)

For 2/10 net 30 that is 2/98 * 365/20 = 37.2% a year, far above what idle cash
earns. PayQuill takes the discount when that return beats the cash yield AND
the 30-day forecast stays above the owner's cash buffer. Otherwise it pays on
the due date, never earlier than needed.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

PROCESSING_DAYS = 1  # release one day early so the payout lands in time


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def short(d: date) -> str:
    return f"{d:%b} {d.day}"  # portable (no %-d, which fails on Windows)


def annualized_discount_return(discount_pct: float, discount_days: int, net_days: int) -> float:
    if discount_pct <= 0 or net_days <= discount_days:
        return 0.0
    d = discount_pct / 100
    return d / (1 - d) * 365 / (net_days - discount_days)


def recommend(
    *,
    amount_cents: int,
    bill_date: date,
    due_date: date,
    discount_pct: float,
    discount_days: int,
    net_days: int,
    today: date,
    cash_yield_rate: float,
    stays_above_buffer: Callable[[date, int], tuple[bool, date | None, int | None]] | None = None,
) -> dict:
    """stays_above_buffer(pay_date, pay_amount) -> (ok, lowest_date, lowest_balance)."""
    rate_pct = cash_yield_rate * 100
    full_pay_date = max(today, due_date - timedelta(days=PROCESSING_DAYS))

    def pay_on_due(headline: str, detail: str, apr: float = 0.0) -> dict:
        return {
            "take_discount": False,
            "pay_date": full_pay_date.isoformat(),
            "pay_amount_cents": amount_cents,
            "savings_cents": 0,
            "annualized_return": round(apr, 4),
            "cash_yield_rate": cash_yield_rate,
            "headline": headline,
            "detail": detail,
        }

    if discount_pct <= 0:
        return pay_on_due(
            f"Pay on {short(full_pay_date)}, the day before it's due.",
            f"No early-payment discount on this bill, so keep the {money(amount_cents)} "
            f"earning {rate_pct:.1f}% until it's due.",
        )

    deadline = bill_date + timedelta(days=discount_days)
    apr = annualized_discount_return(discount_pct, discount_days, net_days)
    savings = int(round(amount_cents * discount_pct / 100))
    discounted = amount_cents - savings

    if deadline < today:
        return pay_on_due(
            f"Discount window closed on {short(deadline)}. Pay on {short(full_pay_date)}.",
            f"The {discount_pct:g}% discount needed payment by {short(deadline)}. "
            f"Keep the cash until the day before it's due.",
            apr,
        )

    if apr <= cash_yield_rate:
        return pay_on_due(
            f"Pay on {short(full_pay_date)}. The discount isn't worth it.",
            f"Taking {discount_pct:g}% off is worth {apr * 100:.1f}% a year, less than the "
            f"{rate_pct:.1f}% your cash earns.",
            apr,
        )

    early_date = max(today, deadline - timedelta(days=PROCESSING_DAYS))
    if stays_above_buffer:
        ok, low_date, low_balance = stays_above_buffer(early_date, discounted)
        if not ok:
            return pay_on_due(
                f"Pay on {short(full_pay_date)} to protect your cash buffer.",
                f"Paying early would save {money(savings)}, but your balance would dip to "
                f"{money(low_balance or 0)} on {short(low_date) if low_date else 'a later day'}, "
                f"below your buffer.",
                apr,
            )

    days_early = (due_date - early_date).days
    return {
        "take_discount": True,
        "pay_date": early_date.isoformat(),
        "pay_amount_cents": discounted,
        "savings_cents": savings,
        "annualized_return": round(apr, 4),
        "cash_yield_rate": cash_yield_rate,
        "headline": f"Pay on {short(early_date)} and save {money(savings)}.",
        "detail": (
            f"Taking the {discount_pct:g}% discount means paying {days_early} days early. "
            f"That is worth {apr * 100:.1f}% a year, versus {rate_pct:.1f}% from holding the cash, "
            f"and your 30-day balance stays above your buffer."
        ),
    }
