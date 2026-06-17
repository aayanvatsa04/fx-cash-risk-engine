"""
dashboard_engine.py — Dashboard Data Transformation Layer

This module sits between the VaR engine (exposure_engine.py) and the frontend
dashboard (calculator.html / dashboard.js). Its sole responsibility is to
transform the raw dict output of calculate_fx_var() into a clean,
chart-ready JSON structure that the frontend can render without performing
any financial mathematics itself.

=== ARCHITECTURE POSITION ===

    app.py
      ↓ calls
    exposure_engine.calculate_fx_var()
      ↓ result passed to
    dashboard_engine.prepare_dashboard_data()   ← THIS FILE
      ↓ returns
    chart-ready JSON dict  →  Flask jsonify  →  dashboard.js

This module:
  - IMPORTS FROM: exposure_engine (indirectly — receives its output dict)
  - IMPORTS FROM: scipy.stats, math (for pre-computing simulation components)
  - IS IMPORTED BY: app.py only
  - HAS NO KNOWLEDGE OF: Flask, HTML, Chart.js, or HTTP request/response

=== WHY PRE-COMPUTE SIMULATION COMPONENTS HERE (NOT IN JS) ===

The client-side simulation sliders update CFaR values live as the user drags.
To avoid needing the VaR formula in JavaScript (which would duplicate math
that only belongs in Python), this module pre-computes the two components of
each position's CFaR:

    CFaR = vol_term ± mu_term
    where:
        vol_term = |net_notional_base| × Z × σ_annual × √(T/252)
        mu_term  = |net_notional_base| × μ_daily × T    (SIGNED — can be ±)

The JavaScript then only needs:
    new_cfar_long  = vol_term * (1 + Δ_vol) - mu_term   [long position]
    new_cfar_short = vol_term * (1 + Δ_vol) + mu_term   [short position]

For a spot rate shift (selected currency only):
    new_cfar = cfar * (1 + Δ_spot)     ← exact, because VaR ∝ E ∝ spot_rate

This keeps all VaR mathematics in Python and keeps the JavaScript as a pure
presentation layer — consistent with the project's separation-of-concerns principle.

For the cumulative period view, vol_term and mu_term are computed using an
exposure-weighted effective T per currency (see _process_cumulative_periods).
This is an approximation for simulation purposes — the actual period_var uses
exact per-position T values in the full covariance matrix.

=== CUMULATIVE PERIOD VIEW (V3) ===

V3 adds a cumulative period filter to the bar chart. For each period, the
frontend receives a 'cumulative_periods' list built by _process_cumulative_periods().
Each period dict contains:
    - period_var: the exact consolidated VaR for positions in this window
    - currencies: list of per-currency dicts with net notional and component_cfar
    - vol_term / mu_term per currency: pre-computed for simulation sliders

The 'cfar' field in each period currency dict is the component VaR (risk
attribution), NOT a simple-sum bucket VaR. Component VaRs across all currencies
in a period sum exactly to the period_var by construction.

=== HEDGE EFFECTIVENESS FORMULA ===

Per-currency, per-bucket hedge effectiveness:

    hedge_effectiveness_pct = (1 - net_var / gross_var_at_bucket_t) × 100

This is mathematically clean because both net_var and gross_var use the SAME
T (bucket midpoint), SAME σ_annual, SAME μ_daily for the same currency.
Since VaR is linear in |E| when T, σ, μ are fixed:

    net_var / gross_var = |net_notional| / Σ|individual_notionals|

The ratio is identical whether computed in VaR or notional space.
Net effect: purely measures within-currency netting (natural hedge).
Nothing else is conflated.

LIMITATION: This metric is WITHIN-BUCKET only. A USD receivable in Bucket 1
and a USD payable in Bucket 2 do NOT net against each other here — they are
in different buckets. Their cross-bucket netting shows up only in the
consolidated V2.4 VaR (via ρ=1 and opposite signed exposures).

=== GROSS STANDALONE RISK — SCOPE (final state) ===

gross_standalone_sum = forwards_standalone_sum + cash_standalone_sum

    forwards_standalone_sum: sum of all Section 3 per-exposure standalone VaRs
                             (bucket midpoint T, no netting, no correlation)
                             Source: var_result['gross_attribution']['exposures']

    cash_standalone_sum:     simple-sum of all cash position VaRs at the FIXED
                             CASH_CONSOLIDATED_T_DAYS horizon (10 trading days)
                             (no diversification — same methodology as forwards)
                             Source: var_result['gross_cash_attribution']['exposures']
                             NOT var_result['spot_risk'] — see below.

This makes gross_standalone_sum scope-consistent with consolidated_var, which
also includes cash + forwards. The risk reduction percentage is therefore
a clean apples-to-apples comparison.

=== WHY CASH HERE USES A FIXED T, NOT cash_horizon ===

cash_standalone_sum is deliberately sourced from
var_result['gross_cash_attribution'] (computed at the fixed
CASH_CONSOLIDATED_T_DAYS = 10, matching the Bucket 1 midpoint convention used
by Bucketed Risk Detail and Consolidated Portfolio VaR) rather than from
var_result['spot_risk']['total_var'] (Section 1's own output, computed at the
user-adjustable Cash VaR Horizon dropdown).

This was a deliberate fix: the Cash VaR Horizon dropdown is documented to the
user as affecting ONLY the standalone Cash Book Risk card. Sourcing this sum
from spot_risk would have made the Gross Standalone Risk stat card — and
therefore Risk Reduction, since Risk Reduction = gross_standalone_sum −
consolidated_var — silently drift whenever the user changed that dropdown,
even though it has nothing to do with those two headline figures. Both now
use the same fixed cash convention as consolidated_var, so changing Cash VaR
Horizon affects the Cash Book Risk card and nothing else on the page.

KNOWN METHODOLOGICAL NOTE: cash VaRs use the fixed CASH_CONSOLIDATED_T_DAYS
(10 days), while forwards use bucket midpoint T (which varies per exposure,
10/42/95/189/315 depending on which bucket it falls in). Both conventions
mirror the exact T values used in the consolidated_var calculation for each
respective position type — see exposure_engine.py's CASH_CONSOLIDATED_T_DAYS
comment for the full rationale. The bucket midpoint approximation for
forwards is separately disclosed in the UI.

=== PORTFOLIO-LEVEL TOTAL RISK REDUCTION ===

    total_risk_reduction = gross_standalone_sum - consolidated_var

where:
    gross_standalone_sum  = sum of all per-position standalone VaRs
                            (forwards at bucket-midpoint T + cash at the
                            fixed CASH_CONSOLIDATED_T_DAYS, both independent
                            of the Cash VaR Horizon dropdown)
                            no netting, no cross-currency diversification
    consolidated_var      = V2.4 portfolio VaR using exact actual-T per position
                            with full cross-currency covariance (min(Ti,Tj))

The reduction combines TWO distinct effects:
    1. Natural hedging (within-currency netting, same bucket)
    2. Diversification benefit (cross-currency imperfect correlation, ρ < 1)
These are not separated at the portfolio level here — the UI labels it clearly
as a combined "Hedge + diversification (approx.)".

KNOWN APPROXIMATION: gross_standalone_sum uses bucket midpoint T for forwards,
while consolidated_var uses each position's actual T. This mismatch slightly
overstates the gross baseline (bucket midpoints can be larger than actual T for
positions early in a bucket), making risk reduction look marginally better than
strict apples-to-apples. This is disclosed in the UI tooltip.
"""

import math
from scipy.stats import norm


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def prepare_dashboard_data(var_result: dict) -> dict:
    """
    Transforms the output of calculate_fx_var() into a chart-ready
    JSON dict for the dashboard frontend.

    This is a PURE TRANSFORMATION function — it contains no financial math
    beyond pre-computing simulation helper values (vol_term, mu_term) from
    the engine's existing output fields. All new computation uses only
    arithmetic on already-computed VaR outputs.

    === OPTION A — CASH INCLUDED IN GROSS BASELINE ===

    gross_standalone_sum now includes cash position standalone VaRs
    (via gross_cash_attribution — computed at the fixed
    CASH_CONSOLIDATED_T_DAYS horizon, deliberately independent of the
    user-adjustable Cash VaR Horizon dropdown / spot_risk's cash_horizon T)
    in addition to the forward standalone VaRs from Section 3.
    This makes the gross baseline scope-consistent with consolidated_var,
    which already includes cash + forwards. See this module's docstring,
    "GROSS STANDALONE RISK — SCOPE (final state)" section above, for the
    full rationale and the bug this fixed.

    Args:
        var_result: The full dict returned by calculate_fx_var().
                    Must contain:
                        'base_ccy':                str
                        'confidence':              float
                        'unified_buckets':         {'buckets': list, 'errors': list}
                        'gross_attribution':       {'exposures': list}
                        'gross_cash_attribution':  {'exposures': list} — cash
                                                    component of the gross
                                                    baseline (Option A above);
                                                    NOT the same as spot_risk
                        'consolidated_var':        {'total_var': float, ...}
                        'spot_risk':               {'total_var': float, ...} —
                                                    used only by the Cash Book
                                                    Risk card; deliberately NOT
                                                    used for gross_standalone_sum
                        'cumulative_vars':         dict — V3: per-period VaR and
                                                    components

    Returns:
        A dict with keys: base_ccy, confidence, z_score, summary, buckets,
        cumulative_periods, simulation_params, errors.
    """
    confidence  = float(var_result.get('confidence', 0.95))
    base_ccy    = var_result.get('base_ccy', '')
    z_score     = float(norm.ppf(confidence))

    # -----------------------------------------------------------------------
    # Step 1: Process each bucket — extract per-currency display data and
    # pre-compute simulation helper values (vol_term, mu_term).
    # These feed the hedge effectiveness table.
    # -----------------------------------------------------------------------
    processed_buckets = []

    for bucket in var_result.get('unified_buckets', {}).get('buckets', []):
        currencies_out = []

        for ccy_data in bucket.get('currencies', []):
            ccy_entry = _process_currency_entry(ccy_data, z_score, bucket['midpoint_days'])
            currencies_out.append(ccy_entry)

        processed_buckets.append({
            'bucket_num':              int(bucket['bucket_num']),
            'bucket_label':            str(bucket['bucket_label']),
            'midpoint_days':           int(bucket['midpoint_days']),
            # covariance-adjusted total CFaR for this bucket (V2.2)
            'bucket_cfar':             round(float(bucket['bucket_var']),              2),
            # simple-sum total for comparison (assumes perfect correlation)
            'bucket_cfar_simple':      round(float(bucket['bucket_var_simple']),       2),
            # reduction from cross-currency imperfect correlation within this bucket
            'diversification_benefit': round(float(bucket['diversification_benefit']), 2),
            'currencies':              currencies_out,
        })

    # -----------------------------------------------------------------------
    # Step 2: Process cumulative period data (V3).
    # Transforms var_result['cumulative_vars'] into the chart-ready period list
    # for the bar chart dropdown filter. Pre-computes vol_term / mu_term per
    # currency per period for the scenario simulation sliders.
    # -----------------------------------------------------------------------
    cumulative_periods = _process_cumulative_periods(
        var_result = var_result,
        z_score    = z_score,
    )

    # -----------------------------------------------------------------------
    # Step 3: Portfolio-level summary numbers.
    # -----------------------------------------------------------------------
    # Consolidated VaR (V2.4): single number using exact individual-position
    # covariance with min(Ti,Tj) cross-horizon terms. Cash + forwards included.
    consolidated_var = float(
        var_result.get('consolidated_var', {}).get('total_var', 0.0)
    )

    # Forwards standalone sum: Section 3 per-exposure VaRs summed with no
    # netting and no diversification. Forwards only, bucket-midpoint T.
    gross_exposures       = var_result.get('gross_attribution', {}).get('exposures', [])
    forwards_standalone   = sum(float(e.get('var', 0.0)) for e in gross_exposures)

    # Cash standalone sum: simple-sum of cash position VaRs at the fixed
    # CASH_CONSOLIDATED_T_DAYS horizon (10 trading days) — NOT cash_horizon.
    # Sourced from gross_cash_attribution (computed in exposure_engine.py at
    # the fixed T) rather than spot_risk (Section 1's own output, computed at
    # the user-adjustable Cash VaR Horizon dropdown). This keeps Gross
    # Standalone Risk — and therefore Risk Reduction — fully independent of
    # that dropdown, which is documented to affect only the Cash Book Risk
    # card. See the module docstring's "WHY CASH HERE USES A FIXED T" section
    # for the full rationale.
    gross_cash_exposures = var_result.get('gross_cash_attribution', {}).get('exposures', [])
    cash_standalone       = sum(float(e.get('var', 0.0)) for e in gross_cash_exposures)

    # Combined gross baseline: forwards (bucket-midpoint T) + cash (fixed
    # CASH_CONSOLIDATED_T_DAYS). No netting anywhere, no diversification
    # benefit applied. Both components are independent of cash_horizon.
    gross_standalone_sum  = forwards_standalone + cash_standalone

    # Total risk reduction: combined benefit of natural hedging AND cross-currency
    # diversification AND cross-horizon netting. Not decomposed here — use the
    # per-bucket hedge_effectiveness_pct for the isolated natural-hedge component.
    # Floored at 0 — cannot show negative risk reduction.
    total_risk_reduction = max(gross_standalone_sum - consolidated_var, 0.0)
    total_risk_reduction_pct = (
        round(total_risk_reduction / gross_standalone_sum * 100, 1)
        if gross_standalone_sum > 0 else 0.0
    )

    # Section 1 cash-only VaR (covariance-adjusted, at user-specified cash_horizon)
    spot_book_var = float(
        var_result.get('spot_risk', {}).get('total_var_cov',
        var_result.get('spot_risk', {}).get('total_var', 0.0))
    )

    # -----------------------------------------------------------------------
    # Step 4: Collect any engine errors to surface in the UI.
    # -----------------------------------------------------------------------
    errors = []
    bucket_errors = var_result.get('unified_buckets', {}).get('errors', [])
    gross_errors  = var_result.get('gross_attribution', {}).get('errors', [])
    for e in bucket_errors + gross_errors:
        msg = e.get('reason', str(e)) if isinstance(e, dict) else str(e)
        if msg not in errors:
            errors.append(msg)

    return {
        'base_ccy':   base_ccy,
        'confidence': round(confidence, 4),
        'z_score':    round(z_score, 6),

        'summary': {
            'consolidated_var':         round(consolidated_var,         2),
            'gross_standalone_sum':     round(gross_standalone_sum,     2),
            'total_risk_reduction':     round(total_risk_reduction,     2),
            'total_risk_reduction_pct': total_risk_reduction_pct,
            'spot_book_var':            round(spot_book_var,            2),
            # Methodology note surfaced in the UI tooltip on the Risk Reduction card.
            # Explains the one remaining known approximation in this metric.
            'methodology_note': (
                "Risk Reduction = gross standalone (all positions, no netting, no correlation) "
                "minus consolidated Portfolio VaR (full netting + correlation). "
                "Gross baseline: forwards use bucket-midpoint T (slight overstatement); "
                "cash uses a fixed 10-trading-day horizon, identical to what Consolidated VaR "
                "uses for cash — so cash contributes no mismatch here. Forwards in Consolidated "
                "VaR use each position's actual settlement T, which is what creates the one "
                "remaining approximation: the gross baseline is very slightly overstated for "
                "forwards. The reduction combines natural hedging (within-currency netting) and "
                "diversification benefit (ρ < 1 across currencies). "
                "See per-currency rows in the hedge table for the isolated natural-hedge component."
            ),
        },

        # Per-bucket data — used by the hedge effectiveness table.
        'buckets': processed_buckets,

        # V3 ADDITION: Cumulative period data for the bar chart dropdown filter.
        # One dict per period key ('1m', '3m', '6m', '12m', 'all').
        # 'cfar' field = component VaR (sums to period_var across all currencies).
        # Ordered as defined by CUMULATIVE_PERIOD_DEFINITIONS in exposure_engine.py.
        'cumulative_periods': cumulative_periods,

        # Slider range configuration. The frontend reads these to set slider
        # min/max, so changing them here automatically updates the UI.
        'simulation_params': {
            'currency_delta_min':  -0.10,   # −10%: selected currency depreciates 10%
            'currency_delta_max':  +0.10,   # +10%: selected currency appreciates 10%
            'vol_delta_min':       -0.25,   # −25%: volatility regime calms significantly
            'vol_delta_max':       +0.25,   # +25%: volatility regime spikes (stress)
        },

        'errors': errors,
    }


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _process_cumulative_periods(var_result: dict, z_score: float) -> list[dict]:
    """
    Transforms var_result['cumulative_vars'] (from exposure_engine.py) into
    the list of chart-ready period dicts consumed by dashboard.js.

    Called once per calculate request during prepare_dashboard_data. Ordered
    as defined by CUMULATIVE_PERIOD_DEFINITIONS ('1m', '3m', '6m', '12m', 'all').

    === TRANSFORMATION PER PERIOD ===

    For each period in cumulative_vars:
        1. For each currency in period['currencies']:
           a. Compute vol_term and mu_term using effective_T (exposure-weighted
              average T from the engine — see calculate_cumulative_period_vars).
              These allow the vol slider to apply without knowing the VaR formula:
                  new_cfar = max(vol_term * (1 + Δ_vol) ∓ mu_term, 0)
           b. Derive net_notional_foreign from base / spot_rate, preserving sign.
           c. Map component_var → cfar (named 'cfar' for simulation compatibility
              with applySimulation() in dashboard.js, which reads ccy.cfar).
        2. Sort currencies by |net_notional_base| descending (largest bars first).
        3. Build the period output dict.

    === SIMULATION APPROXIMATION ===

    vol_term and mu_term use the exposure-weighted effective_T — an approximation
    for currencies with multiple positions at different horizons within the same
    period. The actual period_var uses exact per-position T via the full covariance
    matrix. The approximation only affects the slider simulation (live preview).
    Period VaR shown in the info strip is always the pre-computed exact value.

    Args:
        var_result: Full output dict from calculate_fx_var(). Must contain
                    'cumulative_vars' as added by V3.
        z_score:    norm.ppf(confidence) — must be the SAME value used when
                    computing the period VaRs, to keep vol_term consistent.

    Returns:
        List of period dicts ordered ['1m', '3m', '6m', '12m', 'all'].
        Each dict:
        {
            'key':          str,         — '3m'
            'label':        str,         — 'Next 3 months'
            'period_var':   float,       — exact consolidated VaR for this window
            'max_days':     int|None,    — T cutoff in trading days (None = all)
            'n_positions':  int,         — individual positions included
            'currencies':   list[dict],  — sorted by |net_notional_base| desc
        }

        Each currency dict:
        {
            'currency':             str,     — 'USD'
            'net_notional_base':    float,   — |net signed base|
            'net_notional_foreign': float,   — signed net in foreign ccy
            'net_direction':        str,     — 'long', 'short', 'flat'
            'cfar':                 float,   — component VaR (risk attribution bar)
            'spot_rate':            float,   — base per 1 foreign
            'annualised_vol':       float,   — σ_annual
            'annualised_vol_pct':   float,   — for display as "4.56%"
            'daily_mean':           float,   — μ_daily
            'effective_T':          float,   — exposure-weighted avg T
            'vol_term':             float,   — pre-computed for vol slider
            'mu_term':              float,   — pre-computed for vol slider (signed)
        }
    """
    # Canonical order for period output — must match CUMULATIVE_PERIOD_DEFINITIONS
    PERIOD_ORDER = ['1m', '3m', '6m', '12m', 'all']

    cumulative_vars = var_result.get('cumulative_vars', {})
    periods_out     = []

    for key in PERIOD_ORDER:
        pdata = cumulative_vars.get(key)
        if pdata is None:
            # This period key is missing — skip silently.
            # In practice this shouldn't happen (all 5 keys are always produced),
            # but the defensive check keeps this robust against engine changes.
            continue

        currencies_out = []

        for ccy, cdata in pdata.get('currencies', {}).items():
            net_notional_base = float(cdata.get('net_notional_base', 0.0))
            ann_vol           = float(cdata.get('ann_vol',            0.0))
            daily_mean        = float(cdata.get('daily_mean',         0.0))
            effective_T       = float(cdata.get('effective_T',        1.0))
            net_direction     = str(cdata.get('net_direction',        'flat'))
            component_cfar    = float(cdata.get('component_var',      0.0))
            spot_rate         = float(cdata.get('spot_rate',          1.0))

            # --- Pre-compute simulation vol_term and mu_term ---
            # Uses exposure-weighted effective_T as the representative horizon.
            # This is an approximation — the exact period VaR uses per-position T.
            # See module docstring and _compute_component_vars_by_currency for details.
            #
            # σ_T = σ_annual × √(effective_T / 252)   [horizon-scaled volatility]
            sigma_T  = ann_vol * math.sqrt(effective_T / 252.0) if effective_T > 0 else 0.0
            #
            # vol_term = |net_notional_base| × Z × σ_T   [pure vol contribution to CFaR]
            # Scaled by (1 + Δ_vol) in JS simulation, with mu_term held constant.
            vol_term = net_notional_base * z_score * sigma_T
            #
            # mu_term = |net_notional_base| × μ_daily × effective_T  (SIGNED)
            # Positive μ (FCY appreciating) reduces CFaR for long positions.
            # For short positions it increases CFaR. JavaScript uses:
            #   new_cfar_long  = max(vol_term * (1 + Δv) − mu_term, 0)
            #   new_cfar_short = max(vol_term * (1 + Δv) + mu_term, 0)
            mu_term  = net_notional_base * daily_mean * effective_T

            # --- Net notional in foreign currency ---
            # Derive from base / spot_rate, preserving the direction sign.
            if spot_rate > 0:
                net_notional_foreign_abs = net_notional_base / spot_rate
            else:
                net_notional_foreign_abs = 0.0
            # Apply direction sign: short positions have negative foreign notional
            net_notional_foreign = (
                -net_notional_foreign_abs if net_direction == 'short'
                else net_notional_foreign_abs
            )

            currencies_out.append({
                'currency':             ccy,
                'net_notional_base':    round(net_notional_base,         2),
                'net_notional_foreign': round(net_notional_foreign,       2),
                'net_direction':        net_direction,
                # 'cfar': named to match the field expected by applySimulation() in
                # dashboard.js — for period currencies this is the component VaR
                # (risk attribution) rather than the per-bucket net VaR.
                # Component VaRs sum to period_var across all currencies.
                'cfar':                 round(component_cfar,            2),
                'spot_rate':            round(spot_rate,                  6),
                'annualised_vol':       round(ann_vol,                    6),
                # annualised_vol_pct: for display as "4.56%"
                'annualised_vol_pct':   round(ann_vol * 100.0,            2),
                'daily_mean':           round(daily_mean,                 8),
                'effective_T':          round(effective_T,                1),
                # Simulation components (pre-computed for exact client-side vol slider)
                'vol_term':             round(vol_term,                   2),
                'mu_term':              round(mu_term,                    6),
            })

        # Sort by absolute net notional descending — largest exposure bars appear first
        currencies_out.sort(key=lambda x: x['net_notional_base'], reverse=True)

        periods_out.append({
            'key':         key,
            'label':       str(pdata.get('label', key)),
            'period_var':  round(float(pdata.get('period_var', 0.0)), 2),
            'max_days':    pdata.get('max_days'),          # None for 'all'
            'n_positions': int(pdata.get('n_positions', 0)),
            'currencies':  currencies_out,
        })

    return periods_out


def _process_currency_entry(ccy_data: dict, z_score: float, midpoint_days: int) -> dict:
    """
    Processes a single currency dict from a bucket's 'currencies' list into
    the chart-ready format, including pre-computed simulation helper values.

    Called once per (bucket, currency) pair during prepare_dashboard_data.

    === SIMULATION COMPONENT PRE-COMPUTATION ===

    The CFaR formula for a single net currency position is:

        CFaR_long  = |E| × (Z × σ_T − μ_T)   [fear FCY depreciation]
        CFaR_short = |E| × (Z × σ_T + μ_T)   [fear FCY appreciation]

    Decomposing:
        vol_term = |E| × Z × σ_T    where σ_T = σ_annual × √(T/252)
        mu_term  = |E| × μ_daily × T             (SIGNED: inherits sign of μ_daily)

    With these two values stored, JavaScript can compute exact new CFaR for any
    volatility shift Δ_vol WITHOUT knowing the VaR formula:

        new_CFaR_long  = vol_term × (1 + Δ_vol) − mu_term
        new_CFaR_short = vol_term × (1 + Δ_vol) + mu_term
        (floor both at 0 — VaR cannot be negative)

    For a spot rate shift Δ_spot on the selected currency:
        new_CFaR = CFaR × (1 + Δ_spot)       [exact, because VaR ∝ E ∝ spot_rate]

    WHY IS vol_term ALONE USED FOR THE VOL SLIDER, NOT cfar × (1 + Δ)?
        cfar = vol_term − mu_term (for long)
        cfar × (1 + Δ) = vol_term × (1 + Δ) − mu_term × (1 + Δ)   ← WRONG
        Correct:         vol_term × (1 + Δ) − mu_term                ← mu_term unchanged

    The drift term (mu_term) does NOT scale with volatility — it is driven by
    the historical daily return mean, which is independent of the vol regime.
    So scaling cfar by (1 + Δ) overstates the drift effect for large Δ.
    The decomposed formula is exact.

    === HEDGE EFFECTIVENESS ===

    hedge_effectiveness_pct = (1 − net_var / gross_var_at_bucket_t) × 100

    Only meaningful (and non-zero) when gross_var > net_var, i.e. there is at
    least one offsetting position in this currency bucket.
    When net_direction == 'flat' (perfectly hedged): net_var = 0, effectiveness = 100%.

    === SGD -0 DISPLAY FIX ===

    hedge_benefit is floored at 0.0 before rounding to prevent floating-point
    noise from producing display artifacts like "SGD -0". Theoretically,
    hedge_benefit = gross_var - net_var ≥ 0 always (net VaR ≤ gross VaR by
    construction), but floating-point arithmetic can produce tiny negatives
    (e.g. -0.0003) that round to -0.00. max(benefit, 0.0) eliminates this.

    Args:
        ccy_data:      Single currency dict from the engine's bucket output.
                       Expected keys: 'currency', 'net_notional_base',
                       'net_notional_foreign', 'net_direction', 'net_var',
                       'gross_var_at_bucket_t', 'hedge_benefit', 'spot_rate',
                       'annualised_vol', 'daily_mean'.
        z_score:       norm.ppf(confidence) — the confidence-level Z multiplier.
        midpoint_days: Bucket midpoint T in trading days (e.g. 10 for Bucket 1).

    Returns:
        Dict with all fields needed by the chart and simulation layer.
    """
    T                  = midpoint_days
    ann_vol            = float(ccy_data.get('annualised_vol',         0.0))
    daily_mean         = float(ccy_data.get('daily_mean',             0.0))
    net_notional_base  = float(ccy_data.get('net_notional_base',      0.0))
    net_var            = float(ccy_data.get('net_var',                0.0))
    gross_var          = float(ccy_data.get('gross_var_at_bucket_t',  0.0))
    net_direction      = str(ccy_data.get('net_direction',            'flat'))

    # --- Pre-compute simulation components ---

    # σ_T: annualised vol scaled to the bucket horizon
    # σ_T = σ_annual × √(T / 252)
    sigma_T   = ann_vol * math.sqrt(T / 252.0) if T > 0 else 0.0

    # vol_term: the pure volatility contribution to CFaR (always non-negative)
    # = |net_notional_base| × Z × σ_T
    vol_term  = net_notional_base * z_score * sigma_T

    # mu_term: the drift contribution (SIGNED — inherits sign of μ_daily)
    # = |net_notional_base| × μ_daily × T
    # Positive μ_daily means FCY is appreciating vs base (drifting upward).
    # This REDUCES CFaR for long positions (drift offsets downside risk).
    # This INCREASES CFaR for short positions (drift works against you).
    mu_term   = net_notional_base * daily_mean * T

    # --- Hedge effectiveness ---
    # Clean ratio when gross_var > 0 (there was at least one offsetting position).
    # For flat currencies (net = 0, gross > 0): hedge_effectiveness = 100%.
    # For unhedged currencies (only one direction, no offset): = 0%.
    if gross_var > 0:
        hedge_effectiveness_pct = round((1.0 - net_var / gross_var) * 100.0, 1)
    else:
        # gross_var = 0 only if there were no positions — shouldn't reach this
        # branch since the engine only includes currencies with data, but guard.
        hedge_effectiveness_pct = 0.0

    return {
        'currency':                str(ccy_data.get('currency',              '')),
        # Signed net notional in foreign currency (positive = net long = net receivable)
        'net_notional_foreign':    round(float(ccy_data.get('net_notional_foreign', 0.0)), 2),
        # Unsigned net notional in base currency (absolute value — for bar height)
        'net_notional_base':       round(net_notional_base,                  2),
        # Direction of net position
        'net_direction':           net_direction,
        # CFaR on the net position (covariance-adjusted if bucket has 2+ currencies)
        'cfar':                    round(net_var,                            2),
        # Sum of standalone CFaRs (no netting) at bucket T — the gross baseline
        'gross_cfar':              round(gross_var,                          2),
        # Natural hedge benefit = gross_cfar - cfar (the VaR saved by netting).
        # Floored at 0.0 before rounding to prevent floating-point noise producing
        # display artefacts like "SGD -0" (see docstring for explanation).
        'hedge_benefit':           round(max(float(ccy_data.get('hedge_benefit', 0.0)), 0.0), 2),
        # Hedge effectiveness as a percentage of gross CFaR eliminated by netting
        'hedge_effectiveness_pct': hedge_effectiveness_pct,
        # Market data (for display and info tooltips)
        'spot_rate':               round(float(ccy_data.get('spot_rate',    0.0)), 6),
        'annualised_vol':          round(ann_vol,                            6),
        # annualised_vol_pct: for display as "4.56%"
        'annualised_vol_pct':      round(ann_vol * 100.0,                    2),
        'daily_mean':              round(daily_mean,                         8),
        'midpoint_days':           int(T),
        # --- Simulation components (pre-computed for exact client-side vol slider) ---
        # JavaScript uses: new_cfar = vol_term*(1+Δv) ∓ mu_term (∓ depends on direction)
        # See module docstring and _process_currency_entry docstring for full derivation.
        'vol_term':                round(vol_term,                           2),
        'mu_term':                 round(mu_term,                            6),  # small, keep precision
    }
