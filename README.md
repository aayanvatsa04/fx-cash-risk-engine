# FX Value at Risk (VaR) Engine — V3

**Finmo Product Intern Project**
Built by Aayan Vatsa · June 2026

**Live App:** https://fx-cash-risk-engine.onrender.com

---

## What This Is

A proof-of-concept web application that computes parametric FX Value at Risk
for a multi-currency portfolio covering both current cash holdings and future
FX exposures (payables and receivables).

The engine implements the full delta-normal parametric VaR formula including
drift (μ ≠ 0), time-bucketed netting of future exposures, natural hedging
benefit computation, and cross-currency covariance adjustment.

Everything lives on a single unified page. The user enters their portfolio,
clicks Calculate once, and sees:

1. **Risk Dashboard** — headline stat cards, bar chart with Component CFaR
   labels, cumulative period filter, scenario simulation sliders, and hedge
   effectiveness table. Appears first so executives and managers get the
   summary view immediately.
2. **Cash Book Risk** — standalone VaR on current cash holdings at user-
   specified T, with per-currency breakdown and covariance-adjusted total.
3. **Consolidated Portfolio VaR** — single portfolio number using the exact
   min(Ti,Tj) cross-horizon covariance formula across all positions.
4. **Bucketed Risk Detail** — full per-bucket technical breakdown: individual
   positions, net VaRs, natural hedge benefit, and diversification benefit
   per bucket. Analyst-level detail at the bottom of the page.

All four sections are driven by a single API response from one Calculate click.

---

## Page Layout (Results Order)

```
① Global Settings + inputs (cash positions, horizon, future exposures)
② Calculate VaR button
   ── results appear below ──
③ RISK DASHBOARD
   ├── Stat cards: Portfolio VaR · Spot Book VaR · Gross Standalone · Risk Reduction
   ├── Net Cashflow Exposure bar chart (Component CFaR printed inside bars)
   ├── Scenario Simulation sliders (±10% spot, ±25% vol)
   └── Natural Hedge Effectiveness table (per currency per bucket)
④ Cash Book Risk (standalone cash VaR at user-specified T)
⑤ Consolidated Portfolio VaR (full portfolio, exact individual-T covariance)
⑥ Bucketed Risk Detail (per-bucket netting, attribution, diversification)
```

---

## Features

- **Cash Book Risk:** Standalone parametric VaR on cash holdings at user-
  specified T. Per-currency breakdown. Portfolio total is covariance-adjusted
  (currencies are not perfectly correlated — diversification benefit shown).

- **Bucketed Risk Detail:** Cash holdings routed into Bucket 1 as synthetic
  long positions, netting against same-currency near-term payables.
  Forward exposures assigned to natural buckets by settlement date.
  Natural hedging benefit (within-currency netting) shown per bucket/currency.
  Covariance matrix applied across currencies within each bucket.
  Attribution rows show every exposure's standalone VaR, sorted by risk.

- **Gross Attribution (engine_runner.py only):** Standalone VaR per forward
  exposure at its bucket T with no netting applied. Reference view available
  via `engine_runner.py` — not shown in the web UI.

- **Consolidated Portfolio VaR (V2.4):** Single number across the full portfolio —
  cash + all forwards — using exact individual-position covariance with
  min(Ti, Tj) cross-horizon terms. Each exposure is a separate entry in the n×n
  covariance matrix. Natural hedging between same-currency positions falls out
  automatically through signed exposures (no explicit netting step needed).

- **Gross Standalone Risk (Option A):** Gross baseline includes both forward
  standalone VaRs (Section 3, bucket-midpoint T, no netting/correlation) AND
  cash standalone VaRs (at cash_horizon T, no diversification). This makes the
  scope of the gross baseline consistent with consolidated_var, producing a
  clean apples-to-apples risk reduction percentage.

- **Direction-Aware Formula:** Payables use short formula
  VaR = E × (Z × σ_T + μ_T); receivables and cash use long formula
  VaR = E × (Z × σ_T − μ_T).

- **Cross-Rate Construction:** Thinly traded pairs (e.g. MYR/SGD) automatically
  synthesised via USD legs.

- **Risk Dashboard — Cumulative Period Filter (V3):** Instead of showing one
  bucket at a time, the dashboard shows cumulative time windows (Next 1 month,
  Next 3 months, etc.). Each period runs the full covariance VaR calculation
  across all positions settling within that window, using each position's own
  actual T. Period VaR is decomposed into per-currency Component CFaRs via the
  Euler decomposition theorem — their sum equals the period VaR exactly.

- **Component CFaR Labels (V3):** Component CFaR values are printed directly
  inside each net exposure bar (or above for short/zero bars) via
  chartjs-plugin-datalabels. Labels are visible immediately on render without
  requiring hover. Component CFaR encodes each currency's marginal contribution
  to total portfolio risk, including all cross-currency correlations — it is not
  a standalone single-currency VaR.

- **Comma-Formatted Amount Inputs:** Cash balance and exposure amount fields
  display thousand-separator commas live as the user types (e.g. "2,000,000"),
  making order of magnitude immediately readable. These fields are
  `type="text"` rather than `type="number"` specifically to allow commas;
  `formatWithCommas()` reformats on every keystroke and `toRawNumber()`
  strips commas back out before the value is parsed and sent to the backend.
  The Python engine never sees a comma — this is purely a display-layer
  concern, fully isolated in `calculator.html`'s JS.

---

## File Structure

```
fx-cash-risk-engine/
├── app.py                  Flask web server — routes: GET / and POST /calculate
├── var_engine.py           Core VaR math — σ, μ, parametric formula, covariance
├── exposure_engine.py      Business logic — dates, buckets, netting, unified output
├── dashboard_engine.py     Data transformation — engine output → chart-ready JSON
├── engine_runner.py        Standalone CLI runner — verify engine output without Flask
├── requirements.txt        Python dependencies for deployment
├── Procfile                Tells Render how to start the app
├── .gitignore              Files excluded from Git version control
├── templates/
│   └── calculator.html     Single unified page — form + all results sections
└── static/
    ├── css/
    │   └── dashboard.css   Shared design tokens and component styles
    └── js/
        └── dashboard.js    Chart.js rendering, slider simulation, hedge table
```

### Dependency chain (strictly one-directional)

```
HTTP Request
  ↓
app.py                    (web layer: routing, validation, JSON serialisation)
  ↓ imports                              ↓ also imports
exposure_engine.py             dashboard_engine.py
  ↓ imports                   (data transformation only — no VaR math)
var_engine.py
  ↓ imports
yfinance / numpy / pandas / scipy
```

Strict rules maintained across all files:
- `var_engine.py` has no knowledge of Flask, HTML, exposure logic, or dashboard.
- `exposure_engine.py` has no knowledge of Flask, HTML, or dashboard.
- `dashboard_engine.py` has no knowledge of Flask or HTML — it transforms dicts.
- `app.py` contains no financial math — only routing, validation, and engine calls.
- `dashboard.js` contains no VaR formulas — only arithmetic using pre-computed
  values (vol_term, mu_term) provided by `dashboard_engine.py`.

### When adding features

| New feature type | File to touch |
|---|---|
| New math / statistics | `var_engine.py` |
| New exposure types / bucketing logic | `exposure_engine.py` |
| New dashboard views / stat cards | `dashboard_engine.py` + `calculator.html` |
| New chart behaviour / slider logic | `dashboard.js` |
| New pages or API endpoints | `app.py` |

### How the unified page works

```
User clicks Calculate
  ↓
calculator.html JS  →  POST /calculate
  ↓
app.py calls calculate_fx_var()         — raw engine result
app.py calls prepare_dashboard_data()   — chart-ready JSON attached as 'dashboard' key
  ↓
Single JSON response returned
  ↓
calculator.html JS renders all result sections
  ↓
fires CustomEvent('varResultReady', { detail: result.dashboard })
  ↓
dashboard.js receives event → renders chart, sliders, hedge table
```

### Frontend architecture note: CSS Grid vs native `<table>`

The codebase intentionally mixes two different markup patterns for tabular
content, and it's important to understand why before "cleaning this up":

- **Cash Positions and Future Exposures inputs** (`calculator.html`) are built
  with **CSS Grid** (`<div class="grid-row cash-cols">`, etc.), not `<table>`.
- **The Hedge Effectiveness table** (`calculator.html`, inside `.hedge-section`)
  and the Limitations table are still real `<table>` elements.

This is not an inconsistency to fix — it's a deliberate fix *for* an
inconsistency bug. `dashboard.css` originally had unscoped rules
(`thead th:not(:first-child) { text-align: right; }` and similar) written
for the Hedge Effectiveness table's numeric columns. Because those rules
were never scoped to `.hedge-section`, they applied to *every* `<table>` on
the page — including the Cash Positions and Future Exposures input tables,
silently right-aligning their column headers regardless of what CSS was
written locally in `calculator.html`. The unscoped rule had higher CSS
specificity than the local fix, so it won every time.

Two changes resolved this together:

1. `dashboard.css`'s table rules are now scoped to `.hedge-section table`,
   `.hedge-section thead th`, etc. — they can no longer leak onto any other
   table on the page.
2. The two input grids were rebuilt with CSS Grid instead of `<table>`, so
   even if another unscoped table rule is introduced somewhere in the future,
   these two grids are structurally immune to it (there is no `<table>`,
   `<th>`, or `<td>` for such a rule to match).

**For future developers:** if you add a new data table anywhere in this app,
either (a) scope all of its CSS under a dedicated class the way
`.hedge-section` does, or (b) follow the CSS Grid pattern used by
`.cash-cols` / `.exp-cols` if it's an input form rather than a read-only
data display. Never write bare `table`, `thead th`, `tbody td` selectors in
`dashboard.css` or `calculator.html`'s `<style>` block — they apply
page-wide and will silently affect every table that exists now or is added
later.

---

## Running Locally

**1. Create and activate a virtual environment (one-time):**
```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows
```

**2. Install dependencies (one-time):**
```bash
pip install -r requirements.txt
```

**3. Test the engine without Flask (optional but recommended):**
```bash
python3 engine_runner.py
```
Runs the full engine and prints all sections to the terminal.
Verify: Bucket 1 shows cash positions as synthetic receivables.
Bucket 2 USD shows natural hedge benefit (recv 2mn offset by pay 1mn).
MYR also shows hedge benefit in Bucket 2.

**4. Start the web server:**
```bash
python3 app.py
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

**Portfolio VaR (covariance-adjusted, exact cross-horizon):**

For the consolidated portfolio VaR, every individual position enters the
covariance matrix separately. The exact covariance matrix entry between
position i and j is:

```
Σ[i,j] = ρ[i,j] × σᵢ_daily × σⱼ_daily × min(Tᵢ, Tⱼ)

where:
  σᵢ_daily   = σᵢ_annual / √252
  min(Tᵢ,Tⱼ) = overlapping days: under the random walk assumption, only
                same-day return pairs survive when expanding Cov(rᵢ_Ti, rⱼ_Tj),
                and position i co-exists with position j for min(Ti,Tj) days

  Same-currency pairs:  ρ[i,j] = 1.0  (same exchange rate)
  Different-currency:   ρ[i,j] = historical Pearson correlation
```

Portfolio VaR:
```
Portfolio VaR = Z × √(sᵀ Σ s) − sᵀ μ_T

where:
  sᵢ      = signed exposure of position i in base currency
  μ_T[i]  = μᵢ_daily × Tᵢ   (each position's own T)
```

**Component VaR and Euler decomposition:**

Period VaR is decomposed into per-currency Component CFaRs using:

```
ComponentVaR_i = (sᵢ × (Σs)ᵢ / σ_p) × Z − sᵢ × μᵢ × Tᵢ

where:
  (Σs)ᵢ = i-th element of cov_T @ s    (full covariance matrix × signed exposures)
  σ_p   = √(sᵀ Σ s)                    (portfolio volatility)
```

(Σs)ᵢ encodes every cross-currency correlation involving currency i, weighted
by every other currency's signed exposure — so each component already contains
the full portfolio correlation structure, not just a single-currency standalone VaR.

By the Euler homogeneity theorem:
```
Σᵢ ComponentVaR_i = Portfolio VaR    (exactly)
```

This is why the bars in the chart sum to the Period VaR strip — not because the
components are independent, but because they are constructed as a partition of
the covariance result.

**Why Component CFaR differs from standalone VaR:**

A currency with large net notional can show low Component CFaR when its positions
partially offset others in the covariance matrix (e.g. a long USD position hedges
against short MYR because USDSGD and MYRSGD are positively correlated). This is
mathematically correct — Component CFaR measures marginal risk contribution to the
portfolio, not isolated standalone risk. For standalone risk per position, use the
Bucketed Risk Detail section.

**Zero net exposure / non-zero Component CFaR:**

A currency can have zero net notional within a period but still carry a non-zero
Component CFaR. This happens when positions in the same currency settle at
different dates — the min(Tᵢ,Tⱼ) formula does not fully cancel them because
one position outlives the other. Zero-notional bars appear in grey with CFaR
labels floating in blue above the baseline.

**Time bucketing for future exposures:**

| Bucket | Window       | Midpoint T |
|--------|--------------|------------|
| 1      | 0–1 month    | 10 days    |
| 2      | 1–3 months   | 42 days    |
| 3      | 3–6 months   | 95 days    |
| 4      | 6–12 months  | 189 days   |
| 5      | >12 months   | 315 days   |

Cash holdings are routed into Bucket 1 as synthetic receivables (settlement =
1 trading day). Positions are netted within each bucket/currency before VaR
is computed. Covariance is applied across currencies within each bucket.

**Cumulative period filter (V3):**

| Period key | Label          | Settlement cutoff |
|------------|----------------|-------------------|
| 1m         | Next 1 month   | T ≤ 21 days       |
| 3m         | Next 3 months  | T ≤ 63 days       |
| 6m         | Next 6 months  | T ≤ 126 days      |
| 12m        | Next 12 months | T ≤ 252 days      |
| all        | All            | No cutoff         |

Each period runs the full covariance VaR calculation (same min(Tᵢ,Tⱼ) formula)
on only the positions whose actual settlement T falls within that window. The
'all' period is identical to the consolidated portfolio VaR. The Settlement Cutoff
shown in the info strip is a position filter — it is NOT a horizon used in any
VaR calculation.

---

## Dashboard — Design Decisions

### CFaR vs VaR labelling

| Term | What E represents | Where shown |
|---|---|---|
| VaR | Market value of cash holding | Cash Book Risk section |
| CFaR | Notional of future cash flow | Bucketed Risk Detail |
| Component CFaR | Marginal covariance contribution per currency | Dashboard bar chart |
| Portfolio VaR | All positions combined | Consolidated + period strip |

### Gross Standalone Risk — Scope

Gross Standalone Risk includes both:
- **Forward standalone VaRs** (Section 3 internal, no netting, bucket-midpoint T)
- **Cash standalone VaRs** (simple sum at cash_horizon T, no diversification)

This makes the scope of the gross baseline consistent with the Consolidated
Portfolio VaR (which includes cash + forwards), so the Risk Reduction percentage
is a clean comparison. Both use the "no diversification" methodology (positions
summed assuming perfect correlation) for the gross side.

Known approximation: forwards use bucket-midpoint T (which can overstate for
positions early in a bucket), while the consolidated VaR uses actual T per
position. This makes the gross baseline very slightly overstated, disclosed in
the UI tooltip.

### Simulation slider mathematics and accuracy

**Currency spot shift (Δ_spot), applied to selected currency only:**
```
new_CFaR         = CFaR × (1 + Δ_spot)                  ← exact
new_net_notional = net_notional_base × (1 + Δ_spot)      ← exact
```
VaR is linear in E, and E ∝ spot_rate, so both scale exactly.

**Volatility regime shift (Δ_vol), applied to all currencies:**
```
new_CFaR_long  = max(vol_term × (1 + Δ_vol) − mu_term, 0)
new_CFaR_short = max(vol_term × (1 + Δ_vol) + mu_term, 0)
```
where vol_term and mu_term are pre-computed by `dashboard_engine.py`.
Scaling CFaR directly by (1 + Δ_vol) would be wrong — it would also scale
mu_term, which is independent of the volatility regime.

**At Δ = 0:** Component CFaRs equal their exact server-side covariance values.
Their sum equals Period VaR exactly (Euler decomposition theorem).

**At Δ ≠ 0:** Each component is scaled independently without rerunning the
covariance matrix. The Period VaR strip (sum of components) is a conservative
approximation. The Portfolio VaR stat card is never updated by sliders.

### Hedge effectiveness

```
hedge_effectiveness = (1 − net_var / gross_var) × 100%
```
Clean ratio because both net_var and gross_var use the same bucket-midpoint T,
same σ, same μ for the same currency. Invariant to both slider types.

Limitation: cross-bucket same-currency netting is not captured here — it only
appears in the consolidated portfolio VaR via ρ=1 and opposite-signed exposures.

---

## Known Limitations (PoC)

| Limitation | Status | Planned fix |
|---|---|---|
| yfinance is an unofficial scrape — can break without notice | ⚠️ Open | Replace with FRED + CurrencyLayer or Bloomberg API |
| Normal distribution understates fat tails for crashing currencies (TRY, ARS) | ⚠️ Open | Monte Carlo with Student's t distribution |
| Bucket midpoint T approximates each exposure's actual settlement T | ⚠️ Open | Exposure-weighted average T per bucket |
| No public holiday calendar — counts Mon–Fri only | ⚠️ Open | Market-specific calendar (SGX, NYSE, MAS) |
| Vol slider approximates during simulation (no covariance recomputation in browser) | ⚠️ By design (PoC) | Add /simulate endpoint or pass correlation matrix to frontend |
| Gross standalone uses bucket-midpoint T for forwards (slight overstatement) | ⚠️ Known, disclosed | Recompute at actual T for clean apples-to-apples |
| Simple sum across currencies within a bucket (assumes perfect correlation) | ✅ Fixed V2.2 | Covariance matrix now applied within each bucket |
| Cash vs forward netting not implemented | ✅ Fixed V2.3 | Cash routed into Bucket 1 as synthetic receivables |
| Cross-horizon VaR aggregation (combining bucket VaRs with different T) | ✅ Fixed V2.4 | Exact individual-position covariance with min(Ti,Tj) cross-terms |
| Dashboard bucket filter shows one bucket at a time | ✅ Fixed V3 | Cumulative period filter with period-level Component CFaR decomposition |
| Separate blue CFaR bar distorted chart when notional and CFaR on different scales | ✅ Fixed V3 | Component CFaR printed as text label inside each bar |
| Cash excluded from gross standalone baseline (scope mismatch with portfolio VaR) | ✅ Fixed | Cash standalone VaRs now included in gross baseline (Option A) |
| CFaR labels inside bars only visible on hover | ✅ Fixed | Labels render immediately on chart load (chart.update after _cfarValues set) |
| Floating-point noise producing "SGD -0" in hedge benefit column | ✅ Fixed | max(benefit, 0.0) applied before rounding in dashboard_engine.py |
| Cash Positions / Future Exposures table headers misaligned with their columns | ✅ Fixed | Rebuilt as CSS Grid (see Frontend Architecture Note below); unscoped table CSS in dashboard.css now scoped to .hedge-section |

---

## Deployment (Render)

This app is deployed on [Render](https://render.com) at
**https://fx-cash-risk-engine.onrender.com**.

Render does not auto-read the `Procfile` for native (non-Docker) runtimes —
the Start Command is set explicitly in the Render dashboard to match it:

    gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1

The `PORT` environment variable is injected by Render automatically; gunicorn
binds directly to it via `$PORT` in the start command above. (Note: the
`os.environ.get('PORT', 8080)` fallback inside `app.py`'s `__main__` block is
only exercised when running locally with `python app.py` — gunicorn never
calls that block, since it imports the `app` object directly.)

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
| Frontend | Vanilla HTML/CSS/JS + Chart.js 4.4 + chartjs-plugin-datalabels 2.2 |
| Hosting | Render |
| Version control | Git / GitHub |
