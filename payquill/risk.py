"""Late-payment risk: a small logistic regression trained on the business's own
payment history, written with numpy so every step is visible.

Label:    an invoice counts as "late" when it was paid more than 3 days after its due date.
Features: built only from what was known *before* that invoice was issued.
  1. average days late on the customer's earlier invoices (scaled by 30)
  2. share of earlier invoices paid late
  3. log of this amount over the customer's usual amount
  4. thin history: 1 / (1 + number of earlier invoices)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np

LATE_AFTER_DAYS = 3
FEATURES = ["avg_days_late", "late_share", "size_vs_usual", "thin_history"]


@dataclass
class Record:
    customer_id: int
    amount_cents: int
    due_date: date
    paid_date: date

    @property
    def days_late(self) -> int:
        return max(0, (self.paid_date - self.due_date).days)


def featurize(prior: list[Record], amount_cents: int) -> tuple[np.ndarray, dict]:
    n = len(prior)
    if n:
        lates = [r.days_late for r in prior]
        avg_late = sum(lates) / n
        late_share = sum(1 for d in lates if d > LATE_AFTER_DAYS) / n
        usual = sum(r.amount_cents for r in prior) / n
    else:
        avg_late, late_share, usual = 0.0, 0.0, float(amount_cents)
    size = math.log(max(amount_cents, 1) / max(usual, 1))
    x = np.array([min(avg_late, 60) / 30, late_share, size, 1 / (1 + n)], dtype=float)
    facts = {
        "n": n,
        "avg_late": avg_late,
        "late_count": int(round(late_share * n)),
        "size_ratio": amount_cents / usual if usual else 1.0,
        "recent_lates": [r.days_late for r in prior[-3:]],
    }
    return x, facts


class LateRiskModel:
    def __init__(self, l2: float = 0.05, lr: float = 0.3, epochs: int = 3000):
        self.l2, self.lr, self.epochs = l2, lr, epochs
        self.w = np.zeros(len(FEATURES))
        self.b = 0.0
        self.mean = np.zeros(len(FEATURES))
        self.std = np.ones(len(FEATURES))
        self.trained_on = 0
        self.train_accuracy = None

    def fit(self, history: list[Record]) -> "LateRiskModel":
        by_customer: dict[int, list[Record]] = {}
        for r in sorted(history, key=lambda r: r.due_date):
            by_customer.setdefault(r.customer_id, []).append(r)

        X, y = [], []
        for records in by_customer.values():
            for i, rec in enumerate(records):
                x, _ = featurize(records[:i], rec.amount_cents)
                X.append(x)
                y.append(1.0 if rec.days_late > LATE_AFTER_DAYS else 0.0)
        if len(X) < 5 or len(set(y)) < 2:
            return self  # not enough signal; predict() falls back to the base rate of 0.5

        X = np.vstack(X)
        y = np.array(y)
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std == 0] = 1.0
        Z = (X - self.mean) / self.std

        w, b = np.zeros(Z.shape[1]), 0.0
        for _ in range(self.epochs):
            p = 1 / (1 + np.exp(-(Z @ w + b)))
            grad_w = Z.T @ (p - y) / len(y) + self.l2 * w
            grad_b = float(np.mean(p - y))
            w -= self.lr * grad_w
            b -= self.lr * grad_b
        self.w, self.b = w, b
        self.trained_on = len(y)
        preds = (1 / (1 + np.exp(-(Z @ w + b)))) >= 0.5
        self.train_accuracy = float(np.mean(preds == (y == 1)))
        return self

    def predict(self, prior: list[Record], amount_cents: int) -> dict:
        x, facts = featurize(prior, amount_cents)
        z = (x - self.mean) / self.std
        contrib = self.w * z
        p = float(1 / (1 + math.exp(-(float(contrib.sum()) + self.b)))) if self.trained_on else 0.5
        band = "high" if p >= 0.6 else "medium" if p >= 0.3 else "low"
        return {
            "score": round(p, 3),
            "band": band,
            "reason": explain(band, contrib, facts),
        }


def explain(band: str, contrib: np.ndarray, facts: dict) -> str:
    n = facts["n"]
    if n == 0:
        return "New customer with no payment history yet"
    if band == "low":
        on_time = n - facts["late_count"]
        if n <= 2:
            return f"Only {n} past invoice{'s' if n != 1 else ''}, paid on time"
        return f"Paid {on_time} of {n} past invoices on time"
    top = FEATURES[int(np.argmax(contrib))]
    recent = facts["recent_lates"]
    if top in ("avg_days_late", "late_share") and len(recent) == 3 and min(recent) > LATE_AFTER_DAYS:
        return f"Paid the last 3 invoices {min(recent)}+ days late"
    if top in ("avg_days_late", "late_share"):
        return f"{facts['late_count']} of {n} past invoices paid late, {round(facts['avg_late'])} days on average"
    if top == "size_vs_usual":
        return f"This invoice is {facts['size_ratio']:.1f}x their usual amount"
    return f"Only {n} past invoice{'s' if n != 1 else ''} to judge from"


REMINDER_OFFSETS = {"high": [-3, 0, 3], "medium": [0, 5], "low": [3]}


def reminder_plan(band: str, due: date) -> list[dict]:
    """Riskier customers get earlier, firmer nudges."""
    from datetime import timedelta

    return [{"date": (due + timedelta(days=d)).isoformat(), "sent": False} for d in REMINDER_OFFSETS[band]]
