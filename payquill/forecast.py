"""30-day cash view: today's balance, plus expected customer payments, minus
supplier payments PayQuill has scheduled or recommended."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

HORIZON_DAYS = 30


@dataclass
class Flow:
    day: date
    cents: int  # positive = money in, negative = money out
    label: str


def project(start_balance: int, today: date, flows: list[Flow], horizon: int = HORIZON_DAYS) -> dict:
    by_day: dict[date, list[Flow]] = {}
    for f in flows:
        day = max(f.day, today)  # anything overdue is shown as expected today
        if day <= today + timedelta(days=horizon):
            by_day.setdefault(day, []).append(f)

    balance = start_balance
    series = []
    low = (today, start_balance)
    for i in range(horizon + 1):
        day = today + timedelta(days=i)
        todays = by_day.get(day, [])
        inflow = sum(f.cents for f in todays if f.cents > 0)
        outflow = -sum(f.cents for f in todays if f.cents < 0)
        balance += inflow - outflow
        series.append({
            "date": day.isoformat(),
            "balance_cents": balance,
            "in_cents": inflow,
            "out_cents": outflow,
            "items": [{"label": f.label, "cents": f.cents} for f in todays],
        })
        if balance < low[1]:
            low = (day, balance)

    return {
        "start_cents": start_balance,
        "end_cents": balance,
        "low_date": low[0].isoformat(),
        "low_cents": low[1],
        "series": series,
    }
