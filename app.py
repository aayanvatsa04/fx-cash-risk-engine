"""
app.py — Flask web server for the FX VaR V2 PoC

This file is purely the web layer. It has no financial math in it —
all computation is delegated to exposure_engine.py, which in turn
delegates the core VaR math to var_engine.py.

V2 changes from V1:
  - Imports from exposure_engine.py instead of var_engine.py directly
  - /calculate route now accepts both cash_positions AND exposures
  - Returns the three-layer V2.3 result structure (no combined total_var)

HOW TO RUN:
    1. Install dependencies (one-time):
           pip install flask yfinance numpy pandas scipy

    2. Start the server:
           python app.py

    3. Open your browser at:
           http://127.0.0.1:8080

FOLDER STRUCTURE:
    fx_var_v2/
    ├── app.py               ← this file
    ├── var_engine.py        ← core VaR math (unchanged from V1 except direction param)
    ├── exposure_engine.py   ← V2 business logic: dates, buckets, netting
    ├── test_v2_engine.py    ← standalone test runner (no Flask needed)
    └── templates/
        └── index.html       ← frontend UI
"""

from flask import Flask, request, jsonify, render_template
from exposure_engine import calculate_combined_var_v2

app = Flask(__name__)


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    """Serves the main V2 UI page (templates/index.html)."""
    return render_template('index.html')


@app.route('/calculate', methods=['POST'])
def calculate():
    """
    V2 API endpoint. Accepts both cash positions and future exposures,
    runs the full three-layer VaR engine, returns results as JSON.

    Expected request body (JSON):
        {
            "base_currency":  "SGD",
            "confidence":     0.95,
            "period":         "1y",
            "cash_horizon":   1,
            "cash_positions": [
                { "currency": "USD", "balance": 2000000 }
            ],
            "exposures": [
                {
                    "currency":        "MYR",
                    "amount":          5000000,
                    "settlement_date": "2026-12-31",
                    "direction":       "payable"
                }
            ]
        }

    Returns the three-section result dict from calculate_combined_var_v2():
        {
            "base_ccy":    "SGD",
            "confidence":  0.95,
            "cash_horizon": 1,
            "spot_risk":         { "total_var", "total_var_cov", "diversification_benefit",
                                   "positions", "errors" },
            "unified_buckets":   { "buckets", "errors" },
            "gross_attribution": { "exposures", "errors" }
        }

    No combined total across buckets is returned — each bucket uses a different
    time horizon T and they cannot be meaningfully summed. See exposure_engine.py
    module docstring for the full explanation.
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body must be JSON.'}), 400

    base_currency = data.get('base_currency', '').strip().upper()
    if not base_currency:
        return jsonify({'error': 'base_currency is required.'}), 400

    cash_positions = data.get('cash_positions', [])
    exposures      = data.get('exposures', [])

    if not cash_positions and not exposures:
        return jsonify({
            'error': 'At least one cash position or future exposure is required.'
        }), 400

    # Validate cash positions
    for i, pos in enumerate(cash_positions):
        if 'currency' not in pos or 'balance' not in pos:
            return jsonify({
                'error': f"Cash position {i+1} is missing 'currency' or 'balance'."
            }), 400
        try:
            float(pos['balance'])
        except (ValueError, TypeError):
            return jsonify({
                'error': f"Cash position {i+1} has an invalid balance."
            }), 400

    # Validate future exposures
    for i, exp in enumerate(exposures):
        for field in ('currency', 'amount', 'settlement_date', 'direction'):
            if field not in exp:
                return jsonify({
                    'error': f"Exposure {i+1} is missing '{field}'."
                }), 400
        try:
            float(exp['amount'])
        except (ValueError, TypeError):
            return jsonify({
                'error': f"Exposure {i+1} has an invalid amount."
            }), 400
        if exp.get('direction', '').lower() not in ('payable', 'receivable'):
            return jsonify({
                'error': f"Exposure {i+1} direction must be 'payable' or 'receivable'."
            }), 400

    confidence   = float(data.get('confidence',   0.95))
    period       = data.get('period',   '1y')
    cash_horizon = int(data.get('cash_horizon', 1))

    if cash_horizon < 1 or cash_horizon > 252:
        return jsonify({'error': 'cash_horizon must be between 1 and 252.'}), 400

    try:
        result = calculate_combined_var_v2(
            cash_positions = cash_positions,
            exposures      = exposures,
            base_ccy       = base_currency,
            confidence     = confidence,
            period         = period,
            cash_horizon   = cash_horizon,
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

    # Railway (and most cloud platforms) inject the port to listen on
    # via the PORT environment variable. We fall back to 8080 locally.
    port = int(os.environ.get('PORT', 8080))

    # debug=True only when running locally (not on Railway).
    # On Railway, PORT is always set, so debug will be False there.
    is_local = os.environ.get('PORT') is None
    if is_local:
        local_ip = socket.gethostbyname(socket.gethostname())
        print(f"\n  FX VaR V2 — App running at:")
        print(f"    Local:   http://127.0.0.1:{port}")
        print(f"    Network: http://{local_ip}:{port}\n")

    app.run(debug=is_local, host='0.0.0.0', port=port)
