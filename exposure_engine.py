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
  independently. Cash positions NOT included. This is the before-netting picture,
  useful for reporting how much risk natural hedging removed.

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


def trading_days_to_date(n_days: int) -> str:
    """
    Converts a number of trading days from today into a settlement date string.

    The inverse of count_trading_days: given n trading days, returns the
    calendar date that is n business days (Mon–Fri) from today.

    Used in calculate_combined_var_v2 to convert a cash_horizon (in trading days)
    into a synthetic settlement date, so cash positions can be treated as
    receivables in the forward bucket netting engine.

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
                     calculate_combined_var_v2. The '_source' field is passed
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
    Main V2 entry point. Computes the full three-section FX VaR output.

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
    }
