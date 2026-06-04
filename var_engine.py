"""
var_engine.py — FX Value at Risk computation engine (PoC v1 / V2)

This module is the single source of truth for all FX VaR mathematics.
It is completely independent of Flask, HTML, or any web framework —
it can be imported by the Flask app, run from the command line, or
tested directly without any UI layer.

Architecture note:
  This separation is intentional. When the engine is upgraded (e.g. adding
  Monte Carlo simulation for V3, or swapping yfinance for a live data vendor),
  only this file changes. The Flask app and the HTML frontend are unaffected.

V2 change (minimal):
  calculate_parametric_var() gained a 'direction' parameter ('long' or 'short')
  to correctly handle future payables, where the risk direction is flipped —
  the holder fears FCY appreciation rather than depreciation. All existing V1
  callers are unaffected since 'long' remains the default.
  All other functions (fetch_pair_returns, calculate_portfolio_var, etc.) are
  unchanged from V1. Future exposure logic lives in exposure_engine.py, which
  imports from this module but is never imported by it.
"""

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm


# =============================================================================
# CONSTANTS
# =============================================================================

# Conventional number of trading days in a calendar year, used throughout
# for annualising and de-annualising volatility figures.
TRADING_DAYS_PER_YEAR = 252


# =============================================================================
# TICKER UTILITIES
# =============================================================================

def parse_currencies(ticker: str) -> tuple[str, str]:
    """
    Parses a yfinance forex ticker string into its component currency codes.

    yfinance uses the format "BASEQUOTE=X" for forex pairs.
    e.g. 'SGDUSD=X' -> base='SGD', quote='USD'
         'EURUSD=X' -> base='EUR', quote='USD'

    Args:
        ticker: The yfinance forex ticker string (e.g. 'SGDUSD=X').

    Returns:
        A tuple of (base_currency, quote_currency) as 3-letter ISO codes.
    """
    clean = ticker.replace("=X", "")
    return clean[:3], clean[3:6]


def build_ticker(foreign_ccy: str, base_ccy: str) -> str:
    """
    Constructs the yfinance ticker for a foreign currency quoted against
    the base currency.

    The ticker format is FOREIGNBASE=X, which gives the rate of how many
    units of base_ccy equal 1 unit of foreign_ccy.
    e.g. foreign=USD, base=SGD → 'USDSGD=X' means 1 USD = X SGD.

    Args:
        foreign_ccy: The currency being held (e.g. 'USD').
        base_ccy:    The company's home/reporting currency (e.g. 'SGD').

    Returns:
        yfinance ticker string (e.g. 'USDSGD=X').
    """
    return f"{foreign_ccy}{base_ccy}=X"


# =============================================================================
# MARKET DATA FETCHING
# =============================================================================

def fetch_pair_returns(
    foreign_ccy: str,
    base_ccy:    str,
    period:      str = "1y"
) -> tuple[float, float, pd.Series, float, bool]:
    """
    Fetches historical data for a foreign/base currency pair and computes
    the statistics needed by the VaR formula.

    Tries the direct ticker (e.g. MYRSGD=X) first. If Yahoo Finance returns
    insufficient data for that pair (common for thinly traded or exotic crosses),
    automatically falls back to constructing the rate synthetically via USD:

        spot(FCY/BCY)   = spot(FCY/USD)  × spot(USD/BCY)
        return(FCY/BCY) ≈ return(FCY/USD) + return(USD/BCY)

    The return addition is exact for log-returns and a very close approximation
    for small percentage returns. The two series are trimmed to the same length
    before element-wise operations to handle minor differences in trading
    calendars across markets.

    Args:
        foreign_ccy: The foreign currency being held (e.g. 'MYR').
        base_ccy:    The company's home currency (e.g. 'SGD').
        period:      yfinance lookback period (default '1y').
                     Accepts '6mo', '1y', '2y' etc.

    Returns:
        A tuple of:
            - annualised_vol   (float):      σ_annual = σ_daily × √252
            - daily_mean       (float):      μ_daily = mean of daily pct returns.
                                             Negative = foreign ccy depreciating vs base.
            - daily_returns    (pd.Series):  series of daily percentage returns
            - spot_rate        (float):      most recent closing price (base per 1 foreign)
            - used_cross_rate  (bool):       True if the synthetic USD cross-rate was used

    Raises:
        ValueError if neither the direct nor the cross-rate fetch yields
        sufficient data (minimum 30 data points).
    """
    direct_ticker = build_ticker(foreign_ccy, base_ccy)

    # Attempt 1: direct ticker
    try:
        vol, mu, returns, spot = _fetch_direct(direct_ticker, period)
        return vol, mu, returns, spot, False
    except Exception:
        pass  # Fall through to cross-rate

    # Attempt 2: cross-rate via USD.
    # Not applicable when one leg is already USD — that would be circular.
    if foreign_ccy == 'USD' or base_ccy == 'USD':
        raise ValueError(
            f"Insufficient data for {direct_ticker}. "
            f"Cross-rate via USD is not applicable when one currency is already USD."
        )

    vol, mu, returns, spot = _fetch_cross_rate(foreign_ccy, base_ccy, period)
    return vol, mu, returns, spot, True


def _fetch_direct(ticker: str, period: str) -> tuple[float, float, pd.Series, float]:
    """
    Fetches a single yfinance ticker and computes σ_annual, μ_daily,
    daily returns series, and spot rate.

    This is an internal helper — callers should use fetch_pair_returns()
    which handles the cross-rate fallback automatically.

    Raises:
        ValueError if the ticker returns empty data or fewer than 30 rows.
    """
    data = yf.Ticker(ticker).history(period=period)

    if data.empty:
        raise ValueError(f"No data returned for '{ticker}'.")

    # Most recent closing price = current spot rate (base per 1 foreign)
    spot_rate = data['Close'].iloc[-1]

    # pct_change() computes (P_t - P_{t-1}) / P_{t-1} for each row,
    # producing NaN for the very first row (no prior day to compare against).
    data['Daily_Return'] = data['Close'].pct_change()
    returns = data['Daily_Return'].dropna()

    if len(returns) < 30:
        raise ValueError(f"Insufficient data for '{ticker}': only {len(returns)} returns.")

    # σ_daily: standard deviation of daily percentage returns
    daily_vol = returns.std()

    # μ_daily: mean daily percentage return.
    # Positive = foreign currency appreciated vs base on average over the period.
    # Negative = foreign currency depreciated vs base (e.g. TRY vs USD).
    daily_mean = returns.mean()

    # Annualise volatility using the square-root-of-time rule.
    # Assumes returns are i.i.d. (independently and identically distributed),
    # which is the standard parametric PoC assumption.
    annualised_vol = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)

    return annualised_vol, daily_mean, returns, spot_rate


def _fetch_cross_rate(
    foreign_ccy: str,
    base_ccy:    str,
    period:      str
) -> tuple[float, float, pd.Series, float]:
    """
    Constructs a synthetic FCY/BCY rate via USD as the intermediate currency.

    Many exotic or thinly traded pairs (e.g. MYRSGD, THBSGD) are not
    available directly on Yahoo Finance. The standard market practice is to
    synthesise the rate from two liquid USD legs:

        spot(FCY/BCY)   = spot(FCY/USD)  × spot(USD/BCY)
        return(FCY/BCY) ≈ return(FCY/USD) + return(USD/BCY)

    The return addition is exact for log-returns and very close for small
    percentage returns (the approximation error is second-order, typically
    < 0.001% per day for normal FX moves).

    The two legs may have slightly different data lengths due to differing
    market holidays — we trim both to the shorter series before any
    element-wise operation.

    This is an internal helper — callers should use fetch_pair_returns().

    Raises:
        ValueError if either USD leg returns insufficient data.
    """
    fcy_usd_ticker = f"{foreign_ccy}USD=X"
    usd_bcy_ticker = f"USD{base_ccy}=X"

    # Fetch both USD legs independently
    _, _, fcy_usd_returns, fcy_usd_spot = _fetch_direct(fcy_usd_ticker, period)
    _, _, usd_bcy_returns, usd_bcy_spot = _fetch_direct(usd_bcy_ticker, period)

    # Align series to the same length by trimming the longer one from the front.
    # We keep the most recent data (tail) since that is most relevant for VaR.
    min_len = min(len(fcy_usd_returns), len(usd_bcy_returns))
    fcy_usd_aligned = fcy_usd_returns.iloc[-min_len:].reset_index(drop=True)
    usd_bcy_aligned = usd_bcy_returns.iloc[-min_len:].reset_index(drop=True)

    # Synthetic return series: element-wise sum of the two component return series
    synthetic_returns = fcy_usd_aligned + usd_bcy_aligned

    # Synthetic spot rate: FCY/BCY = FCY/USD × USD/BCY (using most recent closes)
    synthetic_spot = fcy_usd_spot * usd_bcy_spot

    # Compute σ and μ from the synthetic return series
    daily_vol  = synthetic_returns.std()
    daily_mean = synthetic_returns.mean()
    annualised_vol = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)

    return annualised_vol, daily_mean, synthetic_returns, synthetic_spot


# =============================================================================
# VAR FORMULA
# =============================================================================

def calculate_parametric_var(
    exposure_amount:    float,
    annualised_vol:     float,
    daily_mean_return:  float,
    confidence_level:   float = 0.95,
    days:               int   = 1,
    direction:          str   = 'long'
) -> float:
    """
    Calculates the Parametric (Delta-Normal) Value at Risk (VaR) using the
    FULL formula that explicitly accounts for the mean return (drift).

    The general parametric VaR formula from the project spec is:

        VaR = E × (Z_α × σ_T - μ_T)

    Where σ_T and μ_T are both scaled to the same time horizon T:

        σ_T = σ_annual × √(T / 252)    [volatility scales by √T]
        μ_T = μ_daily  × T              [drift scales linearly with T]

    The two different scaling rules reflect fundamental statistical properties:
      - Variance adds linearly across independent days → σ scales by √T
      - Expected return adds linearly across days → μ scales by T

    This asymmetry matters for trending currencies:
      - For stable pairs (e.g. SGD/USD): μ_T is tiny at short horizons because
        μ_daily ≈ 0, so VaR is dominated by the volatility term and the formula
        is nearly symmetric regardless of which direction the pair is quoted.
      - For trending currencies (e.g. TRY/USD): μ_daily is meaningfully
        negative for TRY holders (TRY depreciating), so μ_T grows linearly with
        T and increasingly inflates VaR. At long enough horizons (e.g. 30 days)
        μ_T can exceed Z_α × σ_T entirely, producing a negative raw VaR that
        gets floored to zero — correctly reflecting that USD holders face
        negligible loss risk when TRY has been in sustained decline.

    The direction parameter extends this to future exposures (V2):
      - direction='long'  (cash holding, receivable): formula unchanged from V1.
        Fear = FCY depreciates → left tail of return distribution → subtract μ_T.
      - direction='short' (payable): fear = FCY appreciates → right tail.
        Formula becomes E × (Z × σ_T + μ_T) — positive drift now hurts rather
        than helps, since you need to buy FCY at a higher rate on settlement.
        Implemented by negating μ_T before applying the long formula.

    Key assumptions:
      1. Returns are normally distributed (delta-normal method).
         Reasonable for stable G10 pairs; breaks down for crashing EM currencies
         with fat tails. Monte Carlo with Student's t is planned for V2.
      2. Linear exposures only — the parametric formula cannot price the
         asymmetric payoff of FX options. Monte Carlo handles this in V2.

    Args:
        exposure_amount:   Net exposure in base currency (e.g. SGD).
                           Foreign balance × spot rate (base per foreign).
        annualised_vol:    σ_annual = σ_daily × √252.
        daily_mean_return: μ_daily = mean daily pct return of the pair.
                           Negative for a depreciating foreign currency.
        confidence_level:  e.g. 0.95 for 95%. Z_0.95 ≈ 1.645.
        days:              Time horizon in trading days (e.g. 1, 5, 21, 30, 63).
                           Default is 1. The formula scales σ by √(T/252) and
                           μ by T automatically — no separate scaling step needed.
        direction:         'long'  — holder fears FCY depreciating (cash holding or
                                     receivable). Risk is the left tail of the
                                     return distribution. Formula: E × (Z × σ_T − μ_T).
                           'short' — holder fears FCY appreciating (payable). The
                                     obligation gets more expensive in base currency
                                     terms if FCY strengthens. Risk is the right tail.
                                     Formula: E × (Z × σ_T + μ_T).
                           The sign flip on μ_T is the only difference. For a payable,
                           positive drift (FCY appreciating) increases VaR rather than
                           reducing it, correctly reflecting that the obligation is
                           becoming more expensive. Default is 'long' for full backward
                           compatibility with all V1 callers.

    Returns:
        A tuple of:
            - var_floored (float): VaR floored at 0, for display and totals.
            - var_raw     (float): VaR before flooring. Negative means the
                                   historical drift in the holder's favour
                                   exceeded downside volatility at this
                                   confidence level and horizon. The caller
                                   should surface this to the user — a negative
                                   raw VaR does NOT mean zero risk; a sudden
                                   trend reversal would not be captured.
    """
    # norm.ppf: inverse normal CDF (Percent Point Function).
    # ppf(0.95) ≈ 1.645 — the Z-score such that 95% of the distribution
    # lies below it, i.e. there is a 5% chance of exceeding this loss level.
    z_score = norm.ppf(confidence_level)

    # Scale annualised volatility to the time horizon T.
    # σ_T = σ_annual × √(T/252) — volatility scales by √T
    sigma_t = annualised_vol * np.sqrt(days / TRADING_DAYS_PER_YEAR)

    # Scale daily mean drift linearly to the time horizon T.
    # μ_T = μ_daily × T — expected return scales linearly with T
    mu_t = daily_mean_return * days

    # Direction determines which tail of the distribution represents the loss:
    #
    #   Long  (cash holding / receivable):
    #     Fear: FCY depreciates → loss is at the LEFT tail (low returns).
    #     5th percentile return = μ_T - Z × σ_T
    #     Loss = -(5th percentile return) × E = (Z × σ_T - μ_T) × E
    #     → positive drift reduces VaR (trend in your favour)
    #     → negative drift increases VaR (trend against you)
    #
    #   Short (payable):
    #     Fear: FCY appreciates → loss is at the RIGHT tail (high returns).
    #     95th percentile return = μ_T + Z × σ_T
    #     Loss = (95th percentile return) × E = (Z × σ_T + μ_T) × E
    #     → positive drift INCREASES VaR (FCY getting more expensive to buy)
    #     → negative drift REDUCES VaR (FCY getting cheaper — good for you)
    #     Implemented by negating μ_T: (Z × σ_T - (-μ_T)) = (Z × σ_T + μ_T)
    #
    effective_mu_t = mu_t if direction == 'long' else -mu_t

    raw_var = exposure_amount * (z_score * sigma_t - effective_mu_t)

    # Floor at zero: negative VaR means expected drift gain exceeds downside
    # volatility risk at this confidence level over this horizon.
    # We return both the floored value (for display/totals) and the raw
    # pre-floor value so the caller can flag to the user when flooring occurred.
    return max(raw_var, 0.0), raw_var


# =============================================================================
# PORTFOLIO VAR — main entry point called by the Flask app
# =============================================================================

def calculate_portfolio_var(
    positions:        list[dict],
    base_ccy:         str,
    confidence_level: float = 0.95,
    period:           str   = "1y",
    days:             int   = 1
) -> dict:
    """
    Computes the parametric FX VaR for a multi-currency cash portfolio over
    a user-specified time horizon (days parameter).

    Each position is a foreign currency cash balance held by a company whose
    home/reporting currency is base_ccy. The VaR measures the maximum expected
    loss on these holdings in base currency terms over the chosen horizon.
    The horizon T is passed directly into the VaR formula — σ is scaled by
    √(T/252) and μ is scaled by T — so there is no separate step of computing
    a 1-day VaR and then scaling it up.

    Aggregation method: SIMPLE SUM of per-position VaRs.
    This assumes perfect positive correlation across all currency pairs —
    i.e. on the worst day, every foreign currency moves against the base
    simultaneously. This is conservative (overstates true portfolio risk)
    but transparent and appropriate for a PoC. Correlation-adjusted
    aggregation via a covariance matrix is planned for V2.

    Args:
        positions: List of dicts, each with:
                     'currency' (str):  ISO code of the foreign currency held
                     'balance'  (float): amount held in that foreign currency
        base_ccy:          Company's home currency (e.g. 'SGD').
        confidence_level:  VaR confidence level (default 0.95).
        period:            Historical lookback for volatility (default '1y').
        days:              VaR time horizon in trading days (default 1 = 1-day VaR,
                           the standard horizon for daily risk management). Common
                           values: 5 (1 week), 21 (1 month), 30, 63 (1 quarter).

    Returns:
        A dict with:
            'total_var'      (float): sum of all per-position VaRs in base_ccy
            'base_ccy'       (str):   the base currency
            'confidence'     (float): confidence level used
            'days'           (int):   horizon used
            'positions'      (list):  per-position breakdown, each containing:
                'currency'        (str)
                'balance'         (float)
                'spot_rate'       (float):  base per 1 foreign
                'exposure_base'   (float):  balance × spot_rate, in base_ccy
                'annualised_vol'  (float):  σ_annual
                'daily_mean'      (float):  μ_daily
                'annualised_mean' (float):  μ_daily × 252 (for display)
                'var'             (float):  VaR in base_ccy (floored at 0 for display)
                'used_cross_rate' (bool):   True if synthetic USD route was used
                'drift_warning'   (bool):   True if |annualised_mean| > 10%
                'var_was_floored' (bool):   True if raw VaR was negative and floored to 0
                'var_raw'         (float):  pre-floor VaR (negative = drift dominates)
            'errors'         (list):  currencies that failed to fetch, with reasons
    """
    position_results = []
    errors           = []
    total_var        = 0.0

    for pos in positions:
        foreign_ccy = pos['currency'].upper().strip()
        balance     = float(pos['balance'])

        # Skip if the position is in the base currency — no FX risk
        if foreign_ccy == base_ccy.upper():
            continue

        try:
            ann_vol, daily_mean, _, spot_rate, used_cross = fetch_pair_returns(
                foreign_ccy, base_ccy, period
            )

            # Convert foreign balance to base currency using live spot rate.
            # exposure_base = balance (foreign) × spot_rate (base per foreign)
            exposure_base = balance * spot_rate

            # calculate_parametric_var returns (floored_var, raw_var).
            # floored_var is used for display and totals.
            # raw_var is stored so the UI can flag when flooring occurred.
            var_floored, var_raw = calculate_parametric_var(
                exposure_base, ann_vol, daily_mean, confidence_level, days
            )

            total_var        += var_floored
            annualised_mean   = daily_mean * TRADING_DAYS_PER_YEAR

            position_results.append({
                'currency':        foreign_ccy,
                'balance':         balance,
                'spot_rate':       round(float(spot_rate),    6),
                'exposure_base':   round(float(exposure_base), 2),
                'annualised_vol':  round(float(ann_vol),       6),
                'daily_mean':      round(float(daily_mean),    8),
                'annualised_mean': round(float(annualised_mean), 4),
                'var':             round(float(var_floored),   2),
                # Explicitly cast to Python bool — numpy bools are not
                # JSON serializable by Flask's jsonify out of the box.
                'used_cross_rate': bool(used_cross),
                'drift_warning':   bool(abs(annualised_mean) > 0.10),
                # True when raw VaR was negative and got floored to 0.
                # Does NOT mean zero risk — a trend reversal would not be
                # captured by this parametric model.
                'var_was_floored': bool(var_raw < 0),
                'var_raw':         round(float(var_raw), 2),
            })

        except Exception as e:
            errors.append({
                'currency': foreign_ccy,
                'reason':   str(e)
            })

    return {
        'total_var':  round(float(total_var), 2),
        'base_ccy':   base_ccy,
        'confidence': float(confidence_level),
        'days':       int(days),
        'positions':  position_results,
        'errors':     errors,
    }
