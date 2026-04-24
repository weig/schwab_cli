"""Tests for core strategy analytics.

Covers payoff, breakevens, max P/L, POP, EV, prob_touch, and combined
greeks with hand-computed golden values for the MVP named strategies.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from schwab_cli.analytics.strategy import (
    PricedLeg,
    breakevens,
    combined_greeks,
    ev,
    max_loss,
    max_profit,
    payoff_at_expiry,
    pop,
    prob_touch,
)

EXP = date(2026, 5, 1)


def PL(
    qty: int,
    side: str,
    strike: float,
    premium: float,
    iv: float | None = 0.30,
    delta: float | None = None,
    gamma: float | None = None,
    theta: float | None = None,
    vega: float | None = None,
) -> PricedLeg:
    return PricedLeg(
        qty=qty,
        side=side,  # type: ignore[arg-type]
        expiry=EXP,
        strike=strike,
        premium=premium,
        iv=iv,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
    )


# ---- payoff_at_expiry --------------------------------------------------


def test_payoff_long_call_itm():
    # +1 C 255 @ 2.00 → at S=260, payoff = (5 - 2) * 100 = 300
    legs = [PL(1, "C", 255, 2.00)]
    assert payoff_at_expiry(legs, 260) == pytest.approx(300.0)


def test_payoff_long_call_otm():
    legs = [PL(1, "C", 255, 2.00)]
    # At S=250, intrinsic=0, paid 2.00 → loss 200.
    assert payoff_at_expiry(legs, 250) == pytest.approx(-200.0)


def test_payoff_short_put_otm():
    # -1 P 240 @ 1.50 → at S=250 (OTM put), keep premium 150.
    legs = [PL(-1, "P", 240, 1.50)]
    assert payoff_at_expiry(legs, 250) == pytest.approx(150.0)


def test_payoff_short_put_itm():
    # -1 P 240 @ 1.50 → at S=230, intrinsic=10, owe 10*100=1000, premium 150
    # net = -850.
    legs = [PL(-1, "P", 240, 1.50)]
    assert payoff_at_expiry(legs, 230) == pytest.approx(-850.0)


def test_payoff_bull_call_spread_max_profit_region():
    # +C255 @ 3.00, -C260 @ 1.00 → net debit 2.00.
    # At S=270 (above both strikes): long +15, short -10 → intrinsic +5.
    # P/L = (5 - 2) * 100 = 300.
    legs = [PL(1, "C", 255, 3.00), PL(-1, "C", 260, 1.00)]
    assert payoff_at_expiry(legs, 270) == pytest.approx(300.0)


def test_payoff_bull_call_spread_max_loss_region():
    legs = [PL(1, "C", 255, 3.00), PL(-1, "C", 260, 1.00)]
    # At S=250 (below both): both worthless, debit 2.00 lost.
    assert payoff_at_expiry(legs, 250) == pytest.approx(-200.0)


def test_payoff_iron_condor_max_profit():
    # +1 P 192.5 @ 0.80, -1 P 197.5 @ 1.40, -1 C 207.5 @ 1.60, +1 C 210 @ 0.90
    # Net credit = -0.80 + 1.40 + 1.60 - 0.90 = 1.30.
    # At S=200 (between short strikes): all OTM → keep credit → +130.
    legs = [
        PL(1, "P", 192.5, 0.80),
        PL(-1, "P", 197.5, 1.40),
        PL(-1, "C", 207.5, 1.60),
        PL(1, "C", 210, 0.90),
    ]
    assert payoff_at_expiry(legs, 200) == pytest.approx(130.0)


def test_payoff_iron_condor_max_loss_upper():
    legs = [
        PL(1, "P", 192.5, 0.80),
        PL(-1, "P", 197.5, 1.40),
        PL(-1, "C", 207.5, 1.60),
        PL(1, "C", 210, 0.90),
    ]
    # At S=215: short C 207.5 loses 7.5, long C 210 gains 5 → net call -2.5.
    # Puts worthless.
    # Payoff = (-2.5 + 1.30) * 100 = -120.
    assert payoff_at_expiry(legs, 215) == pytest.approx(-120.0)


# ---- breakevens --------------------------------------------------------


def test_breakevens_long_call():
    # +1 C 255 @ 3.00 → BE at 258.
    legs = [PL(1, "C", 255, 3.00)]
    bes = breakevens(legs)
    assert bes == pytest.approx([258.0])


def test_breakevens_short_put():
    # -1 P 240 @ 2.00 → BE at 238 (below 238, losses exceed credit).
    legs = [PL(-1, "P", 240, 2.00)]
    bes = breakevens(legs)
    assert bes == pytest.approx([238.0])


def test_breakevens_bull_call_spread():
    # +C255 @ 3, -C260 @ 1 → net debit 2 → BE at 257.
    legs = [PL(1, "C", 255, 3.00), PL(-1, "C", 260, 1.00)]
    bes = breakevens(legs)
    assert bes == pytest.approx([257.0])


def test_breakevens_iron_condor_two_sided():
    # Net credit 1.30, short P at 197.5, short C at 207.5.
    # Lower BE = 197.5 - 1.30 = 196.20; upper BE = 207.5 + 1.30 = 208.80.
    legs = [
        PL(1, "P", 192.5, 0.80),
        PL(-1, "P", 197.5, 1.40),
        PL(-1, "C", 207.5, 1.60),
        PL(1, "C", 210, 0.90),
    ]
    bes = breakevens(legs)
    assert len(bes) == 2
    assert bes[0] == pytest.approx(196.20)
    assert bes[1] == pytest.approx(208.80)


def test_breakevens_long_straddle_two_sided():
    # +C255 @ 3, +P255 @ 2 → net debit 5 → BEs at 250 and 260.
    legs = [PL(1, "C", 255, 3.00), PL(1, "P", 255, 2.00)]
    bes = breakevens(legs)
    assert bes == pytest.approx([250.0, 260.0])


# ---- max_profit / max_loss --------------------------------------------


def test_max_profit_bull_call_spread():
    # Width 5, debit 2 → max profit 3*100=300.
    legs = [PL(1, "C", 255, 3.00), PL(-1, "C", 260, 1.00)]
    assert max_profit(legs) == pytest.approx(300.0)


def test_max_loss_bull_call_spread():
    legs = [PL(1, "C", 255, 3.00), PL(-1, "C", 260, 1.00)]
    assert max_loss(legs) == pytest.approx(-200.0)


def test_max_profit_long_call_unlimited():
    legs = [PL(1, "C", 255, 3.00)]
    assert max_profit(legs) is None


def test_max_loss_long_call_bounded():
    legs = [PL(1, "C", 255, 3.00)]
    assert max_loss(legs) == pytest.approx(-300.0)


def test_max_loss_naked_short_call_unlimited():
    legs = [PL(-1, "C", 255, 3.00)]
    assert max_loss(legs) is None


def test_max_profit_naked_short_put_is_credit():
    legs = [PL(-1, "P", 240, 2.00)]
    assert max_profit(legs) == pytest.approx(200.0)


def test_max_loss_naked_short_put_bounded_by_strike():
    # At S=0: loss = (0 - 240 + 2) * 100 = -23800.
    legs = [PL(-1, "P", 240, 2.00)]
    assert max_loss(legs) == pytest.approx(-23800.0)


def test_max_profit_iron_condor():
    legs = [
        PL(1, "P", 192.5, 0.80),
        PL(-1, "P", 197.5, 1.40),
        PL(-1, "C", 207.5, 1.60),
        PL(1, "C", 210, 0.90),
    ]
    assert max_profit(legs) == pytest.approx(130.0)


def test_max_loss_iron_condor():
    # Widths: put 5, call 2.5. Worse side is the put wing: 5 - 1.30 = 3.70 loss.
    legs = [
        PL(1, "P", 192.5, 0.80),
        PL(-1, "P", 197.5, 1.40),
        PL(-1, "C", 207.5, 1.60),
        PL(1, "C", 210, 0.90),
    ]
    assert max_loss(legs) == pytest.approx(-370.0)


def test_max_profit_long_butterfly():
    # +C250 @ 6, -2 C255 @ 3, +C260 @ 1.50 → debit = 6 - 2*3 + 1.50 = 1.50.
    # Max profit at K=255: (255-250 - 1.50)*100 = 350.
    legs = [PL(1, "C", 250, 6.00), PL(-2, "C", 255, 3.00), PL(1, "C", 260, 1.50)]
    assert max_profit(legs) == pytest.approx(350.0)


def test_max_loss_long_butterfly():
    legs = [PL(1, "C", 250, 6.00), PL(-2, "C", 255, 3.00), PL(1, "C", 260, 1.50)]
    # Max loss = -debit = -150.
    assert max_loss(legs) == pytest.approx(-150.0)


# ---- POP ---------------------------------------------------------------


def test_pop_long_call_approx_equals_delta():
    # For ATM long call at same strike as spot, POP = P(S_T > BE).
    # With BE slightly above spot, POP should be slightly below 0.50.
    # Delta ~ 0.50 at ATM, POP ~ slightly lower (because BE > spot).
    legs = [PL(1, "C", 100, 3.00, iv=0.30)]
    p = pop(legs, spot=100, dte=30, r=0.0)
    # BE = 103 > spot, so POP < 0.50. Roughly 0.30-0.40.
    assert 0.25 < p < 0.45


def test_pop_short_put_high_when_deep_otm():
    # Short 90 put with spot 100, vol 30%, 30 DTE, credit 0.50.
    # BE = 89.50. P(S > 89.50) should be very high.
    legs = [PL(-1, "P", 90, 0.50, iv=0.30)]
    p = pop(legs, spot=100, dte=30, r=0.0)
    assert 0.85 < p < 0.99


def test_pop_iron_condor_between_breakevens():
    # Use a symmetric IC around spot=200 with narrow wings.
    # Net credit gives BE wide → POP high.
    legs = [
        PL(1, "P", 185, 0.50, iv=0.30),
        PL(-1, "P", 195, 1.20, iv=0.30),
        PL(-1, "C", 205, 1.20, iv=0.30),
        PL(1, "C", 215, 0.50, iv=0.30),
    ]
    # Net credit = 1.20 + 1.20 - 0.50 - 0.50 = 1.40. BEs: 195-1.40=193.60, 205+1.40=206.40.
    # POP = P(193.60 < S_T < 206.40).
    p = pop(legs, spot=200, dte=30, r=0.0)
    # Hand-computed: σ√T = 0.30 × sqrt(30/365) ≈ 0.0860.
    # d2(193.60) ≈ 0.336, d2(206.40) ≈ -0.409.
    # POP = N(0.336) - N(-0.409) ≈ 0.6316 - 0.3412 = 0.2904.
    assert 0.27 < p < 0.32


def test_pop_clamped_to_unit_interval():
    # Tiny IV, far-OTM short put → POP ≈ 1.0.
    legs = [PL(-1, "P", 50, 1.00, iv=0.05)]
    p = pop(legs, spot=100, dte=30, r=0.0)
    assert p <= 1.0
    assert p > 0.99


def test_pop_zero_dte_is_deterministic():
    # 0 DTE and currently profitable → POP = 1.0.
    legs = [PL(1, "C", 90, 3.00, iv=0.30)]  # long 90 call at spot 100, already ITM, debit 3.
    p = pop(legs, spot=100, dte=0, r=0.0)
    # At S=100 right now, payoff = (10 - 3) * 100 = 700 > 0.
    assert p == 1.0


def test_pop_zero_dte_unprofitable_is_zero():
    legs = [PL(1, "C", 110, 3.00, iv=0.30)]  # OTM long call, paid 3.
    p = pop(legs, spot=100, dte=0, r=0.0)
    assert p == 0.0


# ---- EV ----------------------------------------------------------------


def test_ev_long_call_positive_when_cheap():
    # Long 100 call at spot 100, IV 30%, 30 DTE, paid only 1 (very cheap).
    # Fair BS price is several dollars → EV should be positive.
    legs = [PL(1, "C", 100, 1.00, iv=0.30)]
    e = ev(legs, spot=100, dte=30, r=0.0)
    assert e > 0


def test_ev_long_call_negative_when_overpaid():
    # Same option but paid $20. EV should be strongly negative.
    legs = [PL(1, "C", 100, 20.00, iv=0.30)]
    e = ev(legs, spot=100, dte=30, r=0.0)
    assert e < 0


def test_ev_iron_condor_near_zero_at_fair_premium():
    # Under log-normal, if we price legs at their BS fair value, EV ≈ 0.
    # This test uses premiums crafted to give near-zero EV.
    # (We don't test exact zero because our intervals and premiums were chosen
    # loosely — just sanity-check sign-appropriate magnitude.)
    legs = [
        PL(1, "P", 90, 0.30, iv=0.30),
        PL(-1, "P", 95, 0.80, iv=0.30),
        PL(-1, "C", 105, 0.80, iv=0.30),
        PL(1, "C", 110, 0.30, iv=0.30),
    ]
    e = ev(legs, spot=100, dte=30, r=0.0)
    # Cheap IC with net credit 1.00, BEs at 94 and 106. Under IV 30%, about
    # ±8.6% 1σ → wide margin. EV should be modestly positive or negative
    # but small relative to max profit $100.
    assert -150 < e < 150


# ---- prob_touch --------------------------------------------------------


def test_prob_touch_symmetric_greater_than_endpoint():
    # prob_touch should always be >= P(S_T crosses barrier) due to path-dependence.
    # For zero-drift reflection: P(touch K) = 2 * P(S_T > K | start at spot)
    # when K > spot.
    # Endpoint P(S_T > 110) and touch P should have touch > endpoint.
    p_touch = prob_touch(K=110, spot=100, iv=0.30, dte=30, r=0.0)
    # With 30% IV over 30 days, 1σ ≈ 8.6, so reaching 110 (1.16σ) has
    # endpoint probability ~0.12, touch probability ~0.24.
    assert 0.15 < p_touch < 0.40


def test_prob_touch_zero_when_barrier_equal_spot():
    # Barrier at current spot → probability of touch is 1.0 (trivially already there).
    p_touch = prob_touch(K=100, spot=100, iv=0.30, dte=30, r=0.0)
    assert p_touch == pytest.approx(1.0)


def test_prob_touch_very_far_barrier_near_zero():
    # 5σ barrier should give very small touch probability.
    # IV 20%, 30 DTE → σ√T ≈ 0.057. 5σ target → K/S = exp(5 × 0.057) ≈ 1.33.
    p_touch = prob_touch(K=200, spot=100, iv=0.20, dte=30, r=0.0)
    assert 0.0 <= p_touch < 0.02


# ---- combined greeks --------------------------------------------------


def test_combined_greeks_sums_per_leg():
    legs = [
        PL(1, "C", 100, 3.0, delta=0.50, gamma=0.02, theta=-0.05, vega=0.15),
        PL(-1, "C", 110, 1.0, delta=0.25, gamma=0.015, theta=-0.03, vega=0.10),
    ]
    g = combined_greeks(legs)
    # Net delta = 1*0.5 + (-1)*0.25 = 0.25 (shares equivalent per contract).
    assert g["delta"] == pytest.approx(0.25)
    assert g["gamma"] == pytest.approx(0.02 - 0.015)
    # Theta in $/day per contract → multiplied by 100.
    assert g["theta"] == pytest.approx((-0.05 + 0.03) * 100)
    # Vega in $/vol pt per contract → multiplied by 100.
    assert g["vega"] == pytest.approx((0.15 - 0.10) * 100)


def test_combined_greeks_skips_missing_fields():
    # Legs with None greeks contribute None to that particular sum.
    legs = [
        PL(1, "C", 100, 3.0, delta=0.50, gamma=None, theta=-0.05, vega=None),
    ]
    g = combined_greeks(legs)
    assert g["delta"] == pytest.approx(0.50)
    assert g["gamma"] is None
    assert g["theta"] == pytest.approx(-5.0)
    assert g["vega"] is None
