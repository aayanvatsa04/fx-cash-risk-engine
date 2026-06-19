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

1. **Cash Book Risk** — standalone VaR on current cash holdings at user-
   specified T, with per-currency breakdown and covariance-adjusted total.
   Appears first, directly below its own Cash VaR Horizon dropdown, so the
   one input that affects this card sits right next to the card it affects.
2. **Consolidated Portfolio VaR** — single portfolio number using the exact
   min(Ti,Tj) cross-horizon covariance formula across all positions, with
   its own independent Portfolio Scenario sliders for a live "what-if"
   stressed figure on the same card.
3. **Bucketed Risk Detail** — full per-bucket technical breakdown: individual
   positions, net VaRs, natural hedge benefit, and diversification benefit
   per bucket. Analyst-level detail.
4. **Risk Dashboard** — headline stat cards, a net notional bar chart paired
   with a separate Component CFaR bar section below it, cumulative period
   filter, its own independent scenario simulation sliders, and hedge
   effectiveness table. Appears last, since it summarises numbers already
   computed in the three sections above rather than introducing new ones.

All four sections are driven by a single API response from one Calculate click.

---

## Page Layout (Results Order)

```
① Global Settings + inputs (cash positions, horizon, future exposures)
② Calculate VaR button
   ── results appear below ──
③ Cash Book Risk (standalone cash VaR at user-specified T)
④ Consolidated Portfolio VaR (full portfolio, exact individual-T covariance)
   └── Portfolio Scenario sliders (±10% spot, ±25% vol) → Stressed Portfolio VaR
⑤ Bucketed Risk Detail (per-bucket netting, attribution, diversification)
⑥ RISK DASHBOARD (summary view, shown last)
   ├── Stat cards: Portfolio VaR · Spot Book VaR · Gross Standalone · Risk Reduction
   ├── Net Cashflow Exposure bar chart (net notional, labelled with its own value)
   ├── Component CFaR bars (separate horizontal bars, sorted by risk magnitude)
   ├── Scenario Simulation sliders (±10% spot, ±25% vol) — independent of ④'s sliders
   └── Natural Hedge Effectiveness table (per currency per bucket)
⑦ Model Notes & Limitations — always visible, independent of the Calculate
   button (renders on page load, before any input is entered; sections
   ③–⑥ above are hidden until the first successful Calculate response)
   (formula, confidence-level meaning, output-section definitions,
    Component CFaR, Gross Standalone Risk scope, market data sourcing,
    Known Limitations table)
```

---

## Features

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
  Both bar systems share the same simulation sliders below them.

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
approximation.

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
vol/spot assumption, for either slider pair.

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
| Vol slider approximates during simulation (no covariance recomputation in browser) | ⚠️ By design (PoC) | Add /simulate endpoint or pass correlation matrix to frontend |
| Gross standalone uses bucket-midpoint T for forwards (slight overstatement) | ⚠️ Known, disclosed | Recompute at actual T for clean apples-to-apples |
| Vol slider's flat-currency case (zero net notional, non-zero Component CFaR — the cross-horizon residual) uses a rougher interim approximation: the whole static CFaR is scaled by (1+Δvol) directly, rather than the exact vol/drift decomposition used for long/short currencies. (V3.5 — this replaced a worse bug where ANY non-zero vol delta snapped these currencies' CFaR straight to 0, since vol_term/mu_term are both precomputed as `net_notional_base × (something)` and are therefore always 0 for a flat currency, regardless of how large its real Component CFaR is.) | ⚠️ Known, disclosed (interim fix in place) | Exact fix is available, not just a better approximation: under a uniform vol-regime shift, the volatility-driven part of Component VaR scales EXACTLY linearly for any position (a provable consequence of the covariance matrix being homogeneous of degree 2 in σ) — requires `exposure_engine.py`'s `_compute_component_vars_by_currency` to expose the vol-part/drift-part decomposition per currency, computed from real per-position sums rather than `net_notional_base` |

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
