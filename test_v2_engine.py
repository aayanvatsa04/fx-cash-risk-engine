"""
test_v2_engine.py — Standalone V2 Engine Test Runner

Run this file directly in VSCode (or terminal) to verify the V2 engine output
before wiring it into the Flask app and HTML frontend:

    python test_v2_engine.py

Make sure test_v2_engine.py, exposure_engine.py, and var_engine.py are all in
the same folder before running.

=== FILE DEPENDENCIES ===

    test_v2_engine.py  →  exposure_engine.py  →  var_engine.py

This file only imports from exposure_engine.py. It has no knowledge of Flask
or HTML. It runs standalone exactly like the V1 test runner (fx_var_poc.py).

=== TEST SCENARIO (SGD-based company) ===

Cash positions:
  USD 2,000,000  — long USD, fear USD depreciates vs SGD
  MYR 5,000,000  — long MYR, fear MYR depreciates vs SGD
  AUD 1,000,000  — long AUD, fear AUD depreciates vs SGD

Future exposures:
  A: USD 2mn RECEIVABLE  15-Aug-2026  → Bucket 2 (1–3 months, T=42)
  B: USD 1mn PAYABLE     25-Aug-2026  → Bucket 2 (1–3 months, T=42)
     ↑ A and B are the NATURAL HEDGE pair — same currency, same bucket.
     Net USD Bucket 2: +2mn − 1mn = +1mn long.
     Gross at T=42: VaR(2mn) + VaR(1mn) summed independently.
     Net at T=42:   VaR(1mn) only.
     Hedge benefit: VaR(1mn) saved.

  C: MYR 8mn PAYABLE     20-Aug-2026  → Bucket 2 (1–3 months, T=42)
     No offsetting MYR → no hedge, net = gross for MYR in Bucket 2.

  D: USD 3mn PAYABLE     30-Oct-2026  → Bucket 3 (3–6 months, T=95)
     Different bucket from A/B → no netting with those.

  E: EUR 1mn RECEIVABLE  15-Jan-2027  → Bucket 4 (6–12 months, T=189)

=== WHAT TO LOOK FOR ===

1. Bucket 2 USD should show:
   - net_direction = 'long', net_notional = +1mn
   - hedge_benefit > 0 (the VaR(1mn) saved by natural offset)
   - Two positions listed in attribution (recv 2mn, pay 1mn)

2. Each bucket's VaR printed separately with its T clearly labelled.
   No combined total across buckets — each T is different.

3. Spot VaR has a combined total (all cash share same cash_horizon T).

4. Drift warning fires for MYR (structural trend vs SGD likely > 10%/yr).
"""

from exposure_engine import calculate_combined_var_v2


# =============================================================================
# FORMATTING HELPERS
# =============================================================================

def fmt(n: float) -> str:
    """Formats a number with comma separators and 2 decimal places."""
    return f"{n:,.2f}"


def sep(char: str = '─', width: int = 65) -> str:
    return char * width


def header(title: str) -> str:
    return f"\n{sep('═')}\n  {title}\n{sep('═')}"


def subheader(title: str) -> str:
    return f"\n  {title}\n  {sep('─', 50)}"


# =============================================================================
# PRINT FUNCTIONS
# =============================================================================

def print_spot_risk(spot_risk: dict, base_ccy: str) -> None:
    """
    Prints Layer 1a — spot risk from cash positions.
    All positions share the same cash_horizon T, so the total is meaningful.
    """
    print(subheader(f"LAYER 1a — SPOT RISK  "
                    f"(T = {spot_risk['days']} trading day(s), cash positions)"))

    if not spot_risk['positions']:
        print("    No cash positions.")
    else:
        for pos in spot_risk['positions']:
            drift_str = (f"  ⚠ drift {pos['annualised_mean']*100:.1f}%/yr"
                         if pos['drift_warning']
                         else f"  drift {pos['annualised_mean']*100:.2f}%/yr")
            cross_str = "  [cross-rate]" if pos['used_cross_rate'] else ""
            floor_str = (f"\n           ⚠ Raw VaR: {base_ccy} -{fmt(abs(pos['var_raw']))} "
                         "(floored — drift dominates)"
                         if pos['var_was_floored'] else "")

            print(f"    {pos['currency']:4}  "
                  f"balance {pos['currency']} {fmt(pos['balance']):>16}  "
                  f"spot {pos['spot_rate']:.4f}{cross_str}")
            print(f"          σ_annual {pos['annualised_vol']*100:.2f}%{drift_str}")
            print(f"          VaR: {base_ccy} {fmt(pos['var'])}{floor_str}")

    for err in spot_risk.get('errors', []):
        print(f"    ✗ {err['currency']}: {err['reason']}")

    print(f"\n    {'Spot VaR Total (T=' + str(spot_risk['days']) + ' day):':42} "
          f"{base_ccy} {fmt(spot_risk['total_var'])}")
    print(f"    ↑ This total is meaningful — all positions share the same T.")


def print_gross_reference(forward_gross: dict, base_ccy: str) -> None:
    """
    Prints the V2 gross per-exposure standalone VaRs at their actual T.
    Labelled as reference only — no combined total since Ts differ.
    """
    print(subheader("REFERENCE — GROSS PER-EXPOSURE VaR  "
                    "(V2, standalone, each at actual T)"))
    print("    Shown for reference only. No combined total — each exposure")
    print("    uses its own settlement T, so the sum would mix horizons.\n")

    if not forward_gross['exposures']:
        print("    No forward exposures.")
    else:
        for exp in forward_gross['exposures']:
            drift_str = (f"  ⚠ drift {exp['annualised_mean']*100:.1f}%/yr"
                         if exp['drift_warning'] else "")
            near_str  = "  ⚠ NEAR-TERM" if exp['near_term_warning'] else ""
            floor_str = (f"  [floored from {base_ccy} -{fmt(abs(exp['var_raw']))}]"
                         if exp['var_was_floored'] else "")
            print(f"    [{exp['bucket_label']}] {exp['direction'].upper():12} "
                  f"{exp['currency']:4} {fmt(exp['amount']):>16}  "
                  f"settle {exp['settlement_date']}")
            print(f"           actual T={exp['actual_trading_days']} days  "
                  f"σ {exp['annualised_vol']*100:.2f}%{drift_str}{near_str}")
            # exp['t_used'] is the bucket midpoint T actually used for this
            # VaR computation (since use_bucket_t=True in calculate_gross_forward_var).
            # The label previously said "at actual T" which was wrong — the actual
            # settlement T is exp['actual_trading_days'], but the VaR is computed
            # at exp['t_used'] (bucket midpoint) for consistency with the net VaR.
            print(f"           Standalone VaR (at bucket T={exp['t_used']} days): "
                  f"{base_ccy} {fmt(exp['var'])}{floor_str}")

    for err in forward_gross.get('errors', []):
        print(f"    ✗ {err['currency']} ({err.get('settlement_date','')}): "
              f"{err['reason']}")


def print_forward_net(forward_net: dict, base_ccy: str) -> None:
    """
    Prints Layer 1b & 2 — forward net bucketed VaR.
    Each bucket has its own T — no combined total across buckets.
    Natural hedge benefit shown per bucket/currency where applicable.
    """
    print(subheader("LAYER 1b & 2 — FORWARD RISK  "
                    "(V2.3, time-bucketed net, per-bucket T)"))
    print("    No combined total — each bucket uses a different time horizon T.")
    print("    bucket_var within each bucket is meaningful (all same T).\n")

    if not forward_net['buckets']:
        print("    No forward exposures with valid settlement dates.")
        return

    for bucket in forward_net['buckets']:
        T = bucket['midpoint_days']
        print(f"    ┌─ {bucket['bucket_label'].upper()}  "
              f"(T = {T} trading days)")

        for ccy in bucket['currencies']:
            net_n      = ccy['net_notional_foreign']
            net_dir    = ccy['net_direction'].upper()
            drift_str  = (f"  ⚠ drift {ccy['annualised_mean']*100:.1f}%/yr"
                          if ccy['drift_warning'] else "")
            cross_str  = "  [cross-rate]" if ccy['used_cross_rate'] else ""
            floor_str  = (f"  [floored from {base_ccy} -{fmt(abs(ccy['net_var_raw']))}]"
                          if ccy['var_was_floored'] else "")

            print(f"    │  {ccy['currency']:4}  net {net_dir:9} "
                  f"{fmt(abs(net_n)):>16}  "
                  f"spot {ccy['spot_rate']:.4f}{cross_str}")
            print(f"    │        σ_annual {ccy['annualised_vol']*100:.2f}%{drift_str}")
            print(f"    │        Net VaR (T={T}): "
                  f"{base_ccy} {fmt(ccy['net_var'])}{floor_str}")

            # Natural hedge benefit — only show if > 0 (i.e. netting occurred)
            if ccy['hedge_benefit'] > 0.01:
                pct = (ccy['hedge_benefit'] / ccy['gross_var_at_bucket_t'] * 100
                       if ccy['gross_var_at_bucket_t'] > 0 else 0)
                print(f"    │        ↳ Natural hedge saved: "
                      f"{base_ccy} {fmt(ccy['hedge_benefit'])}  "
                      f"({pct:.1f}% of gross {base_ccy} {fmt(ccy['gross_var_at_bucket_t'])})")

            # Attribution — only show when there are multiple positions in the group
            if len(ccy['positions']) > 1 or ccy['net_direction'] == 'flat':
                print(f"    │        Attribution (standalone at T={T}):")
                for pos in ccy['positions']:
                    arrow = "→" if pos['direction'] == 'receivable' else "←"
                    print(f"    │          {arrow} {pos['direction']:12} "
                          f"{fmt(pos['amount']):>16}  "
                          f"settle {pos['settlement_date']}  "
                          f"standalone {base_ccy} "
                          f"{fmt(pos['standalone_var_at_bucket_t'])}")

        print(f"    │")
        print(f"    │  Bucket {bucket['bucket_num']} VaR (T={T} days): "
              f"{base_ccy} {fmt(bucket['bucket_var'])}")
        print(f"    │  ↑ This sum is meaningful — all currencies above "
              f"share T={T}.")
        print(f"    └{'─'*62}")

    for err in forward_net.get('errors', []):
        print(f"    ✗ {err.get('currency','')} "
              f"({err.get('settlement_date','')}): "
              f"{err.get('reason','')}")

    print(f"\n    No combined forward total — bucket T values differ "
          f"and cannot be summed.")


def print_net_summary(net_summary: list[dict], base_ccy: str) -> None:
    """Prints Layer 3 — net currency summary (informational only)."""
    print(subheader("LAYER 3 — NET CURRENCY SUMMARY  "
                    "(informational, NOT used for VaR)"))
    print("    Combines cash holdings + receivables − payables per currency.\n")

    dir_label = {'long': 'NET LONG ', 'short': 'NET SHORT', 'flat': 'FLAT     '}
    for row in net_summary:
        print(f"    {row['currency']:4}  "
              f"{dir_label.get(row['net_direction'], ''):10}  "
              f"net {base_ccy} {fmt(row['net_base']):>14}")
        if row['cash_base'] > 0.01:
            print(f"           cash          +{base_ccy} {fmt(row['cash_base'])}")
        if row['receivables_base'] > 0.01:
            print(f"           receivables   +{base_ccy} {fmt(row['receivables_base'])}")
        if row['payables_base'] > 0.01:
            print(f"           payables      −{base_ccy} {fmt(row['payables_base'])}")
        print()


def print_summary_panel(result: dict) -> None:
    """
    Prints the final clean summary panel showing spot and each bucket
    side by side. No cross-bucket total — T values are different for each.
    """
    base_ccy = result['base_ccy']
    print(subheader("PORTFOLIO RISK SUMMARY"))
    print(f"    {'Spot VaR (T=' + str(result['cash_horizon']) + ' day, cash positions):':48} "
          f"{base_ccy} {fmt(result['spot_risk']['total_var'])}")
    print(f"    ↑ Single clear horizon. One total is appropriate.\n")

    print(f"    Forward Risk — Net Bucketed VaR:")
    total_hedge = 0.0
    for bucket in result['forward_net']['buckets']:
        print(f"      Bucket {bucket['bucket_num']} ({bucket['bucket_label']}, "
              f"T={bucket['midpoint_days']} days):"
              f"{'':>5}{base_ccy} {fmt(bucket['bucket_var'])}")
        # Show hedge benefits within the bucket if any
        for ccy in bucket['currencies']:
            if ccy['hedge_benefit'] > 0.01:
                pct = (ccy['hedge_benefit'] / ccy['gross_var_at_bucket_t'] * 100
                       if ccy['gross_var_at_bucket_t'] > 0 else 0)
                print(f"        ↳ {ccy['currency']} natural hedge saved: "
                      f"{base_ccy} {fmt(ccy['hedge_benefit'])}  "
                      f"({pct:.1f}%)")
                total_hedge += ccy['hedge_benefit']

    print(f"\n    {'─'*55}")
    print(f"    ⚠ Bucket VaRs above use DIFFERENT time horizons.")
    print(f"    They cannot be summed into a single portfolio total.")
    print(f"    Each figure is meaningful WITHIN its own time window.")
    if total_hedge > 0.01:
        print(f"\n    Total natural hedging benefit across all buckets: "
              f"{base_ccy} {fmt(total_hedge)}")
        print(f"    (This benefit sum is approximate — it aggregates savings")
        print(f"     from different time horizons, shown for context only.)")


# =============================================================================
# MAIN TEST RUN
# =============================================================================

if __name__ == '__main__':

    # -------------------------------------------------------------------------
    # INPUTS
    # -------------------------------------------------------------------------
    BASE_CCY     = 'SGD'
    CONFIDENCE   = 0.95    # 95% → Z ≈ 1.645
    PERIOD       = '1y'    # 1-year historical lookback
    CASH_HORIZON = 1       # 1-day VaR for spot/cash positions

    CASH_POSITIONS = [
        {'currency': 'USD', 'balance': 2_000_000},
        {'currency': 'MYR', 'balance': 5_000_000},
        {'currency': 'AUD', 'balance': 1_000_000},
    ]

    EXPOSURES = [
        # A: recv USD 2mn — long USD, settle Aug-15 → Bucket 2 (T=42)
        {
            'currency':        'USD',
            'amount':          2_000_000,
            'settlement_date': '2026-08-15',
            'direction':       'receivable',
        },
        # B: pay USD 1mn — short USD, settle Aug-25 → Bucket 2 (T=42)
        # NATURAL HEDGE with A: same currency (USD), same bucket (Bucket 2)
        # Net USD Bucket 2 = +2mn − 1mn = net long +1mn
        {
            'currency':        'USD',
            'amount':          1_000_000,
            'settlement_date': '2026-08-25',
            'direction':       'payable',
        },
        # C: pay MYR 8mn — short MYR, settle Aug-20 → Bucket 2 (T=42)
        # No offsetting MYR in Bucket 2 → no natural hedge, net = gross
        {
            'currency':        'MYR',
            'amount':          8_000_000,
            'settlement_date': '2026-08-20',
            'direction':       'payable',
        },
        # D: pay USD 3mn — short USD, settle Oct-30 → Bucket 3 (T=95)
        # Different bucket from A and B → no netting with those USD positions
        {
            'currency':        'USD',
            'amount':          3_000_000,
            'settlement_date': '2026-10-30',
            'direction':       'payable',
        },
        # E: recv EUR 1mn — long EUR, settle Jan-15-2027 → Bucket 4 (T=189)
        {
            'currency':        'EUR',
            'amount':          1_000_000,
            'settlement_date': '2027-01-15',
            'direction':       'receivable',
        },
    ]

    # -------------------------------------------------------------------------
    # RUN ENGINE
    # -------------------------------------------------------------------------
    print(header("V2 FX VAR ENGINE — TEST OUTPUT"))
    print(f"\n  Base Currency: {BASE_CCY}  |  Confidence: {CONFIDENCE:.0%}  "
          f"|  Lookback: {PERIOD}  |  Cash Horizon: {CASH_HORIZON}d")
    print(f"  Inputs: {len(CASH_POSITIONS)} cash positions, "
          f"{len(EXPOSURES)} future exposures")
    print(f"\n  Running engine (fetching market data…)\n")

    result = calculate_combined_var_v2(
        cash_positions = CASH_POSITIONS,
        exposures      = EXPOSURES,
        base_ccy       = BASE_CCY,
        confidence     = CONFIDENCE,
        period         = PERIOD,
        cash_horizon   = CASH_HORIZON,
    )

    # -------------------------------------------------------------------------
    # LAYER 1a: Spot risk
    # -------------------------------------------------------------------------
    print(header("LAYER 1a — SPOT RISK"))
    print_spot_risk(result['spot_risk'], BASE_CCY)

    # -------------------------------------------------------------------------
    # Gross reference (V2 standalone, for attribution context)
    # -------------------------------------------------------------------------
    print(header("REFERENCE — GROSS PER-EXPOSURE VaR (V2)"))
    print_gross_reference(result['forward_gross'], BASE_CCY)

    # -------------------------------------------------------------------------
    # LAYER 1b & 2: Bucketed net forward VaR
    # -------------------------------------------------------------------------
    print(header("LAYER 1b & 2 — FORWARD NET RISK (V2.3)"))
    print_forward_net(result['forward_net'], BASE_CCY)

    # -------------------------------------------------------------------------
    # LAYER 3: Net currency summary
    # -------------------------------------------------------------------------
    print(header("LAYER 3 — NET CURRENCY SUMMARY"))
    print_net_summary(result['net_currency_summary'], BASE_CCY)

    # -------------------------------------------------------------------------
    # Final summary panel
    # -------------------------------------------------------------------------
    print(header("PORTFOLIO RISK SUMMARY"))
    print_summary_panel(result)

    print(f"\n{sep('═')}")
    print("  Test complete.")
    print("  Verify: Bucket 2 USD hedge_benefit > 0 (A recv 2mn offset by B pay 1mn).")
    print("  Verify: No combined cross-bucket total anywhere in the output.")
    print(sep('═'))
