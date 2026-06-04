"""
exposure_engine.py — V2 Future FX Exposure VaR Engine

This module extends the V1 cash-position risk engine to handle forward-looking
FX risk from future payables and receivables (e.g. 'MYR 8mn payable 20-Aug-2026').

=== ARCHITECTURE AND ABSTRACTION LAYERS ===

The dependency chain is strictly one-directional:

    test_v2_engine.py  →  exposure_engine.py  →  var_engine.py
    app.py             ↗

exposure_engine.py imports FROM var_engine.py but is never imported BY it.
var_engine.py has no knowledge that exposure_engine.py exists.
This means:
  - To upgrade the math (e.g. Monte Carlo for V3): only touch var_engine.py.
  - To add new exposure types or change bucketing logic: only touch this file.
  - The Flask app and HTML frontend are completely insulated from both.

=== WHAT THIS MODULE ADDS OVER V1 ===

V1 (var_engine.py) only handles cash positions:
  - All positions are LONG (holder fears FCY depreciation)
  - User specifies the VaR horizon T (e.g. 30 days)
  - VaR computed independently per position, summed

V2 (this module) adds future exposures on top:
  - Positions can be LONG (receivable) or SHORT (payable)
  - Each exposure has a settlement date — VaR horizon T = trading days to settlement
  - Positions are grouped into time buckets and NETTED within each bucket/currency
    before VaR is computed → captures natural hedging benefit

=== THREE-LAYER OUTPUT ===

LAYER 1a — Spot risk (T = cash_horizon, single clear horizon):
  Spot VaR total:        Meaningful sum — all cash positions use same T.

LAYER 1b — Forward risk per bucket (each bucket has its own T):
  Per-bucket net VaR:    One net VaR per time bucket. Each bucket has a
                         bucket_var that is a meaningful sum (all currencies
                         within the bucket share the same midpoint T).
                         There is NO combined total across buckets since
                         T=42, T=95, T=189 cannot be meaningfully added.
  Natural hedge benefit: Embedded per currency within each bucket as
                         hedge_benefit = gross_var_at_bucket_t − net_var.
                         Shown at bucket/currency level, not aggregated.

  DELIBERATELY REMOVED: Combined total_var (spot + forward) and a single
  natural_hedge_benefit total. Summing VaRs across different time horizons
  conflates 1-day, 42-day, and 95-day risks into a misleading single number.

LAYER 2 — Bucket attribution (what drives the risk):
  Per bucket, per currency: net notional, net direction, net VaR
  Per position within each bucket: standalone contribution at bucket T
  This shows exactly which obligations are unhedged and should be targeted
  for explicit hedging.

LAYER 3 — Net currency summary (informational, NOT used for VaR):
  Per currency: cash + receivables − payables = net economic position
  Different time horizons make netting for VaR purposes ambiguous, so this
  is provided as context only.

=== NATURAL HEDGING BENEFIT — KEY CONCEPT ===

The natural hedge benefit is computed as:
  Gross Forward VaR (V2): each exposure's VaR computed at BUCKET MIDPOINT T,
                          independently, all positive, summed
  Net Forward VaR (V2.3): VaR computed on NET notional per bucket/currency
                          at BUCKET MIDPOINT T

Both use the same T (bucket midpoint) so the only difference is netting vs not.
The benefit = Gross − Net ≥ 0 always, since netting reduces or maintains notional.

Note: Cash positions are kept separate and are NOT included in the natural hedge
benefit calculation. Natural hedging only applies within forward exposure buckets
(same currency, same time window). Cash vs forward netting involves different
time horizons and is left for V2.3+ with explicit treasury policy assumptions.
"""

import numpy as np
import pandas as pd
from datetime import date, datetime
from collections import defaultdict
from var_engine import (
    fetch_pair_returns,
    calculate_parametric_var,
    calculate_portfolio_var,
    TRADING_DAYS_PER_YEAR,
)


# =============================================================================
# CONSTANTS
# =============================================================================

# Standard time buckets used throughout the industry for FX exposure management.
# Each bucket has a label, inclusive min (trading days), exclusive max, and
# a midpoint used as the VaR horizon T for all positions within that bucket.
#
# MIDPOINT RATIONALE: Using the bucket midpoint as T rather than each exposure's
# actual settlement days means all positions in the same bucket use the same T,
# which makes netting mathematically consistent. The tradeoff is slight
# approximation for positions near bucket edges. For production, the
# exposure-weighted average settlement day within each bucket would be more precise.
#
# Bucket 5 (>12 months) uses 315 trading days as midpoint (≈ 15 months, midpoint
# of a 12–18 month range). Exposures beyond 18 months are included but this T
# approximation becomes less accurate for very long-dated obligations.
BUCKET_DEFINITIONS = [
    {'num': 1, 'label': '0–1 month',   'min_days': 0,   'max_days': 21,   'midpoint_days': 10},
    {'num': 2, 'label': '1–3 months',  'min_days': 21,  'max_days': 63,   'midpoint_days': 42},
    {'num': 3, 'label': '3–6 months',  'min_days': 63,  'max_days': 126,  'midpoint_days': 95},
    {'num': 4, 'label': '6–12 months', 'min_days': 126, 'max_days': 252,  'midpoint_days': 189},
    {'num': 5, 'label': '>12 months',  'min_days': 252, 'max_days': None, 'midpoint_days': 315},
]

# Valid direction strings for future exposures.
# 'receivable': you WILL RECEIVE this FCY on settlement date → long FCY
# 'payable':    you NEED TO PAY this FCY on settlement date → short FCY
VALID_DIRECTIONS = {'receivable', 'payable'}


# =============================================================================
# DATE AND BUCKET UTILITIES
# =============================================================================

def count_trading_days(settlement_date: str | date) -> int:
    """
    Counts the number of trading days (Monday–Friday) from today to the
    given settlement date, inclusive of today and exclusive of the settlement date.

    Uses pandas bdate_range which counts Mon–Fri business days.
    NOTE: This does NOT account for public holidays — in production a market-
    specific holiday calendar (e.g. SGX calendar for SGD-based companies) should
    be used. For a PoC, Mon–Fri counting is an acceptable approximation.

    Args:
        settlement_date: Future settlement date, as 'YYYY-MM-DD' string or
                         Python date object.

    Returns:
        Integer number of trading days from today to settlement_date.
        Returns 0 if settlement_date is today or in the past (no future risk).

    Example:
        If today is 2026-06-01 and settlement_date is 2026-08-15:
        → pd.bdate_range('2026-06-01', '2026-08-15', inclusive='left') → ~53 days
    """
    today = date.today()

    if isinstance(settlement_date, str):
        settlement_date = datetime.strptime(settlement_date, '%Y-%m-%d').date()

    if settlement_date <= today:
        return 0

    # pd.bdate_range generates a DatetimeIndex of business days in the half-open
    # interval [today, settlement_date). The length is the number of trading days.
    bdays = pd.bdate_range(start=today, end=settlement_date, inclusive='left')
    return len(bdays)


def parse_settlement_date(date_str: str) -> date:
    """
    Parses a settlement date string in 'YYYY-MM-DD' format into a Python date.

    Args:
        date_str: Date string in 'YYYY-MM-DD' format (e.g. '2026-12-31').

    Returns:
        Python date object.

    Raises:
        ValueError with a human-readable message if format is invalid.
    """
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(
            f"Invalid date format '{date_str}'. "
            "Please use YYYY-MM-DD format (e.g. '2026-12-31')."
        )


def assign_to_bucket(trading_days: int) -> dict:
    """
    Assigns a trading day count to the appropriate time bucket from
    BUCKET_DEFINITIONS.

    The bucket is determined by the min_days <= trading_days < max_days rule.
    Bucket 5 has no max_days (open-ended), so any position with 252+ days
    lands there.

    Args:
        trading_days: Number of trading days from today to settlement.

    Returns:
        The matching bucket dict from BUCKET_DEFINITIONS, e.g.:
        {'num': 2, 'label': '1–3 months', 'min_days': 21,
         'max_days': 63, 'midpoint_days': 42}
    """
    for bucket in BUCKET_DEFINITIONS:
        if bucket['max_days'] is None or trading_days < bucket['max_days']:
            if trading_days >= bucket['min_days']:
                return bucket
    # Fallback to last bucket (should only hit if trading_days exactly = max of last bucket)
    return BUCKET_DEFINITIONS[-1]


# =============================================================================
# MARKET DATA BATCH FETCHER (CACHE)
# =============================================================================

def fetch_market_data_batch(
    currencies: list[str],
    base_ccy:   str,
    period:     str = '1y'
) -> dict[str, dict]:
    """
    Fetches market data for all unique foreign currencies in a single pass,
    deduplicating requests so each (foreign_ccy, base_ccy) pair is only
    fetched ONCE regardless of how many positions use it.

    Without this cache, a portfolio with three USD exposures would hit Yahoo
    Finance three times for the same USDSGD=X data — wasteful and slow.

    This is an important V2 addition: V1 (calculate_portfolio_var in var_engine.py)
    fetches once per position since cash positions don't share currencies in the
    same way. V2 has multiple exposures potentially in the same currency across
    different settlement dates — the cache eliminates redundant fetches.

    Args:
        currencies: List of foreign currency ISO codes (may contain duplicates).
        base_ccy:   The company's home currency (e.g. 'SGD').
        period:     yfinance lookback period (e.g. '1y'). Passed to fetch_pair_returns.

    Returns:
        Dict mapping each foreign currency ISO code to a dict with:
            'ann_vol'        (float):  σ_annual for the pair
            'daily_mean'     (float):  μ_daily for the pair
            'spot_rate'      (float):  current spot rate (base per 1 foreign)
            'used_cross_rate' (bool):  True if synthetic USD route was used
            'error'          (str|None): error message if fetch failed, else None

        Currencies equal to base_ccy are excluded (no FX risk).
        Failed fetches are stored with 'error' set so callers can report them.
    """
    market_data = {}

    # Deduplicate — fetch each unique foreign currency only once
    unique_currencies = set(c.upper().strip() for c in currencies
                            if c.upper().strip() != base_ccy.upper())

    for ccy in unique_currencies:
        try:
            ann_vol, daily_mean, _, spot_rate, used_cross = fetch_pair_returns(
                ccy, base_ccy, period
            )
            market_data[ccy] = {
                'ann_vol':         ann_vol,
                'daily_mean':      daily_mean,
                'spot_rate':       spot_rate,
                'used_cross_rate': bool(used_cross),
                'error':           None,
            }
        except Exception as e:
            market_data[ccy] = {
                'ann_vol':         None,
                'daily_mean':      None,
                'spot_rate':       None,
                'used_cross_rate': False,
                'error':           str(e),
            }

    return market_data


# =============================================================================
# GROSS FORWARD VAR — V2 independent per-exposure logic
# =============================================================================

def calculate_gross_forward_var(
    exposures:   list[dict],
    base_ccy:    str,
    market_data: dict,
    confidence:  float,
    use_bucket_t: bool = True
) -> tuple[float, list[dict], list[dict]]:
    """
    Computes VaR for each future exposure INDEPENDENTLY, with no netting.
    This is the V2 gross logic used exclusively for the natural hedging
    benefit calculation in the Layer 1 headline.

    IMPORTANT: use_bucket_t=True means each exposure uses its BUCKET's midpoint
    T rather than its own actual trading days to settlement. This is intentional —
    the natural hedging benefit is computed as:

        benefit = gross (at bucket T) − net (at bucket T)

    Using the same T for both sides makes the benefit calculation purely a
    function of netting, not of T differences. If use_bucket_t=False, each
    exposure uses its actual T, which gives the true V2 per-exposure VaR but
    makes the natural hedging benefit comparison less clean.

    Args:
        exposures:    List of exposure dicts (currency, amount, settlement_date,
                      direction).
        base_ccy:     Company home currency.
        market_data:  Pre-fetched market data from fetch_market_data_batch().
                      Avoids re-fetching the same pair multiple times.
        confidence:   VaR confidence level.
        use_bucket_t: If True (default), use bucket midpoint T for each exposure.
                      If False, use each exposure's actual trading_days T.

    Returns:
        A tuple of:
            - total_gross_var (float): sum of all per-exposure VaRs
            - results (list[dict]):    per-exposure breakdown (see fields below)
            - errors  (list[dict]):    exposures that failed, with reasons

        Each result dict contains:
            'currency', 'amount', 'direction', 'settlement_date',
            'actual_trading_days' (T to settlement),
            'bucket_num', 'bucket_label', 'bucket_midpoint_days' (T used),
            'spot_rate', 'exposure_base', 'annualised_vol', 'daily_mean',
            'annualised_mean', 'var', 'var_raw', 'var_was_floored',
            'used_cross_rate', 'drift_warning', 'near_term_warning'
    """
    results   = []
    errors    = []
    total_var = 0.0

    for exp in exposures:
        ccy       = exp['currency'].upper().strip()
        direction = exp['direction'].lower().strip()
        amount    = float(exp['amount'])
        settle    = exp['settlement_date']

        # Skip base currency positions — no FX risk
        if ccy == base_ccy.upper():
            continue

        # Validate direction
        if direction not in VALID_DIRECTIONS:
            errors.append({'currency': ccy, 'settlement_date': str(settle),
                           'reason': f"Invalid direction '{direction}'"})
            continue

        # Look up pre-fetched market data
        md = market_data.get(ccy)
        if md is None or md['error'] is not None:
            errors.append({'currency': ccy, 'settlement_date': str(settle),
                           'reason': md['error'] if md else 'Not fetched'})
            continue

        # Compute trading days to settlement
        actual_days = count_trading_days(settle)
        if actual_days < 1:
            errors.append({'currency': ccy, 'settlement_date': str(settle),
                           'reason': 'Settlement date is today or in the past.'})
            continue

        # Assign to bucket and select T
        bucket    = assign_to_bucket(actual_days)
        t_to_use  = bucket['midpoint_days'] if use_bucket_t else actual_days

        # Convert to base currency: exposure_base = amount × spot_rate
        exposure_base = amount * md['spot_rate']

        # direction='payable' → 'short' in calculate_parametric_var (right tail)
        # direction='receivable' → 'long' (left tail)
        var_direction = 'short' if direction == 'payable' else 'long'

        var_floored, var_raw = calculate_parametric_var(
            exposure_amount    = exposure_base,
            annualised_vol     = md['ann_vol'],
            daily_mean_return  = md['daily_mean'],
            confidence_level   = confidence,
            days               = t_to_use,
            direction          = var_direction,
        )

        total_var      += var_floored
        ann_mean        = md['daily_mean'] * TRADING_DAYS_PER_YEAR
        settle_str      = settle if isinstance(settle, str) else settle.strftime('%Y-%m-%d')

        results.append({
            'currency':              ccy,
            'amount':                amount,
            'direction':             direction,
            'settlement_date':       settle_str,
            'actual_trading_days':   int(actual_days),
            'bucket_num':            bucket['num'],
            'bucket_label':          bucket['label'],
            'bucket_midpoint_days':  bucket['midpoint_days'],
            't_used':                int(t_to_use),
            'spot_rate':             round(float(md['spot_rate']),    6),
            'exposure_base':         round(float(exposure_base),      2),
            'annualised_vol':        round(float(md['ann_vol']),      6),
            'daily_mean':            round(float(md['daily_mean']),   8),
            'annualised_mean':       round(float(ann_mean),           4),
            'var':                   round(float(var_floored),        2),
            'var_raw':               round(float(var_raw),            2),
            'var_was_floored':       bool(var_raw < 0),
            'used_cross_rate':       bool(md['used_cross_rate']),
            # drift_warning: annualised drift > 10% signals a structural trend
            # that is large enough to materially affect VaR. The normality
            # assumption may also understate tail risk for such currencies.
            'drift_warning':         bool(abs(ann_mean) > 0.10),
            # near_term_warning: very short horizons (< 5 trading days) produce
            # VaR figures dominated by noise and may not be meaningful for hedging.
            'near_term_warning':     bool(actual_days < 5),
        })

    return round(float(total_var), 2), results, errors


# =============================================================================
# BUCKETED NET FORWARD VAR — V2.3 netting logic
# =============================================================================

def calculate_bucketed_forward_var(
    exposures:   list[dict],
    base_ccy:    str,
    market_data: dict,
    confidence:  float
) -> tuple[float, list[dict], list[dict]]:
    """
    Computes parametric VaR on NET notional per time bucket per currency.
    This is the V2.3 headline risk figure — the number the user should report
    and manage against, since it correctly accounts for natural hedging between
    offsetting positions in the same currency and time window.

    === HOW NETTING WORKS ===

    For each (bucket, currency) group:
      1. All receivables contribute a POSITIVE signed notional (long FCY).
      2. All payables contribute a NEGATIVE signed notional (short FCY).
      3. Sum signed notionals to get net notional for that bucket/currency.
      4. If net > 0: fear depreciation → direction='long'
         If net < 0: fear appreciation → direction='short'
         If net ≈ 0: perfectly hedged → VaR = 0
      5. Convert |net_notional| to base currency at spot rate.
      6. Compute VaR on |net_notional_base| at bucket midpoint T with correct direction.

    === WHY THIS GIVES LOWER VaR THAN GROSS ===

    Example: bucket 2 USD, recv 2mn (long) + payable 1mn (short)
      Gross (independent): VaR(2mn, long) + VaR(1mn, short) = sum of both
      Net (this function):  VaR(1mn, long)                    = net long 1mn
      Benefit: VaR(1mn gross) saved by natural hedge

    The natural hedging benefit = gross − net is always ≥ 0.

    === ATTRIBUTION WITHIN EACH BUCKET/CURRENCY GROUP ===

    For each individual exposure within a bucket/currency group, we compute:
      'standalone_var_at_bucket_t': VaR on this exposure alone at bucket T.
    This is used in Layer 2 of the output to show which specific obligations
    are the largest risk drivers and should be targeted for hedging.

    Args:
        exposures:   List of future exposure dicts.
        base_ccy:    Company home currency.
        market_data: Pre-fetched market data from fetch_market_data_batch().
        confidence:  VaR confidence level.

    Returns:
        A tuple of:
            - total_net_var  (float):      sum of all bucket net VaRs
            - bucket_results (list[dict]): one dict per populated bucket
            - errors         (list[dict]): exposures that could not be processed

        Each bucket dict contains:
            'bucket_num', 'bucket_label', 'midpoint_days', 'bucket_var',
            'currencies': list of per-currency dicts, each with:
                'currency', 'net_notional_foreign', 'net_notional_base',
                'net_direction', 'midpoint_days', 'net_var', 'net_var_raw',
                'var_was_floored', 'drift_warning', 'used_cross_rate',
                'gross_var_at_bucket_t' (sum of standalones, for benefit calc),
                'hedge_benefit' (gross − net for this currency/bucket),
                'positions': list of individual exposure attributions
    """
    # -------------------------------------------------------------------
    # Step 1: Group valid exposures by (bucket_num, currency)
    # -------------------------------------------------------------------
    # buckets_data[bucket_num][currency] = list of position dicts
    buckets_data: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    errors = []

    for exp in exposures:
        ccy       = exp['currency'].upper().strip()
        direction = exp['direction'].lower().strip()
        amount    = float(exp['amount'])
        settle    = exp['settlement_date']

        if ccy == base_ccy.upper():
            continue

        if direction not in VALID_DIRECTIONS:
            errors.append({'currency': ccy, 'settlement_date': str(settle),
                           'reason': f"Invalid direction '{direction}'"})
            continue

        md = market_data.get(ccy)
        if md is None or md['error'] is not None:
            continue  # Already reported in gross errors, skip silently here

        actual_days = count_trading_days(settle)
        if actual_days < 1:
            continue

        bucket = assign_to_bucket(actual_days)

        # Signed notional in FOREIGN currency:
        # receivable = positive (long FCY), payable = negative (short FCY)
        signed_amount = amount if direction == 'receivable' else -amount

        settle_str = settle if isinstance(settle, str) else settle.strftime('%Y-%m-%d')

        buckets_data[bucket['num']][ccy].append({
            'signed_amount': signed_amount,
            'actual_days':   actual_days,
            'direction':     direction,
            'settle_str':    settle_str,
            'md':            md,
            'bucket':        bucket,
        })

    # -------------------------------------------------------------------
    # Step 2: Compute net VaR per bucket per currency
    # -------------------------------------------------------------------
    bucket_results = []
    total_net_var  = 0.0

    for bucket_def in BUCKET_DEFINITIONS:
        bnum = bucket_def['num']
        if bnum not in buckets_data:
            continue  # No exposures in this bucket — skip

        T = bucket_def['midpoint_days']  # Common horizon for all in this bucket
        bucket_currency_results = []
        bucket_var = 0.0

        for ccy, positions in buckets_data[bnum].items():
            md = positions[0]['md']  # Same currency → same market data

            # --- Net notional in foreign currency ---
            # Sum all signed amounts: receivables add (+), payables subtract (-)
            net_notional_foreign = sum(p['signed_amount'] for p in positions)

            # --- Handle flat (perfectly hedged) case ---
            if abs(net_notional_foreign) < 0.01:
                # Receivables and payables cancel exactly — no net FX risk.
                # Compute standalone VaRs for attribution but net VaR = 0.
                position_details = _build_position_details(positions, md, T, confidence, ccy)
                gross_sum = sum(p['standalone_var_at_bucket_t'] for p in position_details)
                ann_mean  = md['daily_mean'] * TRADING_DAYS_PER_YEAR

                bucket_currency_results.append({
                    'currency':               ccy,
                    'net_notional_foreign':   0.0,
                    'net_notional_base':      0.0,
                    'net_direction':          'flat',
                    'midpoint_days':          T,
                    'spot_rate':              round(float(md['spot_rate']),  6),
                    'annualised_vol':         round(float(md['ann_vol']),    6),
                    'daily_mean':             round(float(md['daily_mean']), 8),
                    'annualised_mean':        round(float(ann_mean),         4),
                    'net_var':                0.0,
                    'net_var_raw':            0.0,
                    'var_was_floored':        False,
                    'drift_warning':          bool(abs(ann_mean) > 0.10),
                    'used_cross_rate':        bool(md['used_cross_rate']),
                    'gross_var_at_bucket_t':  round(float(gross_sum), 2),
                    'hedge_benefit':          round(float(gross_sum), 2),  # 100% hedged
                    'positions':              position_details,
                })
                continue

            # --- Convert net notional to base currency ---
            # exposure_base = |net_notional| × spot_rate (base per foreign)
            net_notional_base = abs(net_notional_foreign) * md['spot_rate']

            # --- Direction from sign of net notional ---
            # Net positive (more receivables than payables): fear FCY depreciates → long
            # Net negative (more payables than receivables): fear FCY appreciates → short
            net_direction = 'long' if net_notional_foreign > 0 else 'short'

            # --- Compute VaR on net notional at bucket midpoint T ---
            var_floored, var_raw = calculate_parametric_var(
                exposure_amount    = net_notional_base,
                annualised_vol     = md['ann_vol'],
                daily_mean_return  = md['daily_mean'],
                confidence_level   = confidence,
                days               = T,
                direction          = net_direction,
            )

            # --- Per-position attribution ---
            # Compute each position's standalone VaR at bucket T for Layer 2.
            # Using bucket T (not actual days) for consistency — same T as net VaR.
            position_details = _build_position_details(positions, md, T, confidence, ccy)
            gross_sum = sum(p['standalone_var_at_bucket_t'] for p in position_details)

            # Natural hedge benefit for this specific currency/bucket:
            # How much VaR was saved by netting vs computing gross independently
            hedge_benefit = gross_sum - float(var_floored)

            ann_mean = md['daily_mean'] * TRADING_DAYS_PER_YEAR
            bucket_var += float(var_floored)

            bucket_currency_results.append({
                'currency':               ccy,
                'net_notional_foreign':   round(float(net_notional_foreign), 2),
                'net_notional_base':      round(float(net_notional_base),    2),
                'net_direction':          net_direction,
                'midpoint_days':          T,
                'spot_rate':              round(float(md['spot_rate']),    6),
                'annualised_vol':         round(float(md['ann_vol']),      6),
                'daily_mean':             round(float(md['daily_mean']),   8),
                'annualised_mean':        round(float(ann_mean),           4),
                'net_var':                round(float(var_floored),        2),
                'net_var_raw':            round(float(var_raw),            2),
                'var_was_floored':        bool(var_raw < 0),
                'drift_warning':          bool(abs(ann_mean) > 0.10),
                'used_cross_rate':        bool(md['used_cross_rate']),
                # gross_var_at_bucket_t: what VaR would have been without netting
                # (all positions independent, all positive, at bucket T)
                'gross_var_at_bucket_t':  round(float(gross_sum),      2),
                # hedge_benefit: VaR saved by natural hedging within this group
                'hedge_benefit':          round(float(hedge_benefit),   2),
                'positions':              position_details,
            })

        total_net_var += bucket_var
        bucket_results.append({
            'bucket_num':    bnum,
            'bucket_label':  bucket_def['label'],
            'midpoint_days': T,
            'currencies':    bucket_currency_results,
            'bucket_var':    round(float(bucket_var), 2),
        })

    return round(float(total_net_var), 2), bucket_results, errors


def _build_position_details(
    positions: list[dict],
    md:        dict,
    T:         int,
    confidence: float,
    ccy:       str
) -> list[dict]:
    """
    Internal helper. For a list of positions in the same bucket/currency group,
    computes the standalone VaR for each individual exposure at the given T.

    'Standalone VaR at bucket T' means: what would this exposure's VaR be if
    it were the only position in this bucket/currency — using the bucket's
    midpoint T for consistency with the net VaR calculation.

    This is the attribution figure shown in Layer 2. It answers: "if we didn't
    have the offsetting position, how much would this exposure contribute?"

    Args:
        positions: List of position dicts from buckets_data grouping.
        md:        Market data for this currency.
        T:         Bucket midpoint trading days.
        confidence: VaR confidence level.
        ccy:       Currency code (for output labelling).

    Returns:
        List of attribution dicts, one per position.
    """
    details = []
    ann_mean = md['daily_mean'] * TRADING_DAYS_PER_YEAR

    for p in positions:
        # Each position treated as independent at bucket T for attribution
        p_direction   = 'long' if p['signed_amount'] > 0 else 'short'
        p_amount      = abs(p['signed_amount'])
        p_exposure_base = p_amount * md['spot_rate']

        p_var_floored, p_var_raw = calculate_parametric_var(
            exposure_amount   = p_exposure_base,
            annualised_vol    = md['ann_vol'],
            daily_mean_return = md['daily_mean'],
            confidence_level  = confidence,
            days              = T,
            direction         = p_direction,
        )

        details.append({
            'currency':                  ccy,
            'amount':                    p_amount,
            'direction':                 p['direction'],
            'settlement_date':           p['settle_str'],
            'actual_trading_days':       int(p['actual_days']),
            'bucket_midpoint_days':      T,
            'notional_base':             round(float(p_exposure_base),      2),
            # VaR on this exposure alone at bucket T — attribution figure for Layer 2.
            # NOT the same as the V2 standalone VaR (which uses actual_trading_days T).
            # Using bucket T keeps the attribution consistent with the net VaR figure.
            'standalone_var_at_bucket_t': round(float(p_var_floored),       2),
            'standalone_var_raw':        round(float(p_var_raw),            2),
            'var_was_floored':           bool(p_var_raw < 0),
            'used_cross_rate':           bool(md['used_cross_rate']),
            'drift_warning':             bool(abs(ann_mean) > 0.10),
            'near_term_warning':         bool(p['actual_days'] < 5),
        })

    return details


# =============================================================================
# NET CURRENCY SUMMARY (INFORMATIONAL)
# =============================================================================

def build_net_currency_summary(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    spot_rates:     dict[str, float]
) -> list[dict]:
    """
    Builds an informational per-currency net position summary combining
    cash holdings and all future exposures. NOT used for VaR computation.

    WHY NOT USED FOR VaR:
      Cash positions and future exposures have different VaR horizons:
        Cash: user-specified T (e.g. 1 day)
        Receivable: T = days to settlement (e.g. 53 days)
        Payable:    T = days to settlement (e.g. 107 days)
      Netting them for a single VaR figure would require choosing an arbitrary
      common T, which could misrepresent the risk. The bucketed approach handles
      forward-forward netting correctly; cash vs forward netting is left for
      V2.3+ with explicit treasury policy assumptions.

    WHAT IT SHOWS:
      Per currency: net = cash_holdings + receivables − payables
      All in base currency at current spot rates.
      Positive net = overall long FCY (fear depreciation).
      Negative net = overall short FCY (fear appreciation).

    Args:
        cash_positions: List of V1 cash dicts (currency, balance).
        exposures:      List of future exposure dicts (currency, amount, direction).
        base_ccy:       Company home currency.
        spot_rates:     Dict of currency → spot rate (base per foreign), sourced
                        from the market_data cache to avoid re-fetching.

    Returns:
        List of dicts sorted by abs(net_base) descending (largest exposures first).
        Each dict: 'currency', 'cash_base', 'receivables_base', 'payables_base',
                   'net_base', 'net_direction' ('long', 'short', or 'flat')
    """
    summary: dict[str, dict] = {}

    def get_or_create(ccy: str) -> dict:
        if ccy not in summary:
            summary[ccy] = {
                'currency':         ccy,
                'cash_base':        0.0,
                'receivables_base': 0.0,
                'payables_base':    0.0,
            }
        return summary[ccy]

    # Cash holdings — always long FCY (spot risk, no settlement date)
    for pos in cash_positions:
        ccy = pos['currency'].upper().strip()
        if ccy == base_ccy.upper():
            continue
        spot  = spot_rates.get(ccy, 1.0)
        value = float(pos['balance']) * spot
        get_or_create(ccy)['cash_base'] += value

    # Future exposures — direction determines sign
    for exp in exposures:
        ccy       = exp['currency'].upper().strip()
        direction = exp['direction'].lower().strip()
        if ccy == base_ccy.upper():
            continue
        spot  = spot_rates.get(ccy, 1.0)
        value = float(exp['amount']) * spot
        bucket = get_or_create(ccy)
        if direction == 'receivable':
            bucket['receivables_base'] += value
        elif direction == 'payable':
            bucket['payables_base'] += value

    # Compute net and label direction
    result = []
    for ccy, b in summary.items():
        # net = cash (long) + receivables (long) − payables (short)
        net = b['cash_base'] + b['receivables_base'] - b['payables_base']
        net_dir = 'long' if net > 0.01 else ('short' if net < -0.01 else 'flat')
        result.append({
            'currency':         ccy,
            'cash_base':        round(b['cash_base'],        2),
            'receivables_base': round(b['receivables_base'], 2),
            'payables_base':    round(b['payables_base'],    2),
            'net_base':         round(net,                   2),
            'net_direction':    net_dir,
        })

    # Sort by absolute net exposure, largest first
    result.sort(key=lambda x: abs(x['net_base']), reverse=True)
    return result


# =============================================================================
# MAIN ENTRY POINT — called by app.py and test_v2_engine.py
# =============================================================================

def calculate_combined_var_v2(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    confidence:     float = 0.95,
    period:         str   = '1y',
    cash_horizon:   int   = 1
) -> dict:
    """
    Main V2 entry point. Computes the full three-layer FX VaR for a portfolio
    containing both cash positions and future FX exposures.

    === THREE-LAYER OUTPUT ===

    LAYER 1 — Headline numbers (what to report and manage against):
      'spot_risk':              V1 cash VaR (unchanged, user-specified horizon)
      'forward_net':            V2.3 bucketed net VaR (after natural hedging)
      'total_var':              spot + forward_net
      'natural_hedge_benefit':  forward_gross − forward_net
                                (how much risk offsetting positions removed)

    LAYER 2 — Bucket attribution (what drives forward_net):
      Inside 'forward_net.buckets': per-bucket, per-currency breakdown
      showing net notional, net direction, net VaR, and individual position
      attribution figures (standalone VaR at bucket T for each exposure).

    LAYER 3 — Net currency summary (informational):
      'net_currency_summary': combined cash + recv − payables per currency,
      all in base currency at spot rates. NOT used for VaR.

    === WHY NO SINGLE COMBINED GROSS+NET TOTAL ===

    Cash positions and forward exposures use different T values (user-specified
    vs settlement-date-derived). Summing gross cash VaR with gross forward VaR
    would add figures computed at incompatible horizons. The total_var here
    uses spot_var (V1) + forward_NET_var (V2.3), which is the most meaningful
    combination: V1 is accurate for cash, V2.3 is accurate for forwards.

    === KNOWN LIMITATION: CASH VS FORWARD NATURAL HEDGING ===

    Natural hedging is currently only applied WITHIN forward exposure buckets.
    Cash positions are not netted against forward exposures even if they are in
    the same currency (e.g. USD 5mn cash + USD 3mn payable). Netting cash with
    forwards would require treasury policy assumptions (will the cash be held to
    fund the payable, or converted before settlement?). This is left for V2.3+.

    Args:
        cash_positions: List of cash dicts, each with:
                          'currency' (str): foreign currency ISO code
                          'balance'  (float): amount held
        exposures:      List of future exposure dicts, each with:
                          'currency'        (str): foreign currency
                          'amount'          (float): positive amount in FCY
                          'settlement_date' (str): 'YYYY-MM-DD'
                          'direction'       (str): 'payable' or 'receivable'
        base_ccy:       Company home currency (e.g. 'SGD').
        confidence:     VaR confidence level (default 0.95 = 95%).
        period:         Historical lookback for yfinance (default '1y').
        cash_horizon:   VaR horizon in trading days for cash positions
                        (default 1 = 1-day VaR). Does not affect forward VaR,
                        which always uses each exposure's own settlement horizon
                        (bucketed to midpoint T).

    Returns:
        A dict with:
            'base_ccy'              (str)
            'confidence'            (float)
            'cash_horizon'          (int)

            'spot_risk': {          ← Layer 1 spot (V1 format, unchanged)
                'total_var', 'positions', 'errors'
            }

            'forward_gross': {      ← V2 gross per-exposure at actual T
                # No total_var — each exposure uses its own T
                'exposures', 'errors'
            }

            'forward_net': {        ← Layer 1b & 2 forward net (V2.3 bucketed)
                # No total_var — buckets use different Ts
                # bucket_var within each bucket IS meaningful (same T)
                # hedge_benefit embedded per currency in each bucket
                'buckets', 'errors'
            }

            # No total_var — spot T ≠ forward bucket Ts, cannot be summed

            'net_currency_summary'  (list):  Layer 3 informational
    """
    # -----------------------------------------------------------------------
    # Step 1: Collect all unique currencies and batch-fetch market data once.
    # This avoids hitting Yahoo Finance multiple times for the same pair.
    # -----------------------------------------------------------------------
    all_currencies = (
        [pos['currency'] for pos in cash_positions] +
        [exp['currency'] for exp in exposures]
    )
    print(f"  Fetching market data for {len(set(c.upper() for c in all_currencies if c.upper() != base_ccy.upper()))} unique currency pairs…")

    market_data = fetch_market_data_batch(all_currencies, base_ccy, period)

    # -----------------------------------------------------------------------
    # Step 2: Spot risk (V1 cash positions, completely unchanged from V1).
    # Delegates entirely to calculate_portfolio_var in var_engine.py.
    # -----------------------------------------------------------------------
    print("  Computing spot risk (V1 cash positions)…")
    spot_result = calculate_portfolio_var(
        positions        = cash_positions,
        base_ccy         = base_ccy,
        confidence_level = confidence,
        period           = period,
        days             = cash_horizon,
    )

    # -----------------------------------------------------------------------
    # Step 3: Gross forward VaR (V2 independent, for natural hedge calculation).
    # Each exposure at bucket midpoint T, independent, all positive, summed.
    # This is NOT the headline forward risk figure — it's used only to compute
    # how much the bucketed netting saves.
    # -----------------------------------------------------------------------
    print("  Computing gross forward VaR (V2 independent)…")
    gross_forward_var, gross_exposures, gross_errors = calculate_gross_forward_var(
        exposures    = exposures,
        base_ccy     = base_ccy,
        market_data  = market_data,
        confidence   = confidence,
        use_bucket_t = True,   # Use bucket T for consistency with net calc
    )

    # -----------------------------------------------------------------------
    # Step 4: Net forward VaR (V2.3 bucketed netting — the headline figure).
    # Net notional per bucket per currency, VaR on net, at bucket midpoint T.
    # -----------------------------------------------------------------------
    print("  Computing net forward VaR (V2.3 bucketed netting)…")
    net_forward_var, bucket_results, bucket_errors = calculate_bucketed_forward_var(
        exposures    = exposures,
        base_ccy     = base_ccy,
        market_data  = market_data,
        confidence   = confidence,
    )

    # -----------------------------------------------------------------------
    # Step 5: Net currency summary (informational only).
    # There is no combined total_var across spot + forward buckets.
    # Spot VaR is the only meaningful combined figure (all cash share same T).
    # Forward bucket VaRs each have their own T — they cannot be summed.
    # Natural hedge benefit is embedded per bucket/currency in forward_net.
    #
    # Build spot_rates lookup from market_data cache to avoid re-fetching.
    # -----------------------------------------------------------------------
    spot_rates = {
        ccy: md['spot_rate']
        for ccy, md in market_data.items()
        if md['error'] is None and md['spot_rate'] is not None
    }

    net_summary = build_net_currency_summary(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        spot_rates     = spot_rates,
    )

    return {
        'base_ccy':     base_ccy,
        'confidence':   float(confidence),
        'cash_horizon': int(cash_horizon),

        # Layer 1a: Spot risk — total_var is meaningful here because all
        # cash positions share the same user-specified cash_horizon T.
        'spot_risk': spot_result,

        # Layer 1b reference: V2 gross per-exposure at actual settlement T.
        # No total_var — each exposure uses its own actual T, so the sum
        # would mix different horizons and be misleading.
        # These figures give the true standalone VaR per obligation and feed
        # the per-bucket attribution (standalone_var_at_bucket_t) used for
        # the natural hedge benefit calculation in forward_net.
        'forward_gross': {
            'exposures': gross_exposures,
            'errors':    gross_errors,
        },

        # Layer 1b & 2: Forward net (V2.3 bucketed netting).
        # bucket_var within each bucket IS meaningful — all currencies within
        # a bucket share the same midpoint T.
        # There is NO cross-bucket total — T=42, T=95, T=189 cannot be summed.
        # Natural hedge benefit is embedded per currency as:
        #   'gross_var_at_bucket_t' − 'net_var' = 'hedge_benefit'
        'forward_net': {
            'buckets': bucket_results,
            'errors':  bucket_errors,
        },

        # Layer 3: Informational net position per currency.
        # NOT used for VaR — different horizons make cross-position netting
        # ambiguous for risk purposes.
        'net_currency_summary': net_summary,
    }
