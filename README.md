# FX Value at Risk (VaR) Engine — PoC V2

**Finmo Product Intern Project**
Built by Aayan Vatsa · June 2026

---

## What This Is

A proof-of-concept web application that computes parametric FX Value at Risk
for a multi-currency portfolio, covering both current cash holdings and future
FX exposures (payables and receivables).

The engine implements the full delta-normal parametric VaR formula including
drift (μ ≠ 0), time-bucketed netting of future exposures, and natural hedging
benefit computation. All computation runs server-side in Python.

---

## Features

- **Section 1 — Spot Book Risk:** Standalone VaR on current cash holdings at user-specified T. Covariance-adjusted total + diversification benefit (V2.2).
- **Section 2 — Unified Bucketed Risk:** Cash positions routed into Bucket 1 as synthetic receivables, netting against same-currency near-term obligations. Forward exposures in natural buckets. Natural hedging (within-currency) and covariance adjustment (cross-currency) applied per bucket (V2.2/2.3).
- **Section 3 — Gross Attribution:** Standalone VaR per forward exposure at its bucket T. Reference only — no netting applied.
- **Direction-Aware Formula:** Payables use short formula (fear FCY appreciation); receivables and cash use long formula (fear FCY depreciation).
- **Cross-Rate Construction:** Thinly traded pairs synthesised via USD legs.

---

## File Structure

```
fx_var_v2/
├── app.py                  Flask web server — routes only, no math
├── var_engine.py           Core VaR math — σ, μ, parametric formula
├── exposure_engine.py      V2 business logic — dates, buckets, netting
├── test_v2_engine.py       Standalone test runner (no Flask needed)
├── requirements.txt        Python dependencies for deployment
├── Procfile                Tells Railway how to start the app
├── .gitignore              Files excluded from Git version control
└── templates/
    └── index.html          Frontend UI — form and results display
```

### Dependency chain (strictly one-directional)

```
app.py / test_v2_engine.py
    ↓ imports
exposure_engine.py
    ↓ imports
var_engine.py
    ↓ imports
yfinance / numpy / pandas / scipy
```

`var_engine.py` has no knowledge of Flask, HTML, or exposure logic.
`exposure_engine.py` has no knowledge of Flask or HTML.
Upgrading the math only touches `var_engine.py`. Adding new exposure types
only touches `exposure_engine.py`. The UI never changes for either.

---

## Running Locally

**1. Create and activate a virtual environment (one-time):**
```bash
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows
```

**2. Install dependencies (one-time):**
```bash
pip install -r requirements.txt
```

**3. Test the engine without Flask (optional but recommended):**
```bash
python test_v2_engine.py
```
This runs the full V2 engine and prints the three-layer output to the terminal.
Verify the natural hedge benefit fires for Bucket 2 USD before running the web app.

**4. Start the web server:**
```bash
python app.py
```

**5. Open in browser:**
```
http://127.0.0.1:8080
```

---

## VaR Formula

**Full parametric (delta-normal) formula:**

```
VaR = E × (Z_α × σ_T − μ_T)    [long: cash holding or receivable]
VaR = E × (Z_α × σ_T + μ_T)    [short: payable]

where:
  E     = exposure in base currency (balance × spot rate)
  Z_α   = norm.ppf(confidence)  e.g. 1.645 at 95%
  σ_T   = σ_annual × √(T/252)   volatility scaled to horizon T
  μ_T   = μ_daily × T            drift scaled linearly to horizon T
  T     = time horizon in trading days
```

**Why drift (μ) is always included:**
For stable pairs (SGD/USD), μ_daily ≈ 0 so the result is nearly identical to
the simplified μ = 0 formula. For trending currencies (TRY/USD at −0.06%/day),
the drift term compounds linearly with T and becomes material — dropping it
would understate risk for holders of depreciating currencies.

**Time bucketing for future exposures:**

| Bucket | Window         | Midpoint T |
|--------|----------------|------------|
| 1      | 0–1 month      | 10 days    |
| 2      | 1–3 months     | 42 days    |
| 3      | 3–6 months     | 95 days    |
| 4      | 6–12 months    | 189 days   |
| 5      | >12 months     | 315 days   |

Positions are netted within each bucket/currency before VaR is computed.

---

## Known Limitations (PoC)

| Limitation | Status | Planned fix |
|---|---|---|
| yfinance is an unofficial scrape — can break | ⚠️ Open | Replace with FRED + CurrencyLayer |
| Normal distribution understates fat tails for crashing currencies (TRY, ARS) | ⚠️ Open | Monte Carlo with Student's t (V3) |
| Simple sum across currencies within a bucket (assumes perfect correlation) | ✅ Fixed V2.2 | Covariance matrix now used within each bucket; `bucket_var_simple` retained for comparison |
| Cash vs forward netting not implemented (different T values) | ✅ Fixed V2.3 | `combined_net` (Layer 4): nets cash + receivables − payables per currency at T = cash\_horizon with covariance. Policy assumption: cash is immediately available to fund payables. Use bucketed forward VaR (Layer 1b/2) for time-stratified risk |
| Bucket midpoint T approximates each exposure's actual settlement T | ⚠️ Open | Exposure-weighted average T per bucket |
| No holiday calendar — counts Mon–Fri only | ⚠️ Open | Market-specific calendar (SGX, NYSE) |

---

## Deployment (Railway)

This app is deployed on [Railway](https://railway.app).

Railway reads `requirements.txt` to install dependencies and `Procfile` to
start the app. The `PORT` environment variable is injected by Railway
automatically — the app reads it via `os.environ.get('PORT', 8080)`.

To redeploy: push any commit to the `main` branch on GitHub. Railway
automatically detects the push and redeploys within ~2 minutes.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Flask (Python) |
| Production server | Gunicorn |
| Market data | yfinance (Yahoo Finance) |
| Numerical computation | NumPy, Pandas, SciPy |
| Frontend | Vanilla HTML/CSS/JS |
| Hosting | Railway |
| Version control | Git / GitHub |
