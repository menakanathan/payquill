"""Settings, read once from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _dollars_to_cents(name: str, default: float) -> int:
    return int(round(_float(name, default) * 100))


@dataclass(frozen=True)
class Settings:
    # PayPal
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_base_url: str = SANDBOX_BASE
    paypal_mode: str = "mock"  # "sandbox" or "mock"
    buyer_email: str = "buyer@verdestudio-customers.example"
    payee_email: str = "supplier@verdestudio-suppliers.example"

    # AI
    ai_provider: str = "offline"  # "anthropic" or "offline"
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-5-5"

    # App
    db_path: str = str(ROOT / "data" / "payquill.db")
    samples_dir: str = str(ROOT / "data" / "samples")
    public_url: str = "http://localhost:5000"

    # Money rules (all amounts in cents)
    opening_balance_cents: int = 1_800_000
    cash_buffer_cents: int = 500_000
    cash_yield_rate: float = 0.045

    business_name: str = "Verde Studio"
    owner_name: str = "Mitchell Movado"

    extra: dict = field(default_factory=dict)

    @property
    def is_sandbox(self) -> bool:
        return self.paypal_mode == "sandbox"


def load_settings(**overrides) -> Settings:
    client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
    mode = os.getenv("PAYPAL_MODE", "").strip().lower()
    if mode not in {"sandbox", "mock"}:
        mode = "sandbox" if client_id and secret else "mock"

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    provider = os.getenv("AI_PROVIDER", "").strip().lower()
    if provider == "demo":
        provider = "offline"
    if provider not in {"anthropic", "offline"}:
        provider = "anthropic" if api_key else "offline"

    values = dict(
        paypal_client_id=client_id,
        paypal_client_secret=secret,
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", SANDBOX_BASE).rstrip("/"),
        paypal_mode=mode,
        buyer_email=os.getenv("SANDBOX_BUYER_EMAIL", Settings.buyer_email).strip(),
        payee_email=os.getenv("SANDBOX_PAYEE_EMAIL", Settings.payee_email).strip(),
        ai_provider=provider,
        anthropic_api_key=api_key,
        ai_model=os.getenv("AI_MODEL", Settings.ai_model).strip(),
        db_path=os.getenv("PAYQUILL_DB", Settings.db_path),
        samples_dir=os.getenv("PAYQUILL_SAMPLES", Settings.samples_dir),
        public_url=(os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or Settings.public_url).rstrip("/"),
        opening_balance_cents=_dollars_to_cents("OPENING_BALANCE", 18000),
        cash_buffer_cents=_dollars_to_cents("CASH_BUFFER", 5000),
        cash_yield_rate=_float("CASH_YIELD_RATE", 0.045),
    )
    values.update(overrides)
    return Settings(**values)
