/**
 * dashboard.js — Risk Dashboard Rendering Layer (V3.7)
 *
 * This file handles all chart and dashboard rendering inside the unified
 * calculator page (calculator.html). It is loaded once as a separate script
 * and activated by a custom DOM event fired by the calculator's own JS after
 * a successful /calculate response.
 *
 * === HOW IT CONNECTS TO THE CALCULATOR ===
 *
 * The calculator page posts to /calculate and receives a response that
 * contains both the raw engine output AND a 'dashboard' key with pre-formatted
 * chart data. After rendering the engine results, the calculator JS fires:
 *
 *     document.dispatchEvent(new CustomEvent('varResultReady', {
 *         detail: data.dashboard
 *     }));
 *
 * This file listens for that event and calls renderDashboard(). This keeps
 * the two JS files loosely coupled — neither needs to know the other's
 * internal structure. The calculator JS owns the form and engine output
 * (including the STATIC content of the Consolidated Portfolio VaR card —
 * the headline number, interpretation sentence, and position breakdown);
 * this file owns everything that reacts to a slider: the chart, both
 * slider pairs, the Component CFaR bars, and the hedge table.
 *
 * === V3 CHANGE: CUMULATIVE PERIOD FILTER (replaces per-bucket dropdown) ===
 *
 * The bar chart dropdown shows cumulative time windows instead of
 * individual buckets:
 *   'Next 1 month'   → positions in Bucket 1 only
 *   'Next 3 months'  → positions in Buckets 1+2
 *   'Next 6 months'  → positions in Buckets 1+2+3
 *   'Next 12 months' → positions in Buckets 1+2+3+4
 *   'All'            → all 5 buckets
 *
 * Chart data comes from data.cumulative_periods (not data.buckets).
 * The hedge effectiveness table still uses data.buckets (per-bucket view,
 * unchanged from V2).
 *
 * === V3.2 CHANGE: TWO SEPARATE BAR SYSTEMS (replaces inside-bar CFaR labels) ===
 *
 * User testing on the V3.1 design (Component CFaR printed as a text label
 * inside each notional bar) found it unintuitive: bar HEIGHT encoded net
 * notional while the printed LABEL encoded Component CFaR — two unrelated
 * quantities sharing one visual. A currency with near-zero notional could
 * carry the single largest CFaR label on the whole chart (e.g. an exotic,
 * volatile currency with a small position), which reads as a contradiction
 * even though both numbers are individually correct.
 *
 * V3.2 resolves this by answering the two questions a reader actually has —
 * "where is my money?" and "what can I lose?" — with two separate,
 * internally-consistent bar systems instead of one chart trying to encode
 * both:
 *
 *   1. NOTIONAL CHART (top, Chart.js bar chart, unchanged position):
 *      Bar height = net notional. The printed label inside/above each bar
 *      is now the net notional value itself (not CFaR), so the label
 *      always matches what the bar's height visually shows. A currency
 *      with zero notional in this window simply shows no label — there is
 *      nothing to report, no special-casing required.
 *
 *   2. COMPONENT CFaR BARS (new, plain HTML/CSS, stacked directly below the
 *      notional chart inside the same .chart-section card — see
 *      renderCfarBarsForPeriod()): one horizontal bar per currency, bar
 *      LENGTH = Component CFaR, scaled relative to the largest CFaR in the
 *      selected period and sorted with the biggest risk first. This is
 *      where the "currency with small notional but large risk" case now
 *      lives — clearly, on its own scale, explicitly labelled as risk
 *      rather than mixed into the notional chart's scale.
 *
 * Both bar systems are driven by the SAME Risk Dashboard sliders (spot +
 * vol) and the SAME applySimulation() function — only the rendering target
 * differs. Component CFaR is also shown — along with spot rate, vol, and
 * horizon — in the fixed Chart Detail Panel described below; it is no
 * longer printed as a label on the bar itself.
 *
 * === V3.3 CHANGE: FIXED CHART DETAIL PANEL (replaces floating tooltip) ===
 *
 * The notional chart's native Chart.js floating tooltip (which used to
 * show Component CFaR, spot rate, vol, and horizon on hover) has been
 * replaced with a fixed-position DOM panel (#dashChartDetail, rendered by
 * renderChartDetailPanel()) that sits below the chart's legend.
 *
 * This is a structural bug fix, not a cosmetic one. The floating tooltip
 * had a real, reproducible failure: for the LAST bar on the x-axis (no
 * canvas space to its right), Chart.js flips the tooltip box leftward to
 * keep it on-canvas — which places the box's pixels directly on top of the
 * PREVIOUS bar's hover-detection column. Since the chart hit-tests purely
 * by x-proximity (`interaction: { mode: 'index', intersect: false }`),
 * moving the mouse left to actually read the box immediately re-triggers
 * hover on the previous bar, collapsing the box being read. This made the
 * last bar's tooltip effectively unreadable — confirmed via screenshot
 * during testing (MYR, the rightmost bar, with its explanatory zero-
 * notional note).
 *
 * A fixed DOM element has no such failure mode: its screen position never
 * overlaps any bar's hover-detection zone, for ANY bar, on ANY chart
 * width — there's nothing for the mouse to "accidentally" move onto. See
 * renderChartForPeriod()'s onHover callback and renderChartDetailPanel()
 * for the implementation. The panel defaults to showing the largest-
 * exposure currency (index 0) whenever nothing is actively hovered, so it
 * is never empty and never causes a layout-shifting show/hide toggle.
 *
 * === V3.2 ADDITION: PORTFOLIO SCENARIO SLIDERS (Consolidated VaR card) ===
 *
 * A second, fully independent pair of spot/vol sliders now lives inside the
 * Consolidated Portfolio VaR card (#results-consolidated), stressing that
 * card's own headline number ("Stressed Portfolio VaR") rather than the
 * Risk Dashboard's Period VaR strip below. The two slider pairs never
 * affect each other — moving the Risk Dashboard's sliders has no effect on
 * the Consolidated VaR card, and vice versa (separate module-level state:
 * deltaSpot/deltaVol/activeSpotCcy for the Risk Dashboard,
 * portfolioDeltaSpot/portfolioDeltaVol/portfolioActiveSpotCcy for the
 * Consolidated VaR card). Each pair sits physically next to the number it
 * controls, so neither requires scrolling away to see its own effect.
 *
 * This required NO new backend computation. exposure_engine.py guarantees
 * cumulative_vars['all']['period_var'] equals consolidated_var['total_var']
 * exactly — both are computed by the identical min(Tᵢ,Tⱼ) covariance method
 * over the identical full position list (see exposure_engine.py's
 * CUMULATIVE_PERIOD_DEFINITIONS docstring for the guarantee). This means
 * the 'all' period's per-currency vol_part/drift_part/cfar values — already
 * sent to the frontend for the Risk Dashboard's "All" filter option — are
 * equally valid inputs for stressing the Consolidated VaR figure. See
 * updatePortfolioSimulation(), which reuses applySimulation() unchanged.
 *
 * === V3.6 CHANGE: EXACT VOL SLIDER (replaces net_notional_base-derived
 *     vol_term/mu_term with the real per-position vol/drift decomposition) ===
 *
 * Both slider pairs above call the SAME applySimulation() function, which
 * previously approximated a currency's vol-shifted Component CFaR using
 * vol_term/mu_term — two values computed from net_notional_base (the
 * currency's NET signed exposure) and a single exposure-weighted
 * effective_T. Testing against a real portfolio found this approximation
 * could be severely wrong: jumps of several hundred percent — and even
 * sign flips (a currency that genuinely reduces portfolio risk appearing
 * to increase it) — from a vol slider move of a few thousandths of a
 * percent. The root cause: vol_term/mu_term were a standalone,
 * single-position-style estimate that ignored cross-currency covariance
 * entirely, while the exact Component CFaR it was meant to approximate is
 * fundamentally a MARGINAL quantity, dependent on the full covariance
 * matrix. These are different quantities by construction, with no
 * guarantee of agreeing even in the Δ_vol→0 limit. (A more extreme special
 * case of the same root problem — a flat/zero-net-notional currency always
 * showing vol_term=mu_term=0 regardless of its real Component CFaR — was
 * patched as an interim fix in V3.5; V3.6 supersedes that patch entirely.)
 *
 * V3.6 replaces this with vol_part/drift_part — the exact decomposition,
 * computed in exposure_engine.py's _compute_component_vars_by_currency()
 * directly from the same per-position arrays that already produce the
 * exact static Component CFaR. By construction, vol_part − drift_part
 * equals the exact cfar at rest, and under a UNIFORM vol-regime shift
 * (exactly what this slider does), vol_part scales EXACTLY linearly — a
 * provable mathematical identity, not a better approximation. See that
 * function's docstring for the full derivation, and applySimulation()'s
 * docstring below for the resulting single, universal formula (no more
 * long/short/flat branching). Verified empirically: a full engine re-run
 * at a stressed vol level matched this formula's prediction to within
 * floating-point rounding.
 *
 * === V3.7 CHANGE: COMPONENT CFaR BARS — LOCKED ORDER/SCALE, SMOOTH UPDATES ===
 *
 * Even with V3.6's exact numbers underneath, the Component CFaR bars still
 * visually behaved badly on a tiny slider move: renderCfarBarsForPeriod()
 * was being called again on EVERY slider tick, which (a) re-sorted the
 * bars by current |CFaR| every time — letting two currencies close in
 * magnitude visually swap rank from a fractional-percent change — and
 * (b) rebuilt the section's entire innerHTML every time, which silently
 * defeated the .cfar-bar-fill CSS width transition (a transition only
 * animates an EXISTING element's property change; destroying and
 * recreating every element every tick gives the browser nothing to
 * animate from, so bars snapped instead of sliding).
 *
 * V3.7 splits this into two functions with a clear division of labour:
 *   - renderCfarBarsForPeriod() — runs ONCE per period (a fresh Calculate
 *     or a period-dropdown change, both of which reset sliders to 0
 *     first), sorts by |CFaR| descending, and LOCKS that order plus the
 *     scaling denominator (cfarBarsState) for the rest of the period's
 *     lifetime.
 *   - updateCfarBarsInPlace() — runs on every subsequent slider tick,
 *     reusing the LOCKED order/denominator and mutating each row's
 *     existing width/value directly rather than rebuilding anything.
 *
 * Net effect: bars are arranged top-to-bottom once, when a period first
 * renders with no simulation applied — exactly matching "ordered at rest,
 * stable while exploring" — and each bar's width now visibly, smoothly
 * tracks its own value via the CSS transition while a slider is dragged,
 * instead of jumping. See updateCfarBarsInPlace()'s docstring for the
 * full before/after explanation and the "fixed denominator" rationale.
 *
 * === WHAT THIS FILE DOES NOT DO ===
 *
 * - No form handling (that's in calculator.html's inline script)
 * - No fetch calls (the calculator JS fetches; this just consumes the result)
 * - No VaR formulas — simulation uses pre-computed vol_part and drift_part
 *   provided by dashboard_engine.py's _process_cumulative_periods()
 * - Does not render the STATIC content of the Consolidated VaR card
 *   (headline number, interpretation sentence, position breakdown) — that
 *   remains calculator.html's responsibility via renderResults(). This file
 *   only adds the Portfolio Scenario slider panel and its live "Stressed
 *   Portfolio VaR" readout inside that same card.
 *
 * === SIMULATION MATHEMATICS (for reference — used by BOTH slider pairs) ===
 *
 * Volatility slider (Δ_vol, ANY currency — long, short, or flat alike):
 *   new_cfar = (1 + Δ_vol) * vol_part − drift_part
 *   EXACT as of V3.6 — vol_part and drift_part are computed server-side
 *   (exposure_engine.py) from the same per-position arrays that produce the
 *   exact static cfar, not from net_notional_base or any single-T
 *   approximation. One formula for every currency; no direction branching,
 *   no special-casing for zero-net-notional currencies. See
 *   applySimulation()'s docstring for the full proof and the bug history
 *   (V3.0–V3.5) this replaced — jumps of several hundred percent, and even
 *   sign flips, were observed from the old net_notional_base-derived
 *   vol_term/mu_term approach.
 *
 * Spot slider (Δ_spot, selected currency only):
 *   new_cfar = cfar * (1 + Δ_spot)
 *   A smooth, continuous approximation — first-order correct for this
 *   currency's own component, exact only absent correlation with the rest
 *   of the portfolio; other currencies are left frozen rather than
 *   reflecting the small cross-covariance effect a real shift would cause.
 *   See applySimulation()'s docstring, "SPOT SHIFT REMAINS AN
 *   APPROXIMATION — but a SMOOTH one", for the empirically-measured error
 *   size and why this differs in kind from the (now-fixed) vol-slider bug.
 *
 * Neither formula floors at zero (see applySimulation()'s docstring,
 * "NO FLOOR AT ZERO") — a negative Component CFaR is a meaningful, real
 * state (a currency net-hedging the rest of the portfolio), not an error.
 *
 * === SIMULATION APPROXIMATION — what's still approximate vs. now exact, V3.6 ===
 *
 * At Δ=0 (sliders at rest): component CFaRs sum EXACTLY to the relevant
 * server-computed value — period_var for the Risk Dashboard's active
 * period, total_var for the Consolidated VaR card (these are the SAME
 * number when the Risk Dashboard's period happens to be 'all' — see above)
 * — by the Euler decomposition theorem.
 *
 * At Δ_vol ≠ 0, Δ_spot = 0 (pure vol-regime shift — V3.6, now EXACT): the
 * Period VaR strip and the Stressed Portfolio VaR figure now equal exactly
 * what a full server-side re-run at that vol level would produce — not an
 * approximation. This follows directly from vol_part summing the same way
 * at the portfolio level as it does per-currency (see
 * exposure_engine.py's _compute_component_vars_by_currency() docstring for
 * the proof), and was verified empirically: a full engine re-run at a
 * stressed vol level matched the formula's prediction to within
 * floating-point rounding.
 *
 * At Δ_spot ≠ 0 (with or without a simultaneous vol shift): still an
 * approximation. Shifting one currency's exposure changes its
 * cross-covariance terms with every OTHER currency too, which a simple
 * per-currency scale factor cannot capture without rerunning the full n×n
 * matrix server-side. The per-currency sum in this case can diverge from
 * the true diversified VaR by an amount related to that currency's
 * correlation with the rest of the portfolio.
 *
 * This is a deliberate design tradeoff for the remaining spot-shift case:
 * fast live preview vs exact math. All VaR math stays in Python.
 * Re-running Calculate gives the exact figure for any combination of
 * inputs, including a genuinely different spot-rate assumption.
 */

'use strict';

// ============================================================
// GLOBAL STATE
// ============================================================

let dashboardData    = null;  // full 'dashboard' object from /calculate response
let activePeriodKey  = '1m';  // key of currently displayed cumulative period
let activeSpotCcy    = null;  // currency selected in the Risk Dashboard's spot slider dropdown
let deltaSpot        = 0.0;   // Risk Dashboard spot slider delta (−0.10 to +0.10)
let deltaVol         = 0.0;   // Risk Dashboard vol slider delta (−0.25 to +0.25)
let chart            = null;  // Chart.js instance (destroyed + recreated on period change)

// --- Portfolio Scenario state (Consolidated Portfolio VaR card) — V3.2 ---
// Fully independent from the Risk Dashboard state above: a separate slider
// pair lives inside #results-consolidated and stresses that card's own
// "Stressed Portfolio VaR" readout. Always operates on the 'all' cumulative
// period (the full portfolio), regardless of which period is selected in
// the Risk Dashboard's dropdown above. See updatePortfolioSimulation().
let portfolioActiveSpotCcy = null;  // currency selected in the Portfolio Scenario dropdown
let portfolioDeltaSpot     = 0.0;   // Portfolio Scenario spot slider delta (−0.10 to +0.10)
let portfolioDeltaVol      = 0.0;   // Portfolio Scenario vol slider delta (−0.25 to +0.25)

// --- Component CFaR bars locked-baseline state (V3.7) ---
// Established once per period render (renderCfarBarsForPeriod, called from
// renderChartForPeriod on a fresh Calculate or period switch) and then
// reused, UNCHANGED, by every slider tick (updateCfarBarsInPlace) until the
// next period render. This is what stops the bars from re-sorting or
// re-scaling against each other while a slider is being dragged — see
// updateCfarBarsInPlace()'s docstring for the full rationale. null when no
// bars are currently rendered (e.g. the active period has no currencies).
let cfarBarsState = null;


// ============================================================
// INITIALISATION — listen for the calculator's result event
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    // Register the chartjs-plugin-datalabels plugin globally with Chart.js.
    // This must happen before any Chart instance is created (before renderDashboard).
    // The plugin is loaded via CDN in calculator.html immediately before this script.
    // Once registered globally, all Chart instances can use the 'datalabels' plugin
    // option — we configure it per-chart in renderChartForPeriod.
    // Guard in case the CDN fails to load (graceful degradation — bars still render,
    // just without inside-bar net notional labels).
    if (typeof ChartDataLabels !== 'undefined') {
        Chart.register(ChartDataLabels);
    } else {
        console.warn('chartjs-plugin-datalabels not loaded — net notional labels inside bars disabled.');
    }
    /**
     * Listen for the custom event fired by the calculator JS after a
     * successful /calculate response. event.detail is the 'dashboard'
     * sub-object from the response — i.e. prepare_dashboard_data() output.
     */
    document.addEventListener('varResultReady', (event) => {
        dashboardData = event.detail;
        renderDashboard(dashboardData);
    });

    // Wire up period filter dropdown, currency picker, and both Risk
    // Dashboard sliders. These elements exist in the DOM from page load
    // but are hidden until renderDashboard() makes them visible.
    document.getElementById('periodSelect')
        .addEventListener('change', handlePeriodChange);

    document.getElementById('spotCcySelect')
        .addEventListener('change', handleSpotCcyChange);

    document.getElementById('spotSlider')
        .addEventListener('input', handleSpotSlider);

    document.getElementById('volSlider')
        .addEventListener('input', handleVolSlider);

    // Wire up the Portfolio Scenario controls (Consolidated VaR card) — V3.2.
    // Fully independent event chain from the Risk Dashboard controls above:
    // these three call handlePortfolio*() handlers, which update only
    // portfolioDeltaSpot/portfolioDeltaVol/portfolioActiveSpotCcy and only
    // re-render the Consolidated VaR card's own "Stressed Portfolio VaR"
    // figure — they never touch the Risk Dashboard chart or Period VaR strip.
    document.getElementById('portfolioSpotCcySelect')
        .addEventListener('change', handlePortfolioSpotCcyChange);

    document.getElementById('portfolioSpotSlider')
        .addEventListener('input', handlePortfolioSpotSlider);

    document.getElementById('portfolioVolSlider')
        .addEventListener('input', handlePortfolioVolSlider);
});


// ============================================================
// DASHBOARD RENDER — entry point after data arrives
// ============================================================

/**
 * Main render function. Called once each time Calculate is run successfully.
 * Resets BOTH independent simulation states (Risk Dashboard sliders and
 * Portfolio Scenario sliders), then builds all dashboard UI sections.
 *
 * Uses data.cumulative_periods for the bar chart (V3) and the new Component
 * CFaR bars (V3.2), and data.buckets for the hedge effectiveness table
 * (unchanged from V2). The Portfolio Scenario panel (V3.2) also reads from
 * data.cumulative_periods — specifically the 'all' entry — rather than any
 * new field, since cumulative_vars['all'] is guaranteed by exposure_engine.py
 * to equal consolidated_var exactly (see module docstring).
 *
 * @param {Object} data — the 'dashboard' key from the /calculate response
 */
function renderDashboard(data) {
    // Reset Risk Dashboard sliders to centre (0 delta) on every new calculation
    deltaSpot = 0.0;
    deltaVol  = 0.0;
    document.getElementById('spotSlider').value = 0;
    document.getElementById('volSlider').value  = 0;
    updateSliderDisplays();

    // Reset Portfolio Scenario sliders too — independent state, independent
    // reset, so a fresh Calculate always starts both slider pairs at rest.
    portfolioDeltaSpot = 0.0;
    portfolioDeltaVol  = 0.0;
    document.getElementById('portfolioSpotSlider').value = 0;
    document.getElementById('portfolioVolSlider').value  = 0;
    updatePortfolioSliderDisplays();

    // Show the dashboard section (hidden until first run)
    document.getElementById('results-dashboard').style.display = 'block';

    // Populate stat cards with portfolio-level headline numbers
    renderStatCards(data);

    // Populate the cumulative period dropdown and spot CCY picker
    renderPeriodDropdown(data.cumulative_periods);
    renderSpotCurrencyDropdown(data.cumulative_periods);

    // Render chart for the first available period (default: '1m' or first in list)
    const firstPeriod = data.cumulative_periods && data.cumulative_periods[0];
    if (firstPeriod) {
        activePeriodKey = firstPeriod.key;
        document.getElementById('periodSelect').value = firstPeriod.key;
        renderChartForPeriod(activePeriodKey);
    }

    // Hedge effectiveness table still uses per-bucket data (unchanged from V2)
    renderHedgeTable(data.buckets, data.base_ccy);

    // Portfolio Scenario panel (Consolidated VaR card) — V3.2.
    // Populates its currency dropdown from the 'all' period's currency list
    // and computes the initial "Stressed Portfolio VaR" at Δ=0, which is
    // exactly the Consolidated Portfolio VaR figure already shown above it
    // (Euler decomposition theorem — see module docstring). This runs AFTER
    // renderResults() in calculator.html has already populated and shown
    // #results-consolidated (renderResults() runs synchronously before the
    // 'varResultReady' event that triggers this function), so the card's
    // static content is guaranteed to be in place by the time this executes.
    renderPortfolioCurrencyDropdown(data.cumulative_periods);
    updatePortfolioSimulation();
}


// ============================================================
// STAT CARDS
// ============================================================

/**
 * Populates the headline stat cards with portfolio-level numbers.
 * These use the consolidated_var (full portfolio) from the summary.
 * The bar chart shows per-period numbers via renderChartForPeriod.
 *
 * @param {Object} data — dashboard data from /calculate response
 */
function renderStatCards(data) {
    const s       = data.summary;
    const baseCcy = data.base_ccy;
    const conf    = Math.round(data.confidence * 100);

    document.getElementById('dashStatConsolidated').innerHTML =
        `<span class="dash-ccy">${baseCcy}</span>${fmt(s.consolidated_var)}`;

    document.getElementById('dashStatGross').innerHTML =
        `<span class="dash-ccy">${baseCcy}</span>${fmt(s.gross_standalone_sum)}`;

    document.getElementById('dashStatReduction').innerHTML =
        `<span class="dash-ccy">${baseCcy}</span>${fmt(s.total_risk_reduction)}`;

    document.getElementById('dashStatReductionPct').textContent =
        `−${s.total_risk_reduction_pct}%`;

    document.getElementById('dashStatSpotBook').innerHTML =
        `<span class="dash-ccy">${baseCcy}</span>${fmt(s.spot_book_var)}`;

    // Set methodology tooltip on the Risk Reduction card
    const tipEl = document.querySelector('#results-dashboard .dash-tooltip-icon');
    if (tipEl) tipEl.setAttribute('data-tip', s.methodology_note);
}


// ============================================================
// PERIOD DROPDOWN  (V3 — replaces bucket dropdown)
// ============================================================

/**
 * Populates the time-period filter dropdown with available cumulative periods.
 * Options are ordered as received from the server (1m → 3m → 6m → 12m → all).
 *
 * @param {Array} periods — data.cumulative_periods from dashboard response
 */
function renderPeriodDropdown(periods) {
    const sel = document.getElementById('periodSelect');
    sel.innerHTML = '';
    if (!periods || periods.length === 0) return;

    periods.forEach(p => {
        const opt = document.createElement('option');
        opt.value       = p.key;
        // Label shows the human-readable period name plus position count context
        opt.textContent = `${p.label}  (${p.n_positions} position${p.n_positions !== 1 ? 's' : ''})`;
        sel.appendChild(opt);
    });
}

/**
 * Handles period dropdown change event.
 * Resets simulation sliders and re-renders the chart for the selected period.
 */
function handlePeriodChange() {
    activePeriodKey = document.getElementById('periodSelect').value;
    // Reset sliders to centre when switching periods — prevents confusing
    // simulation state carrying over from a different period's data.
    deltaSpot = 0.0;
    deltaVol  = 0.0;
    document.getElementById('spotSlider').value = 0;
    document.getElementById('volSlider').value  = 0;
    updateSliderDisplays();
    renderChartForPeriod(activePeriodKey);
}


// ============================================================
// CHART RENDERING  (V3)
// ============================================================

/**
 * Renders the Chart.js bar chart for the given cumulative period.
 * Destroys any existing chart first to avoid canvas re-use warnings.
 * Also triggers renderCfarBarsForPeriod() so the Component CFaR bars below
 * the chart stay in sync with whichever period is selected, and
 * renderChartDetailPanel() so the fixed detail panel shows the
 * largest-exposure currency by default.
 *
 * === CHART LAYOUT (V3.2) ===
 *
 * ONE dataset: net notional bars, coloured by net direction:
 *   Green  = net long  (receivable — FCY appreciation is a gain)
 *   Red    = net short (payable   — FCY appreciation costs more)
 *   Grey   = flat      (perfectly hedged within this time window)
 *
 * The printed label inside/above each bar is now the bar's OWN value — net
 * notional — rather than Component CFaR. This is the V3.2 fix: previously
 * the label showed a different quantity (CFaR) than the bar's height
 * (notional), which user testing found confusing. Component CFaR is now
 * shown in its own dedicated horizontal-bar section directly below this
 * chart (see renderCfarBarsForPeriod()), and also surfaced — along with
 * spot rate, vol, and horizon — in the fixed Chart Detail Panel below the
 * legend (see renderChartDetailPanel()). See the module docstring's "TWO
 * SEPARATE BAR SYSTEMS" section for the full rationale.
 *
 * Because the label now always equals the bar's own height, the old
 * "zero-notional bar with a non-zero CFaR floating above it" special case
 * no longer needs separate handling here: a currency with ~0 net notional
 * in this window simply gets no label (there is nothing to report), while
 * its Component CFaR — if any — is still visible, clearly, in the section
 * below. (See that function's docstring for why such a currency can still
 * carry meaningful risk despite zero notional: positions cancelling in
 * notional but settling at different dates under the min(Tᵢ,Tⱼ) formula.)
 *
 * === V3.3 CHANGE: FIXED DETAIL PANEL REPLACES FLOATING TOOLTIP ===
 *
 * Chart.js's native floating tooltip (plugins.tooltip) is now fully
 * disabled (`enabled: false`). It used to render per-currency details
 * (Component CFaR, spot rate, vol, horizon) in a box anchored near the
 * cursor. That box had a real, reproducible bug: for the LAST bar on the
 * x-axis (no canvas space to its right), Chart.js flips the box leftward
 * to keep it on-canvas — which places the box's pixels directly on top of
 * the PREVIOUS bar's hover-detection column. Since the chart uses
 * `interaction: { mode: 'index', intersect: false }`, hovering is driven
 * purely by x-proximity to a bar — Chart.js has no concept of "the mouse
 * is now over the tooltip box itself," so moving the mouse left to read
 * the box immediately re-triggers hover on the previous bar instead,
 * collapsing the very box the user was trying to read. This made the last
 * bar's tooltip effectively unreadable.
 *
 * The fix replaces the floating canvas box with `onHover` (below) driving
 * a real, fixed-position DOM element (#dashChartDetail, rendered by
 * renderChartDetailPanel()) that sits below the chart's legend. Because
 * its screen position never overlaps any bar's hover-detection zone — for
 * ANY bar, on ANY chart width — this failure mode is structurally
 * impossible, not just less likely.
 *
 * === DATALABELS POSITIONING LOGIC ===
 *
 * The chartjs-plugin-datalabels anchor/align are set per-bar as functions:
 *   - Short bar (<40px) or zero bar: anchor='end', align based on sign →
 *     label sits just outside the bar tip (or is hidden entirely if the
 *     bar's value rounds to zero — see the `display` callback).
 *   - Tall bar (≥40px): anchor='center', align='center' → centred inside
 *     the bar, white text.
 * Threshold of 40px is empirically chosen for 11px font + 8px padding.
 *
 * Component CFaR is still tracked on chart._cfarValues[] (set after chart
 * creation, updated by updateChartSimulation) — it no longer feeds a
 * tooltip, but renderChartDetailPanel() reads it for the same purpose.
 *
 * @param {string} periodKey — e.g. '3m'
 */
function renderChartForPeriod(periodKey) {
    // Always destroy the previous chart instance before creating a new one.
    // Chart.js re-uses the canvas element, so stale instances cause render errors.
    if (chart) { chart.destroy(); chart = null; }
    if (!dashboardData) return;

    const period   = dashboardData.cumulative_periods.find(p => p.key === periodKey);
    const emptyEl  = document.getElementById('dashChartEmpty');
    const canvasEl = document.getElementById('dashExposureChart');

    if (!period || period.currencies.length === 0) {
        // No data for this period — show placeholder, hide canvas.
        // The Component CFaR section below shows its own matching empty
        // state (renderCfarBarsForPeriod handles a null/empty period itself),
        // and the Chart Detail Panel shows its own empty state too.
        emptyEl.style.display  = 'flex';
        canvasEl.style.display = 'none';
        renderPeriodInfoStrip(period || { period_var: 0, n_positions: 0, max_days: null });
        renderCfarBarsForPeriod(periodKey);
        const detailPanel = document.getElementById('dashChartDetail');
        if (detailPanel) {
            detailPanel.innerHTML = '<div class="cdp-empty">No exposure data for this time period.</div>';
        }
        return;
    }

    emptyEl.style.display  = 'none';
    canvasEl.style.display = 'block';

    const currencies = period.currencies;
    const baseCcy    = dashboardData.base_ccy;
    const conf       = Math.round(dashboardData.confidence * 100);

    // Colour each bar by its net direction in this cumulative window
    const barColors = currencies.map(c => {
        if (c.net_direction === 'long')  return 'rgba(74, 222, 128, 0.70)';   // green
        if (c.net_direction === 'short') return 'rgba(248, 113, 113, 0.70)';  // red
        return 'rgba(107, 114, 128, 0.50)';                                   // grey (flat)
    });

    // Apply current simulation deltas (may be 0 on initial render)
    const simulated    = currencies.map(c => applySimulation(c, deltaSpot, deltaVol, activeSpotCcy));
    const netExposures = simulated.map(s => s.netNotional);
    const cfarValues   = simulated.map(s => s.cfar);

    chart = new Chart(canvasEl, {
        type: 'bar',
        data: {
            labels: currencies.map(c => c.currency),
            datasets: [
                {
                    // === SINGLE DATASET: net notional bars ===
                    // The printed label (configured under datalabels below)
                    // shows this SAME value — net notional — not Component
                    // CFaR. Component CFaR has its own bar section below
                    // the chart (renderCfarBarsForPeriod) and remains
                    // available on hover via the fixed Chart Detail Panel
                    // (renderChartDetailPanel) rather than a floating tooltip.
                    label:              `Net Exposure (${baseCcy})`,
                    data:               netExposures,
                    backgroundColor:    barColors,
                    categoryPercentage: 0.65,
                    barPercentage:      0.85,
                },
            ],
        },
        options: {
            responsive:          true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },

            /**
             * onHover replaces the floating tooltip's job of detecting which
             * bar the mouse is closest to (same 'index'/intersect:false
             * hit-testing Chart.js already uses internally for tooltips —
             * we're just reading its result instead of letting it draw a
             * canvas box with it). See this function's docstring, "V3.3
             * CHANGE: FIXED DETAIL PANEL REPLACES FLOATING TOOLTIP", for why.
             *
             * activeElements is empty when the mouse is outside the canvas
             * entirely (not just between bars — 'index' mode with
             * intersect:false still resolves to the nearest bar anywhere
             * inside the plotting area). When empty, we revert to showing
             * the default (largest-exposure) currency rather than leaving a
             * stale hover state or an empty panel.
             */
            onHover: (event, activeElements) => {
                renderChartDetailPanel(
                    activeElements && activeElements.length > 0 ? activeElements[0].index : 0
                );
            },

            plugins: {
                legend: { display: false },

                // Floating tooltip fully disabled — replaced by the fixed
                // #dashChartDetail panel via onHover above + renderChartDetailPanel().
                tooltip: { enabled: false },

                // ── DATALABELS — net notional value printed inside bars ────────
                // chartjs-plugin-datalabels is registered globally in DOMContentLoaded.
                // V3.2: reads directly from the bar's own dataset value (net
                // notional) rather than from chart._cfarValues, so the printed
                // number always matches what the bar's height visually shows.
                datalabels: {
                    /**
                     * formatter: shows net notional — the bar's own value.
                     * Returns '' (empty) to hide the label when the value is
                     * effectively zero (nothing meaningful to report for a
                     * fully-netted "flat" currency in this window).
                     *
                     * MOBILE FIX: on narrow (phone-width) charts, drop the repeated
                     * currency prefix — it's already shown once in the y-axis title
                     * above the chart, so re-printing it inside every single bar is
                     * redundant exactly when space is tightest — and switch to the
                     * same abbreviated K/M format already used for the y-axis ticks
                     * (fmtShort) instead of the fully comma-expanded number. This is
                     * what actually makes the label narrow enough to fit inside its
                     * own bar rather than overflowing past it and getting clipped by
                     * the canvas edge. Wide (laptop) charts are completely unaffected
                     * — see isNarrowChart() above for the threshold.
                     */
                    formatter: (value, ctx) => {
                        if (Math.abs(value) < 1) return '';  // hide near-zero labels
                        const amount = Math.round(Math.abs(value));
                        return isNarrowChart(ctx.chart)
                            ? fmtShort(amount)
                            : `${baseCcy} ${fmtNum(amount)}`;
                    },

                    /**
                     * anchor: determines which edge of the bar the label attaches to.
                     *   'center' → label at the vertical midpoint of the bar (tall bars)
                     *   'end'    → label at the tip of the bar (short or zero bars)
                     * Threshold of 40px is empirically chosen for 11px font + 8px padding.
                     */
                    anchor: (ctx) => {
                        try {
                            const el  = ctx.chart.getDatasetMeta(ctx.datasetIndex).data[ctx.dataIndex];
                            const h   = Math.abs((el?.base ?? 0) - (el?.y ?? 0));
                            return h < 40 ? 'end' : 'center';
                        } catch (_) { return 'center'; }
                    },

                    /**
                     * align: which side of the anchor point the label appears on.
                     *   'center' → centred on the anchor (inside the bar)
                     *   'top'    → above the anchor (outside positive short bars)
                     *   'bottom' → below the anchor (outside negative short bars)
                     */
                    align: (ctx) => {
                        try {
                            const el  = ctx.chart.getDatasetMeta(ctx.datasetIndex).data[ctx.dataIndex];
                            const h   = Math.abs((el?.base ?? 0) - (el?.y ?? 0));
                            const val = ctx.dataset.data[ctx.dataIndex];
                            if (h < 40) return val >= 0 ? 'top' : 'bottom';  // short bar: outside
                            return 'center';                                 // tall bar: inside centre
                        } catch (_) { return 'center'; }
                    },

                    // White text for every label — the "zero-notional blue label"
                    // case from V3.1 no longer applies, since a zero-value bar now
                    // simply has no label at all (see `display` below) rather than
                    // a floating CFaR figure that needed a distinguishing colour.
                    color: 'rgba(255, 255, 255, 0.92)',

                    /**
                     * font: 11px on charts with room to spare. Drops to 9px on narrow
                     * (phone-width) charts so the shorter compact labels from the
                     * formatter above have an even easier time fitting inside their
                     * bar. See isNarrowChart() for the shared width threshold.
                     */
                    font: (ctx) => ({
                        family: "'DM Mono', monospace",
                        size:   isNarrowChart(ctx.chart) ? 9 : 11,
                        weight: '500',
                    }),
                    padding: { top: 4, bottom: 4, left: 6, right: 6 },

                    // hide label entirely if net notional rounds to zero
                    display: (ctx) => Math.abs(ctx.dataset.data[ctx.dataIndex] ?? 0) >= 1,

                    // prevent labels from overflowing outside the chart canvas
                    clamp: true,
                },
            },

            // ── SCALES ───────────────────────────────────────────────────────
            scales: {
                x: {
                    grid:  { color: '#252a33' },
                    ticks: { color: '#6b7280', font: { family: "'DM Mono'", size: 12 } },
                },
                y: {
                    grid:  { color: '#252a33' },
                    ticks: {
                        color:    '#6b7280',
                        font:     { family: "'DM Mono'", size: 11 },
                        callback: v => `${baseCcy} ${fmtShort(v)}`,
                    },
                    title: {
                        display: true,
                        text:    `Net Exposure (${baseCcy})`,
                        color:   '#6b7280',
                        font:    { family: "'DM Mono'", size: 11 },
                    },
                },
            },
        },
    });

    // Store the current Component CFaR values AND the raw currencies array
    // on the chart instance. _cfarValues is read by renderChartDetailPanel()
    // (V3.3 — previously read by the floating tooltip's afterBody callback,
    // now removed) so a hovered/default currency always shows the live
    // simulated value rather than the static pre-computed one. _currencies
    // gives renderChartDetailPanel() access to the static fields (spot
    // rate, vol, horizon, direction) that never change with simulation.
    // updateChartSimulation() refreshes _cfarValues (not _currencies, which
    // is static per period) on every slider tick.
    //
    // Note: unlike V3.1, no forced chart.update('none') call is needed here.
    // The datalabels plugin now reads its formatter/display values directly
    // from the dataset passed into `new Chart(...)` above, which is already
    // available during Chart.js's synchronous initial render — there is no
    // longer a dependency on data set *after* construction completes.
    chart._cfarValues = [...cfarValues];
    chart._currencies = currencies;

    renderPeriodInfoStrip(period);
    renderCfarBarsForPeriod(periodKey);

    // Populate the Chart Detail Panel immediately with the largest-exposure
    // currency (index 0 — currencies arrive pre-sorted by |net_notional_base|
    // descending from dashboard_engine.py), so it's never empty on first
    // render and the user doesn't have to hover before seeing anything.
    renderChartDetailPanel(0);
}


// ============================================================
// CHART DETAIL PANEL  (V3.3 — replaces the floating Chart.js tooltip)
// ============================================================

/**
 * Renders the fixed-position hover-detail panel for the Net Exposure chart,
 * into the #dashChartDetail container (a plain <div> below the chart's
 * legend, NOT a canvas-anchored floating tooltip — see renderChartForPeriod's
 * docstring, "V3.3 CHANGE: FIXED DETAIL PANEL REPLACES FLOATING TOOLTIP",
 * for the bug this fixes and why a fixed DOM element is the structural fix
 * rather than a cosmetic one).
 *
 * Called from three places:
 *   1. renderChartForPeriod() — once, with idx=0, immediately after building
 *      a new chart, so the panel is never empty on first render.
 *   2. The chart's onHover callback — with whichever bar's index the mouse
 *      is currently closest to (or 0 when the mouse leaves the canvas).
 *   3. updateChartSimulation() — with the currently-displayed index, so the
 *      panel's Component CFaR / Net Exposure figures stay live while a
 *      slider is being dragged, even if the mouse isn't currently hovering
 *      the chart at all (e.g. the user is looking at the panel while
 *      dragging a slider with their other hand/a touch device).
 *
 * @param {number} idx — index into chart._currencies / the active dataset
 */
function renderChartDetailPanel(idx) {
    const panel = document.getElementById('dashChartDetail');
    if (!panel || !chart || !chart._currencies || chart._currencies.length === 0) return;

    // Clamp defensively. Chart.js's onHover always reports a valid index for
    // the active dataset, but this guards against a stale callback firing
    // mid-rebuild (e.g. a slider event landing between chart.destroy() and
    // the next new Chart(...) call), where chart._currencies could already
    // belong to a different, shorter list than the index was computed for.
    const safeIdx = Math.max(0, Math.min(idx, chart._currencies.length - 1));

    // Remembered so updateChartSimulation() can re-render the SAME currency
    // the panel is currently showing (whatever the user last hovered, or the
    // index-0 default) with fresh simulated values on every slider tick,
    // without needing to re-detect what's under the mouse.
    chart._activeDetailIndex = safeIdx;

    const c       = chart._currencies[safeIdx];
    const baseCcy = dashboardData.base_ccy;

    // Read CURRENT simulated values from the chart's own live dataset/cache
    // rather than the static c.net_notional_base / c.cfar fields, so the
    // panel reflects slider state exactly like the bars do.
    const netNotional = chart.data.datasets[0].data[safeIdx];
    const cfarVal      = chart._cfarValues?.[safeIdx] ?? c.cfar;

    // Cross-horizon case note. This is now the ONE place this explanation
    // lives — it used to also be spelled out in full inside the Component
    // CFaR section's header tooltip below, but that made that tooltip long
    // and generic (the same wall of text regardless of whether anything in
    // the current view actually exhibits this case). Moving the full
    // explanation here instead makes it CONTEXTUAL: it only appears
    // attached to a currency that is actually, currently exhibiting zero
    // notional with non-zero CFaR — see _cfarSection's header tooltip in
    // calculator.html for the shorter, general explanation of what
    // Component CFaR means that remains there.
    //
    // Uses the same .dash-tooltip-icon / .tip-inline mechanism as every
    // other "ⓘ" in this app — a plain CSS :hover::after popup reading the
    // data-tip attribute directly off the DOM. This is NOT the Chart.js
    // canvas-anchored floating tooltip that was removed (see
    // renderChartForPeriod's "V3.3 CHANGE" docstring section) — it has no
    // dependency on chart hover/index detection or canvas geometry at all,
    // so it cannot reproduce that bug. It works correctly here even though
    // this whole panel's innerHTML is rebuilt on every hover, because the
    // underlying CSS rule matches on the data-tip attribute at hover-time,
    // not on any JS-side wiring done per element.
    const isZeroNotional = Math.abs(c.net_notional_base) < 100;
    const crossHorizonNote = (isZeroNotional && Math.abs(cfarVal) > 0)
        ? `<div class="cdp-note">Zero net notional — see Component CFaR bars below
             <span class="dash-tooltip-icon tip-inline" data-tip="This currency shows zero or near-zero net notional above but still carries Component CFaR, because its positions settle at different dates — for example, a receivable at T=43 days and a payable at T=100 days cancel in notional but not in risk, since the payable runs 57 extra days uncovered after the receivable settles (the min(Tᵢ,Tⱼ) covariance formula captures this: the cross-term between the two positions uses min(43,100)=43, the same value as the receivable's own variance term, so the receivable's marginal contribution collapses to zero while the payable's does not). The bars below show this risk on its own scale, separately from net notional.">ⓘ</span>
           </div>`
        : '';
    const simNote = (deltaSpot !== 0 || deltaVol !== 0)
        ? '<div class="cdp-note cdp-note-sim">— simulation active —</div>'
        : '';

    // Label : value pairs, in the same order and with the same content the
    // old floating tooltip showed — feature parity, just relocated to a
    // fixed, always-on-screen element instead of a cursor-anchored box.
    panel.innerHTML = `
        <div class="cdp-header">${c.currency}</div>
        <div class="cdp-grid">
          <div class="cdp-label">Net Exposure</div>
          <div class="cdp-value">${baseCcy} ${fmtNum(netNotional)}</div>
          <div class="cdp-label">Component CFaR</div>
          <div class="cdp-value">${baseCcy} ${fmtNum(cfarVal)}</div>
          <div class="cdp-label">Direction</div>
          <div class="cdp-value">${c.net_direction.toUpperCase()}</div>
          <div class="cdp-label">Spot rate</div>
          <div class="cdp-value">${c.spot_rate.toFixed(4)} ${baseCcy}/1 ${c.currency}</div>
          <div class="cdp-label">Ann. vol</div>
          <div class="cdp-value">${c.annualised_vol_pct.toFixed(2)}%</div>
          <div class="cdp-label">Eff. horizon</div>
          <div class="cdp-value">${c.effective_T.toFixed(0)} trading days</div>
        </div>
        ${crossHorizonNote}
        ${simNote}
    `;
}


// ============================================================
// PERIOD INFO STRIP  (V3 — replaces renderBucketInfoStrip)
// ============================================================

/**
 * Updates the info strip below the chart for the selected period.
 *
 * Shows three values:
 *   1. Period VaR — exact consolidated VaR for positions within this time window.
 *      This is the pre-computed value from the engine (not updated by simulation).
 *      The engine uses the same min(Ti,Tj) covariance method as consolidated_var.
 *   2. Positions Included — number of individual positions (cash + forwards) that
 *      settle within this cumulative window, giving context on how broad the view is.
 *   3. Max Horizon — the upper T bound for positions included in this window.
 *      'All horizons' for the 'all' period.
 *
 * During simulation, the Period VaR cell is updated to the sum of simulated
 * per-currency component CFaRs (an approximation — the exact period VaR would
 * require recomputing the full covariance matrix, which is server-side only).
 *
 * @param {Object} period — active period dict from dashboardData.cumulative_periods
 */
function renderPeriodInfoStrip(period) {
    const base = dashboardData ? dashboardData.base_ccy : '';

    // Period VaR: exact pre-computed consolidated VaR for this window
    document.getElementById('dashBiCfar').textContent =
        `${base} ${fmtNum(period ? period.period_var : 0)}`;

    // Positions included: how many individual positions fall in this window
    document.getElementById('dashBiDivers').textContent =
        period ? String(period.n_positions) : '0';

    // Max horizon: the T cutoff for this window
    document.getElementById('dashBiT').textContent = period
        ? (period.max_days ? `≤ ${period.max_days} trading days` : 'All horizons')
        : '—';
}


// ============================================================
// COMPONENT CFaR BARS  (V3.2 — separate from the notional chart;
//                       V3.7 — locked order/scale, in-place updates)
// ============================================================

/**
 * Computes a Component CFaR bar's width percentage relative to a given
 * scaling denominator. Shared by renderCfarBarsForPeriod() (establishing
 * the initial, locked baseline) and updateCfarBarsInPlace() (every
 * subsequent slider tick), so the exact same floor/ceiling rules apply
 * whether a bar is being built fresh or just resized.
 *
 * A small minimum width (2%) is applied to any non-trivial (≥1 unit) CFaR
 * so it remains visibly present as a sliver even when dwarfed by the
 * largest bar in the period, rather than rendering as an invisible
 * zero-width track. A maximum of 100% is also applied — see
 * updateCfarBarsInPlace()'s docstring, "WHY A FIXED DENOMINATOR", for why
 * a simulated value can legitimately want to exceed the locked baseline's
 * scale, and why clamping (rather than letting the bar overflow its track)
 * is the simplest safe handling for that case.
 *
 * @param {number} cfar       — this currency's (possibly simulated) Component CFaR
 * @param {number} maxAbsCfar — the scaling denominator (locked or fresh)
 * @returns {number} width percentage, 0–100
 */
function _cfarBarWidthPct(cfar, maxAbsCfar) {
    const absCfar = Math.abs(cfar);
    if (absCfar < 1) return 0;
    return Math.min(Math.max((absCfar / maxAbsCfar) * 100, 2), 100);
}

/**
 * Renders the Component CFaR horizontal bar section beneath the notional
 * chart, inside the #dashCfarBars container, AND establishes the locked
 * baseline (display order + scaling denominator + DOM element references)
 * that updateCfarBarsInPlace() will reuse for every subsequent slider tick
 * — see that function's docstring for why this two-function split exists.
 *
 * Called only at the start of a period's lifetime: from
 * renderChartForPeriod(), which itself only runs on a fresh Calculate
 * response or a period-dropdown change — both of which reset the Risk
 * Dashboard's sliders to 0 first (see renderDashboard() and
 * handlePeriodChange()). So the baseline locked here is always the true
 * Δ=0 state, exactly matching the intent: bars are ordered top-to-bottom
 * once, when you first see a period with no simulation applied yet, and
 * stay in that order while you experiment with the sliders afterwards.
 *
 * === WHY A SEPARATE BAR SYSTEM (see module docstring for full rationale) ===
 *
 * Bar LENGTH here = Component CFaR, on its OWN scale — completely
 * independent of the notional chart's scale above. This directly answers
 * "what can I lose?" without being entangled with "where is my money?"
 * (the notional chart's question). A currency can show a small or zero bar
 * in the notional chart above but the LONGEST bar here, when it carries
 * disproportionate risk relative to its position size (e.g. an exotic,
 * high-volatility currency, or residual risk from same-currency positions
 * that cancel in notional but settle at different dates under the
 * min(Tᵢ,Tⱼ) covariance formula). That is the entire point of this
 * section — to make that case visible and self-explanatory rather than
 * a confusing label inside an unrelated bar.
 *
 * @param {string} periodKey — e.g. '3m'; looked up fresh from dashboardData
 *                              each call rather than passed pre-resolved,
 *                              so this function is safe to call independently
 *                              of renderChartForPeriod's own period lookup.
 */
function renderCfarBarsForPeriod(periodKey) {
    const container = document.getElementById('dashCfarBars');
    if (!container || !dashboardData) return;

    const period = dashboardData.cumulative_periods.find(p => p.key === periodKey);

    if (!period || period.currencies.length === 0) {
        container.innerHTML =
            '<div class="cfar-bars-empty">No exposure data for this time period.</div>';
        // Nothing to lock for an empty period — and explicitly clearing any
        // PREVIOUS period's lock here matters: without this, a stray
        // updateCfarBarsInPlace() call (e.g. a slider 'input' event firing
        // while data briefly transitions between periods) would otherwise
        // find a stale lock from a different, no-longer-active period and
        // try to update DOM elements that this innerHTML write just
        // destroyed — the periodKey guard in updateCfarBarsInPlace() would
        // catch that too, but clearing the lock here is the more direct,
        // unambiguous fix at the source.
        cfarBarsState = null;
        return;
    }

    const baseCcy = dashboardData.base_ccy;

    // Apply current Risk Dashboard simulation deltas. In practice this is
    // always Δ=0 here (see this function's docstring) — applySimulation()
    // is still used rather than reading c.cfar directly, so this function
    // remains correct even if ever called again with non-zero deltas
    // already active, and so the lock-time values are computed by the
    // exact same code path as every later in-place update.
    const rows = period.currencies.map(c => {
        const sim = applySimulation(c, deltaSpot, deltaVol, activeSpotCcy);
        return { currency: c.currency, direction: c.net_direction, cfar: sim.cfar };
    });

    // Sort by risk magnitude descending — the biggest threat renders first,
    // independent of the notional chart's own currency ordering above.
    // This sort runs ONCE, here, at lock time. It is intentionally NOT
    // repeated by updateCfarBarsInPlace() — see that function's docstring
    // for why re-sorting on every slider tick was a problem worth fixing.
    rows.sort((a, b) => Math.abs(b.cfar) - Math.abs(a.cfar));

    // Scale every bar relative to the largest CFaR in this period. The 0.01
    // floor guards against a divide-by-zero when every currency is fully
    // hedged (all component CFaRs are zero) within the selected window.
    // This denominator is also LOCKED here — see updateCfarBarsInPlace()'s
    // docstring, "WHY A FIXED DENOMINATOR", for why it stays fixed rather
    // than being recomputed from live simulated values on every tick.
    const maxAbsCfar = Math.max(...rows.map(r => Math.abs(r.cfar)), 0.01);

    // data-ccy on each row lets updateCfarBarsInPlace() (and the element-
    // capture step right below) reliably re-locate a specific currency's
    // row after innerHTML parsing, via an explicit attribute match rather
    // than relying on array/DOM ordering staying in lockstep.
    container.innerHTML = rows.map(r => {
        const pct      = _cfarBarWidthPct(r.cfar, maxAbsCfar);
        const dirClass = r.direction;  // 'long' | 'short' | 'flat' — matches bar/legend colours above
        return `
          <div class="cfar-bar-row" data-ccy="${r.currency}">
            <div class="cfar-bar-label">
              <span class="cfar-bar-ccy">${r.currency}</span>
            </div>
            <div class="cfar-bar-track">
              <div class="cfar-bar-fill ${dirClass}" style="width:${pct}%"></div>
            </div>
            <div class="cfar-bar-value">${baseCcy} ${fmtNum(r.cfar)}</div>
          </div>`;
    }).join('');

    // Capture direct references to each row's mutable elements (the fill
    // bar and the value text) — this is what lets updateCfarBarsInPlace()
    // mutate them directly on every slider tick instead of rebuilding
    // innerHTML each time. See that function's docstring for why this
    // matters for both animation (CSS transitions only animate an EXISTING
    // element's property change) and performance (no full DOM rebuild on
    // every 'input' event during a drag).
    const elements = {};
    rows.forEach(r => {
        // Currency codes are always simple 3-letter uppercase ISO codes from
        // the backend (e.g. "USD", "MYR") — never arbitrary or user-controlled
        // strings — so they're safe to interpolate directly into this attribute
        // selector without CSS.escape(). (Deliberately not using CSS.escape()
        // here: it adds a dependency on a Web API absent from some lightweight
        // test/SSR environments, for a risk — special characters in a currency
        // code — that cannot occur given where this data comes from.)
        const rowEl = container.querySelector(`.cfar-bar-row[data-ccy="${r.currency}"]`);
        if (!rowEl) return;  // defensive — should not happen, every row was just created above
        elements[r.currency] = {
            fill:  rowEl.querySelector('.cfar-bar-fill'),
            value: rowEl.querySelector('.cfar-bar-value'),
        };
    });

    cfarBarsState = { periodKey, order: rows.map(r => r.currency), maxAbsCfar, elements };
}

/**
 * Updates the Component CFaR bars' widths and values IN PLACE for the
 * current slider state — mutating each row's existing DOM elements
 * directly, rather than calling renderCfarBarsForPeriod() again (which
 * would rebuild the whole section from scratch on every single slider
 * 'input' event). Called from updateChartSimulation() on every Risk
 * Dashboard slider tick.
 *
 * === THE PROBLEM THIS FIXES ===
 *
 * Before V3.7, every slider tick called renderCfarBarsForPeriod() again,
 * which did two things that made the bars feel chaotic for even a tiny
 * slider move:
 *
 *   1. RE-SORTED on every tick. Two currencies close in |Component CFaR|
 *      could swap visual rank from a fractional-percent input change,
 *      making the whole list appear to reshuffle for what was actually a
 *      small, real change underneath.
 *   2. REBUILT THE ENTIRE innerHTML on every tick. The CSS rule
 *      `transition: width 0.4s ease` on .cfar-bar-fill (see dashboard.css)
 *      was effectively dead code in this situation — a CSS transition only
 *      animates when an EXISTING element's property changes; destroying
 *      and recreating every element on every tick gives the browser
 *      nothing to animate FROM, so bars visually snapped to their new
 *      widths instead of sliding smoothly.
 *
 * V3.7 fixes both: this function uses the LOCKED order and LOCKED scaling
 * denominator from cfarBarsState (established once by
 * renderCfarBarsForPeriod() at Δ=0 — see that function's docstring) and
 * only ever touches each row's OWN width/value, via direct property
 * mutation on the SAME DOM nodes captured at lock time. Bars never
 * reorder or rescale against each other while a slider is being dragged;
 * each one just smoothly tracks its own current value against a fixed
 * ruler, and the CSS transition now has something real to animate between.
 *
 * === WHY A FIXED DENOMINATOR (not recomputed live) ===
 *
 * If maxAbsCfar were recomputed from the CURRENT simulated values on every
 * tick (as it was before V3.7), then even a currency whose own CFaR barely
 * moved could visibly grow or shrink — because the bar it's being measured
 * against changed size too. Locking the denominator at the baseline means
 * every bar's width purely reflects how THAT currency's own CFaR is moving
 * relative to a FIXED reference — which is what actually produces a
 * "continuously chasing from baseline" feel rather than the entire ruler
 * being redrawn under everything else at the same time.
 *
 * One consequence: if a currency's simulated CFaR grows large enough under
 * stress to exceed the locked baseline's max, its bar's proportional width
 * would technically want to exceed 100%. _cfarBarWidthPct() clamps at 100%
 * rather than letting it overflow the track visually — simple and safe;
 * the printed value text is never clamped, so the exact number is always
 * still readable even if the bar itself is visually maxed out.
 *
 * No-ops safely if cfarBarsState is null (e.g. the active period has no
 * currencies) or belongs to a different period than the one currently
 * active (a defensive guard against a stale call landing right after a
 * period switch, before the new period's renderCfarBarsForPeriod() call
 * has re-established the lock).
 */
function updateCfarBarsInPlace() {
    if (!cfarBarsState || cfarBarsState.periodKey !== activePeriodKey || !dashboardData) return;

    const period = dashboardData.cumulative_periods.find(p => p.key === activePeriodKey);
    if (!period) return;

    const baseCcy = dashboardData.base_ccy;

    cfarBarsState.order.forEach(ccy => {
        const refs = cfarBarsState.elements[ccy];
        const c    = period.currencies.find(x => x.currency === ccy);
        if (!refs || !c) return;  // defensive — should not happen in practice

        const sim = applySimulation(c, deltaSpot, deltaVol, activeSpotCcy);
        const pct = _cfarBarWidthPct(sim.cfar, cfarBarsState.maxAbsCfar);

        refs.fill.style.width  = `${pct}%`;
        refs.value.textContent = `${baseCcy} ${fmtNum(sim.cfar)}`;
    });
}


// ============================================================
// SIMULATION
// ============================================================

/**
 * Applies current simulation deltas to a single currency entry.
 *
 * Spot shift (selected currency only):
 *   new_net_notional = net_notional_base × (1 + Δ_spot)
 *   new_cfar         = cfar × (1 + Δ_spot)
 *   Exact ONLY if this currency has no correlation with anything else in
 *   the portfolio. In a realistic portfolio it's an approximation, but a
 *   smoothly continuous one — see "SPOT SHIFT REMAINS AN APPROXIMATION"
 *   below for the full picture (this was previously overstated as simply
 *   "exact" here; corrected after empirical testing).
 *
 * Vol shift (V3.6 — ONE exact formula for every currency, any direction):
 *   new_cfar = (1 + Δ_vol) × vol_part − drift_part
 *
 * === V3.6: EXACT VOL SLIDER — REPLACES THE V3.0–V3.5 APPROXIMATIONS ===
 *
 * vol_part and drift_part arrive from exposure_engine.py's
 * _compute_component_vars_by_currency(), computed from the SAME per-position
 * vol_component/drift_contribution arrays that produce the exact static
 * `cfar` itself — NOT from net_notional_base, and NOT from an
 * exposure-weighted single-T approximation. By construction,
 * vol_part − drift_part = cfar EXACTLY at Δ_vol = 0 (same formula, evaluated
 * at multiplier 1) — so there is no discontinuity at rest, for any currency,
 * including a currency with zero net notional but real Component CFaR (the
 * cross-horizon residual case — see renderChartDetailPanel()'s tooltip).
 *
 * It is also EXACT while sliding, not just continuous at the boundary. Under
 * a uniform vol-regime shift (every currency's σ scaled by the same factor
 * k = 1+Δ_vol — exactly what this slider does), vol_part scales EXACTLY
 * linearly by k: this is a provable identity, not an approximation,
 * following from the covariance matrix being homogeneous of degree 2 in σ.
 * See exposure_engine.py's _compute_component_vars_by_currency() docstring
 * for the full derivation. Verified empirically against a full engine
 * re-run at a stressed vol level: predicted and actual period VaRs matched
 * to within floating-point rounding.
 *
 * This single formula replaces ALL of the old branching logic:
 *   - No more separate long/short formulas (the old
 *     `vol_term*(1+Δv) ∓ mu_term` sign-flip hack — vol_part/drift_part
 *     already carry the correct sign per-position, summed correctly
 *     regardless of mixed directions within a currency).
 *   - No more flat-currency special case (the V3.5 interim patch — a flat
 *     currency's vol_part/drift_part are computed from its real positions,
 *     not from net_notional_base, so they are non-zero whenever the
 *     currency's real Component CFaR is non-zero).
 *
 * NO FLOOR AT ZERO: previous versions applied Math.max(result, 0) to every
 * branch. This was actually a hidden SOURCE of discontinuity, not a safety
 * net — the static ccy.cfar itself is never floored server-side (a
 * currency that hedges the rest of the portfolio can have a genuinely
 * negative component_var — see exposure_engine.py's docstring, "Negative
 * component VaR"), so flooring only the simulated path created a mismatch
 * between Δ_vol = 0 (can show negative) and Δ_vol ≠ 0 (was forced to ≥ 0)
 * right at the boundary. Removed here for both the vol shift AND the spot
 * shift (Step 2 below) so the simulated value is continuous with the exact
 * static value in BOTH sign and magnitude, not just magnitude.
 *
 * === SPOT SHIFT REMAINS AN APPROXIMATION — but a SMOOTH one (unchanged by V3.6) ===
 *
 * The spot slider only shifts ONE currency's exposure. Unlike the uniform
 * vol shift above, this does NOT have a clean exact-linear-scaling
 * identity, for two reasons, both confirmed by testing against a real
 * engine re-run at a +5% spot shift on a correlated currency:
 *
 *   1. OTHER currencies are left completely frozen by `cfar × (1+Δ_spot)`
 *      applying only to the selected currency — but in reality, shifting
 *      one currency's exposure changes its cross-covariance contribution
 *      to every other currency's component too (a real re-run showed two
 *      unrelated currencies move by roughly −4.5% and −3.0% respectively
 *      from a single MYR-only spot shift, purely via that cross-currency
 *      effect — the simulation shows 0% movement for them instead).
 *   2. Even the SELECTED currency's own `cfar × (1+Δ_spot)` is only exact
 *      if that currency has zero correlation with the rest of the
 *      portfolio. With realistic correlation present, the same test showed
 *      about a 0.8% gap between the formula's prediction and the true
 *      re-run value at a 5% shift — small, but not zero, contrary to what
 *      this function's docstring previously (incorrectly) claimed.
 *
 * IMPORTANT — this is a SMOOTH, CONTINUOUS approximation, not a buggy one:
 * unlike the pre-V3.6 vol_term/mu_term formula (which switched to a
 * structurally different calculation the instant Δ_vol left exactly 0,
 * producing real discontinuities — jumps of hundreds of percent from a
 * vol delta of a few thousandths of a percent), `cfar × (1+Δ_spot)` is the
 * SAME simple multiplicative formula across the ENTIRE slider range,
 * including Δ_spot = 0. There is no boundary-switching effect here at all.
 * Tested with shifts as small as ±0.01% around zero (mirroring the
 * vol-slider continuity test): both the real engine values AND this
 * formula's predictions move smoothly and proportionally, with no jump
 * anywhere. The error described above grows gradually and predictably as
 * |Δ_spot| increases — it does not appear or worsen suddenly near rest.
 * This is a disclosed, by-design PoC tradeoff (see README's Known
 * Limitations), not something requiring a fix the way the vol slider did.
 *
 * @param {Object} ccy     — currency entry (from cumulative_periods; must
 *                            include cfar, vol_part, drift_part,
 *                            net_notional_base, net_direction, currency)
 * @param {number} dSpot   — spot delta for the selected currency (−0.10 to +0.10)
 * @param {number} dVol    — vol delta applied to ALL currencies (−0.25 to +0.25)
 * @param {string} spotCcy — which currency the spot slider currently targets
 * @returns {{ netNotional: number, cfar: number }}
 */
function applySimulation(ccy, dSpot, dVol, spotCcy) {
    let netNotional = ccy.net_notional_base;
    let cfar;

    // Step 1: vol shift (applies to all currencies simultaneously).
    // ONE exact formula for every currency — see this function's docstring,
    // "V3.6: EXACT VOL SLIDER", for the full derivation and proof.
    if (dVol !== 0) {
        cfar = (1 + dVol) * ccy.vol_part - ccy.drift_part;
    } else {
        // No vol shift — use the pre-computed exact CFaR from the engine.
        // (Equivalent to the formula above evaluated at dVol=0, by
        // construction — using ccy.cfar directly here is simply more
        // direct than recomputing 1×vol_part − drift_part for the same
        // result, and avoids reintroducing floating-point rounding noise
        // at the one delta value users will see most often: rest.)
        cfar = ccy.cfar;
    }

    // Step 2: spot shift (applies ONLY to the currently selected currency).
    // A smooth, continuous approximation — not exact in a correlated
    // portfolio (this currency's own scaling is only first-order correct,
    // and every other currency is left frozen rather than reflecting the
    // small cross-covariance effect a real spot shift would cause). See
    // this function's docstring, "SPOT SHIFT REMAINS AN APPROXIMATION —
    // but a SMOOTH one", for the empirically-measured error size and why
    // this is a disclosed tradeoff rather than a bug.
    if (spotCcy && ccy.currency === spotCcy && dSpot !== 0) {
        netNotional = ccy.net_notional_base * (1 + dSpot);
        cfar        = cfar * (1 + dSpot);
    }

    return { netNotional, cfar };
}


/**
 * Re-renders the chart data with current simulation deltas, without
 * recreating the Chart.js instance (uses chart.update('none') for
 * smooth performance, skipping animation). Also updates the Component
 * CFaR bar section below the chart IN PLACE (V3.7 — fixed order/scale,
 * smooth CSS transition; see updateCfarBarsInPlace()), refreshes the
 * Chart Detail Panel (V3.3) with live values for whichever currency it's
 * currently showing, and updates the Period VaR strip to show the sum of
 * simulated component CFaRs — EXACT for a pure vol-regime shift as of
 * V3.6 (see applySimulation()'s docstring), still an approximation if the
 * spot slider is also active.
 *
 * Note: this function drives ONLY the Risk Dashboard's chart, CFaR bars,
 * detail panel, and Period VaR strip — it has no effect on the Consolidated
 * VaR card's independent Portfolio Scenario sliders/figure (see
 * updatePortfolioSimulation()).
 */
function updateChartSimulation() {
    if (!chart || !dashboardData) return;

    const period = dashboardData.cumulative_periods.find(p => p.key === activePeriodKey);
    if (!period) return;

    const simulated = period.currencies.map(c =>
        applySimulation(c, deltaSpot, deltaVol, activeSpotCcy)
    );

    // Push simulated net notional values into the single bar dataset.
    // The datalabels formatter (configured in renderChartForPeriod) reads
    // this same dataset directly, so updating it here also refreshes the
    // inside-bar net notional labels automatically — no separate label
    // array needs to be kept in sync for that purpose.
    chart.data.datasets[0].data = simulated.map(s => s.netNotional);

    // Update the CFaR values stored on the chart instance — read by
    // renderChartDetailPanel() below (V3.3 — previously read by the
    // floating tooltip's afterBody callback, now removed).
    chart._cfarValues = simulated.map(s => s.cfar);

    chart.update('none');   // 'none' = no animation, for a responsive feel

    // Keep the Component CFaR bar section below the chart in sync with the
    // same simulation deltas just applied to the notional chart above.
    // V3.7: updates each bar's width/value IN PLACE (mutating the existing
    // DOM elements captured at lock time by renderCfarBarsForPeriod()),
    // rather than rebuilding the section from scratch on every tick — this
    // is what keeps bars from re-sorting mid-drag and lets the CSS width
    // transition actually animate. See updateCfarBarsInPlace()'s docstring
    // for the full rationale.
    updateCfarBarsInPlace();

    // Keep the Chart Detail Panel showing live values too, for whichever
    // currency it's currently displaying (last hovered, or the index-0
    // default — see chart._activeDetailIndex, set by renderChartDetailPanel).
    // This matters even when the mouse isn't over the chart at all: a user
    // dragging a slider with one hand while reading the panel should see it
    // update in step with the bars, not go stale until their next hover.
    renderChartDetailPanel(chart._activeDetailIndex ?? 0);

    // Update the Period VaR strip to the sum of current component CFaRs.
    //
    // === WHY THIS IS ALWAYS CONSISTENT WITH THE BARS, AND EXACT FOR VOL-ONLY (V3.6) ===
    //
    // The Period VaR strip always shows the column total of the Component
    // CFaR bars above it (now rendered separately by renderCfarBarsForPeriod,
    // not as labels inside the notional chart — see module docstring).
    // At Δ=0: components equal the exact server-side covariance decomposition,
    //   and their sum equals period.period_var exactly (Euler decomposition theorem).
    //   This is NOT a simple independent sum — the components already encode all
    //   cross-currency correlations via (cov_T @ s)_i in their construction.
    // At Δ_vol≠0, Δ_spot=0: the sum is EXACT, not an approximation — it equals
    //   what a full server-side re-run at that vol level would produce. See
    //   exposure_engine.py's _compute_component_vars_by_currency() docstring
    //   for the proof, and applySimulation()'s docstring for the summary.
    // At Δ_spot≠0 (with or without a vol shift): the sum remains an approximation,
    //   since shifting one currency's exposure changes its cross-covariance terms
    //   with every other currency too — re-running Calculate gives the exact
    //   figure for a genuinely different spot-rate assumption.
    const simTotal = simulated.reduce((acc, s) => acc + s.cfar, 0);
    document.getElementById('dashBiCfar').textContent =
        `${dashboardData.base_ccy} ${fmtNum(simTotal)}`;
}


function handleSpotCcyChange() {
    activeSpotCcy = document.getElementById('spotCcySelect').value || null;
    updateChartSimulation();
}

function handleSpotSlider() {
    deltaSpot = parseInt(document.getElementById('spotSlider').value, 10) / 1000;
    updateSliderDisplays();
    updateChartSimulation();
}

function handleVolSlider() {
    deltaVol = parseInt(document.getElementById('volSlider').value, 10) / 1000;
    updateSliderDisplays();
    updateChartSimulation();
}

/**
 * Formats a slider delta as a signed percentage string, e.g. 0.1 → "+10.0%",
 * -0.025 → "-2.5%". Shared by both independent slider pairs (Risk Dashboard
 * and Portfolio Scenario) so the display formatting logic exists in exactly
 * one place rather than being duplicated per slider pair.
 *
 * @param {number} delta — slider delta as a decimal fraction (e.g. 0.1 = +10%)
 * @returns {string}
 */
function _formatSliderPercent(delta) {
    const pct = (delta * 100).toFixed(1);
    return delta >= 0 ? `+${pct}%` : `${pct}%`;
}

/** Refreshes the % labels displayed next to the Risk Dashboard's two sliders. */
function updateSliderDisplays() {
    document.getElementById('dashSpotValue').textContent = _formatSliderPercent(deltaSpot);
    document.getElementById('dashVolValue').textContent  = _formatSliderPercent(deltaVol);
}


/**
 * Populates the spot currency dropdown with all unique currencies that
 * appear in any cumulative period (i.e. all currencies across the full
 * portfolio). Deduplicates so each currency appears once.
 *
 * For V3, reads from cumulative_periods instead of buckets. The 'all'
 * period contains the superset of all currencies, so iterating all periods
 * and deduplicating achieves the same result.
 *
 * @param {Array} periods — data.cumulative_periods
 */
function renderSpotCurrencyDropdown(periods) {
    const seen = new Set();
    const sel  = document.getElementById('spotCcySelect');
    sel.innerHTML = '<option value="">— Select —</option>';

    if (!periods) return;

    periods.forEach(p => {
        (p.currencies || []).forEach(c => {
            if (!seen.has(c.currency)) {
                seen.add(c.currency);
                const opt = document.createElement('option');
                opt.value = opt.textContent = c.currency;
                sel.appendChild(opt);
            }
        });
    });

    // Default to first currency
    const first = seen.values().next().value;
    if (first) { sel.value = first; activeSpotCcy = first; }
}


// ============================================================
// PORTFOLIO SCENARIO  (V3.2 — independent sliders, Consolidated VaR card)
// ============================================================

/**
 * Populates the Portfolio Scenario currency dropdown (inside the
 * Consolidated Portfolio VaR card) from the 'all' cumulative period, which
 * by construction already contains every currency in the full portfolio
 * (cash + every forward, across every bucket) — no deduplication across
 * multiple periods is needed here, unlike renderSpotCurrencyDropdown above,
 * since a single period already is the full superset.
 *
 * @param {Array} periods — data.cumulative_periods
 */
function renderPortfolioCurrencyDropdown(periods) {
    const sel = document.getElementById('portfolioSpotCcySelect');
    if (!sel) return;
    sel.innerHTML = '<option value="">— Select —</option>';

    const allPeriod = (periods || []).find(p => p.key === 'all');
    if (!allPeriod) return;

    allPeriod.currencies.forEach(c => {
        const opt = document.createElement('option');
        opt.value = opt.textContent = c.currency;
        sel.appendChild(opt);
    });

    // Default to the first (largest, since currencies arrive pre-sorted by
    // |net_notional_base| descending from dashboard_engine.py) currency.
    const first = allPeriod.currencies[0];
    if (first) { sel.value = first.currency; portfolioActiveSpotCcy = first.currency; }
}

/** Refreshes the % labels displayed next to the Portfolio Scenario's two sliders. */
function updatePortfolioSliderDisplays() {
    document.getElementById('portfolioSpotValue').textContent = _formatSliderPercent(portfolioDeltaSpot);
    document.getElementById('portfolioVolValue').textContent  = _formatSliderPercent(portfolioDeltaVol);
}

/**
 * Recomputes the "Stressed Portfolio VaR" figure in the Consolidated
 * Portfolio VaR card from the current Portfolio Scenario slider deltas.
 *
 * === WHY THIS REUSES THE 'all' PERIOD'S DATA — NO NEW BACKEND MATH ===
 *
 * exposure_engine.py guarantees that cumulative_vars['all']['period_var']
 * equals consolidated_var['total_var'] exactly: both are computed by the
 * identical min(Tᵢ,Tⱼ) covariance method over the identical full position
 * list (see exposure_engine.py's CUMULATIVE_PERIOD_DEFINITIONS docstring
 * for the guarantee, and dashboard_engine.py's module docstring). This
 * means the 'all' period's per-currency vol_part/drift_part/cfar values —
 * already sent to the frontend for the Risk Dashboard's "All" filter
 * option — are equally valid inputs for stressing the Consolidated VaR
 * figure. No new Python computation was required to add this feature:
 * this function reuses applySimulation() completely unchanged, just with
 * a second, independent set of slider state (portfolioDeltaSpot /
 * portfolioDeltaVol / portfolioActiveSpotCcy) targeting the 'all' period.
 *
 * === ACCURACY (identical to the Period VaR strip — see applySimulation()) ===
 *
 * Each currency's Component CFaR is scaled independently and summed. At
 * Δ=0 the sum is exact and equals the Consolidated Portfolio VaR figure
 * shown statically above this panel (Euler decomposition theorem). For a
 * PURE vol-regime shift (Δ_vol≠0, Δ_spot=0), the sum is now EXACT as of
 * V3.6 — not an approximation — see applySimulation()'s docstring for the
 * proof. If the spot slider is also active, the sum remains an
 * approximation, for the same reason described there ("SPOT SHIFT REMAINS
 * AN APPROXIMATION"). This is the exact same method and the exact same
 * accuracy profile as updateChartSimulation()'s Period VaR strip update —
 * just a second, independent instance of it targeting this card instead.
 */
function updatePortfolioSimulation() {
    const elVar = document.getElementById('portfolioStressedVaR');
    if (!elVar || !dashboardData) return;

    const allPeriod = dashboardData.cumulative_periods.find(p => p.key === 'all');
    if (!allPeriod) { elVar.textContent = '—'; return; }

    const simulated = allPeriod.currencies.map(c =>
        applySimulation(c, portfolioDeltaSpot, portfolioDeltaVol, portfolioActiveSpotCcy)
    );
    const stressedTotal = simulated.reduce((acc, s) => acc + s.cfar, 0);

    elVar.textContent = `${dashboardData.base_ccy} ${fmtNum(stressedTotal)}`;
}

function handlePortfolioSpotCcyChange() {
    portfolioActiveSpotCcy = document.getElementById('portfolioSpotCcySelect').value || null;
    updatePortfolioSimulation();
}

function handlePortfolioSpotSlider() {
    portfolioDeltaSpot = parseInt(document.getElementById('portfolioSpotSlider').value, 10) / 1000;
    updatePortfolioSliderDisplays();
    updatePortfolioSimulation();
}

function handlePortfolioVolSlider() {
    portfolioDeltaVol = parseInt(document.getElementById('portfolioVolSlider').value, 10) / 1000;
    updatePortfolioSliderDisplays();
    updatePortfolioSimulation();
}


// ============================================================
// HEDGE EFFECTIVENESS TABLE  (unchanged from V2)
// ============================================================

/**
 * Renders the per-currency, per-bucket hedge effectiveness table.
 * This table uses data.buckets (the per-bucket breakdown, unchanged from V2)
 * rather than cumulative_periods. It shows the within-bucket netting benefit
 * for each individual time bucket in isolation.
 *
 * This table is static — hedge effectiveness is invariant to both slider
 * types (both numerator and denominator scale identically), so it always
 * shows the base-scenario structural hedge.
 *
 * Note on terminology: "Bucket CFaR" in this table = the net VaR for that
 * specific bucket. The "Period VaR" in the chart strip above covers all
 * buckets up to the selected time window — these are different concepts.
 *
 * @param {Array}  buckets  — data.buckets (per-bucket data from V2)
 * @param {string} baseCcy  — base currency code (e.g. 'SGD')
 */
function renderHedgeTable(buckets, baseCcy) {
    const tbody = document.getElementById('dashHedgeTableBody');
    tbody.innerHTML = '';
    let hasRows = false;

    if (!buckets) {
        _renderHedgeTableEmpty(tbody);
        return;
    }

    buckets.forEach(bucket => {
        bucket.currencies.forEach(ccy => {
            hasRows = true;
            const eff  = ccy.hedge_effectiveness_pct;
            const effW = Math.min(Math.max(eff, 0), 100);
            const dirClass = ccy.net_direction;

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>
                    <strong>${ccy.currency}</strong>
                    <span class="dash-dir-badge ${dirClass}">
                        ${ccy.net_direction.toUpperCase()}
                    </span>
                </td>
                <td style="color:var(--muted)">${bucket.bucket_label}</td>
                <td>${baseCcy} ${fmtNum(ccy.net_notional_base)}</td>
                <td style="color:var(--info)">${baseCcy} ${fmtNum(ccy.cfar)}</td>
                <td style="color:var(--muted)">${baseCcy} ${fmtNum(ccy.gross_cfar)}</td>
                <td style="color:var(--hedge)">${baseCcy} ${fmtNum(ccy.hedge_benefit)}</td>
                <td>
                    <div class="dash-eff-wrap">
                        <div class="dash-eff-bg">
                            <div class="dash-eff-fill" style="width:${effW}%"></div>
                        </div>
                        <span style="min-width:42px;font-variant-numeric:tabular-nums">
                            ${eff.toFixed(1)}%
                        </span>
                    </div>
                </td>`;
            tbody.appendChild(tr);
        });
    });

    if (!hasRows) _renderHedgeTableEmpty(tbody);
}

/** Helper: inserts an "empty" placeholder row into the hedge table. */
function _renderHedgeTableEmpty(tbody) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="7"
        style="text-align:center;color:var(--muted);padding:20px">
        No exposure data to display.
    </td>`;
    tbody.appendChild(tr);
}


// ============================================================
// FORMATTING UTILITIES
// ============================================================

/**
 * Formats a number with comma separators, 0 decimal places.
 *
 * NEGATIVE-ZERO FIX: Math.round() on a tiny negative float (e.g. -0.0003,
 * the kind of floating-point noise that survives Python's round(x, 2) as
 * -0.0 and travels through JSON as -0) returns JS's negative-zero value.
 * (-0).toLocaleString('en-US') renders the literal string "-0" — this is
 * what caused values that are mathematically zero to display as "SGD -0"
 * in the UI. The `=== 0` check below catches BOTH +0 and -0 (JS treats
 * them as equal under ==/===, unlike Object.is), so replacing the rounded
 * value with a literal `0` before formatting normalises every such case.
 * This is a formatter-level fix, so it protects every number that flows
 * through fmtNum() — not just the one field that previously triggered this
 * (hedge_benefit) — including any new field a future developer adds here.
 */
function fmtNum(n) {
    if (n === undefined || n === null || isNaN(n)) return '—';
    const rounded = Math.round(n);
    return (rounded === 0 ? 0 : rounded).toLocaleString('en-US');
}

/**
 * Compact alias for fmtNum (used inside stat card innerHTML).
 * Same behaviour — exists for readability at the call site.
 */
function fmt(n) {
    return fmtNum(n);
}

/**
 * Compact axis tick format: 1500000 → "1.5M", 25000 → "25K".
 * Same negative-zero guard as fmtNum() — see that function's docstring.
 * Without this, a tiny negative value below the K/M thresholds would fall
 * through to `v.toFixed(0)`, and (-0.4).toFixed(0) renders as "-0".
 */
function fmtShort(v) {
    const a = Math.abs(v);
    if (a >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
    if (a >= 1_000)     return (v / 1_000).toFixed(0) + 'K';
    if (Math.abs(v) < 0.5) v = 0;   // normalise -0 / sub-unit noise before toFixed(0)
    return v.toFixed(0);
}

/**
 * isNarrowChart(chart) — true when the Net Exposure chart's current
 * rendered width is too narrow for the inside-bar net notional labels
 * to comfortably show their full "SGD 21,584"-style text without
 * overflowing past their own bar's column.
 *
 * Used by the datalabels `formatter` and `font` callbacks in
 * renderChartForPeriod() (see the chart config below) to switch to a
 * compact label style — smaller font, abbreviated K/M number, no
 * repeated currency prefix — on phone-width charts, while wider charts
 * (laptop, tablet) keep showing the exact same full-precision labels as
 * before. Chart.js re-evaluates context callbacks like these on every
 * render, and the chart is `responsive: true`, so this is re-checked
 * automatically on window resize / phone rotation, not just at load.
 *
 * 500px is an empirical threshold: with 5 currency bars sharing the
 * chart's width, anything narrower than that leaves each bar's column
 * too little room for an 11px-font "SGD 21,584"-style label to fit.
 */
function isNarrowChart(chart) {
    return (chart?.width ?? Infinity) < 500;
}
