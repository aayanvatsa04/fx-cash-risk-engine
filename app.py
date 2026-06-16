"""
app.py — Flask web server for the FX VaR V3 PoC

This file is PURELY the web layer. It contains no financial mathematics —
all computation is delegated to exposure_engine.py (which delegates core
VaR math to var_engine.py), and dashboard data transformation is delegated
to dashboard_engine.py.

=== ROUTES ===

    GET  /           → Serves the single unified page (templates/calculator.html)
    POST /calculate  → V3 JSON API: full VaR engine output + dashboard chart data

=== UNIFIED PAGE DESIGN ===

The calculator and dashboard are one page. The user enters their portfolio
once, clicks Calculate, and sees:
  1. Detailed three-section engine output (spot book, bucketed, gross attribution,
     consolidated VaR) — driven by the raw engine result
  2. Risk dashboard (bar chart, sliders, hedge effectiveness table) — driven by
     the dashboard key in the same response, formatted by dashboard_engine.py

There is no separate /dashboard page or /dashboard_data endpoint. One form
submission drives everything.

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
from exposure_engine import calculate_fx_var
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
            'base_ccy':          str,
            'confidence':        float,
            'cash_horizon':      int,
            'spot_risk':         dict,   — Section 1: standalone cash VaR
            'unified_buckets':   dict,   — Section 2: cash + forwards, bucketed
            'gross_attribution': dict,   — Section 3: forwards-only, no netting
            'consolidated_var':  dict,   — exact cross-horizon portfolio VaR

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
