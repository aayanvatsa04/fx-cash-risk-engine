"""
app.py — Flask web server for the FX VaR V3 PoC

This file is PURELY the web layer. It contains no financial mathematics —
all computation is delegated to exposure_engine.py (which delegates core
VaR math to var_engine.py), and dashboard data transformation is delegated
to dashboard_engine.py.

=== ROUTES ===

    GET  /                  → Serves the single unified page (templates/calculator.html)
    POST /calculate         → V3 JSON API: full VaR engine output + dashboard chart data
    POST /recommend_hedges  → V3.8 JSON API: ranked forward-hedge recommendations
                               that reduce Consolidated Portfolio VaR

=== UNIFIED PAGE DESIGN ===

The calculator and dashboard are one page. The user enters their portfolio
once, clicks Calculate, and sees:
  1. Cash Spot Rate Sensitivity — simple scenario table on cash holdings
  2. Cash Book Risk — standalone cash VaR at user-specified T
  3. Consolidated Portfolio VaR — full portfolio with Portfolio Scenario sliders
  4. Hedge Recommendations — ranked forward contracts to reduce Portfolio VaR,
     triggered by a separate "Get Hedge Recommendations" button (POST /recommend_hedges)
  5. Bucketed Risk Detail — per-bucket netting, attribution, diversification
  6. Risk Dashboard — summary stat cards, chart, simulation sliders, hedge table

There is no separate /dashboard page or /dashboard_data endpoint. One form
submission drives sections 1–3 and 5–6; section 4 is triggered separately
on demand (because it requires multiple engine re-runs and is more expensive).

=== DEPENDENCY CHAIN (strictly one-directional) ===

    HTTP Request
      ↓
    app.py                  ← THIS FILE (routing, validation, serialisation only)
      ↓ imports
    exposure_engine.py      (business logic: buckets, netting, unified output)
      ↓ imports
    var_engine.py           (core math: σ, μ, parametric formula, covariance)
      ↓ imports
    yfinance / numpy / pandas / scipy

    app.py also imports:
    dashboard_engine.py     (data transformation: engine output → chart-ready JSON)
      ↓ imports
    scipy.stats, math

No circular imports. No math in app.py. No Flask in var_engine or exposure_engine.

=== DESIGN PRINCIPLE FOR FUTURE DEVELOPERS ===

When adding a new feature:
  - New math/statistics?       → add to var_engine.py
  - New exposure types/logic?  → add to exposure_engine.py
  - New dashboard views/cards? → add to dashboard_engine.py + calculator.html
  - New pages?                 → add a template + a GET route here

app.py should NEVER contain a VaR formula, date arithmetic, or correlation logic.

HOW TO RUN:
    python3 app.py
    → http://127.0.0.1:8080
"""

from flask import Flask, request, jsonify, render_template
from exposure_engine import calculate_fx_var, recommend_hedges
from dashboard_engine import prepare_dashboard_data

app = Flask(__name__)


# =============================================================================
# SHARED INPUT VALIDATION HELPER
# =============================================================================

def _parse_and_validate_request(data: dict) -> tuple[dict | None, str | None]:
    """
    Validates and parses the JSON request body for the /calculate endpoint.

    Expected payload schema:
        {
            "base_currency":  str   — required, 3-letter ISO code (e.g. 'SGD')
            "confidence":     float — optional, default 0.95
            "period":         str   — optional, default '1y' (yfinance lookback)
            "cash_horizon":   int   — optional, default 1 (trading days, 1–252)
            "cash_positions": list  — optional, list of {currency, balance}
            "exposures":      list  — optional, list of {currency, amount,
                                       settlement_date, direction}
        }
        At least one of cash_positions or exposures must be non-empty.

    Args:
        data: Parsed JSON dict from Flask's request.get_json().

    Returns:
        (parsed_params dict, None)       if validation passes
        (None, human-readable error str) if validation fails
    """
    base_currency = data.get('base_currency', '').strip().upper()
    if not base_currency:
        return None, 'base_currency is required (e.g. "SGD").'

    cash_positions = data.get('cash_positions', [])
    exposures      = data.get('exposures', [])

    if not cash_positions and not exposures:
        return None, 'At least one cash position or future exposure is required.'

    # Validate cash positions
    for i, pos in enumerate(cash_positions):
        if 'currency' not in pos or 'balance' not in pos:
            return None, f"Cash position {i+1} is missing 'currency' or 'balance'."
        try:
            float(pos['balance'])
        except (ValueError, TypeError):
            return None, f"Cash position {i+1} has an invalid balance value."

    # Validate future exposures
    for i, exp in enumerate(exposures):
        for field in ('currency', 'amount', 'settlement_date', 'direction'):
            if field not in exp:
                return None, f"Exposure {i+1} is missing '{field}'."
        try:
            float(exp['amount'])
        except (ValueError, TypeError):
            return None, f"Exposure {i+1} has an invalid amount value."
        if exp.get('direction', '').lower() not in ('payable', 'receivable'):
            return None, f"Exposure {i+1} direction must be 'payable' or 'receivable'."

    confidence   = float(data.get('confidence',   0.95))
    period       = str(data.get('period',         '1y'))
    cash_horizon = int(data.get('cash_horizon',   1))

    if not (0.90 <= confidence <= 0.9999):
        return None, 'confidence must be between 0.90 and 0.9999.'
    if not (1 <= cash_horizon <= 252):
        return None, 'cash_horizon must be between 1 and 252 trading days.'

    return {
        'base_currency': base_currency,
        'cash_positions': cash_positions,
        'exposures':      exposures,
        'confidence':     confidence,
        'period':         period,
        'cash_horizon':   cash_horizon,
    }, None


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    """
    Serves the single unified V3 page (templates/calculator.html).

    This page contains both the input form and the results display —
    the detailed engine output AND the risk dashboard chart/sliders all
    appear on the same page after a single form submission.
    """
    return render_template('calculator.html')


@app.route('/calculate', methods=['POST'])
def calculate():
    """
    V3 unified JSON API endpoint. Runs the full VaR engine and returns
    both the raw three-section output AND the dashboard-formatted chart data
    in a single response, so the frontend can drive all UI sections from
    one POST.

    Expected JSON body: see _parse_and_validate_request docstring.

    Returns:
        JSON combining calculate_fx_var() output with dashboard chart data:
        {
            — Raw engine output (unchanged from previous API) —
            'base_ccy':               str,
            'confidence':             float,
            'cash_horizon':           int,
            'spot_risk':              dict,  — Section 1: standalone cash VaR
            'unified_buckets':        dict,  — Section 2: cash + forwards, bucketed
            'gross_attribution':      dict,  — Section 3: forwards-only, no netting
            'gross_cash_attribution': dict,  — cash standalone VaR at the fixed
                                       CASH_CONSOLIDATED_T_DAYS horizon (10 trading
                                       days); the cash component of the Gross
                                       Standalone Risk baseline, deliberately
                                       independent of cash_horizon above — see
                                       exposure_engine.py's CASH_CONSOLIDATED_T_DAYS
                                       comment for why this is a separate key
                                       from spot_risk rather than reusing it
            'consolidated_var':       dict,  — exact cross-horizon portfolio VaR
            'cumulative_vars':        dict,  — V3: per-period VaR + Component CFaR
                                       decomposition, one entry per dashboard
                                       period filter option (1m/3m/6m/12m/all)

            — Dashboard chart data (added by dashboard_engine.prepare_dashboard_data) —
            'dashboard': {
                'base_ccy':          str,
                'confidence':        float,
                'z_score':           float,
                'summary':           dict,   — headline numbers for stat cards
                'buckets':           list,   — per-bucket chart data + simulation values
                'simulation_params': dict,   — slider range configuration
                'errors':            list,
            }
        }

    The 'dashboard' key is the only addition vs the previous /calculate response.
    All existing keys are preserved — no breaking change.

    Error responses:
        400 if request body is invalid or missing required fields.
        500 if the engine raises an unexpected exception.
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body must be JSON.'}), 400

    params, err = _parse_and_validate_request(data)
    if err:
        return jsonify({'error': err}), 400

    try:
        # Step 1: Run the full VaR engine
        var_result = calculate_fx_var(
            cash_positions = params['cash_positions'],
            exposures      = params['exposures'],
            base_ccy       = params['base_currency'],
            confidence     = params['confidence'],
            period         = params['period'],
            cash_horizon   = params['cash_horizon'],
        )

        # Step 2: Transform engine output into chart-ready dashboard data.
        # This is the only addition vs the old /calculate route — the engine
        # result passes through dashboard_engine for formatting, and the
        # result is attached as a 'dashboard' key on the same response.
        var_result['dashboard'] = prepare_dashboard_data(var_result)

        return jsonify(var_result)

    except Exception as e:
        return jsonify({'error': f'Engine error: {str(e)}'}), 500


@app.route('/recommend_hedges', methods=['POST'])
def recommend_hedges_route():
    """
    V3.8 Hedge Recommendation API endpoint. Identifies which forward contracts
    would most reduce Consolidated Portfolio VaR, returned as a ranked list
    the treasurer can act on.

    === WHY A SEPARATE ENDPOINT? ===

    /recommend_hedges is intentionally NOT merged into /calculate:
      1. It is more expensive — it calls calculate_consolidated_portfolio_var
         once per hedge candidate (O(N) engine re-runs where N = candidate count),
         whereas /calculate runs a fixed, bounded set of computations.
      2. It is only needed when the user explicitly wants hedge suggestions —
         not on every Calculate click.
      3. Keeping it separate means no change to the existing /calculate contract,
         so any external callers of /calculate (future API users, tests) are
         completely unaffected by V3.8.

    === INPUT ===

    Accepts the same JSON body as /calculate (validated by the shared
    _parse_and_validate_request helper). The cash_horizon field is accepted
    but has no effect on recommendations — hedge rankings always use
    CASH_CONSOLIDATED_T_DAYS (10 trading days) for cash, consistent with
    how Consolidated Portfolio VaR treats cash throughout the rest of the app.

    === OUTPUT ===

    {
        'base_ccy':                   str,   — home currency
        'baseline_var':               float, — Portfolio VaR before any hedges
        'recommendations': [          — one entry per (currency, bucket) candidate,
                                         ranked by risk-reduction impact
            {
                'rank':                     int,
                'currency':                 str,
                'bucket_num':               int,
                'bucket_label':             str,
                'bucket_midpoint_t':        int,
                'net_notional_fcy':         float,
                'net_notional_base':        float,
                'component_cfar_baseline':  float,
                'hedge_direction':          str,   — 'payable' or 'receivable'
                'hedge_amount_fcy':         float,
                'hedge_settlement_date':    str,   — 'YYYY-MM-DD'
                'hedge_settlement_t':       int,
                'spot_rate':                float,
                'portfolio_var_before':     float,
                'portfolio_var_after':      float,
                'marginal_reduction_abs':   float,
                'marginal_reduction_pct':   float,
                'cumulative_reduction_abs': float,
                'cumulative_reduction_pct': float,
            }
        ],
        'fully_hedged_var':            float,
        'fully_hedged_reduction_pct':  float,
        'errors':                      list,  — market data fetch failures
    }

    Error responses:
        400 if request body is invalid or missing required fields.
        500 if the engine raises an unexpected exception.
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body must be JSON.'}), 400

    params, err = _parse_and_validate_request(data)
    if err:
        return jsonify({'error': err}), 400

    try:
        result = recommend_hedges(
            cash_positions = params['cash_positions'],
            exposures      = params['exposures'],
            base_ccy       = params['base_currency'],
            confidence     = params['confidence'],
            period         = params['period'],
            # cash_horizon deliberately not passed — recommend_hedges always
            # uses CASH_CONSOLIDATED_T_DAYS for cash, matching how the
            # Consolidated Portfolio VaR treats cash throughout the app.
        )
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'Engine error: {str(e)}'}), 500


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import os
    import socket

    # Cloud platforms inject PORT via environment variable; fall back to 8080 locally.
    port = int(os.environ.get('PORT', 8080))

    # Enable Flask debug mode only when running locally (PORT not set by cloud).
    is_local = os.environ.get('PORT') is None
    if is_local:
        local_ip = socket.gethostbyname(socket.gethostname())
        print(f"\n  FX VaR V3 — App running at:")
        print(f"    http://127.0.0.1:{port}")
        print(f"    http://{local_ip}:{port}\n")

    app.run(debug=is_local, host='0.0.0.0', port=port)
