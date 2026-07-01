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

1. **Cash Spot Rate Sensitivity** — a simple scenario table showing how each
   cash holding's base-currency equivalent changes under fixed ±% spot rate
   shifts, with no covariance, volatility, or confidence level involved.
   Appears first, answering the intuitive "if USD moves 10% against SGD,
   what do I gain or lose?" question before the more technical parametric
   VaR figure right below it.
2. **Cash Book Risk** — standalone VaR on current cash holdings at user-
   specified T, with per-currency breakdown and covariance-adjusted total.
   Appears directly below its own Cash VaR Horizon dropdown, so the
   one input that affects this card sits right next to the card it affects.
3. **Consolidated Portfolio VaR** — single portfolio number using the exact
   min(Ti,Tj) cross-horizon covariance formula across all positions, with
   its own independent Portfolio Scenario sliders for a live "what-if"
   stressed figure on the same card.
4. **Hedge Recommendations** — ranked forward-contract proposals that
   reduce Consolidated Portfolio VaR, triggered by a dedicated "Get Hedge
   Recommendations" button on the Consolidated VaR card. Separate from
   Calculate because it re-runs the full covariance engine once per hedge
   candidate (O(N) calls). Rows that would increase portfolio VaR are
   flagged with amber visual warnings so the treasurer knows where to stop.
5. **Bucketed Risk Detail** — full per-bucket technical breakdown: individual
   positions, net VaRs, natural hedge benefit, and diversification benefit
   per bucket. Analyst-level detail.
6. **Risk Dashboard** — headline stat cards, a net notional bar chart paired
   with a separate Component CFaR bar section below it, cumulative period
   filter, its own independent scenario simulation sliders, and hedge
   effectiveness table. Appears last, since it summarises numbers already
   computed in the five sections above rather than introducing new ones.

Sections 1–3 and 5–6 are driven by a single API response from one Calculate
click. Section 4 is triggered separately on demand via a dedicated button
(POST /recommend_hedges).

---

## Page Layout (Results Order)

```
① Global Settings + inputs (cash positions, horizon, future exposures)
② Calculate VaR button
   ── results appear below ──
③ Cash Spot Rate Sensitivity (simple % scenario table on cash, no covariance/VaR)
④ Cash Book Risk (standalone cash VaR at user-specified T)
⑤ Consolidated Portfolio VaR (full portfolio, exact individual-T covariance)
   └── Portfolio Scenario sliders (±10% spot, ±25% vol) → Stressed Portfolio VaR
   └── ⬡ Get Hedge Recommendations button (triggers ⑥ below via POST /recommend_hedges)
⑥ Hedge Recommendations (shown only after button click — separate from Calculate)
   ├── Summary strip: Baseline VaR → Optimal Hedged VaR · % reduction
   ├── Ranked table: proposed forward per (currency, bucket), VaR before/after,
   │   marginal and cumulative reduction — amber row + ⚠ badge for any hedge
   │   that increases VaR (counterproductive due to covariance shift)
   └── PoC Simplifications disclaimer (forward rate ≈ spot, bucket midpoint T,
       cash excluded)
⑦ Bucketed Risk Detail (per-bucket netting, attribution, diversification)
⑧ RISK DASHBOARD (summary view, shown last)
   ├── Stat cards: Portfolio VaR · Spot Book VaR · Gross Standalone · Risk Reduction
   ├── Net Cashflow Exposure bar chart (net notional, labelled with its own value)
   ├── Component CFaR bars (separate horizontal bars, sorted by risk magnitude)
   │   NOTE: same currency order as Hedge Recommendations table — both rank by
   │   Component CFaR from the 'all' period, so the two sections reinforce each other
   ├── Scenario Simulation sliders (±10% spot, ±25% vol) — independent of ⑤'s sliders
   └── Natural Hedge Effectiveness table (per currency per bucket)
⑨ Model Notes & Limitations — always visible, independent of the Calculate
   button (renders on page load, before any input is entered; sections
   ③–⑧ above are hidden until the first successful Calculate response;
   section ⑥ additionally requires the explicit button click)
   (formula, confidence-level meaning, output-section definitions,
    Component CFaR, Gross Standalone Risk scope, market data sourcing,
    Known Limitations table)
```

---

## Features

- **Cash Spot Rate Sensitivity (V3.8):** A simple scenario table showing how
  each cash holding's base-currency equivalent changes under seven fixed spot
  rate scenarios (±20%, ±10%, ±5%, No Change) — deliberately simpler than the
  parametric VaR below it: no covariance, volatility, or confidence level
  involved. Answers the intuitive CFO question "if USD moves 10% against
  SGD, what do I gain or lose?" before the more technical Cash Book Risk
  figure. Cash positions only — forward exposures are excluded, since their
  payable/receivable direction needs additional sign logic already handled
  by the parametric VaR in Bucketed Risk Detail. Rendered entirely by
  `renderSensitivity()` in `calculator.html`, which reuses `spot_risk.positions`
  data already present in the `/calculate` response — no backend, engine, or
  `dashboard_engine.py` changes were required. Hidden at page load; shown
  only when cash positions exist.

- **Hedge Recommendation Engine (V3.8):** Proposes ranked FX forward contracts
  to reduce Consolidated Portfolio VaR, triggered by a dedicated "Get Hedge
  Recommendations" button on the Consolidated VaR card. Served by a new
  `POST /recommend_hedges` endpoint — deliberately separate from `POST /calculate`
  because it re-runs `calculate_consolidated_portfolio_var()` once per hedge
  candidate (O(N) engine calls), which is too expensive to include on every
  Calculate click.

  **Algorithm (three steps):**
  1. *Identify candidates* (`_identify_hedge_candidates()` in `exposure_engine.py`):
     group forward exposures by (currency, bucket), sum signed FCY notionals within
     each group. Each group with non-trivial net exposure becomes one candidate —
     a proposed forward in the exact opposite direction at bucket midpoint T.
     Cash is intentionally excluded: it is a liquid asset already held, not a
     forward obligation that needs hedging.
  2. *Rank candidates*: primary sort by absolute Component CFaR of the currency
     (from the 'all' period, descending) — the same ranking that drives the Risk
     Dashboard Component CFaR bars, so the two sections are visually consistent.
     Secondary sort by absolute net notional within the same currency. Component
     CFaR is the right ranking signal because it already encodes cross-currency
     covariance: a large-notional currency that diversifies against another will
     rank lower than a smaller currency driving marginal portfolio risk.
  3. *Apply cumulatively*: add each hedge to the running exposure list in ranked
     order, re-running the full min(Tᵢ,Tⱼ) covariance matrix after each one.
     This is necessary because the covariance structure changes as hedges are
     applied — the marginal reduction of hedge N depends on hedges 1…N-1 already
     being in place. Simple subtraction of Component CFaRs would give wrong answers.

  **Counterproductive hedge detection:** when a candidate's marginal VaR reduction
  is negative (the hedge increases portfolio risk due to changed covariance
  structure), the row is flagged in amber with a ⚠ badge and "adds risk" label.
  The summary strip shows "Optimal Hedged VaR (stop at rank N)" rather than
  "Fully Hedged VaR", directing the treasurer to the actual minimum-risk stopping
  point. This is a known property of the greedy sequential algorithm: the baseline
  Component CFaR ranking is computed on the original portfolio and becomes stale
  as hedges are applied — a future improvement could re-rank after each step.

  **PoC simplifications disclosed in the UI:**
  - Forward rate ≈ spot rate (interest rate parity not modelled)
  - Settlement dates are bucket midpoint T (e.g. 42 days for Bucket 2), not
    the theoretically optimal exposure-weighted average T within the bucket
  - Cash positions excluded from candidates

  **Mathematical validity:** treating a hedging forward as a regular opposing
  exposure in the signed exposure vector is not an approximation — it is exact.
  F (the locked forward rate) is a constant that drops out of the variance
  calculation: for a perfectly matched hedge, both the volatility term (sᵀΣs)
  and drift term (sᵀμ_T) cancel exactly to zero via [+E, −E]. For imperfect
  T-match, the engine correctly captures the residual risk through the
  min(Tᵢ,Tⱼ) formula with no special-casing needed.

- **Cash Book Risk:** Standalone parametric VaR on cash holdings at user-
  specified T. Per-currency breakdown. Portfolio total is covariance-adjusted
  (currencies are not perfectly correlated — diversification benefit shown).
  A plain-language explainer states the confidence level via its complement
  probability (e.g. "5% chance this is exceeded" at 95% confidence) and notes
  the model doesn't cap the tail. When 2+ currencies are held, a teal strip
  states the diversification saving up front, backed by a two-bar visual
  comparing the gross standalone sum against the diversified portfolio VaR
  on a shared scale — see "Cash Book Risk — Diversification Benefit Display"
  below for the full design rationale.

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

- **Gross Standalone Risk:** Gross baseline includes both forward standalone
  VaRs (Section 3, bucket-midpoint T, no netting/correlation) AND cash
  standalone VaRs (at a fixed 10-trading-day horizon, no diversification).
  This makes the scope of the gross baseline consistent with
  consolidated_var, producing a clean apples-to-apples risk reduction
  percentage. The fixed cash horizon here is deliberately independent of
  the Cash VaR Horizon dropdown, which affects only the Cash Book Risk card.

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

- **Two Separate Bar Systems (V3.2):** The Risk Dashboard's Net Cashflow
  Exposure chart shows net notional bars only, each labelled with its own
  value (not Component CFaR) so the printed number always matches what the
  bar's height visually shows. Component CFaR has its own dedicated
  horizontal bar section directly below, scaled to its own range and sorted
  by risk magnitude — this replaced the V3 design where CFaR was printed
  inside the notional bars, which user testing found confusing (a currency
  could show near-zero notional but the chart's single largest CFaR label).
  Both bar systems share the same simulation sliders below them. As of
  V3.7, the Component CFaR bars' order and scale are locked at the moment a
  period first renders (no simulation applied yet) and stay fixed while the
  sliders are used afterwards — only each bar's own width/value moves, via
  a smooth CSS transition, rather than the whole section re-sorting and
  snapping on every tick.

- **Fixed Chart Detail Panel (V3.3):** Hovering a notional bar shows its
  Component CFaR, spot rate, vol, and horizon in a fixed-position DOM panel
  below the chart's legend — not a Chart.js floating tooltip. This replaced
  a real bug: a canvas-anchored tooltip box for the LAST bar gets flipped
  leftward by Chart.js to stay on-canvas, landing on top of the PREVIOUS
  bar's hover zone, so reading it by moving the mouse left immediately
  collapsed it. The fixed panel has no such failure mode for any bar, at
  any width. Defaults to the largest-exposure currency so it's never empty.

- **Portfolio Scenario Sliders (V3.2):** The Consolidated Portfolio VaR card
  has its own independent ±10% spot / ±25% vol slider pair, driving a live
  "Stressed Portfolio VaR" figure on that same card. This is a separate
  slider pair from the Risk Dashboard's Scenario Simulation sliders further
  down the page — moving one never affects the other. At 0%/0% the Stressed
  Portfolio VaR figure exactly equals the Consolidated Portfolio VaR above
  it, since both are computed from the same underlying covariance result
  (`cumulative_vars['all']` is mathematically guaranteed to equal
  `consolidated_var` — see the Cumulative Period Filter feature below).

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
├── app.py                  Flask web server — routes: GET /, POST /calculate,
│                           POST /recommend_hedges (V3.8)
├── var_engine.py           Core VaR math — σ, μ, parametric formula, covariance
├── exposure_engine.py      Business logic — dates, buckets, netting, unified output,
│                           hedge recommendation engine (V3.8)
├── dashboard_engine.py     Data transformation — engine output → chart-ready JSON
├── engine_runner.py        Standalone CLI runner — 8-section verification covering
│                           all engine outputs before any browser involvement
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
  values (`vol_part`, `drift_part` — V3.6) computed by `exposure_engine.py`
  and relayed unchanged through `dashboard_engine.py`.

### When adding features

| New feature type | File to touch |
|---|---|
| New math / statistics | `var_engine.py` |
| New exposure types / bucketing logic | `exposure_engine.py` |
| New dashboard views / stat cards | `dashboard_engine.py` + `calculator.html` |
| New chart behaviour / slider logic | `dashboard.js` |
| New pages or API endpoints | `app.py` |
| New hedge strategy / ranking logic | `exposure_engine.py` (`_identify_hedge_candidates`, `recommend_hedges`) |
| New engine verification section | `engine_runner.py` (add a new `print_<feature>()` function, wire into `__main__`) |

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
calculator.html JS renders sections ③–⑤ and ⑦–⑧
  ↓
fires CustomEvent('varResultReady', { detail: result.dashboard })
  ↓
dashboard.js receives event → renders chart, sliders, hedge table

── separately, on button click ──

User clicks "Get Hedge Recommendations"
  ↓
calculator.html JS reads same form inputs  →  POST /recommend_hedges
  ↓
app.py calls recommend_hedges()             — O(N) engine re-runs, one per candidate
  ↓
JSON response returned
  ↓
calculator.html JS renders section ⑥ (Hedge Recommendations)
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

**3. Test the engine without Flask (recommended before every push):**
```bash
python3 engine_runner.py
```
Runs all eight verification sections against a representative test scenario
and prints ✓/✗ for every sanity check, with a final PASS/FAIL summary.
No Flask server or browser needed — this is the first abstraction barrier
check: if a number is wrong here, it will be wrong in the browser too.

Sections covered: [S1] Spot Book Risk · [S1b] Cash Sensitivity source data ·
[S2] Bucketed Risk · [S3] Gross Attribution · [S3b] Gross Cash Attribution ·
[S4] Consolidated VaR · [S5] Cumulative Period VaRs + Component CFaR ·
[S6] Hedge Recommendations

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

This is why the Component CFaR bars sum to the Period VaR strip — not because
the components are independent, but because they are constructed as a
partition of the covariance result.

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
one position outlives the other. As of V3.2 this shows naturally and clearly:
the currency's bar in the notional chart correctly shows zero (nothing to
report there), while its bar in the separate Component CFaR section below
shows the real, non-zero risk — no special-casing or floating labels needed,
since the two bar systems are on independent scales by design.

As of V3.4, the full walkthrough explanation of WHY this happens (the
T=43-vs-T=100 receivable/payable example) lives in exactly one place: a
hover tooltip on the note shown inside the Chart Detail Panel, attached
contextually to whichever specific currency is actually exhibiting the case
(`renderChartDetailPanel()` in `dashboard.js`). The Component CFaR section's
own header tooltip keeps only a short, general explanation of what Component
CFaR means and points to the chart for this specific case, rather than
repeating the full walkthrough as a generic disclaimer shown regardless of
whether anything in the current view needs it.

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

## Cash Book Risk — Diversification Benefit Display

The Cash Book Risk card surfaces two pieces of information that previously
lived only in a single quiet sentence, both purely in `calculator.html` —
no engine, math, or API changes were needed for either, since `total_var`,
`total_var_cov`, and `diversification_benefit` were already computed and
returned by `var_engine.py` / `exposure_engine.py`.

**Plain-language explainer.** A second sentence beneath the existing bold
one-liner restates the VaR figure using the complement-probability framing —
e.g. at 95% confidence: "there is a 5% chance that your actual loss... could
exceed [VaR]" — rather than "95% chance of NOT losing more than [VaR]".
This was a deliberate choice over the equally-valid 95%-framing, for two
reasons: it is closer to how VaR is formally defined (the threshold such
that the probability of exceeding it is at most the complement), and it
avoids a known communication problem where a large confident-sounding
percentage reads as reassurance and says nothing about how bad the
remaining tail could be. The explainer always reads the actual `confidence`
value from the result (`compPct = Math.round((1 - confidence) * 100)`), so
it is correct at any Confidence dropdown setting (90/95/99%) and is never
hardcoded to one level. An earlier draft of this sentence also referenced
"1 trading day in 20" as an intuition aid for the 5% case — this was
deliberately dropped, not just for clarity but for correctness: VaR makes a
single one-shot probability statement about the cumulative loss over the
whole horizon, and a day-counting frequency analogy nudges the reader
toward a different (and wrong) per-day repeated-trial mental model.

**Diversification benefit strip + comparison bars.** When 2+ cash currencies
are held and the covariance-adjusted total differs meaningfully from the
simple sum (same condition the old single-sentence note used:
`diversification_benefit > 0.01` and more than one position), a teal strip
states the benefit and percentage saved up front, followed by two stacked
bars on a shared scale: "Gross Standalone Sum" (always the full-width
reference bar, since it is the larger of the two by construction) and
"Portfolio VaR (diversified)" (drawn at `headlineVar / simpleSum × 100`
percent of that width). The bar-width comparison makes the size of the
benefit visually obvious without requiring the reader to parse any numbers.
Colour choice is deliberate: teal (`--teal`) is the same token already used
for "diversification" / "risk reduction" everywhere else in the app (the
Risk Dashboard's Risk Reduction stat card, per-bucket
`diversification_benefit` fields) — kept distinct from the purple
(`--hedge`) used by `.hedge-strip` elsewhere, since that colour is reserved
for *natural* hedging (within-currency netting), a related but different
effect from cross-currency diversification. With a single currency held,
there is nothing to diversify against, so the whole block stays hidden,
consistent with the old note's behaviour.

---

## Dashboard — Design Decisions

### CFaR vs VaR labelling

| Term | What E represents | Where shown |
|---|---|---|
| VaR | Market value of cash holding | Cash Book Risk section |
| CFaR | Notional of future cash flow | Bucketed Risk Detail |
| Component CFaR | Marginal covariance contribution per currency | Dashboard Component CFaR bars |
| Portfolio VaR | All positions combined | Consolidated + period strip |
| *(none — not a VaR/CFaR figure)* | Raw spot-rate scenario arithmetic, no probability or confidence level | Cash Spot Rate Sensitivity table |

### Gross Standalone Risk — Scope

Gross Standalone Risk includes both:
- **Forward standalone VaRs** (Section 3 internal, no netting, bucket-midpoint T)
- **Cash standalone VaRs** (simple sum at a fixed 10-trading-day horizon —
  CASH_CONSOLIDATED_T_DAYS, independent of the Cash VaR Horizon dropdown —
  no diversification)

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
new_CFaR         = CFaR × (1 + Δ_spot)
new_net_notional = net_notional_base × (1 + Δ_spot)      ← exact (notional conversion is genuinely linear in spot rate)
```
`new_net_notional` is exact. `new_CFaR` is only exact if the shifted
currency has zero correlation with the rest of the portfolio — in a
realistic portfolio it's a first-order approximation, confirmed
empirically: a +5% spot shift on a correlated currency showed roughly a
0.8% gap between this formula's prediction and a real engine re-run for
that currency's own component, and other currencies (left frozen by this
formula) moved by a few percent in the real re-run when the formula shows
0% movement for them. This is a disclosed, by-design PoC tradeoff (see
Known Limitations) — **not** the same kind of issue the vol slider had
before V3.6. Tested with shifts as small as ±0.01% around zero: both the
true values and this formula's predictions move smoothly, with no jump at
Δ_spot = 0 or anywhere else — the inaccuracy grows gradually with shift
size rather than appearing suddenly. Re-run Calculate for the exact figure
at a genuinely different spot-rate assumption.

**Volatility regime shift (Δ_vol), applied to all currencies — V3.6, exact:**
```
new_CFaR = (1 + Δ_vol) × vol_part − drift_part
```
ONE formula for every currency — long, short, or flat alike. `vol_part` and
`drift_part` are computed in `exposure_engine.py`'s
`_compute_component_vars_by_currency()` from the same per-position arrays
that produce the exact static CFaR itself (not from `net_notional_base`,
and not from any single-T approximation).

This replaced a prior approach (`vol_term`/`mu_term`, derived from
`net_notional_base` and an exposure-weighted `effective_T`) that could
diverge sharply from the exact CFaR it was meant to approximate — testing
against a real portfolio found jumps of several hundred percent, and even
sign flips, from a vol delta of a few thousandths of a percent. The deeper
flaw: `vol_term`/`mu_term` were a standalone, single-position-style
estimate that ignored cross-currency covariance entirely, while Component
CFaR is fundamentally a *marginal* quantity that only means what it means
because of the full covariance matrix — there's no reason these two
calculations should agree, and empirically they often didn't.

**Why this is now exact, not just continuous:** under a *uniform* vol-regime
shift (every currency's σ scaled by the same factor k = 1+Δ_vol — exactly
what this slider does), the volatility-driven part of Component VaR scales
EXACTLY linearly by k. This is a provable identity:
```
Σ[i,j](k) = ρ[i,j] × (kσᵢ)(kσⱼ) × min(Tᵢ,Tⱼ) = k² × Σ[i,j](1)
⟹ σ_p(k) = k × σ_p(1)   and   (Σ(k)s)ᵢ = k² × (Σ(1)s)ᵢ
⟹ vol_componentᵢ(k) = sᵢ(Σ(k)s)ᵢ/σ_p(k) × Z = k × vol_componentᵢ(1)
```
It holds regardless of net notional — including the cross-horizon residual
case (a currency with zero net notional but real Component CFaR, from
same-currency positions settling at different dates), which previously
forced `vol_term = mu_term = 0` and made that currency's simulated CFaR
snap straight to zero on any vol move (patched as an interim measure in
V3.5, fully superseded by this exact fix).

Verified empirically, not just symbolically: a full engine re-run at a
+25% vol level matched this formula's prediction for every currency, and
for the portfolio-level sum, to within floating-point rounding.

**At Δ = 0:** Component CFaRs equal their exact server-side covariance values.
Their sum equals Period VaR exactly (Euler decomposition theorem).

**At Δ_vol ≠ 0, Δ_spot = 0:** exact — the Period VaR strip (sum of components)
equals what a full server-side re-run at that vol level would produce.

**At Δ_spot ≠ 0 (with or without a simultaneous vol shift):** still an
approximation, for the reason given above — re-run Calculate for the exact
figure at a genuinely different spot-rate assumption.

**No floor at zero:** neither formula clamps its result to ≥ 0. The static
CFaR itself is never floored server-side (a currency that hedges the rest
of the portfolio can have a genuinely negative component VaR — see
`exposure_engine.py`'s docstring, "Negative component VaR"), so flooring
only the simulated path would create a mismatch right at the Δ=0 boundary.

**Two independent slider pairs (V3.2):** The Risk Dashboard's stat cards
(including its own "Portfolio VaR (exact)" card) are never updated by either
slider pair — those stay fixed at the exact server-computed values for the
original inputs, full stop. The ONE deliberate exception is the Consolidated
Portfolio VaR card's own "Stressed Portfolio VaR" figure, which has its own
independent Portfolio Scenario slider pair (separate JS state, separate DOM
elements, fully decoupled from the Risk Dashboard's sliders below) and
recomputes live using the identical method described above — summing
simulated Component CFaRs from the `'all'` cumulative period, which
`exposure_engine.py` guarantees equals `consolidated_var` exactly at Δ=0.
Re-run Calculate to get an exact server-computed figure at a genuinely new
spot assumption, for either slider pair.

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

This table is for things that are still actually true about the current
build — open gaps, deliberate PoC tradeoffs, and known approximations.
Once something here gets fixed, the row moves out of this table and into
Changes Made below, rather than being marked "✅ Fixed" in place — that
keeps this table a reliable list of what to still watch out for, instead
of a mix of "still a problem" and "no longer a problem" rows that a future
developer has to read carefully to tell apart.

| Limitation | Status | Planned fix |
|---|---|---|
| yfinance is an unofficial scrape — can break without notice | ⚠️ Open | Replace with FRED + CurrencyLayer or Bloomberg API |
| Normal distribution understates fat tails for crashing currencies (TRY, ARS) | ⚠️ Open | Monte Carlo with Student's t distribution |
| Bucket midpoint T approximates each exposure's actual settlement T | ⚠️ Open | Exposure-weighted average T per bucket |
| No public holiday calendar — counts Mon–Fri only | ⚠️ Open | Market-specific calendar (SGX, NYSE, MAS) |
| Spot slider is a smooth but imprecise approximation when the shifted currency correlates with others — even its own component is only first-order correct (~0.8% gap measured at a 5% shift), and other currencies are left frozen instead of reflecting the real cross-covariance effect | ⚠️ By design (PoC) | Add /simulate endpoint or pass correlation matrix to frontend |
| Gross standalone uses bucket-midpoint T for forwards (slight overstatement) | ⚠️ Known, disclosed | Recompute at actual T for clean apples-to-apples |

---

## Changes Made

A running log of everything that's been fixed, in roughly the order it
happened. New entries get **appended to the bottom** of this table — that
way the file's own edit history (oldest change at the top, newest at the
bottom) matches how you'd read a normal changelog, and there's never a
question of where a new row goes.

| Change | Detail |
|---|---|
| Covariance matrix applied within each bucket (V2.2) | Replaced the simple sum across currencies within a bucket, which had assumed perfect correlation |
| Cash routed into Bucket 1 as synthetic receivables (V2.3) | Implemented cash vs. forward netting, which wasn't previously possible |
| Exact individual-position covariance with min(Tᵢ,Tⱼ) cross-terms (V2.4) | Replaced naively summing bucket VaRs across different time horizons, which can't be meaningfully aggregated that way |
| Cumulative period filter with period-level Component CFaR decomposition (V3) | Replaced the old per-bucket dropdown, which only showed one bucket at a time |
| Component CFaR printed as a text label inside each bar (V3) | Replaced the separate blue CFaR bar, which distorted the chart whenever notional and CFaR sat on very different scales |
| Cash standalone VaRs included in the Gross Standalone Risk baseline (Option A) | Previously excluded, creating a scope mismatch against the Consolidated Portfolio VaR baseline it's compared to |
| Component CFaR labels inside bars now render immediately on chart load | Previously only appeared on hover; fixed via `chart.update('none')` after `_cfarValues` is set |
| "SGD -0" display bug fixed at the root | Originally point-fixed only in `dashboard_engine.py`'s `hedge_benefit` field via `max(benefit, 0.0)`; recurred in a different field, since JS's `(-0).toLocaleString()` renders the literal string "-0" for any tiny negative float that survives rounding. Fixed properly at the shared formatting chokepoint instead: `fmtNum()`/`fmtShort()` in dashboard.js and `fmt()`/`fmtC()` in calculator.html all now normalise values within half a cent/unit of zero to a real `0` before formatting |
| Horizontal scroll added to input grids and the Hedge Effectiveness table | Previously clipped or crushed their own content on phone-width screens; `overflow-x:auto` (`.table-wrapper` in calculator.html, `.hedge-table-wrapper` in dashboard.css) with a `minmax` floor on the flexible column lets fields scroll into view instead of collapsing to nothing |
| Vertical stack reflow for result cards and the Known Limitations table on phone-width screens | CSS-only, below 640px (`.breakdown-row`, `.ccy-row-top`, `.limitations-table`) — labels injected via `::before`/`nth-child`, no HTML duplication; the JS render functions are unchanged |
| Duplicate Component CFaR tooltip merged into one; tooltip positioning bug fixed | A tooltip icon was rendering pinned to the page's corner instead of next to the chart legend it explains, because it inherited `position:absolute` with no positioned ancestor to anchor to |
| Component CFaR bar labels abbreviated on narrow charts | `isNarrowChart()` switches to a compact K/M format and smaller font below a 500px chart-width threshold, preventing the label from overflowing past its own bar |
| Dead/duplicate CSS removed (`.dir-badge`, `.eff-bar-wrap/bg/fill`); `.direction-recv` renamed to `.direction-receivable` | Leftovers from an earlier `.dash-`-prefix rename; the renamed selector had never matched its actual generated class name (no visible change, since the fallback colour happened to already match) |
| Design tokens (`:root`) consolidated into dashboard.css only | Was duplicated in both dashboard.css and calculator.html with identical values; dashboard.css's own comment claimed "change the `:root` block only," which wasn't true while a second copy existed and would have silently won the cascade |
| Amount/Balance input columns widened on mobile | The flexible column's floor (`minmax(110px, 1fr)`) only left ~62px for digits after padding, clipping 9-figure amounts (e.g. "2,000,000" displayed as "2,000,0…"); widened to `minmax(160px, 1fr)` |
| Future Exposures table header background fixed to span its full scrollable width | `.grid-header`/`.grid-row` had no explicit width, so their box stretched to the viewport instead of their actual (wider) grid content — backgrounds only paint within an element's own box, not its children's overflow, which created a visible seam partway across the row; fixed with `width: max-content` |
| Cash Positions / Future Exposures table headers rebuilt as CSS Grid | Native `<table>` column widths could drift from body row widths depending on browser/zoom; unscoped table CSS in dashboard.css is now scoped to `.hedge-section` so it can't leak onto these grids either |
| Risk Reduction info tooltip popup fixed | Was clipped/cut off by `overflow:hidden` on `.stat-card`; the accent strip was given its own border-radius instead so the card no longer needs to clip overflow |
| Cash VaR Horizon dropdown decoupled from Consolidated VaR / Risk Dashboard / Component CFaR bars | Previously silently affected all of these, not just the Cash Book Risk card it's documented to affect; cash now uses a fixed `CASH_CONSOLIDATED_T_DAYS = 10` everywhere except Section 1, with a new `calculate_gross_cash_var()` decoupling Gross Standalone Risk's cash component too |
| Results sections reordered: Cash Book Risk → Consolidated Portfolio VaR → Bucketed Risk Detail → Risk Dashboard | Previously Risk Dashboard rendered first, ahead of the three detailed sections it summarises; reordered so the primary/granular figures come first and the summary view comes last, and so Cash Book Risk now sits directly below the Cash VaR Horizon dropdown that's the only input affecting it. Pure DOM reorder in `calculator.html` — no JS, CSS, or Python logic changed, since each section is shown/hidden independently via `getElementById` rather than by position |
| Cash Book Risk: diversification benefit raised from a quiet footnote to a teal strip + gross-vs-net comparison bars; VaR explainer expanded with a complement-probability plain-language sentence | Previously the only mention of the diversification saving was a single muted sentence (`#rPortfolioNote`), and the only explanation of the VaR figure was one short bold line; mentor feedback during a demo prompted both additions. Frontend-only — `total_var`, `total_var_cov`, and `diversification_benefit` were already computed by the engine, no Python changes |
| Risk Dashboard chart redesigned into two separate bar systems (V3.2) — net notional bars (unchanged position, now labelled with their own value) above a new horizontal Component CFaR bar section (`renderCfarBarsForPeriod`, sorted by risk magnitude, on its own scale) | User testing found the previous design unintuitive: bar height encoded net notional while the printed label encoded Component CFaR — two different quantities sharing one visual, so a currency with near-zero notional could carry the single largest CFaR label on the chart. Splitting "where is my money" (notional bars) from "what can I lose" (CFaR bars) resolves this without losing any data — Component CFaR is still shown alongside the notional chart's per-currency details (originally a floating tooltip, later replaced — see the V3.3 row below). Frontend-only (`dashboard.js`, `calculator.html`, `dashboard.css`) — no Python changes, since `dashboard_engine.py`'s existing per-currency `cfar`/`vol_term`/`mu_term`/`net_notional_base` fields already covered both bar systems |
| Independent "Portfolio Scenario" spot/vol sliders added to the Consolidated Portfolio VaR card, driving a new "Stressed Portfolio VaR" figure | Previously the only simulation sliders lived in the Risk Dashboard, far down the page from the Consolidated VaR card they'd need to scroll up to see reflected in. This second slider pair is fully independent (separate JS state, separate DOM elements) and stresses the Consolidated VaR card's own figure in place. Required no new backend computation: `exposure_engine.py` already guarantees `cumulative_vars['all']['period_var']` equals `consolidated_var['total_var']` exactly, so the existing `'all'` period's per-currency `vol_term`/`mu_term`/`cfar` values (already sent to the frontend) are reused as-is via the same `applySimulation()` function the Risk Dashboard sliders use — pure frontend wiring, no Python changes |
| Notional chart's floating Chart.js tooltip replaced with a fixed-position DOM panel (V3.3) — `#dashChartDetail`, rendered by the new `renderChartDetailPanel()`, driven by the chart's `onHover` callback instead of `plugins.tooltip` | Real, reproducible bug, caught by screenshot: for the LAST bar on the x-axis, Chart.js flips its floating tooltip box leftward to keep it on-canvas, landing the box's pixels on top of the PREVIOUS bar's hover-detection column (chart uses `interaction: {mode:'index', intersect:false}`, so hover is driven purely by x-proximity). Moving the mouse left to read the box re-triggered hover on that previous bar, collapsing the very box being read — made the last bar's details unreadable, no matter how the mouse moved. A fixed DOM element has no such failure mode for any bar, at any width, since its screen position never overlaps a bar's hover-detection zone. Defaults to showing the largest-exposure currency so it's never empty. Frontend-only (`dashboard.js`, `calculator.html`, `dashboard.css`) — no Python changes |
| Zero-notional/non-zero-CFaR explanation split into two tooltips (V3.4) — the full T=43-vs-T=100 walkthrough moved from the Component CFaR section's header tooltip into a hoverable "ⓘ" on the Chart Detail Panel's own note, attached contextually to whichever currency actually exhibits the case | The header tooltip previously carried the full walkthrough as a generic disclaimer, shown identically regardless of whether anything in the current view needed it, making it long. The Chart Detail Panel's note (added in V3.3) initially showed this as plain, non-interactive text with no further detail. Moving the full explanation there instead — using the same `.dash-tooltip-icon`/`.tip-inline` CSS-only hover mechanism already used elsewhere (a plain `:hover::after` popup reading `data-tip`, NOT the buggy Chart.js canvas tooltip fixed in V3.3 — no shared mechanism, no risk of reintroducing that bug) — makes the explanation appear exactly when and where it's relevant, and lets the header tooltip stay short and general. Frontend-only (`dashboard.js`, `calculator.html`) — no Python changes |
| Vol slider no longer snaps a flat currency's Component CFaR to exactly 0 on any non-zero move (V3.5 interim fix) — `applySimulation()`'s flat-direction branch now scales the static exact `cfar` by `(1+Δvol)` instead of hardcoding `cfar = 0` | Caught via screenshot: a −0.1% vol nudge sent MYR's Component CFaR from SGD 55,406 straight to SGD 0. Root cause was two-layered — an explicit `else { cfar = 0 }` branch for flat currencies, AND the deeper fact that `vol_term`/`mu_term` are both precomputed server-side as `net_notional_base × (something)`, which is always 0 for a flat (net-zero-notional) currency regardless of how large its real cross-horizon-residual Component CFaR is. This interim patch is frontend-only (`dashboard.js`, `calculator.html` sim-note copy) and is an approximation, not an exact fix — it's tracked as a remaining item in Known Limitations, with the exact fix (a provable linear-scaling identity under uniform vol shifts) requiring a backend change to expose a proper vol-part/drift-part decomposition per currency, deliberately deferred per the user's request to scope it as a separate, future, branch-worthy change |
| Vol slider made fully EXACT, not just continuous (V3.6) — superseded the V3.5 interim patch entirely. `net_notional_base`-derived `vol_term`/`mu_term` removed completely; replaced with `vol_part`/`drift_part`, computed in `exposure_engine.py`'s `_compute_component_vars_by_currency()` from the same per-position arrays that produce the exact static Component CFaR. `applySimulation()` is now ONE formula for every currency (long, short, or flat) — `new_cfar = (1+Δvol)×vol_part − drift_part` — with no direction branching and no zero-floor (negative components are meaningful, not errors) | User pushed back on whether a 0.1% vol move should really change numbers as much as it did, even after the V3.5 patch — testing confirmed it was much worse than the flat-currency case alone: real currencies in a real portfolio showed jumps of several hundred percent (one case 4,070%) and even sign flips from a vol delta of a few thousandths of a percent, because the old `vol_term`/`mu_term` was a standalone single-position-style estimate that completely ignored cross-currency covariance — fundamentally a different quantity from the exact, marginal Component CFaR it was meant to approximate. The fix isn't a better approximation; it's mathematically exact, provable from the covariance matrix being homogeneous of degree 2 in σ under a uniform vol-regime shift (see `_compute_component_vars_by_currency`'s docstring for the full derivation). Verified empirically, not just symbolically: every currency's simulated value at +25% vol matched a full engine re-run to within floating-point rounding, including MYR's cross-horizon residual case, with zero special-casing required in the new code. Branch-worthy (`exposure_engine.py`, `dashboard_engine.py` both touched) — `dashboard.js` and `calculator.html` updated to match; `effective_T` retained as a display-only field (Chart Detail Panel), no longer feeding any simulation math. The spot slider's approximation (shifting one currency's exposure doesn't have the same exact-scaling identity) is unchanged and remains disclosed in Known Limitations |
| Component CFaR bars no longer re-sort or rebuild on every slider tick (V3.7) — `renderCfarBarsForPeriod()` now locks display order and the scaling denominator ONCE per period render (a fresh Calculate or a period-dropdown change, both of which reset sliders to 0 first), and a new `updateCfarBarsInPlace()` handles every subsequent slider tick by mutating each row's existing width/value directly instead of rebuilding the section | Even after V3.6 made the underlying numbers exact, the bars still visually behaved badly: `renderCfarBarsForPeriod()` was being called again on every tick, re-sorting by current `\|CFaR\|` (letting two close-magnitude currencies visually swap rank from a small real change) and rebuilding the section's entire `innerHTML` every time (which silently defeated the `.cfar-bar-fill` CSS width transition — a transition only animates an EXISTING element's property change, and destroying/recreating every element every tick gave the browser nothing to animate from, so bars snapped instead of sliding). Verified empirically with a deliberately-engineered two-currency scenario where one genuinely overtakes the other in magnitude under a vol shift: order stayed locked at its Δ=0 baseline throughout the full slider range, and the same DOM nodes persisted across every tick (confirmed via object identity), proving the CSS transition can now actually animate. Frontend-only (`dashboard.js`, plus a `dashboard.css` comment update) — no Python changes. Also removed an unnecessary `CSS.escape()` call added during this work (currency codes are always simple 3-letter uppercase ISO codes from the backend, never arbitrary/user-controlled strings, so escaping added a Web API dependency for no real benefit) |
| New Cash Spot Rate Sensitivity card added above Cash Book Risk (V3.8) — `renderSensitivity()` and `fmtSensChange()` in `calculator.html` render a simple ±20/±10/±5/No-Change scenario table per cash currency | Mentor feedback: stakeholders wanted an immediate, intuitive "what if the rate moves X%" answer ahead of the more technical parametric VaR figure, without needing to understand confidence levels or covariance first. Implemented as pure spot-rate arithmetic (`change_base = exposure_base × (scenario_pct / 100)`) reusing `exposure_base` already present in `spot_risk.positions` — no backend, engine, or `dashboard_engine.py` changes required. Scoped to cash positions only; forward exposures are excluded since their payable/receivable sign logic is already handled by the parametric VaR in Bucketed Risk Detail. Frontend-only — same negative-zero formatting guard as `fmt()`/`fmtC()` reused via the new `fmtSensChange()` |
| Hedge Recommendation Engine added (V3.8) — new `POST /recommend_hedges` endpoint (`app.py`), `_identify_hedge_candidates()` + `recommend_hedges()` in `exposure_engine.py`, ranked table UI with "Get Hedge Recommendations" button in `calculator.html` | Adds a sixth output section proposing ranked FX forward contracts to reduce Consolidated Portfolio VaR. Algorithm: (1) identify one hedge candidate per (currency, bucket) from forward exposures only — cash excluded; (2) rank by baseline Component CFaR magnitude (same ranking as Risk Dashboard Component CFaR bars, so the two sections are visually consistent); (3) apply cumulatively, re-running `calculate_consolidated_portfolio_var()` after each hedge for an exact marginal reduction figure. Treating hedging forwards as opposing exposures in the signed exposure vector is mathematically exact — F (the locked forward rate) is a constant that drops out of the variance calculation. Intentionally separate from `/calculate` because it is O(N) engine re-runs vs a fixed computation set. `dashboard_engine.py` untouched — hedge recommendation data is already clean for the frontend. Section hidden at page load and reset on every new Calculate click so stale results never persist. |
| Counterproductive hedge visual warning added — rows where `marginal_reduction_abs < 0` (hedge increases portfolio VaR due to changed covariance structure after prior hedges) are flagged in amber: row tint, ⚠ rank badge, amber "adds risk" label in the marginal column | After verifying hedge recommendation output on a real portfolio, rank 7 (AUD) produced a marginal reduction of −SGD 379 — the hedge increased VaR by 0.4%. This is a known property of the greedy sequential algorithm: the ranking uses baseline Component CFaR, which becomes stale as hedges are applied and the portfolio covariance structure changes. The summary strip now distinguishes "Optimal Hedged VaR (stop at rank N)" from "Fully Hedged VaR" when counterproductive rows exist, directing the treasurer to the actual minimum-risk stopping point. Frontend-only (`calculator.html`) — no engine changes. |
| `getHedgeRecommendations()` DOM selector bug fixed — wrong class names (`.cash-row`, `.cash-ccy` etc.), wrong `toRawNumber()` usage (passed `.value` string instead of DOM element), wrong element IDs (`baseCurrency` → `baseCcy`, `period` → `lookback`) | Button was silently collecting zero positions due to non-existent class selectors, causing an early return before any network call fired. Root cause: function was written without reading the actual DOM structure — input rows are `<div class="grid-row">` inside `#cashBody`/`#expBody`, with inputs accessed via element ID (`cashCcy-N`, `cashBal-N` etc.), not via class selectors. Fixed to mirror `calculate()` exactly, with a detailed comment explaining the DOM pattern for future developers. |
| `engine_runner.py` expanded from 3 sections to 8 — now covers every engine output at the terminal level | New sections: [S1b] Cash Sensitivity source data verification (applies the same scenario arithmetic in Python to confirm what the browser will show), [S3b] Gross Cash Attribution (confirms fixed T=10 independent of `cash_horizon`), [S4] Consolidated VaR (full position breakdown), [S5] Cumulative Period VaRs + Component CFaR (verifies Euler identity Σ component_var = period_var for every period, vol_part/drift_part decomposition, and the critical cross-check that 'all' period = consolidated_var), [S6] Hedge Recommendations (all mathematical invariants A–G including cross-check of baseline_var against consolidated_var). Each section returns a boolean; a final pass/fail summary table prints at the bottom. S1 sanity check A updated from absolute threshold (< 1 SGD) to 0.01% relative tolerance to handle cross-rate rounding (MYR/SGD via USD cross-multiplication leaves a ~2 SGD difference on 5mn MYR balance — not an engine error, just stored spot_rate precision). |

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
