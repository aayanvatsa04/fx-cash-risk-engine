# FX Value at Risk (VaR) Engine — PoC V2

**Finmo Product Intern Project**
Built by Aayan Vatsa · June 2026

---

## What This Is

A proof-of-concept web application that computes parametric FX Value at Risk
for a multi-currency portfolio, covering both current cash holdings and future
FX exposures (payables and receivables).

The engine implements the full delta-normal parametric VaR formula including
drift (μ ≠ 0), time-bucketed netting of future exposures, natural hedging
benefit computation, and cross-currency covariance adjustment. All computation
runs server-side in Python.

---

## Features

- **Section 1 — Spot Book Risk:** Standalone VaR on current cash holdings at
  user-specified T. Per-currency breakdown with individual VaR figures.
  Portfolio headline is covariance-adjusted (currencies not perfectly correlated
  — they don't all move against you simultaneously).

- **Section 2 — Unified Bucketed Risk:** Cash holdings routed into Bucket 1 as
  synthetic long positions, netting against same-currency near-term payables.
  Forward exposures assigned to natural buckets by settlement date.
  Natural hedging benefit (within-currency netting) shown per bucket/currency.
  Covariance matrix applied silently across currencies within each bucket.
  Attribution rows show every exposure's standalone VaR, sorted by risk.

- **Section 3 — Gross Attribution (test runner only):** Standalone VaR per
  forward exposure at its bucket T with no netting applied. Reference view
  available via `test_v2_engine.py` — not shown in the web UI.

- **Direction-Aware Formula:** Payables use short formula
  VaR = E × (Z × σ_T + μ_T); receivables and cash use long formula
  VaR = E × (Z × σ_T − μ_T).

- **Cross-Rate Construction:** Thinly traded pairs (e.g. MYR/SGD) automatically
  synthesised via USD legs.

---

## File Structure

```
fx_var_v2/
├── app.py                  Flask web server — routes only, no math
├── var_engine.py           Core VaR math — σ, μ, parametric formula, covariance
├── exposure_engine.py      V2 business logic — dates, buckets, netting, unified output
├── test_v2_engine.py       Standalone test runner (no Flask needed)
├── requirements.txt        Python dependencies for deployment
├── Procfile                Tells Render how to start the app
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
Runs the full engine and prints the three-section output to the terminal.
Verify: Bucket 1 shows cash positions as synthetic receivables. Bucket 2 USD
shows natural hedge benefit (recv 2M offset by pay 1M).

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

**Portfolio VaR (covariance-adjusted):**

```
Portfolio VaR = Z × √(s^T Σ_T s) − Σᵢ(sᵢ × μᵢ × T)

where:
  sᵢ        = signed exposure in base currency (+long, −short)
  Σ_T[i,j]  = ρ[i,j] × σ_T_i × σ_T_j   (covariance matrix at horizon T)
  ρ[i,j]    = Pearson correlation of daily returns between pair i and j
```

This replaces the simple sum (which assumed ρ = 1 for all pairs) with the
actual cross-currency correlation structure from historical returns.

**Time bucketing for future exposures:**

| Bucket | Window         | Midpoint T |
|--------|----------------|------------|
| 1      | 0–1 month      | 10 days    |
| 2      | 1–3 months     | 42 days    |
| 3      | 3–6 months     | 95 days    |
| 4      | 6–12 months    | 189 days   |
| 5      | >12 months     | 315 days   |

Cash holdings are routed into Bucket 1 as synthetic receivables (settlement =
1 trading day). Positions are netted within each bucket/currency before VaR
is computed. Covariance is applied across currencies within each bucket.

---

## Known Limitations (PoC)

| Limitation | Status | Planned fix |
|---|---|---|
| yfinance is an unofficial scrape — can break | ⚠️ Open | Replace with FRED + CurrencyLayer |
| Normal distribution understates fat tails for crashing currencies (TRY, ARS) | ⚠️ Open | Monte Carlo with Student's t (V3) |
| Simple sum across currencies within a bucket (assumes perfect correlation) | ✅ Fixed V2.2 | Covariance matrix now used within each bucket and for spot portfolio |
| Cash vs forward netting not implemented (different T values) | ✅ Fixed V2.3 | Cash routed into Bucket 1 as synthetic receivables — nets against same-currency near-term payables. Longer-dated forwards remain in their natural buckets |
| Bucket midpoint T approximates each exposure's actual settlement T | ⚠️ Open | Exposure-weighted average T per bucket |
| No holiday calendar — counts Mon–Fri only | ⚠️ Open | Market-specific calendar (SGX, NYSE) |

---

## Deployment (Render)

This app is deployed on [Render](https://render.com).

Render reads `requirements.txt` to install dependencies and `Procfile` to
start the app. The `PORT` environment variable is injected by Render
automatically — the app reads it via `os.environ.get('PORT', 8080)`.

To redeploy: push any commit to the `main` branch on GitHub. Render
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
| Hosting | Render |
| Version control | Git / GitHub |
