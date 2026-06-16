"""
engine_runner.py — Standalone Engine Runner (V3)

Run this file directly in the terminal to verify the engine output before
pushing to Render — no Flask server needed:

    python3 engine_runner.py

=== THREE-SECTION OUTPUT ===

SECTION 1 — Spot Book Risk (standalone, T = cash_horizon)
  VaR on current cash holdings at the user-specified horizon.
  Covariance-adjusted total + diversification benefit.

SECTION 2 — Unified Bucketed Risk (cash + forwards)
  Cash positions routed into Bucket 1 as synthetic receivables.
  Forward exposures in their natural buckets.
  Natural hedging (within-currency netting) + covariance (cross-currency)
  both applied per bucket.

SECTION 3 — Gross Attribution (reference, forwards only, no netting)
  What each forward exposure's VaR would be without any netting.

=== TEST SCENARIO (SGD-based company) ===

Cash:
  USD 2,000,000  MYR 5,000,000  AUD 1,000,000

Forwards:
  A: recv USD 2mn  15-Aug-2026  → Bucket 2
  B: pay  USD 1mn  25-Aug-2026  → Bucket 2  [natural hedge with A]
  C: pay  MYR 8mn  20-Aug-2026  → Bucket 2
  D: pay  USD 3mn  30-Oct-2026  → Bucket 3
  E: recv EUR 1mn  15-Jan-2027  → Bucket 4
"""

from exposure_engine import calculate_fx_var


# =============================================================================
# FORMATTING HELPERS
# =============================================================================

def fmt(n: float) -> str:
    return f"{n:,.2f}"

def sep(char: str = '─', width: int = 65) -> str:
    return char * width

def header(title: str) -> str:
    return f"\n{sep('═')}\n  {title}\n{sep('═')}"

def subheader(title: str) -> str:
    return f"\n  {title}\n  {sep('─', 50)}"


# =============================================================================
# SECTION 1 — SPOT BOOK RISK
# =============================================================================

def print_spot_risk(spot_risk: dict, base_ccy: str) -> None:
    T = spot_risk['days']
    print(subheader(f"SECTION 1 — SPOT BOOK RISK  (standalone, T={T} trading day(s))"))
    print("    Current cash holdings only. No forwards involved.\n")

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
            print(f"    {pos['currency']:4}  balance {pos['currency']} {fmt(pos['balance']):>16}  "
                  f"spot {pos['spot_rate']:.4f}{cross_str}")
            print(f"          σ_annual {pos['annualised_vol']*100:.2f}%{drift_str}")
            print(f"          VaR (T={T}): {base_ccy} {fmt(pos['var'])}{floor_str}")

    for err in spot_risk.get('errors', []):
        print(f"    ✗ {err['currency']}: {err['reason']}")

    print(f"\n    {'Simple sum VaR (T=' + str(T) + ' day):':46} "
          f"{base_ccy} {fmt(spot_risk['total_var'])}")

    cov = spot_risk.get('total_var_cov', spot_risk['total_var'])
    ben = spot_risk.get('diversification_benefit', 0.0)
    if ben > 0.01:
        pct = ben / spot_risk['total_var'] * 100 if spot_risk['total_var'] > 0 else 0
        print(f"    {'Covariance-adjusted VaR:':46} "
              f"{base_ccy} {fmt(cov)}")
        print(f"    {'Diversification benefit:':46} "
              f"{base_ccy} {fmt(ben)}  ({pct:.1f}%)")
    else:
        print(f"    (Single position — covariance = simple sum)")


# =============================================================================
# SECTION 2 — UNIFIED BUCKETED RISK
# =============================================================================

def print_unified_buckets(unified_buckets: dict, base_ccy: str) -> None:
    print(subheader("SECTION 2 — UNIFIED BUCKETED RISK  "
                    "(cash in Bucket 1 + forwards, covariance-adjusted)"))
    print("    Cash holdings routed into Bucket 1 as synthetic receivables.")
    print("    Natural hedge benefit shown where within-currency netting occurred.")
    print("    Diversification benefit shown where 2+ currencies in a bucket.")
    print("    No combined total — each bucket uses a different time horizon T.\n")

    if not unified_buckets['buckets']:
        print("    No positions.")
        return

    for bucket in unified_buckets['buckets']:
        T = bucket['midpoint_days']
        print(f"    ┌─ BUCKET {bucket['bucket_num']}: {bucket['bucket_label'].upper()}"
              f"  (T = {T} trading days)")

        for ccy in bucket['currencies']:
            net_n     = ccy['net_notional_foreign']
            net_dir   = ccy['net_direction'].upper()
            drift_str = (f"  ⚠ drift {ccy['annualised_mean']*100:.1f}%/yr"
                         if ccy['drift_warning'] else "")
            cross_str = "  [cross-rate]" if ccy['used_cross_rate'] else ""
            floor_str = (f"  [floored from {base_ccy} -{fmt(abs(ccy['net_var_raw']))}]"
                         if ccy['var_was_floored'] else "")

            print(f"    │  {ccy['currency']:4}  net {net_dir:9} "
                  f"{fmt(abs(net_n)):>16}  spot {ccy['spot_rate']:.4f}{cross_str}")
            print(f"    │        σ_annual {ccy['annualised_vol']*100:.2f}%{drift_str}")
            print(f"    │        Net VaR (T={T}): {base_ccy} {fmt(ccy['net_var'])}{floor_str}")

            if ccy['hedge_benefit'] > 0.01:
                pct = (ccy['hedge_benefit'] / ccy['gross_var_at_bucket_t'] * 100
                       if ccy['gross_var_at_bucket_t'] > 0 else 0)
                print(f"    │        ↳ Natural hedge saved: {base_ccy} {fmt(ccy['hedge_benefit'])}"
                      f"  ({pct:.1f}% of gross {base_ccy} {fmt(ccy['gross_var_at_bucket_t'])})")

            # Attribution — show when multiple positions or flat
            if len(ccy['positions']) > 1 or ccy['net_direction'] == 'flat':
                print(f"    │        Attribution (standalone at T={T}):")
                for pos in ccy['positions']:
                    arrow  = "→" if pos['direction'] == 'receivable' else "←"
                    src    = "[cash]" if pos.get('source') == 'cash' else f"settle {pos['settlement_date']}"
                    print(f"    │          {arrow} {pos['direction']:12} "
                          f"{fmt(pos['amount']):>16}  {src}"
                          f"  standalone {base_ccy} {fmt(pos['standalone_var_at_bucket_t'])}")

        print(f"    │")
        bv_simple = bucket.get('bucket_var_simple', bucket['bucket_var'])
        bv_cov    = bucket['bucket_var']
        ben       = bucket.get('diversification_benefit', 0.0)
        if ben > 0.01:
            pct = ben / bv_simple * 100 if bv_simple > 0 else 0
            print(f"    │  Bucket {bucket['bucket_num']} VaR (covariance-adjusted, T={T}): "
                  f"{base_ccy} {fmt(bv_cov)}")
            print(f"    │  ↳ Simple sum: {base_ccy} {fmt(bv_simple)}  "
                  f"Correlation saved: {base_ccy} {fmt(ben)}  ({pct:.1f}%)")
        else:
            print(f"    │  Bucket {bucket['bucket_num']} VaR (T={T} days): "
                  f"{base_ccy} {fmt(bv_cov)}  (single currency)")
        print(f"    └{'─'*62}")

    for err in unified_buckets.get('errors', []):
        print(f"    ✗ {err.get('currency','')} "
              f"({err.get('settlement_date','')}): {err.get('reason','')}")

    print(f"\n    ⚠ Bucket VaRs use different time horizons — cannot be summed.")


# =============================================================================
# SECTION 3 — GROSS ATTRIBUTION
# =============================================================================

def print_gross_attribution(gross_attribution: dict, base_ccy: str) -> None:
    print(subheader("SECTION 3 — GROSS ATTRIBUTION  "
                    "(reference, forwards only, no netting)"))
    print("    Standalone VaR per forward exposure at its bucket T.")
    print("    Cash positions not included. No netting applied.\n")

    if not gross_attribution['exposures']:
        print("    No forward exposures.")
        return

    current_bucket = None
    for exp in gross_attribution['exposures']:
        if exp['bucket_num'] != current_bucket:
            current_bucket = exp['bucket_num']
            print(f"    [Bucket {exp['bucket_num']}: {exp['bucket_label']}]")

        arrow = "→" if exp['direction'] == 'receivable' else "←"
        drift_str = (f"  ⚠ drift {exp['annualised_mean']*100:.1f}%/yr"
                     if exp['drift_warning'] else "")
        print(f"      {arrow} {exp['direction']:12} {exp['currency']:4} "
              f"{fmt(exp['amount']):>16}  settle {exp['settlement_date']}")
        print(f"         σ {exp['annualised_vol']*100:.2f}%{drift_str}  "
              f"standalone VaR (T={exp['t_used']}): {base_ccy} {fmt(exp['var'])}")

    for err in gross_attribution.get('errors', []):
        print(f"    ✗ {err.get('currency','')} "
              f"({err.get('settlement_date','')}): {err.get('reason','')}")


# =============================================================================
# SUMMARY PANEL
# =============================================================================

def print_summary_panel(result: dict) -> None:
    base_ccy = result['base_ccy']
    print(subheader("PORTFOLIO RISK SUMMARY"))

    # Section 1
    spot = result['spot_risk']
    cov  = spot.get('total_var_cov', spot['total_var'])
    ben  = spot.get('diversification_benefit', 0.0)
    print(f"    Section 1 — Spot Book (T={result['cash_horizon']}d, standalone):")
    print(f"      Simple sum:            {base_ccy} {fmt(spot['total_var'])}")
    if ben > 0.01:
        print(f"      Covariance-adjusted:   {base_ccy} {fmt(cov)}")
        print(f"      Diversification saved: {base_ccy} {fmt(ben)}")

    # Section 2
    print(f"\n    Section 2 — Unified Bucketed Risk (cash + forwards):")
    total_hedge = 0.0
    for bucket in result['unified_buckets']['buckets']:
        bv_cov  = bucket['bucket_var']
        bv_simp = bucket.get('bucket_var_simple', bv_cov)
        ben_b   = bucket.get('diversification_benefit', 0.0)
        print(f"      Bucket {bucket['bucket_num']} ({bucket['bucket_label']}, "
              f"T={bucket['midpoint_days']}d): {base_ccy} {fmt(bv_cov)}", end="")
        if ben_b > 0.01:
            print(f"  [corr saved {fmt(ben_b)}]")
        else:
            print()
        for ccy in bucket['currencies']:
            if ccy['hedge_benefit'] > 0.01:
                pct = (ccy['hedge_benefit'] / ccy['gross_var_at_bucket_t'] * 100
                       if ccy['gross_var_at_bucket_t'] > 0 else 0)
                print(f"        ↳ {ccy['currency']} natural hedge saved: "
                      f"{base_ccy} {fmt(ccy['hedge_benefit'])}  ({pct:.1f}%)")
                total_hedge += ccy['hedge_benefit']

    print(f"\n    {'─'*55}")
    print(f"    ⚠ Bucket VaRs above use different T — cannot be summed.")
    if total_hedge > 0.01:
        print(f"    Total natural hedging benefit (all buckets): "
              f"{base_ccy} {fmt(total_hedge)}")

    # Consolidated VaR (V2.4)
    cv = result.get('consolidated_var', {})
    if cv.get('total_var', 0) > 0:
        print(f"\n    {'─'*55}")
        print(f"    Consolidated Portfolio VaR (V2.4 — single number):")
        print(f"      Method: exact individual-position covariance, min(T) cross-terms")
        print(f"      Positions in matrix: {cv['n_positions']}")
        print(f"      VaR: {base_ccy} {fmt(cv['total_var'])}", end="")
        if cv.get('var_was_floored'):
            print(f"  [floored from {base_ccy} -{fmt(abs(cv['total_var_raw']))}]")
        else:
            print()
        print(f"\n      Individual positions (sorted by size):")
        for p in cv.get('position_breakdown', []):
            sign_str  = '+' if p['signed_exposure_base'] > 0 else '-'
            date_str  = f"  settle {p['settlement_date']}" if p['settlement_date'] else f"  [cash, T={p['t_days']}d]"
            print(f"        {p['currency']:4}  {p['type']:7}  {p['direction']:12}  "
                  f"{sign_str}{base_ccy} {fmt(abs(p['signed_exposure_base'])):>14}"
                  f"  T={p['t_days']:3}d{date_str}")

    print(f"\n    {'─'*55}")
    print(f"    Note: consolidated VaR < sum of bucket VaRs because:")
    print(f"      1. Same-currency positions net across ALL buckets simultaneously")
    print(f"      2. Cross-currency diversification applied to full portfolio")


# =============================================================================
# MAIN TEST RUN
# =============================================================================

if __name__ == '__main__':

    BASE_CCY     = 'SGD'
    CONFIDENCE   = 0.95
    PERIOD       = '1y'
    CASH_HORIZON = 1

    CASH_POSITIONS = [
        {'currency': 'USD', 'balance': 2_000_000},
        {'currency': 'MYR', 'balance': 5_000_000},
        {'currency': 'AUD', 'balance': 1_000_000},
    ]

    EXPOSURES = [
        {'currency': 'USD', 'amount': 2_000_000,
         'settlement_date': '2026-08-15', 'direction': 'receivable'},
        {'currency': 'USD', 'amount': 1_000_000,
         'settlement_date': '2026-08-25', 'direction': 'payable'},
        {'currency': 'MYR', 'amount': 8_000_000,
         'settlement_date': '2026-08-20', 'direction': 'payable'},
        {'currency': 'USD', 'amount': 3_000_000,
         'settlement_date': '2026-10-30', 'direction': 'payable'},
        {'currency': 'EUR', 'amount': 1_000_000,
         'settlement_date': '2027-01-15', 'direction': 'receivable'},
    ]

    print(header("V3 FX VaR ENGINE — RUNNER OUTPUT"))
    print(f"\n  Base: {BASE_CCY}  |  Confidence: {CONFIDENCE:.0%}  "
          f"|  Lookback: {PERIOD}  |  Cash Horizon: {CASH_HORIZON}d")
    print(f"  {len(CASH_POSITIONS)} cash positions, {len(EXPOSURES)} forward exposures\n")

    result = calculate_fx_var(
        cash_positions = CASH_POSITIONS,
        exposures      = EXPOSURES,
        base_ccy       = BASE_CCY,
        confidence     = CONFIDENCE,
        period         = PERIOD,
        cash_horizon   = CASH_HORIZON,
    )

    print(header("SECTION 1 — SPOT BOOK RISK"))
    print_spot_risk(result['spot_risk'], BASE_CCY)

    print(header("SECTION 2 — UNIFIED BUCKETED RISK"))
    print_unified_buckets(result['unified_buckets'], BASE_CCY)

    print(header("SECTION 3 — GROSS ATTRIBUTION"))
    print_gross_attribution(result['gross_attribution'], BASE_CCY)

    print(header("SUMMARY"))
    print_summary_panel(result)

    print(f"\n{sep('═')}")
    print("  Test complete.")
    print("  Verify: Bucket 1 shows cash positions as synthetic receivables.")
    print("  Verify: Bucket 2 USD hedge_benefit > 0 (recv 2mn offset by pay 1mn).")
    print("  Verify: Bucket 2 diversification_benefit > 0 (USD + MYR, 2 currencies).")
    print("  Verify: consolidated_var < sum of all bucket VaRs.")
    print("  Verify: consolidated_var position_breakdown shows all individual positions.")
    print(sep('═'))
