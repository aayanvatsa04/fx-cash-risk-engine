/**
 * dashboard.js — Risk Dashboard Rendering Layer (V3.2)
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
 * the 'all' period's per-currency vol_term/mu_term/cfar values — already
 * sent to the frontend for the Risk Dashboard's "All" filter option — are
 * equally valid inputs for stressing the Consolidated VaR figure. See
 * updatePortfolioSimulation(), which reuses applySimulation() unchanged.
 *
 * === WHAT THIS FILE DOES NOT DO ===
 *
 * - No form handling (that's in calculator.html's inline script)
 * - No fetch calls (the calculator JS fetches; this just consumes the result)
 * - No VaR formulas — simulation uses pre-computed vol_term and mu_term
 *   provided by dashboard_engine.py's _process_cumulative_periods()
 * - Does not render the STATIC content of the Consolidated VaR card
 *   (headline number, interpretation sentence, position breakdown) — that
 *   remains calculator.html's responsibility via renderResults(). This file
 *   only adds the Portfolio Scenario slider panel and its live "Stressed
 *   Portfolio VaR" readout inside that same card.
 *
 * === SIMULATION MATHEMATICS (for reference — used by BOTH slider pairs) ===
 *
 * Volatility slider (Δ_vol, long/short currencies):
 *   new_cfar_long  = Math.max(vol_term * (1 + Δ_vol) - mu_term, 0)
 *   new_cfar_short = Math.max(vol_term * (1 + Δ_vol) + mu_term, 0)
 *   Note: mu_term (drift) does NOT scale with vol — it is independent of the
 *         volatility regime. vol_term uses exposure-weighted effective_T per
 *         currency (an approximation for multi-horizon periods).
 *
 * Volatility slider (Δ_vol, FLAT currencies — V3.5 interim fix):
 *   new_cfar = Math.max(cfar * (1 + Δ_vol), 0)
 *   A flat (net-notional-zero) currency can still carry real Component CFaR
 *   from the cross-horizon residual case — vol_term/mu_term are both 0 for
 *   these currencies (derived from net_notional_base), so they cannot be
 *   used. This scales the static exact cfar directly instead — an interim
 *   approximation, not the exact result. See applySimulation()'s full
 *   docstring for the bug this replaced and the proper (deferred, backend-
 *   touching) fix.
 *
 * Spot slider (Δ_spot, selected currency only):
 *   new_cfar = Math.max(cfar * (1 + Δ_spot), 0)   ← exact: VaR ∝ E ∝ spot rate
 *
 * === SIMULATION APPROXIMATION — why the Period VaR strip AND the Stressed
 *     Portfolio VaR figure both diverge from the exact value while sliding ===
 *
 * At Δ=0 (sliders at rest): component CFaRs sum EXACTLY to the relevant
 * server-computed value — period_var for the Risk Dashboard's active
 * period, total_var for the Consolidated VaR card (these are the SAME
 * number when the Risk Dashboard's period happens to be 'all' — see above)
 * — by the Euler decomposition theorem.
 *
 * At Δ_vol ≠ 0: the vol slider scales each currency's vol_term independently.
 * Cross-currency correlations (ρ terms in the covariance matrix) are NOT
 * recomputed in the browser — doing so would require sending the full n×n
 * matrix and running matrix multiplication on every slider tick. The
 * per-currency sum therefore slightly OVERSTATES the true diversified VaR
 * (conservative bias proportional to ρ). This applies identically to both
 * the Period VaR strip and the Stressed Portfolio VaR figure, since both
 * are built from the same applySimulation() summation pattern.
 *
 * This is a deliberate design tradeoff: fast live preview vs exact math.
 * All VaR math stays in Python. Re-running Calculate gives the exact figure.
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
// COMPONENT CFaR BARS  (V3.2 — new, separate from the notional chart)
// ============================================================

/**
 * Renders the Component CFaR horizontal bar section beneath the notional
 * chart, inside the #dashCfarBars container. Plain HTML/CSS — no Chart.js
 * instance, no canvas — since these bars only ever need a simple
 * proportional width, not interactive tooltips or axes.
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
 * === SCALING AND SORTING ===
 *
 * Bar widths are scaled relative to the LARGEST |Component CFaR| present
 * in the currently selected period (not relative to net notional, and not
 * relative to Period VaR) — so the single riskiest currency always renders
 * a full-width reference bar, exactly like the existing .compare-bars
 * pattern used in the Cash Book Risk diversification display (gross vs.
 * net VaR on a shared scale). Rows are sorted by |Component CFaR|
 * descending, so the biggest threat always appears first regardless of how
 * the notional chart above happens to be ordered.
 *
 * A small minimum width (2%) is applied to any non-trivial (≥ 1 unit) CFaR
 * so it remains visibly present as a sliver even when dwarfed by the
 * largest bar in the period, rather than rendering as an invisible
 * zero-width track.
 *
 * === SIMULATION ===
 *
 * Called both on initial period render (from renderChartForPeriod) and on
 * every Risk Dashboard slider tick (from updateChartSimulation), using the
 * SAME applySimulation() function as the notional chart — so both bar
 * systems always reflect identical, internally consistent slider deltas.
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
        return;
    }

    const baseCcy = dashboardData.base_ccy;

    // Apply current Risk Dashboard simulation deltas — identical inputs to
    // the notional chart above, so both sections always stay consistent
    // with each other and with the Period VaR strip's running total.
    const rows = period.currencies.map(c => {
        const sim = applySimulation(c, deltaSpot, deltaVol, activeSpotCcy);
        return { currency: c.currency, direction: c.net_direction, cfar: sim.cfar };
    });

    // Sort by risk magnitude descending — the biggest threat renders first,
    // independent of the notional chart's own currency ordering above.
    rows.sort((a, b) => Math.abs(b.cfar) - Math.abs(a.cfar));

    // Scale every bar relative to the largest CFaR in this period. The 0.01
    // floor guards against a divide-by-zero when every currency is fully
    // hedged (all component CFaRs are zero) within the selected window.
    const maxAbsCfar = Math.max(...rows.map(r => Math.abs(r.cfar)), 0.01);

    container.innerHTML = rows.map(r => {
        const absCfar  = Math.abs(r.cfar);
        // Proportional width, with a 2% visible-sliver floor for any
        // non-trivial (≥1 unit) CFaR so it never renders as an invisible
        // zero-width track purely due to being dwarfed by the top bar.
        const pct      = absCfar >= 1
            ? Math.max((absCfar / maxAbsCfar) * 100, 2)
            : 0;
        const dirClass = r.direction;  // 'long' | 'short' | 'flat' — matches bar/legend colours above
        return `
          <div class="cfar-bar-row">
            <div class="cfar-bar-label">
              <span class="cfar-bar-ccy">${r.currency}</span>
            </div>
            <div class="cfar-bar-track">
              <div class="cfar-bar-fill ${dirClass}" style="width:${pct}%"></div>
            </div>
            <div class="cfar-bar-value">${baseCcy} ${fmtNum(r.cfar)}</div>
          </div>`;
    }).join('');
}


// ============================================================
// SIMULATION
// ============================================================

/**
 * Applies current simulation deltas to a single currency entry.
 * Works identically for both bucket currencies (V2) and period currencies (V3)
 * because both have the same field names: cfar, vol_term, mu_term,
 * net_notional_base, net_direction.
 *
 * Spot shift (selected currency only):
 *   new_net_notional = net_notional_base × (1 + Δ_spot)
 *   new_cfar         = cfar × (1 + Δ_spot)   ← exact (VaR ∝ E ∝ spot_rate)
 *
 * Vol shift (long/short currencies):
 *   new_cfar_long  = max(vol_term × (1 + Δ_vol) − mu_term, 0)
 *   new_cfar_short = max(vol_term × (1 + Δ_vol) + mu_term, 0)
 *   Note: mu_term does NOT scale with vol — drift is independent of vol regime.
 *         Scaling the whole cfar by (1+Δ) would incorrectly also scale mu_term.
 *
 * For period currencies, vol_term and mu_term use the exposure-weighted
 * effective_T (a per-currency approximation for multi-horizon periods).
 * For single-position currencies the formula is exact.
 *
 * Vol shift (flat currencies — V3.5 INTERIM FIX, see below):
 *   new_cfar = max(cfar × (1 + Δ_vol), 0)
 *
 * === V3.5 BUG FIX: FLAT CURRENCIES NO LONGER SNAP TO ZERO ON ANY VOL MOVE ===
 *
 * Previously this branch was `cfar = 0` unconditionally whenever
 * net_direction === 'flat' and dVol !== 0 — i.e. ANY non-zero vol delta,
 * even ±0.1%, instantly zeroed a flat currency's CFaR. This was wrong: a
 * currency can be net-flat in notional while still carrying real, non-zero
 * Component CFaR from the cross-horizon residual case (same-currency
 * positions that cancel in notional but settle at different dates — see
 * renderChartDetailPanel()'s cross-horizon tooltip for the full
 * min(Tᵢ,Tⱼ) explanation). That residual risk doesn't vanish just because
 * a vol slider moved a fraction of a percent; it should scale smoothly
 * with the vol regime, not collapse discontinuously to zero.
 *
 * WHY vol_term/mu_term CAN'T BE USED HERE: both are precomputed server-side
 * as `net_notional_base × (something)` (see dashboard_engine.py's
 * _process_currency_entry). For a flat currency, net_notional_base = 0, so
 * vol_term = 0 and mu_term = 0 are already baked in from the backend —
 * using them would still produce zero regardless of this function's logic.
 * They were designed assuming "one currency = one net position," which
 * does not hold for the cross-horizon residual case (the real Component
 * CFaR there comes from the INDIVIDUAL receivable/payable legs, not from
 * their — zero — sum).
 *
 * INTERIM APPROXIMATION (this fix): scale the static, exact ccy.cfar
 * directly by (1 + Δ_vol), treating the entire residual as if it were pure
 * volatility-driven risk. This is NOT exact — it ignores whatever (usually
 * small) drift contribution is mixed into that residual, which we cannot
 * separate out without a proper per-position vol/drift decomposition. But
 * it is a much better approximation than snapping to zero: it moves
 * smoothly and in the right direction as the vol slider moves, with no
 * discontinuity at Δ_vol = 0.
 *
 * TODO — PROPER FIX (flagged for a future, backend-touching change): there
 * is an EXACT fix available, not just a better approximation. Under a
 * UNIFORM vol-regime shift (every currency's σ scaled by the same factor
 * k = 1+Δ_vol, which is what this slider does), the volatility-driven part
 * of EVERY position's Component VaR scales EXACTLY linearly by k — this
 * follows from the covariance matrix being homogeneous of degree 2 in σ
 * (every Σ[i,j] term contains σᵢ×σⱼ, so the whole matrix scales by k²,
 * which exactly cancels against portfolio vol's own k-scaling in the
 * component formula sᵢ(Σs)ᵢ/σ_p). This holds regardless of net notional —
 * including the flat/cross-horizon case. To use this exactly, the backend
 * (exposure_engine.py's _compute_component_vars_by_currency) would need to
 * expose two new per-currency sums — the vol-driven part and the
 * drift-driven part of Component CFaR, computed from the real per-position
 * structure — rather than the current net_notional_base-derived
 * vol_term/mu_term. That is an engine-file change (branch first, per
 * project convention), deliberately deferred here in favour of this
 * smaller, frontend-only interim patch.
 *
 * @param {Object} ccy     — currency entry (from either buckets or cumulative_periods)
 * @param {number} dSpot   — spot delta for the selected currency (−0.10 to +0.10)
 * @param {number} dVol    — vol delta applied to ALL currencies (−0.25 to +0.25)
 * @param {string} spotCcy — which currency the spot slider currently targets
 * @returns {{ netNotional: number, cfar: number }}
 */
function applySimulation(ccy, dSpot, dVol, spotCcy) {
    let netNotional = ccy.net_notional_base;
    let cfar;

    // Step 1: vol shift (applies to all currencies simultaneously).
    // Uses the decomposed vol_term and mu_term rather than scaling cfar directly,
    // because mu_term (drift) is independent of the volatility regime.
    if (dVol !== 0) {
        const newVolTerm = ccy.vol_term * (1 + dVol);
        if (ccy.net_direction === 'long') {
            // Long: CFaR_long = vol_term*(1+Δv) − mu_term  [drift helps you]
            cfar = Math.max(newVolTerm - ccy.mu_term, 0);
        } else if (ccy.net_direction === 'short') {
            // Short: CFaR_short = vol_term*(1+Δv) + mu_term  [drift hurts you]
            cfar = Math.max(newVolTerm + ccy.mu_term, 0);
        } else {
            // Flat (net notional ≈ 0, so vol_term/mu_term are both 0 — see
            // this function's "V3.5 BUG FIX" docstring section above for
            // the full explanation). INTERIM approximation: scale the
            // static exact cfar directly, rather than snapping to zero.
            cfar = Math.max(ccy.cfar * (1 + dVol), 0);
        }
    } else {
        // No vol shift — use the pre-computed exact CFaR from the engine
        cfar = ccy.cfar;
    }

    // Step 2: spot shift (applies ONLY to the currently selected currency).
    // VaR ∝ exposure ∝ spot_rate, so the scaling is exact (not an approximation).
    if (spotCcy && ccy.currency === spotCcy && dSpot !== 0) {
        netNotional = ccy.net_notional_base * (1 + dSpot);
        cfar        = Math.max(cfar * (1 + dSpot), 0);
    }

    return { netNotional, cfar };
}


/**
 * Re-renders the chart data with current simulation deltas, without
 * recreating the Chart.js instance (uses chart.update('none') for
 * smooth performance, skipping animation). Also re-renders the Component
 * CFaR bar section below the chart, refreshes the Chart Detail Panel (V3.3)
 * with live values for whichever currency it's currently showing, and
 * updates the Period VaR strip to show the sum of simulated component
 * CFaRs as an approximation. (Exact period VaR needs the full covariance
 * matrix — use the pre-computed value in period.period_var for accuracy.)
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
    renderCfarBarsForPeriod(activePeriodKey);

    // Keep the Chart Detail Panel showing live values too, for whichever
    // currency it's currently displaying (last hovered, or the index-0
    // default — see chart._activeDetailIndex, set by renderChartDetailPanel).
    // This matters even when the mouse isn't over the chart at all: a user
    // dragging a slider with one hand while reading the panel should see it
    // update in step with the bars, not go stale until their next hover.
    renderChartDetailPanel(chart._activeDetailIndex ?? 0);

    // Update the Period VaR strip to the sum of current component CFaRs.
    //
    // === WHY THIS IS ALWAYS CONSISTENT WITH THE BARS ===
    //
    // The Period VaR strip always shows the column total of the Component
    // CFaR bars above it (now rendered separately by renderCfarBarsForPeriod,
    // not as labels inside the notional chart — see module docstring).
    // At Δ=0: components equal the exact server-side covariance decomposition,
    //   and their sum equals period.period_var exactly (Euler decomposition theorem).
    //   This is NOT a simple independent sum — the components already encode all
    //   cross-currency correlations via (cov_T @ s)_i in their construction.
    // At Δ≠0: components are individually scaled by applySimulation(), which does
    //   not rerun the covariance matrix. Sum is an approximation (conservative —
    //   overstates risk) because cross-currency correlations are not recomputed.
    //   Exact result requires re-running Calculate with a changed vol assumption.
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
 * means the 'all' period's per-currency vol_term/mu_term/cfar values —
 * already sent to the frontend for the Risk Dashboard's "All" filter
 * option — are equally valid inputs for stressing the Consolidated VaR
 * figure. No new Python computation was required to add this feature:
 * this function reuses applySimulation() completely unchanged, just with
 * a second, independent set of slider state (portfolioDeltaSpot /
 * portfolioDeltaVol / portfolioActiveSpotCcy) targeting the 'all' period.
 *
 * === WHY THIS IS AN APPROXIMATION (identical caveat to the Period VaR strip) ===
 *
 * Each currency's Component CFaR is scaled independently and summed —
 * cross-currency correlations are NOT recomputed when a slider moves
 * (that would require the full n×n covariance matrix server-side). At
 * Δ=0 the sum is exact and equals the Consolidated Portfolio VaR figure
 * shown statically above this panel (Euler decomposition theorem). At
 * Δ≠0 the sum is a conservative approximation (slightly overstates risk).
 * This is the exact same method and the exact same caveat as
 * updateChartSimulation()'s Period VaR strip update — just a second,
 * independent instance of it targeting this card instead.
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
