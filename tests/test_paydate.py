from datetime import date, timedelta

from payquill.paydate import annualized_discount_return, recommend

TODAY = date(2026, 11, 8)


def base(**kw):
    args = dict(amount_cents=482_645, bill_date=TODAY - timedelta(days=3), due_date=TODAY + timedelta(days=27),
                discount_pct=2, discount_days=10, net_days=30, today=TODAY, cash_yield_rate=0.045)
    args.update(kw)
    return args


def test_two_ten_net_thirty_is_37_percent_a_year():
    assert round(annualized_discount_return(2, 10, 30) * 100, 1) == 37.2


def test_takes_discount_one_day_before_deadline():
    rec = recommend(**base())
    assert rec["take_discount"] is True
    assert rec["pay_date"] == (TODAY + timedelta(days=6)).isoformat()  # deadline Nov 15, release Nov 14
    assert rec["savings_cents"] == 9653
    assert rec["pay_amount_cents"] == 482_645 - 9653
    assert "37.2%" in rec["detail"] and "4.5%" in rec["detail"]


def test_buffer_check_overrides_discount():
    rec = recommend(**base(), stays_above_buffer=lambda d, amt: (False, d, 120_000))
    assert rec["take_discount"] is False
    assert rec["pay_date"] == (TODAY + timedelta(days=26)).isoformat()
    assert "buffer" in rec["headline"]


def test_no_discount_pays_day_before_due():
    rec = recommend(**base(discount_pct=0, discount_days=0))
    assert rec["take_discount"] is False
    assert rec["pay_amount_cents"] == 482_645


def test_small_discount_not_worth_it_when_cash_yield_is_higher():
    rec = recommend(**base(discount_pct=0.1, discount_days=10, net_days=60, cash_yield_rate=0.05))
    assert rec["take_discount"] is False
    assert "isn't worth it" in rec["headline"]


def test_closed_discount_window():
    rec = recommend(**base(bill_date=TODAY - timedelta(days=15)))
    assert rec["take_discount"] is False
    assert "closed" in rec["headline"]
