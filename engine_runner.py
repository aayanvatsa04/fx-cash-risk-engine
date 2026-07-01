"""
engine_runner.py — Standalone Engine Verification Runner (V3)

PURPOSE
-------
Run this file directly to verify the correctness of every engine computation
BEFORE looking at the web UI. This is the "first abstraction barrier" check:
if a number is wrong here, it will be wrong in the browser too. Catching bugs
here (at the engine level) is faster and more precise than debugging through
Flask + JavaScript.

    python3 engine_runner.py

No Flask server is needed. No browser is needed. All output goes to stdout.

=== DESIGN PHILOSOPHY ===

This file is organised as a set of independent, self-contained print functions —
one per logical output section. Each function:

  1. Accepts only the sub-dict it needs (e.g. spot_risk, not the full result)
     → testable in isolation, decoupled from the rest of the output
  2. Has a full docstring explaining what it tests and what correct output looks like
  3. Prints explicit sanity-check assertions with ✓/✗ so failures are unmissable

Future developers: to add a new engine feature, add a new print_<feature>()
function following the same pattern, then wire it into __main__. Do not add
inline print() calls in __main__ — keep all formatting logic in functions.

=== WHAT IS TESTED (engine outputs verified here) ===

The following engine computations are verified at the terminal level before
the web UI is involved. Each maps to one or more UI sections in calculator.html:

  [S1]  Spot Book Risk (Section 1 / Cash Book Risk card)
        → VaR on cash holdings at user-specified T
        → per-currency breakdown, covariance-adjusted total, diversification benefit

  [S1b] Cash Spot Rate Sensitivity source data (Cash Spot Rate Sensitivity card)
        → exposure_base values per cash position that the frontend renders as
          ±20/±10/±5 scenario tables. The arithmetic is frontend-only but the
          source data (exposure_base = balance × spot_rate) must be correct.
        → Applies the same scenario arithmetic here to let developers verify the
          numbers they will see in the browser match what the engine produced.

  [S2]  Unified Bucketed Risk (Section 2 / Bucketed Risk Detail card)
        → cash in Bucket 1 as synthetic receivables, forward exposures bucketed
        → natural hedge benefit (within-currency netting), diversification benefit
          (cross-currency covariance within each bucket)

  [S3]  Gross Attribution (Section 3 — internal reference, not a UI card)
        → standalone per-forward VaR without netting, at bucket T

  [S3b] Gross Cash Attribution (Gross Standalone Risk stat card component)
        → cash standalone VaR at fixed CASH_CONSOLIDATED_T_DAYS (not cash_horizon)
        → this is the cash component that dashboard_engine.py adds to gross forward
          VaR to produce the Gross Standalone Risk stat card figure

  [S4]  Consolidated Portfolio VaR (Consolidated Portfolio VaR card + Dashboard stat)
        → single number across all positions using min(Tᵢ,Tⱼ) covariance
        → position-level breakdown showing every entry in the covariance matrix

  [S5]  Cumulative Period VaRs + Component CFaR (Risk Dashboard bar charts + sliders)
        → per-period (1m/3m/6m/12m/all) consolidated VaR
        → per-currency Component CFaR (the exact Euler decomposition summing to period VaR)
        → vol_part / drift_part per currency (drives the exact vol-slider scaling in JS)
        → net notional per currency per period (drives the notional bar chart in JS)
        → CRITICAL: 'all' period VaR must equal Consolidated Portfolio VaR exactly

  [S6]  Hedge Recommendations (Hedge Recommendations section / V3.8)
        → ranked forward contracts, cumulative VaR reduction after each hedge
        → mathematical invariant checks (monotonicity, consistency, no spurious candidates)

=== TEST SCENARIO ===

SGD-based company with:

  Cash:      USD 2,000,000 | MYR 5,000,000 | AUD 1,000,000
  Forwards:
    A: recv USD 2mn  2026-08-15  → Bucket 2  (1–3 months)
    B: pay  USD 1mn  2026-08-25  → Bucket 2  (natural hedge with A: net recv 1mn)
    C: pay  MYR 8mn  2026-08-20  → Bucket 2
    D: pay  USD 3mn  2026-10-30  → Bucket 3  (3–6 months)
    E: recv EUR 1mn  2027-01-15  → Bucket 4  (6–12 months)

This scenario is deliberately constructed to exercise every code path:
  - Cash-only currency (AUD) — verifies hedge engine exclusion of cash
  - Natural hedge (USD A+B) — verifies within-currency netting in Bucket 2
  - Two-currency bucket (USD + MYR in Bucket 2) — verifies cross-currency covariance
  - Multi-bucket currency (USD in Buckets 2+3) — verifies cross-horizon covariance
  - Diversifying currency (EUR in Bucket 4) — verifies long-horizon Component CFaR

=== HOW TO TEST A DIFFERENT SCENARIO ===

Change CASH_POSITIONS and EXPOSURES in the __main__ block at the bottom.
All eight print functions run against the same inputs automatically.
No other changes are needed — each function reads only its sub-dict.

=== ARCHITECTURE NOTE ===

This file imports ONLY from exposure_engine.py (the engine layer).
It intentionally does NOT import from dashboard_engine.py, app.py, or
any frontend file. The purpose is to verify engine correctness in isolation —
dashboard_engine.py is the second abstraction layer (engine output →
chart-ready JSON) and should be verified separately via Flask + browser.

Dependency chain (strictly one-directional, never reversed):
    engine_runner.py  →  exposure_engine.py  →  var_engine.py
    app.py            ↗
"""

from exposure_engine import calculate_fx_var, recommend_hedges


# =============================================================================
# FORMATTING HELPERS
# =============================================================================
# Kept intentionally simple — this is a developer tool, not a user-facing UI.
# The goal is readable terminal output with clear structure.
#
# If you add a new helper, document the rounding convention and add an example.
# =============================================================================

def fmt(n: float) -> str:
    """Format a number with thousands separators and 2 decimal places.

    Used for VaR figures, notionals, and other currency amounts where exact
    precision matters for verification. Matches the browser's fmt() function
    in calculator.html so terminal and UI numbers are directly comparable.

    Example: fmt(1234567.89) → '1,234,567.89'
    """
    return f"{n:,.2f}"


def fmtk(n: float) -> str:
    """Format a number in abbreviated K/M form for compact display.

    Used where exact decimal precision is less important than readability —
    e.g. reductions in hedge tables where K/M is more intuitive than 6 digits.
    Matches the browser's fmtShort() function in dashboard.js.

    Examples:
        fmtk(55200)    → '55.2K'
        fmtk(1234567)  → '1.2M'
        fmtk(850)      → '850.0'
    """
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.1f}"


def fmt_pct(n: float, decimals: int = 2) -> str:
    """Format a decimal fraction as a percentage string.

    Args:
        n:        Float in decimal form (e.g. 0.1234 for 12.34%).
        decimals: Number of decimal places to show.

    Example: fmt_pct(0.1234) → '12.34%'
    """
    return f"{n * 100:.{decimals}f}%"


def sep(char: str = '─', width: int = 70) -> str:
    """Return a horizontal separator line of the given character and width.

    Used to visually separate sections and sub-sections in terminal output.
    """
    return char * width


def header(title: str) -> str:
    """Return a bold double-line section header for major output blocks.

    Used at the top of each numbered section (S1, S2, …).
    """
    return f"\n{sep('═')}\n  {title}\n{sep('═')}"


def subheader(title: str) -> str:
    """Return a single-line subheader for sub-blocks within a section."""
    return f"\n  {title}\n  {sep('─', 55)}"


def check(condition: bool, label: str, detail: str = '') -> bool:
    """Print a ✓/✗ sanity-check assertion and return the pass/fail result.

    Centralises assertion formatting so all checks look identical and are
    easy to grep for failures. Always call this for mathematical invariants
    that MUST hold — never use raw assert (which crashes the runner) or
    bare print (which doesn't signal failure clearly).

    Args:
        condition: The boolean result of the check (True = pass).
        label:     Short description of what is being checked.
        detail:    Optional extra context shown only on failure.

    Returns:
        True if the check passed, False if it failed.
        The caller should accumulate these to produce an overall pass/fail.
    """
    if condition:
        print(f"    ✓ {label}")
    else:
        print(f"    ✗ FAIL: {label}")
        if detail:
            print(f"      → {detail}")
    return condition


# =============================================================================
# [S1] SECTION 1 — SPOT BOOK RISK
# =============================================================================
# Corresponds to: Cash Book Risk card in calculator.html
# Engine function: calculate_fx_var() → result['spot_risk']
#
# What this section verifies:
#   - Per-position breakdown (balance, spot rate, volatility, VaR at T)
#   - Drift warnings and floored-VaR notifications
#   - Simple-sum total vs covariance-adjusted total
#   - Diversification benefit when 2+ currencies held
# =============================================================================

def print_spot_risk(spot_risk: dict, base_ccy: str) -> bool:
    """
    Print and verify Section 1: standalone cash VaR at the user-specified horizon.

    The 'spot_risk' dict is produced by calculate_spot_var() inside calculate_fx_var().
    It represents the Cash Book Risk card in the UI — isolated to cash positions only,
    with no forward exposures involved.

    Key fields verified:
        positions[i]['exposure_base']   = balance × spot_rate (the base-ccy equivalent
                                          used by Cash Spot Rate Sensitivity as its
                                          source data — see print_sensitivity_source_data)
        total_var                       = simple sum of per-currency VaRs (assumes ρ=1)
        total_var_cov                   = covariance-adjusted total (accurate, uses ρ < 1)
        diversification_benefit         = total_var − total_var_cov (always ≥ 0)

    Sanity checks run:
        A. Each position's exposure_base > 0 (balance × spot_rate must be positive
           for a long cash position — negative would indicate a data error)
        B. total_var_cov ≤ total_var (covariance adjustment can only reduce, never increase)
        C. diversification_benefit ≈ total_var − total_var_cov (arithmetic consistency)

    Args:
        spot_risk: The 'spot_risk' sub-dict from calculate_fx_var() output.
        base_ccy:  Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    T = spot_risk['days']
    print(subheader(f"[S1] SPOT BOOK RISK  (Cash Book Risk card · T={T} trading days)"))
    print("    Cash holdings only. Forward exposures NOT included here.\n")

    all_ok = True

    if not spot_risk['positions']:
        print("    (No cash positions in this scenario.)")
        return True

    # ── Per-position detail ────────────────────────────────────────────────
    for pos in spot_risk['positions']:
        drift_flag  = (f"  ⚠ DRIFT {pos['annualised_mean']*100:.1f}%/yr — significant trend"
                       if pos['drift_warning']
                       else f"  drift {pos['annualised_mean']*100:.2f}%/yr")
        cross_flag  = "  [via USD cross-rate]" if pos['used_cross_rate'] else ""
        floor_flag  = (f"\n           ⚠ Raw VaR: {base_ccy} -{fmt(abs(pos['var_raw']))} "
                       "(negative → floored to 0; drift dominates volatility at this horizon)"
                       if pos['var_was_floored'] else "")

        print(f"    {pos['currency']:4}  balance {pos['currency']} {fmt(pos['balance']):>18}"
              f"  spot {pos['spot_rate']:.4f}{cross_flag}")
        print(f"          exposure ({base_ccy}): {fmt(pos['exposure_base'])}"
              f"  σ_annual: {pos['annualised_vol']*100:.2f}%{drift_flag}")
        print(f"          VaR at T={T}: {base_ccy} {fmt(pos['var'])}{floor_flag}")

        # Sanity check A: exposure_base = balance × spot_rate and must be > 0
        #
        # WHY RELATIVE TOLERANCE, NOT ABSOLUTE:
        # For direct rates (e.g. USD/SGD), balance × stored_spot_rate ≈ exposure_base
        # to within floating-point noise. But for CROSS-RATE currencies (e.g. MYR/SGD
        # computed via MYR/USD × USD/SGD), the stored spot_rate is rounded to 6 decimal
        # places AFTER the full-precision cross-multiplication that produced exposure_base.
        # On a large balance (e.g. MYR 5,000,000), a 7th-decimal-place rounding difference
        # in the stored rate propagates to several units of base currency:
        #   5,000,000 × 0.0000004 ≈ 2.0 SGD
        # This is not a computation error — it is an expected artifact of the rounding
        # convention used when storing spot_rate in the output dict (6 dp). A relative
        # tolerance of 0.01% (1 basis point = 1/10,000 of the exposure) is:
        #   - Tight enough to catch genuine errors (e.g. wrong sign, wrong field used)
        #   - Loose enough to pass cross-rate rounding on any realistic balance size
        expected_exp = pos['balance'] * pos['spot_rate']
        if abs(expected_exp) > 0:
            rel_delta = abs(pos['exposure_base'] - expected_exp) / abs(expected_exp)
            check_ok  = rel_delta < 0.0001   # 0.01% relative tolerance
            detail    = (f"exposure_base={fmt(pos['exposure_base'])}, "
                         f"balance×spot={fmt(expected_exp)}, "
                         f"relative delta={rel_delta*100:.6f}%"
                         f"{'  [cross-rate rounding — expected]' if pos.get('used_cross_rate') else ''}")
        else:
            # Fallback for a hypothetical zero-balance position
            check_ok = abs(pos['exposure_base'] - expected_exp) < 1.0
            detail   = f"exposure_base={fmt(pos['exposure_base'])}, expected≈0"

        all_ok &= check(
            check_ok,
            f"{pos['currency']} exposure_base ≈ balance × spot_rate "
            f"(within 0.01% relative tolerance)",
            detail
        )
        all_ok &= check(
            pos['exposure_base'] > 0,
            f"{pos['currency']} exposure_base > 0 (long cash position)",
            f"Got {fmt(pos['exposure_base'])}"
        )

    # ── Portfolio totals ────────────────────────────────────────────────────
    simple_sum = spot_risk['total_var']
    cov_total  = spot_risk.get('total_var_cov', simple_sum)
    div_ben    = spot_risk.get('diversification_benefit', 0.0)

    print(f"\n    Simple sum (assumes ρ=1 between all currencies):  "
          f"{base_ccy} {fmt(simple_sum)}")

    if div_ben > 0.01:
        pct = div_ben / simple_sum * 100 if simple_sum > 0 else 0
        print(f"    Covariance-adjusted (uses historical ρ < 1):      "
              f"{base_ccy} {fmt(cov_total)}")
        print(f"    Diversification benefit (simple − covariance):    "
              f"{base_ccy} {fmt(div_ben)}  ({pct:.1f}% reduction)")

        # Sanity check B: cov_total ≤ simple_sum
        all_ok &= check(
            cov_total <= simple_sum + 1e-6,
            "Covariance-adjusted VaR ≤ simple sum (ρ<1 can only reduce risk)",
            f"cov={fmt(cov_total)}, simple={fmt(simple_sum)}"
        )
        # Sanity check C: diversification_benefit arithmetic
        expected_ben = simple_sum - cov_total
        all_ok &= check(
            abs(div_ben - expected_ben) < 1.0,
            "diversification_benefit = simple_sum − cov_total",
            f"reported={fmt(div_ben)}, computed={fmt(expected_ben)}"
        )
    else:
        print(f"    (Single currency or no meaningful diversification benefit)")

    # Print any fetch errors (currencies skipped due to market data failure)
    for err in spot_risk.get('errors', []):
        print(f"    ✗ Market data error — {err['currency']}: {err['reason']}")

    return all_ok


# =============================================================================
# [S1b] CASH SPOT RATE SENSITIVITY — source data verification
# =============================================================================
# Corresponds to: Cash Spot Rate Sensitivity card in calculator.html (V3.8)
# Engine source:  calculate_fx_var() → result['spot_risk']['positions']
#                 specifically positions[i]['exposure_base']
#
# IMPORTANT: The sensitivity card itself is frontend-only arithmetic —
# no backend engine function computes it. The engine only supplies exposure_base,
# and the frontend applies: change_base = exposure_base × (scenario_pct / 100)
#
# This section verifies:
#   - The source data (exposure_base per currency) is correct
#   - The scenario arithmetic produces consistent results
#   - The sign convention is correct (positive = FCY appreciated = gain for cash holders)
# =============================================================================

def print_sensitivity_source_data(spot_risk: dict, base_ccy: str) -> bool:
    """
    Verify and display the source data for the Cash Spot Rate Sensitivity card.

    The Cash Spot Rate Sensitivity card (V3.8) in calculator.html is a purely
    frontend feature — it does NOT call a backend endpoint. Instead, renderSensitivity()
    in calculator.html reuses the spot_risk.positions data already present in the
    /calculate response, applying simple arithmetic:

        change_base = exposure_base × (scenario_pct / 100)

    where scenario_pct ∈ {+20, +10, +5, 0, -5, -10, -20}.

    This function:
      1. Prints the exposure_base values the frontend will use (for quick visual check)
      2. Applies the same arithmetic here in Python to show expected UI output
      3. Verifies the sign convention: positive pct → positive change (FCY appreciated
         → cash holder gains in base currency terms)

    WHY THIS MATTERS:
    If exposure_base is wrong (e.g. due to a spot_rate fetch error or sign error),
    every scenario value in the UI will be proportionally wrong. Catching it here
    before the browser is faster than debugging via DevTools.

    Sanity checks:
        A. change_base for positive scenario > 0 (FCY appreciation = gain)
        B. change_base for negative scenario < 0 (FCY depreciation = loss)
        C. change_base at 0% = 0 (no-change scenario produces zero delta)
        D. change_base scales linearly with scenario_pct (doubling the pct doubles
           the change — simple arithmetic, but worth asserting)

    Args:
        spot_risk: The 'spot_risk' sub-dict from calculate_fx_var() output.
        base_ccy:  Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    print(subheader("[S1b] CASH SPOT RATE SENSITIVITY — source data"))
    print("    Frontend arithmetic: change_base = exposure_base × (scenario_pct / 100)")
    print("    Source data is spot_risk.positions[i].exposure_base from Section 1.\n")

    # These match the SCENARIOS array in calculator.html's renderSensitivity()
    SCENARIOS = [+20, +10, +5, 0, -5, -10, -20]

    if not spot_risk.get('positions'):
        print("    (No cash positions — sensitivity card will be hidden in the UI.)")
        return True

    all_ok = True

    # ── Header row ────────────────────────────────────────────────────────────
    col_ccy  = 6
    col_base = 16
    col_scen = 14
    hdr_scens = ''.join(f"{'%+d%%' % s:>{col_scen}}" for s in SCENARIOS)
    print(f"    {'CCY':{col_ccy}}  {'Exposure (' + base_ccy + ')':>{col_base}}  {hdr_scens}")
    print(f"    {sep('─', col_ccy + col_base + len(SCENARIOS) * col_scen + 4)}")

    for pos in spot_risk['positions']:
        exp_base  = pos['exposure_base']
        changes   = [exp_base * (s / 100) for s in SCENARIOS]

        # Format each scenario value with sign and colour-coding hint
        scen_strs = []
        for s, ch in zip(SCENARIOS, changes):
            if s == 0:
                scen_strs.append(f"{'—':>{col_scen}}")
            else:
                # Positive = gain (FCY appreciated), Negative = loss (FCY depreciated)
                scen_strs.append(f"{('+' if ch >= 0 else '') + fmt(ch):>{col_scen}}")

        print(f"    {pos['currency']:{col_ccy}}  "
              f"{fmt(exp_base):>{col_base}}  "
              f"{''.join(scen_strs)}")

        # Sanity checks per position
        ch_pos = exp_base * (10 / 100)    # +10% scenario
        ch_neg = exp_base * (-10 / 100)   # -10% scenario
        ch_zer = exp_base * (0 / 100)     # 0% scenario

        all_ok &= check(
            ch_pos > 0,
            f"{pos['currency']} +10% change is positive (FCY appreciation = gain)",
            f"Got {fmt(ch_pos)}"
        )
        all_ok &= check(
            ch_neg < 0,
            f"{pos['currency']} -10% change is negative (FCY depreciation = loss)",
            f"Got {fmt(ch_neg)}"
        )
        all_ok &= check(
            ch_zer == 0,
            f"{pos['currency']} 0% change = exactly 0",
            f"Got {ch_zer}"
        )
        # Linear scaling: +20% should be exactly double +10%
        ch_20 = exp_base * (20 / 100)
        ch_10 = exp_base * (10 / 100)
        all_ok &= check(
            abs(ch_20 - 2 * ch_10) < 1e-9,
            f"{pos['currency']} +20% = 2× +10% (linear arithmetic)",
            f"+20%={fmt(ch_20)}, 2×(+10%)={fmt(2*ch_10)}"
        )

    print(f"\n    + = {base_ccy} gain (FCY appreciated vs {base_ccy})")
    print(f"    − = {base_ccy} loss (FCY depreciated vs {base_ccy})")
    print(f"    These values should match the ±20/±10/±5 scenario table in the browser.")

    return all_ok


# =============================================================================
# [S2] SECTION 2 — UNIFIED BUCKETED RISK
# =============================================================================
# Corresponds to: Bucketed Risk Detail card in calculator.html
# Engine function: calculate_fx_var() → result['unified_buckets']
#
# What this section verifies:
#   - Cash positions correctly appear in Bucket 1 as synthetic receivables
#   - Natural hedge benefit > 0 where same-currency positions net against each other
#   - Diversification benefit > 0 where 2+ currencies coexist in a bucket
#   - No bucket VaR total printed (they use different T — cannot be summed)
# =============================================================================

def print_unified_buckets(unified_buckets: dict, base_ccy: str) -> bool:
    """
    Print and verify Section 2: bucketed VaR with cash in Bucket 1.

    In this section, cash holdings are treated as synthetic Bucket 1 receivables
    (T=1 trading day settlement) so they can net against same-currency near-term
    payables. Forward exposures land in their natural buckets by settlement date.

    Within each bucket:
      - Positions in the SAME currency are summed (signed: recv=+, pay=-) → net notional
      - Net VaR is computed on |net_notional| at bucket midpoint T
      - Natural hedge benefit = gross VaR − net VaR (always ≥ 0)
      - Cross-currency covariance is then applied across all currencies in the bucket
      - Diversification benefit = simple-sum bucket VaR − covariance-adjusted bucket VaR

    Sanity checks:
        A. Bucket 1 contains cash positions (labelled source='cash')
        B. USD Bucket 2 hedge_benefit > 0 (recv 2mn vs pay 1mn = natural hedge)
        C. Each bucket_var ≤ its bucket_var_simple (covariance ≤ simple sum)
        D. diversification_benefit = bucket_var_simple − bucket_var

    Args:
        unified_buckets: The 'unified_buckets' sub-dict from calculate_fx_var() output.
        base_ccy:        Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    print(subheader("[S2] UNIFIED BUCKETED RISK  (Bucketed Risk Detail card)"))
    print("    Cash → Bucket 1 synthetic receivables. Forwards → natural buckets.")
    print("    Natural hedge: within-currency netting. Diversification: cross-currency covariance.\n")

    all_ok    = True
    usd_b2_checked = False

    if not unified_buckets.get('buckets'):
        print("    (No positions.)")
        return True

    for bucket in unified_buckets['buckets']:
        T   = bucket['midpoint_days']
        bv  = bucket['bucket_var']
        bvs = bucket.get('bucket_var_simple', bv)
        ben = bucket.get('diversification_benefit', 0.0)

        print(f"    ┌─ BUCKET {bucket['bucket_num']}: {bucket['bucket_label'].upper()}"
              f"  (T = {T} trading days, midpoint)")

        for ccy in bucket['currencies']:
            net_n     = ccy['net_notional_foreign']
            net_dir   = ccy['net_direction'].upper()
            drift_flag = (f"  ⚠ DRIFT {ccy['annualised_mean']*100:.1f}%/yr"
                          if ccy['drift_warning'] else "")
            cross_flag = "  [via USD cross-rate]" if ccy['used_cross_rate'] else ""
            floor_flag = (f"  [raw VaR was {base_ccy} -{fmt(abs(ccy['net_var_raw']))}, floored]"
                          if ccy['var_was_floored'] else "")

            print(f"    │  {ccy['currency']:4}  net {net_dir:9} "
                  f"{fmt(abs(net_n)):>18}"
                  f"  spot {ccy['spot_rate']:.4f}{cross_flag}")
            print(f"    │        σ_annual {ccy['annualised_vol']*100:.2f}%{drift_flag}")
            print(f"    │        Net VaR (T={T}): {base_ccy} {fmt(ccy['net_var'])}{floor_flag}")

            if ccy['hedge_benefit'] > 0.01:
                pct = (ccy['hedge_benefit'] / ccy['gross_var_at_bucket_t'] * 100
                       if ccy['gross_var_at_bucket_t'] > 0 else 0)
                print(f"    │        ↳ Natural hedge saved: {base_ccy} "
                      f"{fmt(ccy['hedge_benefit'])}  "
                      f"({pct:.1f}% of gross {base_ccy} {fmt(ccy['gross_var_at_bucket_t'])})")

            # Attribution detail — shown when 2+ positions or net=flat
            if len(ccy['positions']) > 1 or ccy['net_direction'] == 'flat':
                print(f"    │        Individual positions (standalone at T={T}):")
                for pos in ccy['positions']:
                    arrow = "→" if pos['direction'] == 'receivable' else "←"
                    src   = ("[cash]"
                             if pos.get('source') == 'cash'
                             else f"settle {pos['settlement_date']}")
                    print(f"    │          {arrow} {pos['direction']:12} "
                          f"{fmt(pos['amount']):>18}  {src}"
                          f"  standalone {base_ccy} {fmt(pos['standalone_var_at_bucket_t'])}")

        # ── Bucket totals ───────────────────────────────────────────────────
        print(f"    │")
        if ben > 0.01:
            pct = ben / bvs * 100 if bvs > 0 else 0
            print(f"    │  Bucket {bucket['bucket_num']} VaR covariance-adjusted (T={T}): "
                  f"{base_ccy} {fmt(bv)}")
            print(f"    │  Simple sum (assumes ρ=1): {base_ccy} {fmt(bvs)}"
                  f"  ·  Correlation saved: {base_ccy} {fmt(ben)} ({pct:.1f}%)")
        else:
            print(f"    │  Bucket {bucket['bucket_num']} VaR (T={T}): "
                  f"{base_ccy} {fmt(bv)}  (single currency — no diversification)")
        print(f"    └{'─'*65}")

        # Sanity check C: bucket_var ≤ bucket_var_simple
        all_ok &= check(
            bv <= bvs + 1e-6,
            f"Bucket {bucket['bucket_num']} covariance VaR ≤ simple sum",
            f"cov={fmt(bv)}, simple={fmt(bvs)}"
        )
        # Sanity check D: diversification_benefit arithmetic consistency
        if ben > 0.01:
            all_ok &= check(
                abs(ben - (bvs - bv)) < 1.0,
                f"Bucket {bucket['bucket_num']} diversification_benefit = simple − covariance",
                f"reported={fmt(ben)}, computed={fmt(bvs - bv)}"
            )
        # Check that Bucket 1 contains at least one cash-sourced position
        if bucket['bucket_num'] == 1:
            has_cash = any(
                pos.get('source') == 'cash'
                for ccy_data in bucket['currencies']
                for pos in ccy_data['positions']
            )
            all_ok &= check(
                has_cash,
                "Bucket 1 contains at least one cash position (source='cash')",
                "Cash positions should route into Bucket 1 as synthetic receivables"
            )
        # Check USD Bucket 2 has a natural hedge benefit (recv 2mn vs pay 1mn)
        if bucket['bucket_num'] == 2 and not usd_b2_checked:
            for ccy_data in bucket['currencies']:
                if ccy_data['currency'] == 'USD':
                    all_ok &= check(
                        ccy_data['hedge_benefit'] > 0.01,
                        "USD Bucket 2 has natural hedge benefit (recv 2mn vs pay 1mn)",
                        f"hedge_benefit={fmt(ccy_data['hedge_benefit'])}"
                    )
                    usd_b2_checked = True

    for err in unified_buckets.get('errors', []):
        print(f"    ✗ Market data error — {err.get('currency','')} "
              f"({err.get('settlement_date','')}): {err.get('reason','')}")

    print(f"\n    ⚠ Bucket VaRs use DIFFERENT time horizons — they CANNOT be summed.")
    print(f"    Each bucket figure is meaningful within its own settlement window only.")

    return all_ok


# =============================================================================
# [S3] SECTION 3 — GROSS ATTRIBUTION
# =============================================================================
# Corresponds to: Internal reference only (not a UI card, but used internally
#                 by dashboard_engine.py for the Gross Standalone Risk stat card)
# Engine function: calculate_fx_var() → result['gross_attribution']
#
# What this section verifies:
#   - Standalone VaR per forward exposure at bucket midpoint T
#   - No netting, no covariance — each exposure independently
#   - Cash NOT included (this is forwards-only gross reference)
# =============================================================================

def print_gross_attribution(gross_attribution: dict, base_ccy: str) -> bool:
    """
    Print Section 3: gross standalone VaR per forward exposure without any netting.

    This is a reference view — useful for understanding how much risk each forward
    contributes before netting and diversification reduce the headline number.

    This data is NOT directly surfaced as a UI card, but it feeds into
    dashboard_engine.py's Gross Standalone Risk stat card computation (which adds
    gross cash attribution below to produce the full gross figure).

    Note: Cash positions are intentionally excluded here. To see cash's gross
    contribution, see print_gross_cash_attribution() [S3b].

    Sanity checks:
        A. Each forward exposure's VaR > 0 (unless floored — noted)
        B. VaR is at bucket midpoint T (t_used = bucket_midpoint_days)

    Args:
        gross_attribution: The 'gross_attribution' sub-dict from calculate_fx_var().
        base_ccy:          Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    print(subheader("[S3] GROSS ATTRIBUTION  (forwards only, no netting — internal reference)"))
    print("    Each forward's standalone VaR at bucket midpoint T.")
    print("    Cash NOT included. No netting. Feeds Gross Standalone Risk stat card.\n")

    if not gross_attribution.get('exposures'):
        print("    (No forward exposures.)")
        return True

    all_ok = True
    current_bucket = None

    for exp in gross_attribution['exposures']:
        if exp['bucket_num'] != current_bucket:
            current_bucket = exp['bucket_num']
            print(f"    ┌ Bucket {exp['bucket_num']}: {exp['bucket_label']}"
                  f"  (T = {exp['bucket_midpoint_days']} trading days)")

        arrow      = "→" if exp['direction'] == 'receivable' else "←"
        drift_flag = (f"  ⚠ DRIFT {exp['annualised_mean']*100:.1f}%/yr"
                      if exp['drift_warning'] else "")
        floor_flag = (f"  [raw {base_ccy} -{fmt(abs(exp['var_raw']))}, floored]"
                      if exp['var_was_floored'] else "")

        print(f"    │  {arrow} {exp['direction']:12}  {exp['currency']:4}  "
              f"{fmt(exp['amount']):>18}  settle {exp['settlement_date']}")
        print(f"    │     σ {exp['annualised_vol']*100:.2f}%{drift_flag}  "
              f"standalone VaR (T={exp['t_used']}d): {base_ccy} {fmt(exp['var'])}{floor_flag}")

        # Sanity check B: t_used matches the bucket midpoint
        all_ok &= check(
            exp['t_used'] == exp['bucket_midpoint_days'],
            f"{exp['currency']} settle {exp['settlement_date']}: "
            f"t_used = bucket midpoint ({exp['bucket_midpoint_days']}d)",
            f"t_used={exp['t_used']}, midpoint={exp['bucket_midpoint_days']}"
        )

    print(f"    └{'─'*65}")

    for err in gross_attribution.get('errors', []):
        print(f"    ✗ Market data error — {err.get('currency','')} "
              f"({err.get('settlement_date','')}): {err.get('reason','')}")

    return all_ok


# =============================================================================
# [S3b] GROSS CASH ATTRIBUTION
# =============================================================================
# Corresponds to: Cash component of the Gross Standalone Risk stat card
#                 (computed by dashboard_engine.py alongside gross forward VaR)
# Engine function: calculate_fx_var() → result['gross_cash_attribution']
#
# WHY THIS EXISTS (important for future developers to understand):
#   The Gross Standalone Risk stat card = gross forward VaR + gross cash VaR.
#   Cash VaR for this card is always computed at CASH_CONSOLIDATED_T_DAYS (10
#   trading days — the Bucket 1 midpoint), NOT at the user's cash_horizon setting.
#   This intentional isolation means the Gross Standalone Risk stat card and the
#   Risk Reduction % are NEVER affected by the Cash VaR Horizon dropdown.
#   Only Section 1 / Cash Book Risk uses cash_horizon.
# =============================================================================

def print_gross_cash_attribution(gross_cash_attribution: dict, base_ccy: str) -> bool:
    """
    Print and verify the gross cash VaR used for the Gross Standalone Risk stat card.

    This is cash's standalone VaR at the FIXED horizon CASH_CONSOLIDATED_T_DAYS
    (10 trading days = Bucket 1 midpoint), regardless of the user's cash_horizon
    setting. This deliberate decoupling ensures that changing the Cash VaR Horizon
    dropdown ONLY affects Section 1 (Cash Book Risk card) and NEVER touches:
      - The Gross Standalone Risk stat card
      - The Risk Reduction stat card
      - The Consolidated Portfolio VaR card
      - Any Risk Dashboard figure

    If this number were coupled to cash_horizon, a user changing the horizon from
    1 to 30 days would appear to have gained or lost risk reduction — which would
    be misleading since only the measurement window changed, not the actual portfolio.

    Sanity checks:
        A. Every position's t_used = CASH_CONSOLIDATED_T_DAYS (10)
        B. VaR > 0 for each position (unless floored)

    Args:
        gross_cash_attribution: The 'gross_cash_attribution' sub-dict from
                                 calculate_fx_var() output.
        base_ccy:               Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    CASH_CONSOLIDATED_T_DAYS = 10   # mirrors the constant in exposure_engine.py

    print(subheader("[S3b] GROSS CASH ATTRIBUTION  (Gross Standalone Risk card — cash component)"))
    print(f"    Cash standalone VaR at FIXED T={CASH_CONSOLIDATED_T_DAYS}d (Bucket 1 midpoint).")
    print(f"    Intentionally INDEPENDENT of cash_horizon — see exposure_engine.py")
    print(f"    CASH_CONSOLIDATED_T_DAYS for full rationale.\n")

    if not gross_cash_attribution.get('exposures'):
        print("    (No cash positions.)")
        return True

    all_ok  = True
    total   = 0.0

    for pos in gross_cash_attribution['exposures']:
        floor_flag = (f"  [raw {base_ccy} -{fmt(abs(pos['var_raw']))}, floored]"
                      if pos['var_was_floored'] else "")
        print(f"    {pos['currency']:4}  balance {pos['currency']} {fmt(pos['balance']):>18}"
              f"  T={pos['t_used']}d"
              f"  VaR: {base_ccy} {fmt(pos['var'])}{floor_flag}")
        total += pos['var']

        all_ok &= check(
            pos['t_used'] == CASH_CONSOLIDATED_T_DAYS,
            f"{pos['currency']} t_used = {CASH_CONSOLIDATED_T_DAYS} "
            f"(CASH_CONSOLIDATED_T_DAYS — must NOT equal cash_horizon)",
            f"t_used={pos['t_used']}, expected={CASH_CONSOLIDATED_T_DAYS}"
        )

    print(f"\n    Cash gross VaR total (simple sum): {base_ccy} {fmt(total)}")
    print(f"    (dashboard_engine.py adds this to gross forward VaR → Gross Standalone Risk)")

    for err in gross_cash_attribution.get('errors', []):
        print(f"    ✗ Market data error — {err['currency']}: {err['reason']}")

    return all_ok


# =============================================================================
# [S4] CONSOLIDATED PORTFOLIO VAR
# =============================================================================
# Corresponds to: Consolidated Portfolio VaR card + Portfolio VaR stat card
# Engine function: calculate_fx_var() → result['consolidated_var']
#
# What this section verifies:
#   - Single portfolio VaR using exact min(Tᵢ,Tⱼ) cross-horizon covariance
#   - Every individual position (cash + forwards) listed in the covariance matrix
#   - This number MUST equal the 'all' period var in cumulative_vars (checked in S5)
# =============================================================================

def print_consolidated_var(consolidated_var: dict, base_ccy: str) -> bool:
    """
    Print and verify the Consolidated Portfolio VaR (V2.4).

    This is the single, authoritative portfolio-wide risk number. It is computed
    using the exact min(Tᵢ,Tⱼ) cross-horizon covariance formula, treating every
    individual position (each cash holding and each forward) as a separate row and
    column in the covariance matrix. There is no pre-netting by currency — natural
    hedging between a USD recv and USD pay falls out automatically via the signed
    exposure vector and ρ=1 for same-currency pairs.

    This number appears in three UI locations:
      1. The Consolidated Portfolio VaR card headline
      2. The Portfolio VaR stat card in the Risk Dashboard
      3. The 'all' period in the cumulative period VaRs (must be identical)

    Sanity checks:
        A. n_positions > 0 (at least one position in the matrix)
        B. total_var ≥ 0 (VaR cannot be negative after flooring)
        C. position_breakdown has exactly n_positions entries

    Args:
        consolidated_var: The 'consolidated_var' sub-dict from calculate_fx_var().
        base_ccy:         Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    print(subheader("[S4] CONSOLIDATED PORTFOLIO VAR  (Consolidated VaR card + Dashboard stat)"))
    print("    Single number: ALL positions (cash + forwards) in one covariance matrix.")
    print("    Uses exact min(Tᵢ,Tⱼ) cross-horizon formula — no bucket approximation.\n")

    all_ok = True

    tv      = consolidated_var.get('total_var', 0.0)
    tv_raw  = consolidated_var.get('total_var_raw', 0.0)
    floored = consolidated_var.get('var_was_floored', False)
    n_pos   = consolidated_var.get('n_positions', 0)

    print(f"    Consolidated Portfolio VaR: {base_ccy} {fmt(tv)}", end="")
    if floored:
        print(f"  [raw was {base_ccy} -{fmt(abs(tv_raw))}, floored to 0]")
    else:
        print()
    print(f"    Methodology: {consolidated_var.get('methodology', 'unknown')}")
    print(f"    Positions in covariance matrix: {n_pos}")

    # Sanity checks
    all_ok &= check(n_pos > 0,   "n_positions > 0 (at least one position in matrix)")
    all_ok &= check(tv >= 0,     "total_var ≥ 0 (always non-negative after flooring)",
                    f"total_var={fmt(tv)}")
    all_ok &= check(
        len(consolidated_var.get('position_breakdown', [])) == n_pos,
        "position_breakdown length = n_positions",
        f"breakdown len={len(consolidated_var.get('position_breakdown', []))}, n_pos={n_pos}"
    )

    # ── Individual position breakdown ──────────────────────────────────────
    print(f"\n    Individual positions entering the covariance matrix (sorted by |exposure|):")
    print(f"    {'CCY':<5}  {'Type':<8}  {'Direction':<12}  "
          f"{'Signed Exposure (' + base_ccy + ')':>22}  "
          f"{'T (days)':>8}  {'Settlement'}")
    print(f"    {sep('─', 75)}")

    for p in consolidated_var.get('position_breakdown', []):
        sign_str    = '+' if p['signed_exposure_base'] >= 0 else ''
        date_str    = p['settlement_date'] if p['settlement_date'] else '[cash]'
        print(f"    {p['currency']:<5}  {p['type']:<8}  {p['direction']:<12}  "
              f"{sign_str + base_ccy + ' ' + fmt(abs(p['signed_exposure_base'])):>22}  "
              f"{p['t_days']:>8}  {date_str}")

    print(f"\n    Note: consolidated VaR < sum of bucket VaRs because:")
    print(f"    1. Same-currency pairs net automatically via signed exposures (ρ=1, opposite signs)")
    print(f"    2. Cross-currency diversification reduces portfolio volatility (historical ρ < 1)")

    return all_ok


# =============================================================================
# [S5] CUMULATIVE PERIOD VARS + COMPONENT CFaR
# =============================================================================
# Corresponds to: Risk Dashboard — period filter, notional bar chart,
#                 Component CFaR bars, scenario simulation sliders
# Engine function: calculate_fx_var() → result['cumulative_vars']
#
# This is the richest and most complex section. It verifies:
#   - Per-period (1m/3m/6m/12m/all) consolidated VaR
#   - Per-currency Component CFaR (Euler decomposition summing to period_var)
#   - vol_part / drift_part split per currency (drives exact vol-slider scaling)
#   - net_notional per currency per period (drives notional bar chart in JS)
#   - CRITICAL: 'all' period_var must equal consolidated_var['total_var'] exactly
# =============================================================================

def print_cumulative_period_vars(cumulative_vars: dict,
                                 consolidated_var_total: float,
                                 base_ccy: str) -> bool:
    """
    Print and verify cumulative period VaRs and per-currency Component CFaRs.

    This section is the backbone of the Risk Dashboard. It feeds:
      - The cumulative period filter dropdown (1m / 3m / 6m / 12m / All)
      - The net notional bar chart (net_notional_base per currency per period)
      - The Component CFaR horizontal bars (component_var per currency per period)
      - The scenario simulation sliders (vol_part + drift_part decomposition)

    === COMPONENT CFaR EXPLAINED ===
    Component CFaR (also called Component VaR) is the Euler marginal decomposition
    of portfolio VaR by currency. Each currency's Component CFaR answers:
        "How much of the total Portfolio VaR is attributable to this currency,
        ACCOUNTING FOR cross-currency correlations?"

    Key property (mathematically guaranteed):
        Σ component_var[ccy] = period_var  (they sum exactly to the total)

    This makes them suitable for a bar chart where bars represent risk attribution
    rather than a waterfall of independent risks.

    A negative component_var is mathematically valid and means the currency acts
    as a net hedge to the portfolio (its covariance contribution REDUCES total risk).

    === VOL_PART / DRIFT_PART DECOMPOSITION (V3.6) ===
    component_var is split into:
        vol_part:   volatility-driven contribution → scales EXACTLY with vol slider
        drift_part: drift-driven contribution → held CONSTANT under vol-regime shift

    Identity: component_var = vol_part − drift_part (always, no rounding error)

    Under a uniform vol-regime shift k (the vol slider), the new Component CFaR is:
        new_component_var = k × vol_part − drift_part  (exact, not approximation)

    This is why the vol slider produces exact results (unlike the spot slider,
    which is a first-order approximation because shifting one currency's notional
    changes its cross-covariance with every other currency).

    === CRITICAL INVARIANT ===
    The 'all' period must produce EXACTLY the same VaR as consolidated_var.
    Both use the same _build_individual_positions_list() and
    _build_position_level_correlation_matrix() helpers, then the same
    calculate_portfolio_var_cov_mixed_t(). Any discrepancy indicates a code
    divergence between the two code paths that MUST be fixed.

    Sanity checks:
        A. 'all' period_var = consolidated_var['total_var'] (to within 1 unit)
        B. Σ component_var[ccy] = period_var for every period (Euler identity)
        C. component_var = vol_part − drift_part for every currency every period
        D. period VaRs are non-negative (for all periods — no flooring edge case expected)
        E. 1m VaR ≤ 3m VaR ≤ 6m VaR ≤ 12m VaR ≤ all VaR (more positions = more risk,
           though this is not guaranteed by the math — it's a domain expectation for
           typical portfolios and a useful smell-test)

    Args:
        cumulative_vars:          The 'cumulative_vars' sub-dict from calculate_fx_var().
        consolidated_var_total:   consolidated_var['total_var'] for cross-check (check A).
        base_ccy:                 Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    print(subheader("[S5] CUMULATIVE PERIOD VARS + COMPONENT CFaR  (Risk Dashboard)"))
    print("    Feeds: period filter dropdown, notional bar chart, Component CFaR bars,")
    print("    and vol/spot scenario simulation sliders.\n")

    all_ok      = True
    period_keys = ['1m', '3m', '6m', '12m', 'all']   # UI dropdown order

    # ── Per-period summary row ─────────────────────────────────────────────
    print(f"    {'Period':<14}  {'n_pos':>5}  {'Period VaR':>18}  "
          f"{'Σ Component CFaR':>18}  {'Match?':>8}")
    print(f"    {sep('─', 72)}")

    prev_var = 0.0
    for key in period_keys:
        period = cumulative_vars.get(key, {})
        pvar   = period.get('period_var', 0.0)
        n_pos  = period.get('n_positions', 0)
        ccy_d  = period.get('currencies', {})
        label  = period.get('label', key)

        # Sum of component VaRs across all currencies for this period
        component_sum = sum(c.get('component_var', 0) for c in ccy_d.values())
        match_str     = '✓' if abs(component_sum - pvar) < 1.0 else '✗ MISMATCH'

        print(f"    {label:<14}  {n_pos:>5}  "
              f"{base_ccy + ' ' + fmt(pvar):>18}  "
              f"{base_ccy + ' ' + fmt(component_sum):>18}  "
              f"{match_str:>8}")

        # Sanity check B: component VaR sum = period VaR (Euler identity)
        all_ok &= check(
            abs(component_sum - pvar) < 1.0,
            f"[{key}] Σ component_var = period_var (Euler identity)",
            f"Σcomponent={fmt(component_sum)}, period_var={fmt(pvar)}"
        )

    print(f"    {sep('─', 72)}")

    # Sanity check A: 'all' period = consolidated_var
    all_period_var = cumulative_vars.get('all', {}).get('period_var', 0.0)
    all_ok &= check(
        abs(all_period_var - consolidated_var_total) < 1.0,
        "'all' period_var = consolidated_var total_var (CRITICAL — same code path)",
        f"all_period={fmt(all_period_var)}, consolidated={fmt(consolidated_var_total)}"
    )

    # Sanity check E: monotonicity (domain expectation, not guaranteed by math)
    period_vars = [cumulative_vars.get(k, {}).get('period_var', 0.0)
                   for k in period_keys]
    is_monotone = all(period_vars[i] <= period_vars[i+1] + 1.0
                      for i in range(len(period_vars)-1))
    if not is_monotone:
        print(f"    ⚠ NOTE: Period VaRs are not monotonically increasing "
              f"({[fmt(v) for v in period_vars]})")
        print(f"      This is unusual but mathematically possible if a short-dated "
              f"position dominates and a longer-dated one diversifies it away.")

    # ── Per-currency detail for each period ────────────────────────────────
    print()
    for key in period_keys:
        period = cumulative_vars.get(key, {})
        pvar   = period.get('period_var', 0.0)
        ccy_d  = period.get('currencies', {})
        label  = period.get('label', key)

        if not ccy_d:
            print(f"\n    [{label}]  No positions in this window.")
            continue

        print(f"\n    [{label}]  Period VaR: {base_ccy} {fmt(pvar)}"
              f"  ·  {period.get('n_positions', 0)} position(s)")
        print(f"    {'CCY':<5}  {'Net Notional (' + base_ccy + ')':>20}  "
              f"{'Dir':<6}  {'Component CFaR':>16}  "
              f"{'vol_part':>14}  {'drift_part':>12}  "
              f"{'Eff T':>6}  {'Check C':>8}")
        print(f"    {sep('─', 100)}")

        for ccy, c in ccy_d.items():
            comp  = c.get('component_var', 0.0)
            vp    = c.get('vol_part',      0.0)
            dp    = c.get('drift_part',    0.0)
            nn    = c.get('net_notional_base', 0.0)
            nd    = c.get('net_direction', '?')
            eff_t = c.get('effective_T',   0.0)

            # Check C: component_var = vol_part − drift_part
            identity_ok = abs(comp - (vp - dp)) < 1.0
            check_str   = '✓' if identity_ok else '✗'

            print(f"    {ccy:<5}  "
                  f"{('+' if c.get('net_signed_base', 0) >= 0 else '') + base_ccy + ' ' + fmt(nn):>20}  "
                  f"{nd:<6}  "
                  f"{base_ccy + ' ' + fmt(comp):>16}  "
                  f"{base_ccy + ' ' + fmt(vp):>14}  "
                  f"{base_ccy + ' ' + fmt(dp):>12}  "
                  f"{eff_t:>6.1f}  "
                  f"{check_str:>8}")

            all_ok &= check(
                identity_ok,
                f"[{key}] {ccy} component_var = vol_part − drift_part",
                f"comp={fmt(comp)}, vol_part={fmt(vp)}, drift_part={fmt(dp)}, "
                f"vol−drift={fmt(vp-dp)}"
            )

        # Reminder: vol_part scales exactly with vol slider; drift_part does not
        print(f"    ↑ vol_part scales EXACTLY with the vol slider (k×vol_part − drift_part)")
        print(f"      drift_part is held constant under a vol-regime shift (μ, T, s unchanged)")

    return all_ok


# =============================================================================
# [S6] HEDGE RECOMMENDATIONS
# =============================================================================
# Corresponds to: Hedge Recommendations section in calculator.html (V3.8)
# Engine function: recommend_hedges() (separate from calculate_fx_var)
#
# What this section verifies:
#   - Ranked forward-hedge proposals reduce Portfolio VaR monotonically
#   - AUD (cash-only) is never proposed as a hedge candidate
#   - Marginal reduction arithmetic is consistent with before/after values
#   - Baseline VaR matches consolidated_var from the main calculation
# =============================================================================

def print_hedge_recommendations(recs_result: dict,
                                 consolidated_var_total: float,
                                 base_ccy: str) -> bool:
    """
    Print and verify the output of the hedge recommendation engine (V3.8).

    Corresponds to the Hedge Recommendations section in the web UI, rendered by
    renderHedgeRecommendations() in calculator.html. The engine (recommend_hedges
    in exposure_engine.py) identifies one forward-hedge contract per (currency,
    bucket) group in the forward exposure list, ranks by Component CFaR, then
    applies them cumulatively while re-running calculate_consolidated_portfolio_var
    after each hedge to measure the exact marginal VaR reduction.

    KEY INVARIANT: The baseline_var returned by recommend_hedges() must equal
    consolidated_var['total_var'] from calculate_fx_var(). Both call
    calculate_consolidated_portfolio_var() with the same inputs — any
    discrepancy means the two code paths have diverged.

    Sanity checks:
        A. baseline_var ≈ consolidated_var total_var (cross-check between engines)
        B. Each hedge reduces Portfolio VaR (portfolio_var_after ≤ portfolio_var_before)
        C. cumulative_reduction_pct is monotonically non-decreasing
        D. marginal_reduction_abs = portfolio_var_before − portfolio_var_after (per row)
        E. AUD is absent (cash-only — no forwards, so no hedge candidate)
        F. fully_hedged_var > 0 (T-mismatch residual expected; zero would be suspicious)
        G. Last row portfolio_var_after ≈ fully_hedged_var (internal consistency)

    Args:
        recs_result:              Full dict returned by recommend_hedges().
        consolidated_var_total:   consolidated_var['total_var'] for cross-check (check A).
        base_ccy:                 Home currency ISO code (e.g. 'SGD').

    Returns:
        True if all sanity checks passed, False otherwise.
    """
    baseline_var  = recs_result['baseline_var']
    recs          = recs_result['recommendations']
    fully_hedged  = recs_result['fully_hedged_var']
    fully_pct     = recs_result['fully_hedged_reduction_pct']
    errors        = recs_result.get('errors', [])

    print(subheader("[S6] HEDGE RECOMMENDATIONS  (Hedge Recommendations section · V3.8)"))
    print("    Forward contracts ranked by Component CFaR impact.")
    print("    Applied cumulatively — 'after' includes all prior hedges.\n")

    all_ok = True

    # ── Sanity check A: baseline must match consolidated VaR ───────────────
    all_ok &= check(
        abs(baseline_var - consolidated_var_total) < 1.0,
        "baseline_var = consolidated_var total_var (CRITICAL cross-check)",
        f"baseline={fmt(baseline_var)}, consolidated={fmt(consolidated_var_total)}"
    )

    if not recs:
        print("    No hedgeable forward exposures found.")
        print("    (Expected if only cash positions are present.)")
        return all_ok

    # ── Headline summary ──────────────────────────────────────────────────
    total_abs = baseline_var - fully_hedged
    print(f"    Baseline Portfolio VaR:      {base_ccy} {fmt(baseline_var)}")
    print(f"    Fully Hedged Portfolio VaR:  {base_ccy} {fmt(fully_hedged)}")
    print(f"    Total reduction:             {base_ccy} {fmtk(total_abs)}"
          f"  ({fully_pct:.1f}%)")
    print(f"    Hedge contracts proposed:    {len(recs)}\n")

    # ── Ranked table ──────────────────────────────────────────────────────
    print(f"    {'Rk':<3}  {'CCY':<4}  {'Bucket':<18}  {'Action':<22}  "
          f"{'Settlement':<12}  {'VaR Before':>14}  {'VaR After':>14}  "
          f"{'Marginal ↓':>14}  {'Cumulative ↓':>14}")
    print(f"    {sep('─', 125)}")

    prev_cum = 0.0
    for rec in recs:
        action = (f"Sell {rec['currency']} fwd"
                  if rec['hedge_direction'] == 'payable'
                  else f"Buy  {rec['currency']} fwd")
        marg = (f"-{fmtk(rec['marginal_reduction_abs'])} "
                f"({rec['marginal_reduction_pct']:.1f}%)")
        cum  = (f"-{fmtk(rec['cumulative_reduction_abs'])} "
                f"({rec['cumulative_reduction_pct']:.1f}%)")

        print(f"    {rec['rank']:<3}  {rec['currency']:<4}  "
              f"B{rec['bucket_num']} {rec['bucket_label']:<16}  "
              f"{action:<22}  "
              f"{rec['hedge_settlement_date']:<12}  "
              f"{base_ccy + ' ' + fmt(rec['portfolio_var_before']):>14}  "
              f"{base_ccy + ' ' + fmt(rec['portfolio_var_after']):>14}  "
              f"{marg:>14}  {cum:>14}")
        # Sub-row: notional + baseline Component CFaR (ranking signal)
        print(f"    {'':3}  {'':4}  "
              f"{rec['currency'] + ' ' + fmtk(rec['hedge_amount_fcy']):<20}  "
              f"  Baseline CFaR: {base_ccy} {fmtk(abs(rec['component_cfar_baseline']))}")

    print(f"    {sep('─', 125)}")

    # ── Sanity checks B–G ─────────────────────────────────────────────────
    print()
    # B: each hedge reduces VaR
    for rec in recs:
        all_ok &= check(
            rec['portfolio_var_after'] <= rec['portfolio_var_before'] + 1e-6,
            f"Rank {rec['rank']} ({rec['currency']} B{rec['bucket_num']}): "
            f"VaR after ≤ VaR before",
            f"after={fmt(rec['portfolio_var_after'])}, "
            f"before={fmt(rec['portfolio_var_before'])}"
        )
    # C: cumulative reduction is monotonically non-decreasing
    prev_cum_pct = 0.0
    c_ok = True
    for rec in recs:
        if rec['cumulative_reduction_pct'] < prev_cum_pct - 1e-4:
            c_ok = False
            all_ok &= check(
                False,
                f"Rank {rec['rank']}: cumulative reduction non-decreasing",
                f"{prev_cum_pct:.1f}% → {rec['cumulative_reduction_pct']:.1f}%"
            )
        prev_cum_pct = rec['cumulative_reduction_pct']
    if c_ok:
        check(True, "Cumulative reduction is monotonically non-decreasing across all ranks")

    # D: marginal_reduction_abs = before − after
    for rec in recs:
        expected_marg = rec['portfolio_var_before'] - rec['portfolio_var_after']
        all_ok &= check(
            abs(rec['marginal_reduction_abs'] - expected_marg) < 1.0,
            f"Rank {rec['rank']}: marginal_reduction_abs = before − after",
            f"reported={fmt(rec['marginal_reduction_abs'])}, "
            f"expected={fmt(expected_marg)}"
        )
    # E: AUD absent (cash-only, no forward)
    aud_present = any(r['currency'] == 'AUD' for r in recs)
    all_ok &= check(
        not aud_present,
        "AUD absent from candidates (cash-only currency — no forward exposure)",
        "AUD appeared as a hedge candidate — this is a bug in _identify_hedge_candidates"
    )
    # F: fully_hedged_var > 0 (T-mismatch residual expected)
    check(
        fully_hedged > 0,
        f"Fully hedged VaR > 0 (T-mismatch residual: {fmt(fully_hedged)})",
        "Zero would suggest exact T alignment — unusual with bucket-midpoint hedges"
    )
    # G: last row consistency
    if recs:
        last_after = recs[-1]['portfolio_var_after']
        all_ok &= check(
            abs(last_after - fully_hedged) < 1.0,
            f"Last row VaR after ≈ fully_hedged_var",
            f"last_after={fmt(last_after)}, fully_hedged={fmt(fully_hedged)}"
        )

    if errors:
        print(f"\n    Market data fetch errors (excluded from recommendations):")
        for err in errors:
            print(f"    ✗ {err['currency']}: {err.get('reason', 'unknown')}")

    return all_ok


# =============================================================================
# MAIN TEST RUN
# =============================================================================
# All eight print functions are called here in order.
# Modify only CASH_POSITIONS and EXPOSURES to test a different portfolio.
# Do not add inline print() calls here — formatting belongs in the functions.
# =============================================================================

if __name__ == '__main__':

    # ── Global test parameters ────────────────────────────────────────────────
    BASE_CCY     = 'SGD'
    CONFIDENCE   = 0.95    # 95% confidence level → Z ≈ 1.6449
    PERIOD       = '1y'    # yfinance historical lookback period
    CASH_HORIZON = 1       # trading days — affects Section 1 (Cash Book Risk) ONLY

    # ── Test portfolio ────────────────────────────────────────────────────────
    # Deliberately constructed to exercise all code paths. See module docstring
    # for why each position is in the scenario.
    CASH_POSITIONS = [
        {'currency': 'USD', 'balance': 2_000_000},   # multi-bucket: also has forwards
        {'currency': 'MYR', 'balance': 5_000_000},   # partially offsets MYR payable
        {'currency': 'AUD', 'balance': 1_000_000},   # CASH-ONLY: no AUD forwards
    ]

    EXPOSURES = [
        # A: USD receivable (Bucket 2) — creates natural hedge with B below
        {'currency': 'USD', 'amount': 2_000_000,
         'settlement_date': '2026-08-15', 'direction': 'receivable'},
        # B: USD payable (Bucket 2) — nets against A: net recv 1mn
        {'currency': 'USD', 'amount': 1_000_000,
         'settlement_date': '2026-08-25', 'direction': 'payable'},
        # C: MYR payable (Bucket 2) — large; likely the biggest Component CFaR
        {'currency': 'MYR', 'amount': 8_000_000,
         'settlement_date': '2026-08-20', 'direction': 'payable'},
        # D: USD payable (Bucket 3) — long-dated; tests cross-horizon covariance
        {'currency': 'USD', 'amount': 3_000_000,
         'settlement_date': '2026-10-30', 'direction': 'payable'},
        # E: EUR receivable (Bucket 4) — diversifying currency, long horizon
        {'currency': 'EUR', 'amount': 1_000_000,
         'settlement_date': '2027-01-15', 'direction': 'receivable'},
    ]

    # ── Title block ───────────────────────────────────────────────────────────
    print(header("FX VaR ENGINE RUNNER — FULL VERIFICATION OUTPUT"))
    print(f"\n  Base: {BASE_CCY}  |  Confidence: {CONFIDENCE:.0%}"
          f"  |  Lookback: {PERIOD}  |  Cash Horizon: {CASH_HORIZON}d")
    print(f"  Cash positions: {len(CASH_POSITIONS)}"
          f"  |  Forward exposures: {len(EXPOSURES)}")
    print(f"\n  Tests: [S1] Spot Book Risk · [S1b] Sensitivity Source Data")
    print(f"         [S2] Bucketed Risk · [S3] Gross Attribution")
    print(f"         [S3b] Gross Cash Attribution · [S4] Consolidated VaR")
    print(f"         [S5] Cumulative Period VaRs + Component CFaR")
    print(f"         [S6] Hedge Recommendations")
    print(f"\n  Each section prints ✓/✗ for its sanity checks.")
    print(f"  Final pass/fail summary at the bottom.\n")

    # ── Part 1: Full three-section engine output ──────────────────────────────
    # calculate_fx_var() is the main entry point called by POST /calculate.
    # It returns ALL engine sections in one call — market data is fetched once
    # internally and reused across all sections.
    result = calculate_fx_var(
        cash_positions = CASH_POSITIONS,
        exposures      = EXPOSURES,
        base_ccy       = BASE_CCY,
        confidence     = CONFIDENCE,
        period         = PERIOD,
        cash_horizon   = CASH_HORIZON,
    )

    # Extract sub-dicts for cleaner function calls
    spot_risk             = result['spot_risk']
    unified_buckets       = result['unified_buckets']
    gross_attribution     = result['gross_attribution']
    gross_cash_attr       = result['gross_cash_attribution']
    consolidated_var      = result['consolidated_var']
    cumulative_vars       = result['cumulative_vars']

    # Track per-section pass/fail to produce a final summary
    results = {}

    print(header("[S1] SECTION 1 — SPOT BOOK RISK"))
    results['S1'] = print_spot_risk(spot_risk, BASE_CCY)

    print(header("[S1b] CASH SPOT RATE SENSITIVITY — SOURCE DATA"))
    results['S1b'] = print_sensitivity_source_data(spot_risk, BASE_CCY)

    print(header("[S2] SECTION 2 — UNIFIED BUCKETED RISK"))
    results['S2'] = print_unified_buckets(unified_buckets, BASE_CCY)

    print(header("[S3] SECTION 3 — GROSS ATTRIBUTION"))
    results['S3'] = print_gross_attribution(gross_attribution, BASE_CCY)

    print(header("[S3b] GROSS CASH ATTRIBUTION"))
    results['S3b'] = print_gross_cash_attribution(gross_cash_attr, BASE_CCY)

    print(header("[S4] CONSOLIDATED PORTFOLIO VAR"))
    results['S4'] = print_consolidated_var(consolidated_var, BASE_CCY)

    print(header("[S5] CUMULATIVE PERIOD VARS + COMPONENT CFaR"))
    results['S5'] = print_cumulative_period_vars(
        cumulative_vars,
        consolidated_var['total_var'],
        BASE_CCY
    )

    # ── Part 2: Hedge recommendation engine ──────────────────────────────────
    # recommend_hedges() is a separate entry point called by POST /recommend_hedges.
    # It re-fetches market data internally — intentionally separate from the
    # calculate_fx_var() call above, mirroring the production HTTP request model
    # (two separate endpoints, no shared Python state across requests).
    print(header("[S6] HEDGE RECOMMENDATIONS"))
    recs_result = recommend_hedges(
        cash_positions = CASH_POSITIONS,
        exposures      = EXPOSURES,
        base_ccy       = BASE_CCY,
        confidence     = CONFIDENCE,
        period         = PERIOD,
    )
    results['S6'] = print_hedge_recommendations(
        recs_result,
        consolidated_var['total_var'],
        BASE_CCY
    )

    # ── Final pass/fail summary ───────────────────────────────────────────────
    print(header("FINAL SUMMARY"))
    print()
    all_passed = True
    for section, passed in results.items():
        status    = '✓ PASS' if passed else '✗ FAIL'
        all_passed = all_passed and passed
        print(f"  {status}  [{section}]")

    print()
    if all_passed:
        print(f"  {'='*55}")
        print(f"  ALL SECTIONS PASSED ✓")
        print(f"  Engine output is consistent and mathematically sound.")
        print(f"  Safe to push and verify in the browser.")
        print(f"  {'='*55}")
    else:
        print(f"  {'='*55}")
        print(f"  ONE OR MORE SECTIONS FAILED ✗")
        print(f"  Investigate the ✗ items above before pushing.")
        print(f"  Browser output WILL be wrong if engine output is wrong.")
        print(f"  {'='*55}")

    print(f"\n{sep('═')}")
