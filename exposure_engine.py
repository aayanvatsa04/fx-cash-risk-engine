"""
exposure_engine.py — FX Exposure VaR Engine (V3)

This module extends the V1 cash-position risk engine to handle forward-looking
FX risk from future payables and receivables (e.g. 'MYR 8mn payable 20-Aug-2026').

=== ARCHITECTURE AND ABSTRACTION LAYERS ===

The dependency chain is strictly one-directional:

    engine_runner.py  →  exposure_engine.py  →  var_engine.py
    app.py            ↗

exposure_engine.py imports FROM var_engine.py but is never imported BY it.
var_engine.py has no knowledge that exposure_engine.py exists.
This means:
  - To upgrade the math (e.g. Monte Carlo for V3): only touch var_engine.py.
  - To add new exposure types or change bucketing logic: only touch this file.
  - The Flask app and HTML frontend are completely insulated from both.

=== THREE-SECTION OUTPUT ===

SECTION 1 — Spot book risk (standalone, T = cash_horizon):
  VaR on current cash holdings at the user-specified horizon T.
  Each position computed independently using calculate_portfolio_var from V1.
  V2.2 addition: covariance-adjusted total + diversification benefit across
  currencies, using the historical correlation matrix of their return series.
  Completely separate from forward exposures — this is the daily cash book.

SECTION 2 — Unified bucketed risk (cash + forwards, covariance-adjusted):
  Cash positions are converted to synthetic receivables settling in 1 trading
  day, routing them into Bucket 1 alongside any same-currency near-term payables
  and receivables. This allows cash to net against Bucket 1 forward obligations.
  Forward exposures are assigned to their natural time bucket by settlement date.

  Within each bucket, positions are NETTED per currency before VaR is computed:
    receivables → positive signed notional (long FCY)
    payables    → negative signed notional (short FCY)
    net         → VaR computed on |net_notional| at bucket midpoint T
  Natural hedge benefit = gross VaR (independent) − net VaR (after netting).

  V2.2 addition: after per-currency netting, the bucket VaR is computed using
  a covariance matrix across all currencies in the bucket rather than a simple
  sum. Diversification benefit = simple-sum bucket VaR − covariance-adjusted VaR.

  There is NO combined total across buckets since T=10, T=42, T=95, T=189, T=315
  are different time horizons that cannot be meaningfully added.

SECTION 3 — Gross attribution (reference, forwards only, no netting):
  Each forward exposure's standalone VaR at its bucket midpoint T, computed
  independently. Cash positions NOT included in Section 3 itself.
  NOTE: dashboard_engine.py adds cash standalone VaRs — sourced from THIS
  function's separate 'gross_cash_attribution' output below (computed at the
  fixed CASH_CONSOLIDATED_T_DAYS horizon, NOT Section 1's spot_risk and NOT
  the user-adjustable cash_horizon) — on top of Section 3 when computing the
  Gross Standalone Risk stat card, so the dashboard baseline covers cash +
  forwards consistently with consolidated_var (which also includes both).
  This is "Option A". Sourcing this from spot_risk instead would silently
  couple the Cash VaR Horizon dropdown to the Gross Standalone Risk / Risk
  Reduction stat cards, even though that dropdown is documented to affect
  only the standalone Cash Book Risk card — see CASH_CONSOLIDATED_T_DAYS
  further down for the full rationale.

=== KEY CONCEPTS ===

Natural hedge benefit (within-currency netting):
  Same currency, same time bucket. Receivable and payable offset each other.
  benefit = gross_var_at_bucket_T − net_var_at_bucket_T ≥ 0 always.
  Cash holdings in Bucket 1 participate in this netting (V2.3).

Diversification benefit (cross-currency covariance):
  Different currencies within the same bucket. Because USD/SGD and MYR/SGD
  are not perfectly correlated (ρ < 1), the true portfolio VaR is less than
  the sum of individual VaRs.
  benefit = bucket_var_simple − bucket_var_cov ≥ 0 when ρ < 1.

Cash as synthetic Bucket 1 receivables (V2.3):
  Each cash holding is treated as a long position settling in 1 trading day,
  routing it into Bucket 1 (0–21 trading days). This allows it to net against
  any same-currency Bucket 1 payables or receivables. The cash retains a
  'source': 'cash' tag in attribution output so it is clearly labelled.
  Its VaR in Section 2 uses the Bucket 1 midpoint T=10, not the user-specified
  cash_horizon — use Section 1 for the standalone T=cash_horizon cash VaR.
"""

import numpy as np
import pandas as pd
from datetime import date, datetime
from collections import defaultdict
from var_engine import (
    fetch_pair_returns,
    calculate_parametric_var,
    calculate_portfolio_var,
    build_correlation_matrix,
    calculate_portfolio_var_cov,
    calculate_portfolio_var_cov_mixed_t,
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

# =============================================================================
# CUMULATIVE PERIOD DEFINITIONS — V3 Dashboard Filter
# =============================================================================

# Defines the cumulative time windows available in the dashboard bar chart filter.
# Each window is INCLUSIVE of all positions settling within max_days trading days
# from today. The chart for a given period shows ALL currencies present in any
# included bucket, and uses the period-level consolidated VaR computed with the
# exact min(Ti,Tj) covariance method restricted to positions within that window.
#
# IMPORTANT: The order here is the order they appear in the UI dropdown.
# 'max_days': None means include ALL positions regardless of horizon (= full portfolio).
# The 'all' key's period_var must equal consolidated_var exactly — this is guaranteed
# because calculate_cumulative_period_vars applies the same min(Ti,Tj) formula
# to the same position list with no filtering.
#
# Bucket correspondence (for developer reference):
#   'Next 1 month'   → positions in Bucket 1 only    (0–21 trading days)
#   'Next 3 months'  → positions in Buckets 1+2      (0–63 trading days)
#   'Next 6 months'  → positions in Buckets 1+2+3    (0–126 trading days)
#   'Next 12 months' → positions in Buckets 1+2+3+4  (0–252 trading days)
#   'All'            → all 5 buckets                 (no T limit)
CUMULATIVE_PERIOD_DEFINITIONS = [
    {'key': '1m',  'label': 'Next 1 month',   'max_days': 21},
    {'key': '3m',  'label': 'Next 3 months',  'max_days': 63},
    {'key': '6m',  'label': 'Next 6 months',  'max_days': 126},
    {'key': '12m', 'label': 'Next 12 months', 'max_days': 252},
    {'key': 'all', 'label': 'All',            'max_days': None},
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


def trading_days_to_date(n_days: int) -> str:
    """
    Converts a number of trading days from today into a settlement date string.

    The inverse of count_trading_days: given n trading days, returns the
    calendar date that is n business days (Mon–Fri) from today.

    Used in calculate_fx_var with a fixed value of 1 trading day to convert
    each cash position into a synthetic settlement date one day from today,
    so cash positions can be routed into Bucket 1 (0–21 days) alongside any
    same-currency near-term forward obligations for netting purposes. This
    routing date is intentionally NOT derived from cash_horizon — see
    CASH_CONSOLIDATED_T_DAYS and _build_individual_positions_list for the
    separate, fixed T convention used once cash is inside the forward
    bucketing / consolidated VaR / dashboard engines.

    Args:
        n_days: Number of trading days from today (must be ≥ 1).

    Returns:
        Date string in 'YYYY-MM-DD' format.

    Example:
        If today is Monday 2026-06-08 and n_days=5 → returns '2026-06-15'
        (the 5th business day from Monday is the following Monday, assuming
        no holidays — Mon–Fri counting only, same caveat as count_trading_days).
    """
    future = pd.Timestamp(date.today()) + pd.offsets.BDay(n_days)
    return future.strftime('%Y-%m-%d')


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
            ann_vol, daily_mean, returns, spot_rate, used_cross = fetch_pair_returns(
                ccy, base_ccy, period
            )
            market_data[ccy] = {
                'ann_vol':         ann_vol,
                'daily_mean':      daily_mean,
                'returns':         returns,   # V2.2: stored for covariance matrix
                'spot_rate':       spot_rate,
                'used_cross_rate': bool(used_cross),
                'error':           None,
            }
        except Exception as e:
            market_data[ccy] = {
                'ann_vol':         None,
                'daily_mean':      None,
                'returns':         None,
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
    Used to produce Section 3 (gross attribution reference) in the output.

    Also called internally during natural hedge benefit computation: the
    'gross_var_at_bucket_t' shown per currency in Section 2 is each exposure's
    standalone VaR at the bucket midpoint T, which this function computes.

    IMPORTANT: use_bucket_t=True means each exposure uses its BUCKET's midpoint
    T rather than its own actual trading days to settlement. This is intentional —
    the natural hedging benefit within a bucket is computed as:

        benefit = gross (at bucket T) − net (at bucket T)

    Using the same T for both sides means the benefit is purely from netting,
    not from T differences. If use_bucket_t=False, each exposure uses its actual
    T, which gives the true per-exposure standalone VaR but makes the benefit
    comparison less clean.

    Note: Only forward exposures are passed here. Cash holdings (which appear
    in Section 2 Bucket 1 as synthetic receivables) are NOT included — Section 3
    is a forwards-only reference view.

    Args:
        exposures:    List of forward exposure dicts (currency, amount,
                      settlement_date, direction). No cash synthetic receivables.
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
    Computes parametric VaR on NET notional per time bucket per currency,
    with covariance-adjusted cross-currency aggregation within each bucket.

    === V2.2 UPGRADE: COVARIANCE-ADJUSTED BUCKET VAR ===

    Previously, per-currency VaRs within a bucket were simply summed, which
    assumed perfect positive correlation between all currency pairs — i.e. on
    the worst day, every foreign currency moves against the base simultaneously.
    This overstates true portfolio risk when currencies are imperfectly correlated.

    V2.2 adds a covariance matrix within each bucket:
        bucket_var_cov = Z × √(s^T Σ_T s) − portfolio_drift_T

    where s is the vector of signed net exposures in base currency across all
    currencies in that bucket, and Σ_T is their covariance matrix at bucket T.

    Output changes vs V2:
        bucket_var         → covariance-adjusted (was simple sum)
        bucket_var_simple  → new field: the old simple-sum value (for reference)
        diversification_benefit → new field: bucket_var_simple − bucket_var

    === HOW NETTING WORKS (unchanged from V2) ===

    For each (bucket, currency) group:
      1. Receivables contribute a POSITIVE signed notional (long FCY).
      2. Payables contribute a NEGATIVE signed notional (short FCY).
      3. Sum signed notionals → net notional for that bucket/currency.
      4. If net > 0: direction='long'; if net < 0: direction='short'.
      5. Compute VaR on |net_notional_base| at bucket midpoint T.

    Args:
        exposures:   List of exposure dicts. In normal use (Section 2) this
                     includes BOTH actual forward exposures AND synthetic cash
                     receivables (dicts with '_source': 'cash') generated by
                     calculate_fx_var. The '_source' field is passed
                     through to position attribution so cash holdings are
                     labelled distinctly from forward obligations.
                     In Section 3 gross attribution use, only forward exposures
                     are passed (no cash synthetic receivables).
        base_ccy:    Company home currency.
        market_data: Pre-fetched market data (must include 'returns' series — V2.2).
        confidence:  VaR confidence level.

    Returns:
        (total_net_var, bucket_results, errors)
        total_net_var uses covariance-adjusted bucket_var for each bucket.
    """
    # -------------------------------------------------------------------
    # Step 1: Group valid exposures by (bucket_num, currency)
    # -------------------------------------------------------------------
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
            continue

        actual_days = count_trading_days(settle)
        if actual_days < 1:
            continue

        bucket = assign_to_bucket(actual_days)
        signed_amount = amount if direction == 'receivable' else -amount
        settle_str = settle if isinstance(settle, str) else settle.strftime('%Y-%m-%d')

        buckets_data[bucket['num']][ccy].append({
            'signed_amount': signed_amount,
            'actual_days':   actual_days,
            'direction':     direction,
            'settle_str':    settle_str,
            'md':            md,
            'bucket':        bucket,
            # 'cash' when routed from a cash holding synthetic receivable; 'forward' otherwise
            'source':        exp.get('_source', 'forward'),
        })

    # -------------------------------------------------------------------
    # Step 2: For each bucket — compute per-currency nets, then apply
    #         covariance across currencies within the bucket.
    # -------------------------------------------------------------------
    bucket_results  = []
    total_net_var   = 0.0

    for bucket_def in BUCKET_DEFINITIONS:
        bnum = bucket_def['num']
        if bnum not in buckets_data:
            continue

        T = bucket_def['midpoint_days']

        # --- Pass A: compute per-currency net positions ---
        # We do this first so we have all signed exposures available
        # before building the cross-currency covariance matrix.
        per_ccy: dict[str, dict] = {}

        for ccy, positions in buckets_data[bnum].items():
            md = positions[0]['md']
            net_notional_foreign = sum(p['signed_amount'] for p in positions)
            position_details = _build_position_details(positions, md, T, confidence, ccy)
            gross_sum = sum(p['standalone_var_at_bucket_t'] for p in position_details)
            ann_mean  = md['daily_mean'] * TRADING_DAYS_PER_YEAR

            if abs(net_notional_foreign) < 0.01:
                # Perfectly hedged — no net risk, full hedge benefit
                per_ccy[ccy] = {
                    'flat': True,
                    'net_notional_foreign':   0.0,
                    'net_notional_base':      0.0,
                    'net_notional_base_signed': 0.0,
                    'net_direction':          'flat',
                    'var_floored':            0.0,
                    'var_raw':                0.0,
                    'gross_sum':              gross_sum,
                    'hedge_benefit':          gross_sum,
                    'ann_mean':               ann_mean,
                    'position_details':       position_details,
                    'md':                     md,
                }
                continue

            net_notional_base = abs(net_notional_foreign) * md['spot_rate']
            net_direction     = 'long' if net_notional_foreign > 0 else 'short'
            # Signed: positive for long (fear depreciation), negative for short
            net_base_signed   = net_notional_base if net_direction == 'long' else -net_notional_base

            var_floored, var_raw = calculate_parametric_var(
                net_notional_base, md['ann_vol'], md['daily_mean'],
                confidence, T, net_direction
            )

            per_ccy[ccy] = {
                'flat': False,
                'net_notional_foreign':     net_notional_foreign,
                'net_notional_base':        net_notional_base,
                'net_notional_base_signed': net_base_signed,
                'net_direction':            net_direction,
                'var_floored':              float(var_floored),
                'var_raw':                  float(var_raw),
                'gross_sum':                gross_sum,
                'hedge_benefit':            gross_sum - float(var_floored),
                'ann_mean':                 ann_mean,
                'position_details':         position_details,
                'md':                       md,
            }

        # --- Pass B: covariance-adjusted bucket VaR ---
        # Collect active (non-flat) currencies and build cross-currency
        # covariance matrix from their historical returns.
        active_ccys    = [c for c in per_ccy if not per_ccy[c]['flat']]
        bucket_var_simple = sum(per_ccy[c]['var_floored'] for c in active_ccys)

        bucket_var_cov = bucket_var_simple   # default: falls back to simple sum

        if len(active_ccys) >= 2:
            returns_dict = {
                c: per_ccy[c]['md']['returns']
                for c in active_ccys
                if per_ccy[c]['md'].get('returns') is not None
            }

            if len(returns_dict) >= 2:
                corr_matrix, corr_ccys = build_correlation_matrix(returns_dict)

                # Build vectors aligned to corr_ccys order
                sig   = [per_ccy[c]['net_notional_base_signed'] for c in corr_ccys]
                vols  = [per_ccy[c]['md']['ann_vol']             for c in corr_ccys]
                means = [per_ccy[c]['md']['daily_mean']          for c in corr_ccys]

                cov_var, _ = calculate_portfolio_var_cov(
                    sig, vols, means, corr_matrix, confidence, T
                )

                # Add individual VaRs for any currencies without returns data
                # (edge case — should not occur in practice)
                missing_ccys = [c for c in active_ccys if c not in returns_dict]
                for c in missing_ccys:
                    cov_var += per_ccy[c]['var_floored']

                bucket_var_cov = cov_var

        diversification_benefit = round(max(bucket_var_simple - bucket_var_cov, 0.0), 2)

        # --- Pass C: build per-currency result dicts ---
        bucket_currency_results = []
        for ccy in sorted(per_ccy.keys()):   # sorted for deterministic output order
            d  = per_ccy[ccy]
            md = d['md']

            bucket_currency_results.append({
                'currency':               ccy,
                'net_notional_foreign':   round(float(d['net_notional_foreign']),  2),
                'net_notional_base':      round(float(d['net_notional_base']),     2),
                'net_direction':          d['net_direction'],
                'midpoint_days':          T,
                'spot_rate':              round(float(md['spot_rate']),    6),
                'annualised_vol':         round(float(md['ann_vol']),      6),
                'daily_mean':             round(float(md['daily_mean']),   8),
                'annualised_mean':        round(float(d['ann_mean']),      4),
                'net_var':                round(float(d['var_floored']),   2),
                'net_var_raw':            round(float(d['var_raw']),       2),
                'var_was_floored':        bool(d['var_raw'] < 0),
                'drift_warning':          bool(abs(d['ann_mean']) > 0.10),
                'used_cross_rate':        bool(md['used_cross_rate']),
                'gross_var_at_bucket_t':  round(float(d['gross_sum']),    2),
                'hedge_benefit':          round(float(d['hedge_benefit']), 2),
                'positions':              d['position_details'],
            })

        total_net_var += bucket_var_cov

        bucket_results.append({
            'bucket_num':              bnum,
            'bucket_label':            bucket_def['label'],
            'midpoint_days':           T,
            'currencies':              bucket_currency_results,
            # bucket_var: covariance-adjusted (V2.2 default)
            'bucket_var':              round(float(bucket_var_cov),          2),
            # bucket_var_simple: old simple-sum, shown for transparency
            'bucket_var_simple':       round(float(bucket_var_simple),       2),
            # diversification_benefit: VaR reduction from cross-currency correlations
            'diversification_benefit': diversification_benefit,
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
    computes the standalone VaR for each individual position at the bucket T.

    'Standalone VaR at bucket T' means: what would this position's VaR be if
    it were the only position in this bucket/currency — using the bucket's
    midpoint T for consistency with the net VaR calculation.

    This is the attribution figure shown in Section 2. It answers: "if we didn't
    have the offsetting position, how much would this one contribute?"

    Each position dict in `positions` comes from the buckets_data grouping and
    may have a 'source' field:
        'forward' (default): a normal future payable or receivable
        'cash':              a cash holding routed into Bucket 1 as a synthetic
                             receivable (settlement = tomorrow). Labelled clearly
                             in the attribution output so cash and forwards are
                             visually distinct.

    Args:
        positions:  List of position dicts from buckets_data grouping. Each has:
                    'signed_amount', 'actual_days', 'direction', 'settle_str',
                    'md', 'bucket', 'source' ('cash' or 'forward').
        md:         Market data for this currency (ann_vol, daily_mean, etc.).
        T:          Bucket midpoint trading days — used as VaR horizon.
        confidence: VaR confidence level.
        ccy:        Currency code (for output labelling).

    Returns:
        List of attribution dicts, one per position. Each dict contains:
            'currency', 'amount', 'direction', 'settlement_date',
            'actual_trading_days', 'bucket_midpoint_days', 'notional_base',
            'standalone_var_at_bucket_t', 'standalone_var_raw', 'var_was_floored',
            'used_cross_rate', 'drift_warning', 'near_term_warning',
            'source' ('cash' or 'forward')
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
            'standalone_var_at_bucket_t': round(float(p_var_floored),       2),
            'standalone_var_raw':        round(float(p_var_raw),            2),
            'var_was_floored':           bool(p_var_raw < 0),
            'used_cross_rate':           bool(md['used_cross_rate']),
            'drift_warning':             bool(abs(ann_mean) > 0.10),
            'near_term_warning':         bool(p['actual_days'] < 5),
            # 'cash' for positions routed from cash holdings; 'forward' otherwise
            'source':                    p.get('source', 'forward'),
        })

    return details


# =============================================================================
# COVARIANCE HELPERS FOR SPOT RISK — V2.2
# =============================================================================

def _add_covariance_to_spot_risk(
    spot_risk:   dict,
    market_data: dict,
    confidence:  float,
) -> dict:
    """
    Augments the spot_risk dict (produced by calculate_portfolio_var) with
    a covariance-adjusted total VaR across cash positions.

    calculate_portfolio_var sums individual VaRs (perfect correlation).
    This function re-computes the total using the covariance matrix of the
    actual return series, adding:

        'total_var_cov':           covariance-adjusted portfolio VaR
        'diversification_benefit': total_var (simple sum) − total_var_cov

    If fewer than 2 positions exist, or insufficient aligned data, falls back
    to simple sum (total_var_cov = total_var, benefit = 0).

    Args:
        spot_risk:   The dict returned by calculate_portfolio_var. Modified in place.
        market_data: Cache from fetch_market_data_batch, which now includes 'returns'.
        confidence:  VaR confidence level (needed to re-run portfolio VaR formula).

    Returns:
        The same spot_risk dict with two new keys added.
    """
    positions = spot_risk.get('positions', [])
    T         = spot_risk['days']

    if len(positions) < 2:
        spot_risk['total_var_cov']           = spot_risk['total_var']
        spot_risk['diversification_benefit'] = 0.0
        return spot_risk

    returns_dict = {}
    for p in positions:
        md = market_data.get(p['currency'])
        if md and md.get('returns') is not None:
            returns_dict[p['currency']] = md['returns']

    if len(returns_dict) < 2:
        spot_risk['total_var_cov']           = spot_risk['total_var']
        spot_risk['diversification_benefit'] = 0.0
        return spot_risk

    corr_matrix, corr_ccys = build_correlation_matrix(returns_dict)

    # All cash positions are LONG (fear FCY depreciation) → signed = +exposure_base
    data_by_ccy = {p['currency']: p for p in positions}

    sig   = [data_by_ccy[c]['exposure_base']   for c in corr_ccys if c in data_by_ccy]
    vols  = [data_by_ccy[c]['annualised_vol']   for c in corr_ccys if c in data_by_ccy]
    means = [data_by_ccy[c]['daily_mean']       for c in corr_ccys if c in data_by_ccy]

    # Add any currencies that had no returns series (fall back to simple contribution)
    missing_var = sum(
        data_by_ccy[c]['var']
        for c in data_by_ccy
        if c not in returns_dict
    )

    if len(sig) >= 2:
        total_var_cov, _ = calculate_portfolio_var_cov(
            sig, vols, means, corr_matrix, confidence, T
        )
        total_var_cov += missing_var
    else:
        total_var_cov = spot_risk['total_var']

    spot_risk['total_var_cov']           = round(float(total_var_cov), 2)
    spot_risk['diversification_benefit'] = round(
        float(max(spot_risk['total_var'] - total_var_cov, 0.0)), 2
    )
    return spot_risk


# =============================================================================
# SHARED HELPERS — used by both consolidated VaR and cumulative period VaR
# =============================================================================

# =============================================================================
# CASH_CONSOLIDATED_T_DAYS — fixed cash horizon for Consolidated VaR and the
# cumulative-period Dashboard views (Component CFaR chart, Portfolio VaR,
# Gross Standalone Risk, Risk Reduction).
# =============================================================================
#
# === WHY THIS EXISTS ===
#
# The Cash VaR Horizon dropdown on the page is documented to the user as
# affecting ONLY the standalone Cash Book Risk card ("Section 1"). Before this
# constant was introduced, _build_individual_positions_list silently used the
# user's cash_horizon selection as the T for cash positions wherever it was
# called — including from calculate_consolidated_portfolio_var AND
# calculate_cumulative_period_vars (which powers every Dashboard period view).
# This meant changing the Cash VaR Horizon dropdown — meant to be an isolated,
# independent setting for one specific card — silently changed the Consolidated
# Portfolio VaR, the Gross Standalone Risk baseline, the Risk Reduction
# percentage, and every Component CFaR bar in the dashboard chart for any
# period that includes cash. Worse: if cash_horizon was set HIGHER than a
# given period's cutoff (e.g. cash_horizon=63 days while viewing "Next 1
# month", cutoff=21 days), cash positions were filtered out of that period
# entirely — disappearing from both the bar chart's notional and its CFaR,
# despite being held right now and obviously part of any near-term exposure.
#
# === THE FIX ===
#
# Cash's T inside _build_individual_positions_list (and therefore inside
# Consolidated VaR and every cumulative-period Dashboard calculation) is now
# a FIXED constant, completely decoupled from the Cash VaR Horizon dropdown.
# The value chosen — 10 trading days — matches the Bucket 1 midpoint already
# used by calculate_bucketed_forward_var (Bucketed Risk Detail / "Section 2"),
# so cash is now treated identically wherever it appears outside the one
# section explicitly designed to let the user vary its horizon. This also
# resolves a separate, pre-existing inconsistency where Bucketed Risk Detail
# (fixed T=10) and Consolidated VaR (previously cash_horizon) disagreed with
# each other on cash's horizon.
#
# === RESULT ===
#
# The Cash VaR Horizon dropdown now affects ONLY the Cash Book Risk card
# (Section 1) — exactly as the UI's own helper text claims. Every other
# number on the page (Bucketed Risk Detail, Consolidated Portfolio VaR, the
# Risk Dashboard's stat cards, the Component CFaR chart for every period) is
# completely unaffected by this dropdown, because none of them call
# calculate_portfolio_var with cash_horizon any more — they all flow through
# _build_individual_positions_list, which now uses this fixed constant.
CASH_CONSOLIDATED_T_DAYS = 10


def calculate_gross_cash_var(
    cash_positions: list[dict],
    base_ccy:       str,
    market_data:    dict,
    confidence:     float,
) -> tuple[float, list[dict], list[dict]]:
    """
    Computes standalone VaR for each cash position INDEPENDENTLY, at the
    fixed CASH_CONSOLIDATED_T_DAYS horizon (10 trading days) — deliberately
    NOT the user-adjustable Cash VaR Horizon dropdown (cash_horizon), which
    affects only the standalone Cash Book Risk card (Section 1).

    === WHY THIS EXISTS ===

    This is the cash counterpart to calculate_gross_forward_var (which
    computes the forwards-only standalone sum for Section 3 / the Gross
    Standalone Risk stat card). Before this function existed, the dashboard
    sourced cash's contribution to Gross Standalone Risk from
    spot_risk['total_var'] — Section 1's own output, computed at the
    user-selected cash_horizon. That made the Gross Standalone Risk stat
    card (and therefore Risk Reduction, since Risk Reduction =
    gross_standalone_sum − consolidated_var) silently drift whenever the
    user changed the Cash VaR Horizon dropdown — even though that dropdown
    is documented, and intended, to affect ONLY the standalone Cash Book
    Risk card. Those two headline dashboard figures should be stable,
    reportable numbers that a stakeholder can trust are not moving because
    of an unrelated, exploratory control elsewhere on the page.

    This function gives the dashboard a cash standalone VaR computed at the
    SAME fixed T=10 convention used everywhere else outside Section 1
    (Bucketed Risk Detail, Consolidated Portfolio VaR, every cumulative
    period view) — see the CASH_CONSOLIDATED_T_DAYS comment above for the
    full rationale. Mirrors calculate_gross_forward_var's return shape so
    dashboard_engine.py can sum the two sources identically.

    Args:
        cash_positions: List of cash position dicts (currency, balance).
        base_ccy:       Company home currency.
        market_data:    Pre-fetched market data from fetch_market_data_batch().
                        Avoids re-fetching the same pair multiple times.
        confidence:     VaR confidence level.

    Returns:
        A tuple of:
            - total_gross_var (float): sum of all per-position standalone VaRs
            - results (list[dict]):    per-position breakdown (see fields below)
            - errors  (list[dict]):    positions that failed, with reasons

        Each result dict contains:
            'currency', 'balance', 't_used' (always CASH_CONSOLIDATED_T_DAYS),
            'spot_rate', 'exposure_base', 'annualised_vol', 'daily_mean',
            'annualised_mean', 'var', 'var_raw', 'var_was_floored',
            'used_cross_rate'
    """
    results   = []
    errors    = []
    total_var = 0.0

    for pos in cash_positions:
        ccy     = pos['currency'].upper().strip()
        balance = float(pos['balance'])

        # Skip base currency positions — no FX risk
        if ccy == base_ccy.upper():
            continue

        # Look up pre-fetched market data
        md = market_data.get(ccy)
        if md is None or md['error'] is not None:
            errors.append({'currency': ccy,
                           'reason': md['error'] if md else 'Not fetched'})
            continue

        # Convert to base currency: exposure_base = balance × spot_rate
        exposure_base = balance * md['spot_rate']

        # Cash is always 'long' — holder fears FCY depreciation (left tail)
        var_floored, var_raw = calculate_parametric_var(
            exposure_amount    = exposure_base,
            annualised_vol     = md['ann_vol'],
            daily_mean_return  = md['daily_mean'],
            confidence_level   = confidence,
            days               = CASH_CONSOLIDATED_T_DAYS,
            direction          = 'long',
        )

        total_var += var_floored
        ann_mean   = md['daily_mean'] * TRADING_DAYS_PER_YEAR

        results.append({
            'currency':         ccy,
            'balance':          balance,
            't_used':           CASH_CONSOLIDATED_T_DAYS,
            'spot_rate':        round(float(md['spot_rate']),    6),
            'exposure_base':    round(float(exposure_base),      2),
            'annualised_vol':   round(float(md['ann_vol']),      6),
            'daily_mean':       round(float(md['daily_mean']),   8),
            'annualised_mean':  round(float(ann_mean),           4),
            'var':              round(float(var_floored),        2),
            'var_raw':          round(float(var_raw),            2),
            'var_was_floored':  bool(var_raw < 0),
            'used_cross_rate':  bool(md['used_cross_rate']),
        })

    return round(float(total_var), 2), results, errors


def _build_individual_positions_list(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    market_data:    dict,
) -> list[dict]:
    """
    Builds the flat list of ALL individual positions for the covariance matrix.

    This is the shared data-preparation step used by BOTH:
        - calculate_consolidated_portfolio_var (builds the full portfolio VaR)
        - calculate_cumulative_period_vars (builds one per cumulative time window)

    Keeping this in one place guarantees that both functions always work from
    exactly the same position list — ensuring that the 'all' period VaR from
    calculate_cumulative_period_vars equals consolidated_var exactly.

    === POSITION STRUCTURE ===

    Each entry represents a single individual exposure — one cash holding or one
    forward payable/receivable. Positions are NOT pre-netted by currency.
    Natural hedging between offsetting positions (e.g. a USD recv and a USD pay)
    emerges automatically through the covariance matrix (ρ=1 for same currency,
    opposite signs → negative cross-term reduces portfolio variance).

    Cash positions:
        - direction: always 'long' (holder fears FCY depreciation)
        - t_days:    CASH_CONSOLIDATED_T_DAYS (fixed at 10 trading days — matches
                     the Bucket 1 midpoint used elsewhere in the engine, and is
                     deliberately NOT the user-adjustable Cash VaR Horizon
                     dropdown; see the CASH_CONSOLIDATED_T_DAYS comment above
                     for the full rationale)
        - signed_base: + (positive: long exposure)

    Forward exposures:
        - direction: 'receivable' (long) or 'payable' (short)
        - t_days:    count_trading_days(settlement_date) — ACTUAL days, not bucket midpoint
        - signed_base: + for receivable (long), − for payable (short)

    Positions where:
        - currency == base_ccy are skipped (no FX risk)
        - market_data is missing/errored are skipped (no data available)
        - actual_t < 1 are skipped (already settled — no remaining risk)

    Args:
        cash_positions: List of cash position dicts (currency, balance).
        exposures:      List of forward exposure dicts (currency, amount,
                        settlement_date, direction).
        base_ccy:       Company home currency (e.g. 'SGD').
        market_data:    Pre-fetched market data dict from fetch_market_data_batch().
                        Must include 'ann_vol', 'daily_mean', 'spot_rate' per currency.

    Returns:
        List of position dicts. Each dict has:
            'currency':        str   — foreign currency ISO code
            'type':            str   — 'cash' or 'forward'
            'direction':       str   — 'long', 'receivable', or 'payable'
            'signed_base':     float — signed exposure in base currency
                                       (+ = long/receivable, − = payable)
            't_days':          int   — actual trading days to settlement/horizon
            'settlement_date': str|None — 'YYYY-MM-DD' for forwards, None for cash
            'ann_vol':         float — annualised volatility for this currency pair
            'daily_mean':      float — daily mean return for this currency pair
    """
    all_positions = []

    # --- Cash positions: always long, T = CASH_CONSOLIDATED_T_DAYS (fixed) ---
    for pos in cash_positions:
        ccy = pos['currency'].upper().strip()
        if ccy == base_ccy.upper():
            continue   # home currency — no FX risk
        md = market_data.get(ccy)
        if md is None or md['error'] is not None:
            continue   # no market data — skip silently

        # Positive signed base: holding FCY, fear it depreciates against base
        signed_base = float(pos['balance']) * md['spot_rate']

        all_positions.append({
            'currency':        ccy,
            'type':            'cash',
            'direction':       'long',
            'signed_base':     signed_base,
            't_days':          CASH_CONSOLIDATED_T_DAYS,
            'settlement_date': None,
            'ann_vol':         md['ann_vol'],
            'daily_mean':      md['daily_mean'],
        })

    # --- Forward exposures: direction determines sign, T = actual days ---
    for exp in exposures:
        ccy = exp['currency'].upper().strip()
        if ccy == base_ccy.upper():
            continue
        md = market_data.get(ccy)
        if md is None or md['error'] is not None:
            continue

        actual_t = count_trading_days(exp['settlement_date'])
        if actual_t < 1:
            continue   # already settled

        # Receivable: +1 (long FCY, fear depreciation before settlement date)
        # Payable:    -1 (short FCY, fear appreciation — obligation gets more expensive)
        sign        = +1 if exp['direction'].lower() == 'receivable' else -1
        signed_base = sign * float(exp['amount']) * md['spot_rate']
        settle_str  = (exp['settlement_date']
                       if isinstance(exp['settlement_date'], str)
                       else exp['settlement_date'].strftime('%Y-%m-%d'))

        all_positions.append({
            'currency':        ccy,
            'type':            'forward',
            'direction':       exp['direction'].lower(),
            'signed_base':     signed_base,
            't_days':          actual_t,
            'settlement_date': settle_str,
            'ann_vol':         md['ann_vol'],
            'daily_mean':      md['daily_mean'],
        })

    return all_positions


def _build_position_level_correlation_matrix(
    positions:   list[dict],
    market_data: dict,
) -> np.ndarray:
    """
    Builds the n×n position-level correlation matrix for a given list of positions.

    This is the shared correlation matrix construction step used by both
    calculate_consolidated_portfolio_var and calculate_cumulative_period_vars.
    Factoring it out ensures both use identical logic, so 'all' period VaR
    equals consolidated_var exactly.

    === CORRELATION RULES ===

    For each pair (i, j):

        Same currency (cᵢ = cⱼ): ρ = 1.0
            Both positions are driven by the SAME exchange rate (e.g. USDSGD).
            Their daily returns are perfectly correlated regardless of T.
            Natural hedging emerges through the covariance matrix: signed_base
            for a recv is + and for a pay is −, so the cross-term is negative,
            reducing portfolio variance. No explicit netting is needed.

        Different currencies with data: ρ = historical Pearson correlation
            Computed from build_correlation_matrix() using aligned daily return
            series of the two exchange rate pairs.
            Captures cross-currency diversification (e.g. partial offset between
            a USD position and a MYR position that are mildly correlated).

        Different currencies, no data: ρ = 0.0
            Conservative independence assumption. Only applies if yfinance
            failed to return a returns series for one of the currencies —
            which is already flagged in market_data['error'].

    The matrix is symmetric (ρ[i,j] = ρ[j,i]) and positive semi-definite.
    The diagonal is always 1.0 (each position correlated with itself).

    Args:
        positions:   List of position dicts from _build_individual_positions_list().
                     Must include 'currency' key per entry.
        market_data: Pre-fetched market data dict. Must include 'returns' series
                     per currency (populated by fetch_market_data_batch).

    Returns:
        np.ndarray of shape (n, n) — the position-level correlation matrix,
        where n = len(positions). Symmetric with diagonal = 1.0.
    """
    n = len(positions)

    # Collect return series for all unique currencies that appear in this position set
    unique_ccys = list(dict.fromkeys(p['currency'] for p in positions))
    returns_dict = {
        c: market_data[c]['returns']
        for c in unique_ccys
        if market_data.get(c) and market_data[c].get('returns') is not None
    }

    # Build currency-level correlation matrix (mxm where m = unique currencies with data)
    if len(returns_dict) >= 2:
        ccy_corr, corr_ccys = build_correlation_matrix(returns_dict)
        ccy_to_corr_idx = {c: i for i, c in enumerate(corr_ccys)}
    else:
        # Not enough return series to build a meaningful correlation matrix;
        # will fall back to ρ=1 (same ccy) or ρ=0 (different ccy) below.
        ccy_corr        = None
        ccy_to_corr_idx = {}

    # Build n×n position-level correlation matrix
    # Initialise with identity (diagonal = 1.0, off-diagonal will be filled below)
    full_corr = np.eye(n, dtype=float)

    for i in range(n):
        for j in range(i + 1, n):   # upper triangle only; mirror to lower at end
            ci = positions[i]['currency']
            cj = positions[j]['currency']

            if ci == cj:
                # Same exchange rate: perfectly correlated
                rho = 1.0

            elif (ccy_corr is not None
                  and ci in ccy_to_corr_idx
                  and cj in ccy_to_corr_idx):
                # Different currencies with aligned return data: historical ρ
                rho = float(ccy_corr[ccy_to_corr_idx[ci], ccy_to_corr_idx[cj]])

            else:
                # No correlation data: assume independent
                rho = 0.0

            full_corr[i, j] = rho
            full_corr[j, i] = rho   # symmetric matrix

    return full_corr


def _compute_component_vars_by_currency(
    positions:   list[dict],
    corr_matrix: np.ndarray,
    z:           float,
) -> dict[str, dict]:
    """
    Computes component VaR (marginal risk contribution) for each individual
    position and sums contributions by currency. ALSO returns the vol/drift
    split of that sum per currency (V3.6 — see "WHY THE SPLIT IS RETURNED"
    below), so the frontend simulation sliders can scale Component CFaR
    EXACTLY under a vol-regime shift, rather than approximating it.

    === WHAT IS COMPONENT VAR? ===

    Component VaR answers: "of the total portfolio VaR, how much comes from
    each currency?" It decomposes the portfolio VaR exactly so that:

        Σᵢ ComponentVaR_i = Portfolio VaR   (by construction)

    This makes it useful for bar chart attribution: each currency's bar shows
    its exact risk contribution to the period's portfolio VaR, and all bars
    sum to that number.

    === THE FORMULA ===

    Starting from the portfolio VaR formula:
        Portfolio_VaR = Z × √(sᵀ Σ s) − sᵀ μ_T

    Define:
        σ_p = √(sᵀ Σ s)                   portfolio volatility
        Σs  = cov_T @ s                    vector of marginal contributions to variance

    The component VaR for individual position i is:
        vol_component_i  = (sᵢ × (Σs)ᵢ / σ_p) × Z
        drift_component_i = sᵢ × μᵢ_daily × Tᵢ
        ComponentVaR_i   = vol_component_i − drift_component_i

    Verification that components sum to portfolio VaR:
        Σᵢ vol_component_i  = (Σᵢ sᵢ × (Σs)ᵢ / σ_p) × Z
                             = (sᵀ Σ s / σ_p) × Z
                             = σ_p × Z              ← (because σ_p² = sᵀΣs)
        Σᵢ drift_component_i = sᵀ μ_T              ← portfolio drift
        Sum = σ_p × Z − sᵀ μ_T = Portfolio_VaR ✓

    === WHY THE VOL/DRIFT SPLIT IS RETURNED (V3.6) ===

    Per-currency, define:
        vol_part_c   = Σ_{i∈c} vol_component_i
        drift_part_c = Σ_{i∈c} drift_component_i
        component_var_c = vol_part_c − drift_part_c   (same as before)

    Under a UNIFORM vol-regime shift — every currency's annualised vol
    scaled by the same factor k (this is exactly what the frontend's
    "Volatility Regime Change" slider does: "Scale all currency pair
    volatilities up or down") — the vol-driven part scales EXACTLY
    linearly by k, for ANY position, REGARDLESS of net notional:

        Σ[i,j](k) = ρ[i,j] × (k·σᵢ) × (k·σⱼ) × min(Tᵢ,Tⱼ) = k² × Σ[i,j](1)
        ⟹ σ_p(k)² = sᵀΣ(k)s = k² × sᵀΣ(1)s  ⟹  σ_p(k) = k × σ_p(1)
        ⟹ (Σ(k)s)ᵢ = k² × (Σ(1)s)ᵢ
        ⟹ vol_component_i(k) = sᵢ(Σ(k)s)ᵢ/σ_p(k) × Z
                              = sᵢ · k²(Σ(1)s)ᵢ / (k·σ_p(1)) × Z
                              = k × vol_component_i(1)

    This is a provable identity, not an approximation — it follows purely
    from the covariance matrix being homogeneous of degree 2 in σ. It holds
    even for a currency whose NET notional is zero (the cross-horizon
    residual case — e.g. a receivable and payable in the same currency that
    cancel in notional but settle at different dates), because the proof
    never depends on net notional, only on the per-position vol_component
    values summing correctly.

    drift_part_c does NOT scale with a vol shift (μ, T, s are unaffected by
    a volatility-regime change) — it is held constant, exactly as the old
    (now-replaced) mu_term was designed to be.

    So the frontend can now compute, for ANY currency, an EXACT simulated
    Component CFaR at vol multiplier k = (1 + Δ_vol):

        new_component_var_c = k × vol_part_c − drift_part_c

    — a single formula, with no long/short/flat branching needed (unlike
    the old net_notional_base-derived vol_term/mu_term, which were 0 for
    flat currencies and only a rough approximation for multi-position
    currencies — see dashboard_engine.py's module docstring for the bug
    history this replaces). Verified empirically against a full engine
    re-run at a stressed vol level — predicted and actual periods VaRs
    matched to within floating-point rounding (see project test history).

    Summing across currencies recovers the SAME exact-scaling identity at
    the portfolio level, which is why the Period VaR strip / Stressed
    Portfolio VaR figures also become exact under a pure vol-regime shift,
    not just individually-consistent approximations — see dashboard.js's
    module docstring for the user-facing implication of this.

    NOTE: this exactness applies to a UNIFORM vol shift only (the only kind
    this app's vol slider performs). The SEPARATE spot-rate slider, which
    shifts only ONE currency's exposure, does NOT have this property —
    shifting one currency's notional changes its cross-covariance terms
    with every other currency too, which a simple per-currency scale factor
    cannot capture exactly. The spot slider's existing approximation is
    unaffected by this change.

    === EDGE CASES ===

    portfolio_vol ≈ 0 (all positions perfectly offset each other):
        vol_component = 0 for all positions. Only drift components remain.
        This is a degenerate case (completely hedged) and component VaRs
        may be negative — meaning this currency's drift is reducing overall risk.

    Negative component VaR:
        A position acting as a hedge (reducing portfolio variance or contributing
        positive drift on a long position) will have a negative component VaR.
        This is mathematically correct and meaningful — it shows the portfolio
        benefit of that position. In the UI, we floor per-currency bars at 0 for
        display clarity but preserve the signed value in the data.

    Args:
        positions:   List of position dicts from _build_individual_positions_list().
                     Must include 'signed_base', 't_days', 'ann_vol', 'daily_mean',
                     'currency'.
        corr_matrix: n×n position-level correlation matrix from
                     _build_position_level_correlation_matrix(). Must align with
                     positions (same order, same length).
        z:           Confidence-level Z-score (e.g. norm.ppf(0.95) ≈ 1.6449).
                     Must be the SAME z used for the portfolio VaR computation.

    Returns:
        Dict mapping currency ISO code → {
            'component_var': float,  — vol_part − drift_part (same value as
                                        the pre-V3.6 return; sums to portfolio VaR)
            'vol_part':      float,  — volatility-driven part (scales EXACTLY
                                        linearly under a uniform vol-regime shift)
            'drift_part':    float,  — drift-driven part (constant under a
                                        vol-regime shift; SIGNED, can be ±)
        }
        A currency with two positions will have both positions' values summed
        into each of the three fields. component_var (and the vol/drift parts
        individually) can be negative (hedging positions).
    """
    n = len(positions)
    if n == 0:
        return {}

    # Build numpy arrays aligned with positions list order
    s           = np.array([p['signed_base'] for p in positions], dtype=float)
    t           = np.array([p['t_days']      for p in positions], dtype=float)
    ann_vols    = np.array([p['ann_vol']     for p in positions], dtype=float)
    daily_means = np.array([p['daily_mean']  for p in positions], dtype=float)

    # Build covariance matrix using the exact min(Ti,Tj) formula
    # (same as calculate_portfolio_var_cov_mixed_t)
    sigma_daily = ann_vols / np.sqrt(TRADING_DAYS_PER_YEAR)
    min_T       = np.minimum(t[:, None], t[None, :])
    cov_T       = corr_matrix * np.outer(sigma_daily, sigma_daily) * min_T

    # Portfolio variance and vol
    portfolio_variance = float(s @ cov_T @ s)
    portfolio_vol      = np.sqrt(max(portfolio_variance, 0.0))

    # Marginal covariance vector: (cov_T @ s)[i] = Σⱼ Cov(i,j) × sⱼ
    # This is the gradient of portfolio variance with respect to sᵢ (divided by 2).
    # Each element represents how much position i co-moves with the rest of the portfolio.
    marginal = cov_T @ s   # shape (n,)

    # Vol component: measures each position's contribution to portfolio volatility
    # = (sᵢ × marginalᵢ / σ_p) × Z
    # Note: sᵢ × marginalᵢ = sᵢ × Σⱼ Cov(i,j)sⱼ = contribution to portfolio variance.
    # Dividing by σ_p converts from variance to volatility space.
    # Multiplying by Z gives the VaR-level contribution.
    #
    # This array (vol_component, per-position) is exactly what the V3.6
    # vol_part return value sums by currency — it is THE quantity proven
    # above to scale exactly linearly under a uniform vol-regime shift.
    if portfolio_vol > 1e-12:
        # Normal case: portfolio has non-trivial volatility
        vol_component = (s * marginal / portfolio_vol) * z
    else:
        # Degenerate case: portfolio is essentially flat (perfectly hedged).
        # No volatility contribution from any position.
        vol_component = np.zeros(n)

    # Drift component: sᵢ × μᵢ_daily × Tᵢ
    # This is the position's expected P&L over its own horizon.
    # Positive drift on a long position REDUCES VaR (subtracted in formula).
    # This array is exactly what the V3.6 drift_part return value sums by
    # currency — held constant under a vol-regime shift (μ, T, s don't
    # change when only volatility is being stressed).
    drift_contribution = s * daily_means * t

    # Component VaR per position = vol_component_i − drift_component_i
    # (same structure as portfolio VaR = Z×σ_p − portfolio_drift)
    component_vars = vol_component - drift_contribution

    # Aggregate by currency: sum vol_component, drift_contribution, AND their
    # difference (component_var) across all positions of each currency.
    # All three are accumulated together in one pass for efficiency and to
    # guarantee they stay numerically consistent with each other (component_var
    # is always exactly vol_part - drift_part for the SAME currency, never
    # computed from a separately-rounded intermediate value).
    result: dict[str, dict] = {}
    for i, pos in enumerate(positions):
        ccy = pos['currency']
        if ccy not in result:
            result[ccy] = {'component_var': 0.0, 'vol_part': 0.0, 'drift_part': 0.0}
        result[ccy]['vol_part']      += float(vol_component[i])
        result[ccy]['drift_part']    += float(drift_contribution[i])
        result[ccy]['component_var'] += float(component_vars[i])

    return result


# =============================================================================
# CONSOLIDATED PORTFOLIO VAR — V2.4 Cross-horizon aggregation
# =============================================================================

def calculate_consolidated_portfolio_var(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    market_data:    dict,
    confidence:     float,
) -> dict:
    """
    Computes a single consolidated VaR across the ENTIRE portfolio — every cash
    position and every forward exposure — using the exact individual-position
    covariance method with min(Ti, Tj) cross-horizon terms.

    === WHY THIS EXISTS ===

    The bucketed Section 2 output correctly shows VaR per time bucket, but
    deliberately avoids summing across buckets because different buckets use
    different T values and VaRs at different horizons cannot be meaningfully added.
    This function solves that problem and produces the single portfolio-level number
    that was previously missing.

    === METHODOLOGY: EXACT INDIVIDUAL-POSITION COVARIANCE ===

    Every individual exposure (each cash holding and each forward payable/receivable)
    is kept as a SEPARATE entry in the covariance matrix. There is no pre-netting
    by currency — natural hedging between a USD recv and USD pay falls out
    automatically through the covariance matrix.

    Step 1 — Build individual position list:
        For each cash position:
            signed_base = balance × spot_rate   (always positive — long FCY)
            T           = CASH_CONSOLIDATED_T_DAYS (fixed at 10 trading days,
                          matching the Bucket 1 midpoint — deliberately NOT
                          the user-adjustable Cash VaR Horizon dropdown, which
                          affects only the standalone Cash Book Risk card)

        For each forward exposure:
            signed_base = ±amount × spot_rate   (+ recv, − payable)
            T           = count_trading_days(settlement_date)   (actual days)

        Each position is a separate row/column in the covariance matrix.

    Step 2 — Build n×n position-level correlation matrix:
        For positions i and j:
            Same currency (ci = cj):  ρ[i,j] = 1.0
                Both positions are driven by the same exchange rate (e.g. USD/SGD).
                They are perfectly correlated in their daily return series.
                Natural hedging between recv and pay in the same currency emerges
                automatically: the signed exposures (+E, -E) combined with ρ=1
                produce a negative cross-term that reduces portfolio variance.

            Different currencies (ci ≠ cj): ρ[i,j] = historical Pearson correlation
                Computed from aligned daily return series of the two exchange rates.
                Captures cross-currency diversification.

            Unknown (no return data):  ρ[i,j] = 0.0
                Conservative assumption: independent. This only occurs if yfinance
                fails to return data for one of the currencies.

    Step 3 — Compute exact covariance matrix:
        Σ[i,j] = ρ[i,j] × σᵢ_daily × σⱼ_daily × min(Tᵢ, Tⱼ)

        The min(Tᵢ, Tⱼ) term is derived from the random walk assumption:
        under independence of daily returns across time, only same-day pairs
        survive when expanding Cov(rᵢ_Ti, rⱼ_Tj). Position i exists for Ti
        days, position j for Tj days — they co-exist for only min(Ti,Tj) days.
        See calculate_portfolio_var_cov_mixed_t docstring for the full proof.

    Step 4 — Compute portfolio VaR:
        Portfolio VaR = Z × √(sᵀ Σ s) − sᵀ μ_T

        where sᵀ Σ s sums all individual variances and pairwise covariances,
        each scaled to min(Ti,Tj) overlapping days, and μ_T is the portfolio
        drift (each position's own drift scaled to its own T).

    === WHY THE CONSOLIDATED NUMBER IS LOWER THAN THE SUM OF BUCKET VaRs ===

    Two reasons:

    1. Full cross-currency netting across buckets:
       A large USD payable in Bucket 3 can offset a USD receivable in Bucket 2
       (with some reduction due to the different T values). In Section 2, they
       sit in different buckets and never interact. Here they are in the same
       covariance matrix and their negative cross-term reduces variance.

    2. Cross-currency diversification applied simultaneously:
       In Section 2, diversification is applied independently within each bucket.
       Here it is applied once across the whole portfolio, capturing the full
       picture of how all currencies interact simultaneously.

    === RELATIONSHIP TO BUCKETED OUTPUT ===

    Section 2 (bucketed):   "Which settlement window is riskiest?"
                             Per-bucket, per-currency. Actionable for hedging
                             specific obligations. Uses bucket midpoint T.

    Consolidated (this):    "What is my total portfolio risk in one number?"
                             Single figure. Uses actual T per position.
                             Exact cross-horizon covariance via min(T).

    Both are needed — Section 2 for operational hedging decisions, the
    consolidated number for overall risk reporting.

    Args:
        cash_positions: List of cash position dicts (currency, balance).
        exposures:      List of forward exposure dicts (currency, amount,
                        settlement_date, direction).
        base_ccy:       Company home currency.
        market_data:    Batch-fetched market data (must include 'returns' series).
        confidence:     VaR confidence level (e.g. 0.95).

    Returns:
        {
            'total_var':          float,   # consolidated exact VaR
            'total_var_raw':      float,   # before flooring to 0
            'var_was_floored':    bool,    # True if raw_var was negative
            'methodology':        str,     # 'exact_individual_position_min_t'
            'n_positions':        int,     # total individual positions in matrix
            'position_breakdown': list,    # one entry per individual position
        }

        position_breakdown entries (sorted by |signed_exposure_base| descending):
            {
                'currency':             str,
                'type':                 str,    # 'cash' or 'forward'
                'direction':            str,    # 'long', 'receivable', 'payable'
                'signed_exposure_base': float,  # signed SGD exposure
                't_days':               int,    # actual T used for this position
                'settlement_date':      str,    # 'YYYY-MM-DD' for forwards, None for cash
            }
    """
    # -------------------------------------------------------------------------
    # Step 1: Build the full individual position list — one entry per position,
    # no pre-netting. Delegated to the shared helper so that this function and
    # calculate_cumulative_period_vars always work from the SAME position list.
    # This guarantees that the 'all' period VaR equals consolidated_var exactly.
    # cash_horizon is deliberately NOT passed here — cash's T inside this list
    # is the fixed CASH_CONSOLIDATED_T_DAYS constant, independent of the user's
    # Cash VaR Horizon dropdown. See CASH_CONSOLIDATED_T_DAYS for the rationale.
    # -------------------------------------------------------------------------
    all_positions = _build_individual_positions_list(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        market_data    = market_data,
    )

    if not all_positions:
        return {
            'total_var':          0.0,
            'total_var_raw':      0.0,
            'var_was_floored':    False,
            'methodology':        'exact_individual_position_min_t',
            'n_positions':        0,
            'position_breakdown': [],
        }

    n = len(all_positions)

    # -------------------------------------------------------------------------
    # Step 2+3: Build the n×n position-level correlation matrix.
    # Delegated to the shared helper. Uses ρ=1 for same-currency pairs,
    # historical Pearson correlation for cross-currency pairs, 0 if data missing.
    # See _build_position_level_correlation_matrix docstring for full rules.
    # -------------------------------------------------------------------------
    full_corr = _build_position_level_correlation_matrix(all_positions, market_data)

    # -------------------------------------------------------------------------
    # Step 4: Compute consolidated VaR using exact min(T) covariance formula.
    # Σ[i,j] = ρ[i,j] × σᵢ_daily × σⱼ_daily × min(Tᵢ, Tⱼ)
    # Portfolio VaR = Z × √(sᵀ Σ s) − sᵀ μ_T
    # See var_engine.calculate_portfolio_var_cov_mixed_t for full derivation.
    # -------------------------------------------------------------------------
    signed_exps  = [p['signed_base']  for p in all_positions]
    ann_vols_lst = [p['ann_vol']      for p in all_positions]
    daily_means  = [p['daily_mean']   for p in all_positions]
    t_values     = [p['t_days']       for p in all_positions]

    var_f, var_r = calculate_portfolio_var_cov_mixed_t(
        signed_exposures_base = signed_exps,
        ann_vols              = ann_vols_lst,
        daily_means           = daily_means,
        t_values              = t_values,
        correlation_matrix    = full_corr,
        confidence_level      = confidence,
    )

    # -------------------------------------------------------------------------
    # Step 5: Build position breakdown for output
    # -------------------------------------------------------------------------
    position_breakdown = [
        {
            'currency':             p['currency'],
            'type':                 p['type'],
            'direction':            p['direction'],
            'signed_exposure_base': round(float(p['signed_base']), 2),
            't_days':               p['t_days'],
            'settlement_date':      p['settlement_date'],
        }
        for p in all_positions
    ]
    # Sort by absolute exposure descending — largest risk contributors first
    position_breakdown.sort(
        key=lambda x: abs(x['signed_exposure_base']), reverse=True
    )

    return {
        'total_var':          round(float(var_f), 2),
        'total_var_raw':      round(float(var_r), 2),
        'var_was_floored':    bool(var_r < 0),
        'methodology':        'exact_individual_position_min_t',
        'n_positions':        n,
        'position_breakdown': position_breakdown,
    }


# =============================================================================
# CUMULATIVE PERIOD VaR — V3 Dashboard feature
# =============================================================================

def calculate_cumulative_period_vars(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    market_data:    dict,
    confidence:     float,
) -> dict:
    """
    Computes a consolidated VaR for each cumulative time window defined in
    CUMULATIVE_PERIOD_DEFINITIONS, plus per-currency component VaR decomposition.

    This powers the V3 dashboard bar chart filter: the user selects a time
    window (e.g. "Next 3 months") and the chart shows:
        - per-currency net notional exposure across all included positions
        - per-currency component VaR (exact risk attribution summing to period VaR)

    === WHY CUMULATIVE WINDOWS INSTEAD OF INDIVIDUAL BUCKETS? ===

    The existing Section 2 bucketed view shows risk PER bucket (a snapshot of
    each settlement window in isolation). The cumulative view answers a different
    question: "what is my TOTAL risk exposure from now until T months out?"

    This is more actionable for rolling hedge decisions: a treasury manager
    deciding whether to hedge the next 3 months of exposures wants to see ALL
    positions settling within those 3 months in a single view, not scattered
    across two bucket windows.

    === METHODOLOGY ===

    For each cumulative period with max_days = M:
        1. Filter the full position list to positions where t_days ≤ M.
           For 'all', include every position (M = None).
        2. Build the position-level correlation matrix for this filtered subset.
           Uses the same rules as the full consolidated_var:
           ρ=1 for same-currency pairs, historical Pearson for cross-currency.
        3. Compute consolidated VaR using calculate_portfolio_var_cov_mixed_t.
           Uses exact Σ[i,j] = ρ[i,j] × σᵢ_daily × σⱼ_daily × min(Tᵢ, Tⱼ).
        4. Compute component VaR decomposition using _compute_component_vars_by_currency.
           ComponentVaR_i = (sᵢ × (Σs)ᵢ / σ_p) × Z − sᵢ × μᵢ × Tᵢ
           Σ ComponentVaR_i = period_var (by construction).
        5. Aggregate per-currency net notionals and component VaRs.

    === EQUALITY GUARANTEE: 'all' PERIOD = consolidated_var ===

    The 'all' period is computed with no position filter — the same position list
    and the same correlation matrix as calculate_consolidated_portfolio_var. Both
    call the same _build_individual_positions_list and _build_position_level_
    correlation_matrix helpers, then the same calculate_portfolio_var_cov_mixed_t.
    The result is identical to consolidated_var by construction.

    === EXPOSURE-WEIGHTED EFFECTIVE T — DISPLAY ONLY (V3.6 — no longer used for simulation math) ===

    Each currency in a period may have multiple positions at different T values
    (e.g. a cash position at T=1 and a forward at T=45, both for USD, in a "3m"
    view). This function still computes a single representative T per currency:

        effective_T = Σ(|signed_base_i| × T_i) / Σ|signed_base_i|
                      for all positions of that currency in this period

    Prior to V3.6, this was also used to pre-compute vol_term/mu_term for the
    scenario simulation sliders — an approximation that could diverge sharply
    from the exact component_var it was meant to approximate (see
    dashboard_engine.py's module docstring, "V3.6 — vol_term/mu_term REMOVED",
    for the bug history: jumps of several hundred percent, and even sign
    flips, were observed for currencies with multi-position or strongly
    diversifying structures). As of V3.6, the simulation sliders use the
    EXACT vol_part/drift_part decomposition below instead, which needs no
    single-T summary at all — each position's own real T is already baked
    into vol_part_c and drift_part_c via the full covariance matrix.

    effective_T is retained in this function's output ONLY because it is
    still shown to the user as a display field (the Chart Detail Panel's
    "Eff. horizon" row, in dashboard.js's renderChartDetailPanel()) — purely
    informational, not load-bearing for any VaR or CFaR math.

    Args:
        cash_positions: List of cash dicts (currency, balance).
        exposures:      List of forward exposure dicts (currency, amount,
                        settlement_date, direction).
        base_ccy:       Company home currency (e.g. 'SGD').
        market_data:    Pre-fetched market data from fetch_market_data_batch().
                        Must include 'ann_vol', 'daily_mean', 'spot_rate', 'returns'.
        confidence:     VaR confidence level (e.g. 0.95).

    Returns:
        Dict keyed by period key ('1m', '3m', '6m', '12m', 'all'). Each value:
        {
            'period_var':     float,  — consolidated VaR for this time window
            'period_var_raw': float,  — before flooring to 0
            'label':          str,    — 'Next 3 months'
            'max_days':       int|None, — position T cutoff (None = all)
            'n_positions':    int,    — number of individual positions included
            'currencies': {
                'USD': {
                    'net_signed_base':   float,  — signed net exposure (+ long, − short)
                    'net_notional_base': float,  — |net_signed_base|
                    'net_direction':     str,    — 'long', 'short', or 'flat'
                    'component_var':     float,  — this currency's risk contribution
                                                    sums across currencies = period_var
                    'vol_part':          float,  — V3.6: volatility-driven part of
                                                    component_var. Scales EXACTLY
                                                    linearly under a uniform vol-
                                                    regime shift — see
                                                    _compute_component_vars_by_currency's
                                                    docstring for the proof.
                    'drift_part':        float,  — V3.6: drift-driven part of
                                                    component_var (SIGNED). Constant
                                                    under a vol-regime shift.
                                                    component_var = vol_part - drift_part
                                                    exactly, always.
                    'ann_vol':           float,  — σ_annual for this currency pair
                    'daily_mean':        float,  — μ_daily for this currency pair
                    'effective_T':       float,  — exposure-weighted avg T. DISPLAY
                                                    ONLY as of V3.6 (see above) — no
                                                    longer feeds any simulation math.
                    'spot_rate':         float,  — current spot rate (base per 1 foreign)
                },
                ...
            }
        }
    """
    from scipy.stats import norm
    z = norm.ppf(confidence)   # confidence-level Z-score (e.g. 1.6449 for 95%)

    # -------------------------------------------------------------------------
    # Step 1: Build the complete individual position list once.
    # This is shared with calculate_consolidated_portfolio_var via the helper,
    # ensuring the 'all' period produces exactly the same result.
    # cash_horizon is deliberately NOT passed here — see CASH_CONSOLIDATED_T_DAYS.
    # -------------------------------------------------------------------------
    all_positions = _build_individual_positions_list(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        market_data    = market_data,
    )

    # -------------------------------------------------------------------------
    # Step 2: Compute VaR and component attribution for each cumulative period.
    # -------------------------------------------------------------------------
    result = {}

    for period_def in CUMULATIVE_PERIOD_DEFINITIONS:
        key      = period_def['key']
        label    = period_def['label']
        max_days = period_def['max_days']

        # --- Filter positions to those within this cumulative time window ---
        # 'all' has max_days=None → no filtering, include every position.
        if max_days is None:
            period_positions = all_positions
        else:
            # Keep only positions whose actual T is within this window.
            # Example: for '3m' (max_days=63), include positions with t_days ≤ 63.
            # This spans Buckets 1 (0-21) and 2 (21-63) cumulatively.
            period_positions = [p for p in all_positions if p['t_days'] <= max_days]

        # Handle the empty-period case (no positions settle within this window)
        if not period_positions:
            result[key] = {
                'period_var':     0.0,
                'period_var_raw': 0.0,
                'label':          label,
                'max_days':       max_days,
                'n_positions':    0,
                'currencies':     {},
            }
            continue

        # --- Build position-level correlation matrix for this period's subset ---
        # Uses the same ρ rules as the full consolidated_var matrix.
        # For 'all', this is identical to calculate_consolidated_portfolio_var's matrix.
        full_corr = _build_position_level_correlation_matrix(
            positions   = period_positions,
            market_data = market_data,
        )

        # --- Compute consolidated VaR for this period ---
        # Exact min(Ti,Tj) formula: Σ[i,j] = ρ[i,j] × σᵢ_daily × σⱼ_daily × min(Tᵢ,Tⱼ)
        # Portfolio VaR = Z × √(sᵀ Σ s) − sᵀ μ_T
        var_f, var_r = calculate_portfolio_var_cov_mixed_t(
            signed_exposures_base = [p['signed_base'] for p in period_positions],
            ann_vols              = [p['ann_vol']     for p in period_positions],
            daily_means           = [p['daily_mean']  for p in period_positions],
            t_values              = [p['t_days']      for p in period_positions],
            correlation_matrix    = full_corr,
            confidence_level      = confidence,
        )

        # --- Compute component VaR decomposition ---
        # ComponentVaR_ccy = Σᵢ[(sᵢ × (Σs)ᵢ / σ_p) × Z − sᵢ × μᵢ × Tᵢ]
        # summed over all positions of that currency in this period.
        # Sum across all currencies = period_var (by construction).
        #
        # V3.6: also returns vol_part_ccy and drift_part_ccy per currency —
        # the exact split that lets the frontend simulation sliders scale
        # Component CFaR EXACTLY under a vol-regime shift, rather than via
        # the old net_notional_base-derived approximation. See
        # _compute_component_vars_by_currency's docstring for the full proof.
        component_vars_by_ccy = _compute_component_vars_by_currency(
            positions   = period_positions,
            corr_matrix = full_corr,
            z           = z,
        )

        # --- Aggregate per-currency net notionals and effective T ---
        # Net notional: sum of signed_base across all positions of each currency.
        # Exposure-weighted effective T: DISPLAY ONLY as of V3.6 (see this
        # function's "EXPOSURE-WEIGHTED EFFECTIVE T" docstring section above)
        # — no longer used to compute any simulation value.
        ccy_accum: dict[str, dict] = {}
        for pos in period_positions:
            ccy = pos['currency']
            if ccy not in ccy_accum:
                # First position for this currency — initialise accumulators.
                # ann_vol and daily_mean are the same for all positions of the same
                # currency (fetched once per currency from market_data).
                md = market_data.get(ccy, {})
                ccy_accum[ccy] = {
                    'net_signed_base': 0.0,
                    'total_abs_base':  0.0,   # for exposure-weighted T denominator
                    'weighted_T_sum':  0.0,   # for exposure-weighted T numerator
                    'ann_vol':         float(md.get('ann_vol', 0.0)),
                    'daily_mean':      float(md.get('daily_mean', 0.0)),
                    'spot_rate':       float(md.get('spot_rate', 1.0)),
                }
            ccy_accum[ccy]['net_signed_base'] += pos['signed_base']
            ccy_accum[ccy]['total_abs_base']  += abs(pos['signed_base'])
            # Weighted-T accumulator: |exposure| × T for each position
            ccy_accum[ccy]['weighted_T_sum']  += abs(pos['signed_base']) * pos['t_days']

        # --- Build final currency dicts for this period ---
        currencies: dict[str, dict] = {}
        for ccy, acc in ccy_accum.items():
            net_signed = acc['net_signed_base']
            net_abs    = abs(net_signed)

            # Direction threshold: ±0.01 base currency to avoid floating-point
            # noise from near-perfectly-hedged currency pairs declaring a direction.
            if net_signed > 0.01:
                net_dir = 'long'
            elif net_signed < -0.01:
                net_dir = 'short'
            else:
                net_dir = 'flat'

            # Exposure-weighted average T — DISPLAY ONLY as of V3.6 (shown in
            # the Chart Detail Panel's "Eff. horizon" row; no longer feeds any
            # simulation math — see this function's docstring).
            # Example: USD cash (T=CASH_CONSOLIDATED_T_DAYS=10, notional=1M) +
            #   USD forward recv (T=45, notional=2M)
            #   effective_T = (1M×10 + 2M×45) / (1M + 2M) = 100M / 3M ≈ 33.3
            # The fallback below (used only if total_abs_base is 0 — meaning no
            # position actually contributed to this currency, an edge case that
            # should not occur in practice) also uses the fixed constant rather
            # than cash_horizon, for full consistency with the rest of this
            # function's cash treatment.
            effective_T = (
                acc['weighted_T_sum'] / acc['total_abs_base']
                if acc['total_abs_base'] > 0
                else float(CASH_CONSOLIDATED_T_DAYS)
            )

            # V3.6: pull the exact vol_part/drift_part split for this currency
            # (defaulting to an all-zero dict for a currency that somehow has
            # no positions in component_vars_by_ccy — should not occur in
            # practice, since ccy_accum and component_vars_by_ccy are built
            # from the exact same period_positions list, but guarded
            # defensively rather than assuming dict key presence).
            ccy_component = component_vars_by_ccy.get(
                ccy, {'component_var': 0.0, 'vol_part': 0.0, 'drift_part': 0.0}
            )

            currencies[ccy] = {
                'net_signed_base':   round(net_signed,                        2),
                'net_notional_base': round(net_abs,                           2),
                'net_direction':     net_dir,
                # component_var: this currency's exact risk contribution.
                # Negative = this currency is acting as a net hedge to the portfolio.
                'component_var':     round(float(ccy_component['component_var']), 2),
                # vol_part / drift_part: V3.6 exact decomposition for the
                # frontend simulation sliders. component_var always equals
                # vol_part - drift_part exactly (not just approximately) —
                # both are rounded to the same precision here so that
                # identity is preserved even after JSON round-tripping.
                'vol_part':          round(float(ccy_component['vol_part']),   2),
                'drift_part':        round(float(ccy_component['drift_part']), 2),
                'ann_vol':           acc['ann_vol'],
                'daily_mean':        acc['daily_mean'],
                'effective_T':       round(effective_T,                        1),
                'spot_rate':         acc['spot_rate'],
            }

        result[key] = {
            'period_var':     round(float(var_f), 2),
            'period_var_raw': round(float(var_r), 2),
            'label':          label,
            'max_days':       max_days,
            'n_positions':    len(period_positions),
            'currencies':     currencies,
        }

    return result


# =============================================================================
# MAIN ENTRY POINT — called by app.py and engine_runner.py
# =============================================================================

def calculate_fx_var(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    confidence:     float = 0.95,
    period:         str   = '1y',
    cash_horizon:   int   = 1
) -> dict:
    """
    Main V3 entry point. Computes the full three-section FX VaR output.

    === THREE-SECTION OUTPUT ===

    SECTION 1 — Spot book risk (standalone, T = cash_horizon):
        VaR on current cash holdings at the user-specified horizon.
        Covariance-adjusted total + diversification benefit added (V2.2).
        Completely independent of forward exposures.

    SECTION 2 — Unified bucketed risk (cash + forwards, covariance-adjusted):
        Cash positions are treated as synthetic receivables settling in
        1 trading day, routing them into Bucket 1 alongside any same-currency
        near-term payables/receivables. This allows cash to net against
        Bucket 1 forward obligations — fixing the V2 limitation where cash
        and forwards were completely separate pipelines.
        All five buckets use covariance-adjusted VaR across currencies (V2.2).
        Natural hedging benefit shown per currency where netting occurred.
        Diversification benefit shown per bucket where 2+ currencies exist.

    SECTION 3 — Gross attribution (reference, forwards only, no netting):
        Shows each forward exposure's standalone VaR at its bucket T.
        Used to illustrate what risk would have been without any netting.
        Cash positions are NOT included here — this is a forwards-only reference.

    Args:
        cash_positions: List of cash dicts with 'currency' and 'balance'.
        exposures:      List of future exposure dicts with 'currency', 'amount',
                        'settlement_date', and 'direction'.
        base_ccy:       Company home currency (e.g. 'SGD').
        confidence:     VaR confidence level (default 0.95).
        period:         Historical lookback for yfinance (default '1y').
        cash_horizon:   T in trading days for Section 1 spot VaR (default 1).

    Returns:
        {
            'base_ccy':          str,
            'confidence':        float,
            'cash_horizon':      int,

            'spot_risk':         dict,   # Section 1 — standalone cash VaR
            'unified_buckets':   dict,   # Section 2 — cash + forwards unified
            'gross_attribution': dict,   # Section 3 — reference, no netting
        }
    """
    # -----------------------------------------------------------------------
    # Step 1: Batch-fetch all market data once.
    # -----------------------------------------------------------------------
    all_currencies = (
        [pos['currency'] for pos in cash_positions] +
        [exp['currency'] for exp in exposures]
    )
    print(f"  Fetching market data for {len(set(c.upper() for c in all_currencies if c.upper() != base_ccy.upper()))} unique currency pairs…")
    market_data = fetch_market_data_batch(all_currencies, base_ccy, period)

    # -----------------------------------------------------------------------
    # Step 2: Section 1 — standalone spot book VaR at cash_horizon.
    # Uses V1 calculate_portfolio_var for individual breakdowns,
    # then augments with covariance-adjusted total (V2.2).
    # -----------------------------------------------------------------------
    print("  Section 1 — spot book VaR (standalone, T={})…".format(cash_horizon))
    spot_result = calculate_portfolio_var(
        positions        = cash_positions,
        base_ccy         = base_ccy,
        confidence_level = confidence,
        period           = period,
        days             = cash_horizon,
    )
    _add_covariance_to_spot_risk(spot_result, market_data, confidence)

    # -----------------------------------------------------------------------
    # Step 3: Section 2 — unified bucketed VaR (cash + forwards).
    # Convert cash positions to synthetic Bucket 1 receivables (settlement
    # = 1 trading day from today → always falls in Bucket 1: 0–21 days).
    # Combine with actual forward exposures and run bucket netting + covariance.
    # -----------------------------------------------------------------------
    print("  Section 2 — unified bucketed VaR (cash in Bucket 1 + forwards)…")
    settle_bucket1 = trading_days_to_date(1)

    cash_as_receivables = [
        {
            'currency':        pos['currency'].upper().strip(),
            'amount':          float(pos['balance']),
            'settlement_date': settle_bucket1,
            'direction':       'receivable',
            '_source':         'cash',   # label for attribution display
        }
        for pos in cash_positions
        if pos['currency'].upper().strip() != base_ccy.upper()
    ]

    unified_exposures = cash_as_receivables + exposures

    _, bucket_results, bucket_errors = calculate_bucketed_forward_var(
        exposures    = unified_exposures,
        base_ccy     = base_ccy,
        market_data  = market_data,
        confidence   = confidence,
    )

    # -----------------------------------------------------------------------
    # Step 4: Section 3 — gross attribution (forwards only, no netting).
    # Cash positions are NOT included here — this is a reference view of
    # forward exposures before any netting is applied.
    # -----------------------------------------------------------------------
    print("  Section 3 — gross attribution (forwards only, no netting)…")
    _, gross_exposures, gross_errors = calculate_gross_forward_var(
        exposures    = exposures,
        base_ccy     = base_ccy,
        market_data  = market_data,
        confidence   = confidence,
        use_bucket_t = True,
    )

    # -----------------------------------------------------------------------
    # Step 4b: Cash's contribution to Gross Standalone Risk, at the fixed
    # CASH_CONSOLIDATED_T_DAYS horizon — NOT cash_horizon. This keeps the
    # Gross Standalone Risk / Risk Reduction dashboard cards fully isolated
    # from the Cash VaR Horizon dropdown, which affects only Section 1
    # (Cash Book Risk) above. See calculate_gross_cash_var's docstring.
    # -----------------------------------------------------------------------
    print("  Gross cash attribution (fixed T={} — independent of cash_horizon)…".format(
        CASH_CONSOLIDATED_T_DAYS))
    _, gross_cash_exposures, gross_cash_errors = calculate_gross_cash_var(
        cash_positions = cash_positions,
        base_ccy       = base_ccy,
        market_data    = market_data,
        confidence     = confidence,
    )

    # -----------------------------------------------------------------------
    # Step 5: Consolidated portfolio VaR (V2.4).
    # Single number across ALL positions (cash + all forwards) using variance
    # aggregation with per-currency exposure-weighted effective T values.
    #
    # This solves the cross-horizon aggregation problem: instead of summing
    # bucket VaRs (which conflates different T values), each currency's net
    # position enters a single covariance matrix at its own effective T,
    # and the portfolio VaR is extracted once from the combined P&L distribution.
    #
    # NOTE: cash_horizon is deliberately NOT passed below. Cash's T inside
    # this calculation is the fixed CASH_CONSOLIDATED_T_DAYS constant (10
    # trading days), independent of the Cash VaR Horizon dropdown — that
    # dropdown affects only Section 1 (Cash Book Risk) above. See the
    # CASH_CONSOLIDATED_T_DAYS comment near _build_individual_positions_list
    # for the full rationale.
    #
    # See calculate_consolidated_portfolio_var for full methodology details.
    # -----------------------------------------------------------------------
    print("  Step 5 — consolidated portfolio VaR (V2.4 mixed-T covariance)…")
    consolidated_var = calculate_consolidated_portfolio_var(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        market_data    = market_data,
        confidence     = confidence,
    )

    # -----------------------------------------------------------------------
    # Step 6: Cumulative period VaRs (V3 dashboard feature).
    # Computes a consolidated VaR for each cumulative time window (1m, 3m, etc.),
    # using the SAME min(Ti,Tj) method as consolidated_var but restricted to
    # positions settling within the window. The 'all' period equals consolidated_var.
    # Also computes component VaR decomposition (per-currency risk attribution)
    # for the bar chart. See calculate_cumulative_period_vars for full details.
    #
    # NOTE: cash_horizon is deliberately NOT passed below, for the same reason
    # as Step 5 above — every Dashboard period view (including the Component
    # CFaR bars) is unaffected by the Cash VaR Horizon dropdown.
    # -----------------------------------------------------------------------
    print("  Step 6 — cumulative period VaRs (V3 dashboard filter)…")
    cumulative_vars = calculate_cumulative_period_vars(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        market_data    = market_data,
        confidence     = confidence,
    )

    return {
        'base_ccy':     base_ccy,
        'confidence':   float(confidence),
        'cash_horizon': int(cash_horizon),

        # Section 1: standalone spot book VaR (unchanged from V1 per-position).
        # V2.2 additions: total_var_cov, diversification_benefit.
        'spot_risk': spot_result,

        # Section 2: unified bucketed VaR — cash in Bucket 1 + all forwards.
        # bucket_var: covariance-adjusted (V2.2).
        # bucket_var_simple: old simple sum (for comparison).
        # diversification_benefit: bucket_var_simple − bucket_var.
        # hedge_benefit per currency: VaR saved by within-currency netting.
        # source field in attribution: 'cash' or 'forward'.
        'unified_buckets': {
            'buckets': bucket_results,
            'errors':  bucket_errors,
        },

        # Section 3: gross per-exposure standalone VaRs at bucket T.
        # Forwards only — no cash, no netting. Reference use only.
        'gross_attribution': {
            'exposures': gross_exposures,
            'errors':    gross_errors,
        },

        # Cash's standalone VaR at the fixed CASH_CONSOLIDATED_T_DAYS horizon
        # (10 trading days) — independent of the Cash VaR Horizon dropdown.
        # Used by dashboard_engine.py as the cash component of Gross Standalone
        # Risk, so that stat card (and Risk Reduction) stay fully isolated
        # from cash_horizon, which affects only spot_risk above.
        'gross_cash_attribution': {
            'exposures': gross_cash_exposures,
            'errors':    gross_cash_errors,
        },

        # Consolidated portfolio VaR (V2.4): single number across the full
        # portfolio — cash + all forwards — using per-currency net positions
        # with exposure-weighted effective T and cross-currency covariance.
        # Answers: "what is my total FX risk right now in one number?"
        # methodology: 'exact_individual_position_min_t'
        # position_breakdown: one entry per individual exposure, sorted by size.
        'consolidated_var': consolidated_var,

        # Cumulative period VaRs (V3): one entry per cumulative time window.
        # Each uses the same min(Ti,Tj) method as consolidated_var, restricted
        # to positions settling within the window. Includes component VaR
        # decomposition per currency for the dashboard bar chart.
        # The 'all' key's period_var equals consolidated_var['total_var'] exactly.
        'cumulative_vars': cumulative_vars,
    }


# =============================================================================
# HEDGE RECOMMENDATION ENGINE — V3.8
# =============================================================================
#
# Public entry point:  recommend_hedges()
# Private helper:      _identify_hedge_candidates()
#
# PURPOSE
# -------
# Given an existing portfolio (cash + forwards), this engine identifies which
# forward-hedge contracts would most reduce Portfolio VaR, and ranks them by
# marginal impact so a treasurer can prioritise their hedging activity.
#
# CONCEPTUAL NOTE: WHY FORWARD CONTRACTS AS OPPOSING EXPOSURES?
# -------------------------------------------------------------
# A hedging forward (e.g. buy MYR at 3.20 on 20-Aug) has a LOCKED settlement
# rate, which might suggest it has no outcome uncertainty. However, Portfolio
# VaR measures variance of mark-to-market value, not settlement-rate uncertainty.
# The forward's mark-to-market value fluctuates with spot rates daily, in exactly
# the opposite direction to the underlying exposure it hedges. Under the signed-
# exposure vector:
#     s = [+E (receivable), −E (hedge payable)]
# both the volatility term (sᵀ Σ s) and the drift term (sᵀ μ_T) cancel exactly
# to zero for a perfectly matched hedge — the same result regardless of whether
# the locked rate F is modelled explicitly or not. F is a constant that shifts
# expected P&L but does not affect variance. So treating the hedging forward as a
# regular opposing exposure is not a conceptual shortcut but a mathematically
# exact result. See design docs / README for the full derivation.
#
# WHAT IS NOT MODELLED (known PoC limitations):
#   - Hedging costs (bid-ask spread, bank credit lines, collateral) — all hedges
#     are assumed costless in this model. In practice a high-cost hedge on a small
#     exposure might not be worth entering. See Known Limitations in README.
#   - Options — this engine only proposes forward contracts (linear instruments).
#     The delta-normal VaR model cannot represent option payoffs, which are
#     non-linear in spot rate. Options are a V4+ feature (requires Monte Carlo).
#   - Cross-currency proxy hedges — only direct same-currency forwards are proposed.
#     Using a correlated currency to hedge an illiquid one introduces basis risk
#     beyond this engine's scope.


def _identify_hedge_candidates(
    exposures:   list[dict],
    base_ccy:    str,
    market_data: dict,
) -> list[dict]:
    """
    Identifies hedge opportunities from the forward exposure list.

    For each (currency, bucket) pair that has a non-trivial net forward
    exposure, proposes an offsetting forward contract at the bucket midpoint T.

    === WHAT IS A HEDGE CANDIDATE? ===

    A hedge candidate is a proposed forward contract that opposes the existing
    net forward position in a (currency, bucket) pair:
        - Net receivable in a currency/bucket → propose payable (sell FCY forward)
        - Net payable in a currency/bucket    → propose receivable (buy FCY forward)

    The proposed notional equals the absolute net FCY notional for that group,
    so that — if entered — it would drive net notional to zero in that
    (currency, bucket) pair.

    === CASH IS DELIBERATELY EXCLUDED ===

    Cash positions are NOT included in candidate generation:
        - Cash is a physical asset the company already holds; it does not need
          a forward contract to "hedge" it. The cash itself IS the hedge for
          same-currency Bucket 1 payables.
        - Cash's natural offset against forward obligations is already captured
          in the Consolidated Portfolio VaR (and therefore in the VaR reduction
          measured by recommend_hedges) — it doesn't need to appear as a
          separate hedge candidate.
        - Proposing "sell your USD cash via a forward" would confuse a treasurer
          who already holds the cash and does not need that transaction.

    This means: if a user holds USD 2mn cash AND has a USD 1mn Bucket 2 payable,
    the hedge candidate for that payable will be "buy USD 1mn forward (Bucket 2
    T=42d)". The cash has nothing to do with the forward hedge candidate.

    === THRESHOLD ===

    Candidates with abs(net_notional_base) < 1.0 are skipped as floating-point
    noise (e.g. a near-perfectly-netted forward pair in the same bucket where
    only a rounding residual remains). The threshold is 1 unit of base currency
    — small enough to be invisible on any real P&L statement.

    Args:
        exposures:   Forward exposure list from the user (currency, amount,
                     settlement_date, direction). Must NOT include cash synthetic
                     receivables — pass only the original user-supplied list.
        base_ccy:    Company home currency (e.g. 'SGD').
        market_data: Pre-fetched market data from fetch_market_data_batch().
                     Must include 'spot_rate' and 'error' per currency.

    Returns:
        List of candidate dicts (UNSORTED — recommend_hedges ranks them).
        Each dict contains:
            'currency':              str   — FCY ISO code
            'bucket_num':            int   — bucket number (1–5)
            'bucket_label':          str   — human-readable bucket window
            'bucket_midpoint_t':     int   — trading days T used for the hedge
            'net_notional_fcy':      float — signed net FCY across all forwards in
                                             this (ccy, bucket) — positive = net
                                             receivable, negative = net payable
            'net_notional_base':     float — net_notional_fcy × spot_rate (signed,
                                             base currency)
            'hedge_direction':       str   — 'payable'    (if net receivable > 0,
                                              propose selling FCY)
                                             'receivable' (if net payable < 0,
                                              propose buying FCY)
            'hedge_amount_fcy':      float — abs(net_notional_fcy) — proposed
                                             contract notional in FCY
            'hedge_settlement_date': str   — 'YYYY-MM-DD' at bucket midpoint T
            'spot_rate':             float — current spot rate (base per 1 FCY)
    """
    # Accumulate net SIGNED FCY notional per (currency, bucket_num).
    # Signed convention: receivable = +FCY (long), payable = -FCY (short).
    # defaultdict ensures first-access initialises to 0.0 without key checks.
    net_fcy: dict[tuple, float] = defaultdict(float)

    # Bucket metadata keyed by (ccy, bucket_num) — same values for all
    # exposures in the same group, so we just overwrite; idempotent.
    bucket_meta: dict[tuple, dict] = {}

    for exp in exposures:
        ccy       = exp['currency'].upper().strip()
        direction = exp.get('direction', '').lower().strip()
        amount    = float(exp.get('amount', 0))

        # Skip base currency (no FX risk) and invalid directions
        if ccy == base_ccy.upper():
            continue
        if direction not in VALID_DIRECTIONS:
            continue

        # Skip currencies where market data fetch failed
        md = market_data.get(ccy)
        if md is None or md.get('error') is not None:
            continue

        # Skip already-settled exposures
        actual_days = count_trading_days(exp['settlement_date'])
        if actual_days < 1:
            continue

        bucket = assign_to_bucket(actual_days)
        key    = (ccy, bucket['num'])

        # Receivable: +FCY (we will receive this currency — long position)
        # Payable:    -FCY (we need to pay this currency — short position)
        signed_fcy = amount if direction == 'receivable' else -amount
        net_fcy[key] += signed_fcy

        if key not in bucket_meta:
            bucket_meta[key] = {
                'bucket_num':        bucket['num'],
                'bucket_label':      bucket['label'],
                'bucket_midpoint_t': bucket['midpoint_days'],
                'spot_rate':         float(md['spot_rate']),
            }

    # Build candidate list, skipping trivially small residuals
    candidates = []
    for (ccy, _bucket_num), net_fcy_val in net_fcy.items():
        meta     = bucket_meta[(ccy, _bucket_num)]
        spot     = meta['spot_rate']
        net_base = net_fcy_val * spot

        # Ignore near-zero residuals (< 1 unit of base currency).
        # These arise from near-perfectly-netted pairs where only
        # floating-point rounding remains — not worth recommending a contract.
        if abs(net_base) < 1.0:
            continue

        # Hedge direction is the OPPOSITE of the net position:
        #   net > 0 (net receivable) → we have FCY coming in →
        #       hedge by selling FCY forward → propose payable
        #   net < 0 (net payable)    → we need to pay FCY →
        #       hedge by buying FCY forward → propose receivable
        hedge_dir        = 'payable' if net_fcy_val > 0 else 'receivable'
        hedge_settlement = trading_days_to_date(meta['bucket_midpoint_t'])

        candidates.append({
            'currency':              ccy,
            'bucket_num':            meta['bucket_num'],
            'bucket_label':          meta['bucket_label'],
            'bucket_midpoint_t':     meta['bucket_midpoint_t'],
            'net_notional_fcy':      round(net_fcy_val, 2),
            'net_notional_base':     round(net_base,    2),
            'hedge_direction':       hedge_dir,
            'hedge_amount_fcy':      round(abs(net_fcy_val), 2),
            'hedge_settlement_date': hedge_settlement,
            'spot_rate':             spot,
        })

    return candidates


def recommend_hedges(
    cash_positions: list[dict],
    exposures:      list[dict],
    base_ccy:       str,
    confidence:     float = 0.95,
    period:         str   = '1y',
) -> dict:
    """
    Hedge Recommendation Engine (V3.8). Identifies which forward contracts
    would most reduce Consolidated Portfolio VaR, ranked by marginal impact.

    === ALGORITHM ===

    1. Fetch market data once — reused for all VaR re-computations.
    2. Compute BASELINE Consolidated Portfolio VaR (before any hedges).
    3. Compute BASELINE Component CFaRs (from 'all' cumulative period) —
       used to rank currencies by their risk contribution.
    4. Identify hedge CANDIDATES from forward exposures only (not cash) via
       _identify_hedge_candidates(). One candidate per (currency, bucket)
       where net forward FCY ≠ 0.
    5. RANK candidates:
           Primary key:   abs(Component CFaR of that currency) — descending.
                          This correctly accounts for cross-currency covariance:
                          a large-notional currency that diversifies against
                          another will have a LOWER Component CFaR and rank lower
                          than a smaller-notional currency that drives portfolio risk.
           Secondary key: abs(net_notional_base) of this specific (ccy, bucket)
                          pair — descending. Tie-breaks within the same currency.
    6. CUMULATIVE APPLICATION: apply hedges one at a time in ranked order.
       After each hedge is appended to the running exposure list,
       calculate_consolidated_portfolio_var is re-run with the full
       min(Tᵢ,Tⱼ) covariance matrix — so each subsequent hedge's impact
       correctly reflects the changed portfolio structure (including all
       previously applied hedges).
    7. Return ranked table with before/after Portfolio VaR, marginal reduction
       (this hedge's incremental impact on top of previous hedges), and
       cumulative reduction (total from baseline to this point).

    === WHY RE-RUN THE ENGINE EACH ITERATION? ===

    The impact of hedge N depends on hedges 1..N-1 already being in place —
    cross-currency covariance terms change as the portfolio structure changes.
    A simple "each hedge reduces portfolio VaR by its Component CFaR" formula
    would be wrong because Component CFaRs are computed for the ORIGINAL portfolio;
    they don't auto-update when hedges are added. Full re-computation is necessary
    for honest marginal-reduction figures.

    === MARKET DATA EFFICIENCY ===

    Market data is fetched ONCE at the start and reused across all re-computations.
    Each hedge iteration calls calculate_consolidated_portfolio_var directly
    (bypassing calculate_fx_var's full three-section computation) so only the
    consolidated VaR is recalculated — not bucketed VaR, gross attribution, etc.

    === INPUTS ===

    Same structure as calculate_fx_var — no new required inputs. The route
    handler at app.py reuses _parse_and_validate_request, so the same JSON
    payload validates for both /calculate and /recommend_hedges.

    Note: cash_horizon is deliberately NOT an input here. The consolidated
    Portfolio VaR used for recommendations always uses CASH_CONSOLIDATED_T_DAYS
    (10 trading days) for cash positions — the same convention used by
    calculate_consolidated_portfolio_var. This keeps the recommendation results
    independent of the Cash VaR Horizon dropdown, consistent with all other
    Portfolio VaR / Risk Dashboard figures.

    Args:
        cash_positions: List of cash position dicts ({currency, balance}).
        exposures:      List of forward exposure dicts ({currency, amount,
                        settlement_date, direction}).
        base_ccy:       Company home currency (e.g. 'SGD').
        confidence:     VaR confidence level (e.g. 0.95 for 95%).
        period:         yfinance historical lookback (e.g. '1y').

    Returns:
        {
            'base_ccy':                   str,   — home currency
            'baseline_var':               float, — Portfolio VaR before any hedges
            'recommendations': [          — ranked list, one entry per candidate
                {
                    'rank':                     int,
                    'currency':                 str,
                    'bucket_num':               int,
                    'bucket_label':             str,
                    'bucket_midpoint_t':        int,   — T used for the hedge
                    'net_notional_fcy':         float, — signed net forward FCY
                    'net_notional_base':        float, — net_notional_fcy × spot
                    'component_cfar_baseline':  float, — currency's Component CFaR
                                                         in the ORIGINAL portfolio
                                                         (used for ranking; changes
                                                         as hedges are added)
                    'hedge_direction':          str,   — 'payable' or 'receivable'
                    'hedge_amount_fcy':         float, — proposed contract size (FCY)
                    'hedge_settlement_date':    str,   — 'YYYY-MM-DD'
                    'hedge_settlement_t':       int,   — same as bucket_midpoint_t
                    'spot_rate':                float, — current rate (display only)
                    'portfolio_var_before':     float, — VaR just before this hedge
                    'portfolio_var_after':      float, — VaR just after this hedge
                    'marginal_reduction_abs':   float, — this hedge's own reduction
                    'marginal_reduction_pct':   float, — as % of portfolio_var_before
                    'cumulative_reduction_abs': float, — total reduction from baseline
                    'cumulative_reduction_pct': float, — as % of baseline_var
                }
            ],
            'fully_hedged_var':            float, — VaR after ALL hedges applied
            'fully_hedged_reduction_pct':  float, — total % reduction
            'errors':                      list,  — any market data issues
        }
    """
    # -------------------------------------------------------------------------
    # Step 1: Fetch market data once — reused for every VaR re-computation.
    # Same batch approach as calculate_fx_var; results cached in market_data dict.
    # -------------------------------------------------------------------------
    print("recommend_hedges: fetching market data…")
    all_ccys   = (
        [p['currency'] for p in cash_positions] +
        [e['currency'] for e in exposures]
    )
    market_data = fetch_market_data_batch(all_ccys, base_ccy, period)

    # Collect any fetch errors for the response (currencies that will be skipped)
    fetch_errors = [
        {'currency': ccy, 'reason': md['error']}
        for ccy, md in market_data.items()
        if md.get('error') is not None
    ]

    # -------------------------------------------------------------------------
    # Step 2: Compute BASELINE Consolidated Portfolio VaR.
    # This is the "before any hedges" figure — the starting point for all
    # reduction calculations below.
    # -------------------------------------------------------------------------
    print("recommend_hedges: computing baseline consolidated VaR…")
    baseline_result = calculate_consolidated_portfolio_var(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        market_data    = market_data,
        confidence     = confidence,
    )
    baseline_var = baseline_result['total_var']

    # -------------------------------------------------------------------------
    # Step 3: Compute BASELINE Component CFaRs for ranking.
    # The 'all' cumulative period covers the entire portfolio and its
    # 'currencies' dict gives each currency's Component CFaR — the single
    # best signal for which currency is driving the most portfolio risk.
    #
    # Component CFaR is preferable to raw notional for ranking because it
    # already encodes cross-currency covariance: a large-notional currency
    # that diversifies against another will have lower Component CFaR than
    # a smaller-notional currency that is the primary risk driver.
    # -------------------------------------------------------------------------
    print("recommend_hedges: computing baseline Component CFaRs for ranking…")
    baseline_cumulative = calculate_cumulative_period_vars(
        cash_positions = cash_positions,
        exposures      = exposures,
        base_ccy       = base_ccy,
        market_data    = market_data,
        confidence     = confidence,
    )
    # 'all' period currencies dict: {ccy: {component_var, vol_part, drift_part, ...}}
    cfar_by_ccy = baseline_cumulative.get('all', {}).get('currencies', {})

    # -------------------------------------------------------------------------
    # Step 4: Identify hedge candidates from FORWARD exposures only.
    # Cash positions are deliberately excluded — see _identify_hedge_candidates
    # docstring for the full rationale.
    # -------------------------------------------------------------------------
    candidates = _identify_hedge_candidates(
        exposures    = exposures,
        base_ccy     = base_ccy,
        market_data  = market_data,
    )

    # Early exit: no hedgeable forward exposures found
    if not candidates:
        return {
            'base_ccy':                   base_ccy,
            'baseline_var':               round(baseline_var, 2),
            'recommendations':            [],
            'fully_hedged_var':           round(baseline_var, 2),
            'fully_hedged_reduction_pct': 0.0,
            'errors':                     fetch_errors,
        }

    # -------------------------------------------------------------------------
    # Step 5: Rank candidates by descending Component CFaR magnitude.
    #
    # Primary key:   abs(Component CFaR of that currency) — largest risk driver
    #                first. Uses baseline 'all' period Component CFaR.
    # Secondary key: abs(net_notional_base) of this (ccy, bucket) pair —
    #                tie-breaks within the same currency (larger bucket first).
    #
    # Note: we rank by CURRENCY-level Component CFaR even though candidates are
    # per (currency, bucket). This means all MYR buckets will be ranked
    # consecutively (before moving to USD), ordered within MYR by notional.
    # This is intentional: "fix your biggest currency risk driver first, then
    # move to the next" is more natural than interleaving buckets across CCYs.
    # -------------------------------------------------------------------------
    def _rank_key(candidate: dict) -> tuple:
        ccy      = candidate['currency']
        cfar_mag = abs(cfar_by_ccy.get(ccy, {}).get('component_var', 0.0))
        notl_mag = abs(candidate['net_notional_base'])
        # Negative values → sort descending (Python sorts ascending by default)
        return (-cfar_mag, -notl_mag)

    candidates.sort(key=_rank_key)

    # -------------------------------------------------------------------------
    # Step 6: Cumulatively apply hedges, re-running Consolidated VaR each time.
    #
    # running_exposures starts as a COPY of the original forward list.
    # Each iteration appends one hedge forward and re-runs the full covariance
    # calculation. The result captures exactly how much risk each additional
    # hedge removes, given all previously-applied hedges already in place.
    #
    # Each hedge forward entry is a standard exposure dict:
    #     currency, amount, settlement_date, direction
    # The '_is_hedge' flag is metadata for future tooling (e.g. UI distinction
    # between "original exposure" and "proposed hedge") — the VaR engine never
    # reads it (see _build_individual_positions_list — it only uses the four
    # fields above). It is safe to include in the list without affecting math.
    # -------------------------------------------------------------------------
    print(f"recommend_hedges: evaluating {len(candidates)} candidate hedge(s)…")
    running_exposures = list(exposures)   # copy — never modify caller's list
    running_var       = baseline_var
    recommendations   = []

    for rank_idx, candidate in enumerate(candidates):
        ccy = candidate['currency']

        # Build the hedge as a standard forward exposure entry
        hedge_entry = {
            'currency':        ccy,
            'amount':          candidate['hedge_amount_fcy'],
            'settlement_date': candidate['hedge_settlement_date'],
            'direction':       candidate['hedge_direction'],
            '_is_hedge':       True,   # metadata only — not read by VaR math
        }
        running_exposures.append(hedge_entry)

        # Re-run consolidated VaR with this hedge in the exposure list.
        # Uses the same market_data already fetched — no new network call.
        new_result = calculate_consolidated_portfolio_var(
            cash_positions = cash_positions,
            exposures      = running_exposures,
            base_ccy       = base_ccy,
            market_data    = market_data,
            confidence     = confidence,
        )
        new_var = new_result['total_var']

        # Marginal reduction: THIS hedge's own impact on top of prior hedges.
        # running_var holds the VaR from the PREVIOUS iteration (or baseline).
        marginal_abs = running_var - new_var
        marginal_pct = (marginal_abs / running_var * 100) if running_var > 1e-9 else 0.0

        # Cumulative reduction: total impact from the original baseline.
        cumulative_abs = baseline_var - new_var
        cumulative_pct = (cumulative_abs / baseline_var * 100) if baseline_var > 1e-9 else 0.0

        recommendations.append({
            'rank':                     rank_idx + 1,
            'currency':                 ccy,
            'bucket_num':               candidate['bucket_num'],
            'bucket_label':             candidate['bucket_label'],
            'bucket_midpoint_t':        candidate['bucket_midpoint_t'],
            'net_notional_fcy':         candidate['net_notional_fcy'],
            'net_notional_base':        candidate['net_notional_base'],
            # component_cfar_baseline: this currency's Component CFaR in the
            # ORIGINAL (unhedged) portfolio. Used for display context — shows
            # the user WHY this was ranked where it was.
            'component_cfar_baseline':  round(float(
                cfar_by_ccy.get(ccy, {}).get('component_var', 0.0)
            ), 2),
            'hedge_direction':          candidate['hedge_direction'],
            'hedge_amount_fcy':         candidate['hedge_amount_fcy'],
            'hedge_settlement_date':    candidate['hedge_settlement_date'],
            'hedge_settlement_t':       candidate['bucket_midpoint_t'],
            'spot_rate':                candidate['spot_rate'],
            'portfolio_var_before':     round(running_var, 2),
            'portfolio_var_after':      round(new_var,     2),
            'marginal_reduction_abs':   round(marginal_abs,   2),
            'marginal_reduction_pct':   round(marginal_pct,   1),
            'cumulative_reduction_abs': round(cumulative_abs, 2),
            'cumulative_reduction_pct': round(cumulative_pct, 1),
        })

        # Advance running VaR to the new (post-hedge) level for next iteration
        running_var = new_var

    # Final state: fully hedged Portfolio VaR after ALL candidates applied
    fully_hedged_var = running_var
    fully_hedged_pct = (
        (baseline_var - fully_hedged_var) / baseline_var * 100
        if baseline_var > 1e-9 else 0.0
    )

    print(f"recommend_hedges: complete. Baseline {baseline_var:.2f} → "
          f"fully hedged {fully_hedged_var:.2f} ({fully_hedged_pct:.1f}% reduction).")

    return {
        'base_ccy':                   base_ccy,
        'baseline_var':               round(baseline_var,      2),
        'recommendations':             recommendations,
        'fully_hedged_var':            round(fully_hedged_var,  2),
        'fully_hedged_reduction_pct':  round(fully_hedged_pct,  1),
        'errors':                      fetch_errors,
    }
