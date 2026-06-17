/**
 * dashboard.js — Risk Dashboard Rendering Layer (V3)
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
 * internal structure. The calculator JS owns the form and engine output;
 * this file owns the chart, sliders, and hedge table.
 *
 * === V3 CHANGE: CUMULATIVE PERIOD FILTER (replaces per-bucket dropdown) ===
 *
 * The bar chart dropdown now shows cumulative time windows instead of
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
 * === CHART DESIGN (V3.1 — single dataset + datalabels) ===
 *
 * ONE dataset per chart: net notional bars (green=long, red=short, grey=flat).
 * The previous side-by-side blue CFaR bars have been removed. Component CFaR
 * is now printed INSIDE each bar via chartjs-plugin-datalabels (loaded via CDN
 * in calculator.html and registered globally here in DOMContentLoaded).
 *
 * Bar label logic:
 *   - Tall bars (≥40px): Component CFaR centred inside the bar, white text.
 *   - Short bars (<40px): Component CFaR floated above/below the bar tip.
 *   - Zero-notional bars with non-zero CFaR: label floated above baseline in
 *     blue — these arise when same-currency positions cancel in notional but
 *     not in risk (cross-horizon mismatch). A tooltip on the chart explains why.
 *
 * CFaR labels are stored on chart._cfarValues[] so updateChartSimulation() can
 * update them without recreating the Chart.js instance.
 *
 * === WHAT THIS FILE DOES NOT DO ===
 *
 * - No form handling (that's in calculator.html's inline script)
 * - No fetch calls (the calculator JS fetches; this just consumes the result)
 * - No VaR formulas — simulation uses pre-computed vol_term and mu_term
 *   provided by dashboard_engine.py's _process_cumulative_periods()
 *
 * === SIMULATION MATHEMATICS (for reference) ===
 *
 * Volatility slider (Δ_vol, all currencies):
 *   new_cfar_long  = Math.max(vol_term * (1 + Δ_vol) - mu_term, 0)
 *   new_cfar_short = Math.max(vol_term * (1 + Δ_vol) + mu_term, 0)
 *   Note: mu_term (drift) does NOT scale with vol — it is independent of the
 *         volatility regime. vol_term uses exposure-weighted effective_T per
 *         currency (an approximation for multi-horizon periods).
 *
 * Spot slider (Δ_spot, selected currency only):
 *   new_cfar = Math.max(cfar * (1 + Δ_spot), 0)   ← exact: VaR ∝ E ∝ spot rate
 *
 * === SIMULATION APPROXIMATION — why the Period VaR strip diverges during sliding ===
 *
 * At Δ=0 (sliders at rest): component CFaRs sum EXACTLY to server-computed period_var
 * by the Euler decomposition theorem. The strip shows the exact value.
 *
 * At Δ_vol ≠ 0: the vol slider scales each currency's vol_term independently.
 * Cross-currency correlations (ρ terms in the covariance matrix) are NOT recomputed
 * in the browser — doing so would require sending the full n×n matrix and running
 * matrix multiplication on every slider tick. The per-currency sum therefore slightly
 * OVERSTATES the true diversified VaR (conservative bias proportional to ρ).
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
let activeSpotCcy    = null;  // currency selected in spot slider dropdown
let deltaSpot        = 0.0;   // current spot slider delta (−0.10 to +0.10)
let deltaVol         = 0.0;   // current vol slider delta (−0.25 to +0.25)
let chart            = null;  // Chart.js instance (destroyed + recreated on period change)


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
    // just without inside-bar CFaR labels).
    if (typeof ChartDataLabels !== 'undefined') {
        Chart.register(ChartDataLabels);
    } else {
        console.warn('chartjs-plugin-datalabels not loaded — CFaR labels inside bars disabled.');
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

    // Wire up period filter dropdown, currency picker, and both sliders.
    // These elements exist in the DOM from page load but are hidden
    // until renderDashboard() makes them visible.
    document.getElementById('periodSelect')
        .addEventListener('change', handlePeriodChange);

    document.getElementById('spotCcySelect')
        .addEventListener('change', handleSpotCcyChange);

    document.getElementById('spotSlider')
        .addEventListener('input', handleSpotSlider);

    document.getElementById('volSlider')
        .addEventListener('input', handleVolSlider);
});


// ============================================================
// DASHBOARD RENDER — entry point after data arrives
// ============================================================

/**
 * Main render function. Called once each time Calculate is run successfully.
 * Resets simulation state, then builds all dashboard UI sections.
 *
 * Uses data.cumulative_periods for the bar chart (V3) and
 * data.buckets for the hedge effectiveness table (unchanged from V2).
 *
 * @param {Object} data — the 'dashboard' key from the /calculate response
 */
function renderDashboard(data) {
    // Reset simulation sliders to centre (0 delta) on every new calculation
    deltaSpot = 0.0;
    deltaVol  = 0.0;
    document.getElementById('spotSlider').value = 0;
    document.getElementById('volSlider').value  = 0;
    updateSliderDisplays();

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
 *
 * === CHART LAYOUT (V3.1) ===
 *
 * ONE dataset: net notional bars, coloured by net direction:
 *   Green  = net long  (receivable — FCY appreciation is a gain)
 *   Red    = net short (payable   — FCY appreciation costs more)
 *   Grey   = flat      (perfectly hedged within this time window)
 *
 * Component CFaR is printed INSIDE each bar via chartjs-plugin-datalabels
 * rather than shown as a separate blue bar. This avoids the proportion distortion
 * that the side-by-side layout caused when notional and CFaR were on very different
 * scales (e.g. large notional but low-vol currency → tiny CFaR bar looked broken).
 *
 * === ZERO-NOTIONAL BARS ===
 *
 * A currency can have zero net notional within a period but still carry a non-zero
 * Component CFaR. This happens when same-currency positions cancel in notional
 * (e.g. recv 2mn + pay 2mn = 0) but settle at different dates (T=43 vs T=100).
 * The min(Tᵢ,Tⱼ) covariance formula does NOT fully cancel them because they
 * don't co-exist for the same duration — the payable runs on after the receivable
 * settles, leaving residual risk. The CFaR label for such bars floats in blue above
 * the zero baseline. A tooltip on the chart title explains this to users.
 *
 * === DATALABELS POSITIONING LOGIC ===
 *
 * The chartjs-plugin-datalabels anchor/align are set per-bar as functions:
 *   - Zero-height bar (|net notional| ≈ 0): anchor='end', align='top' → floats above
 *   - Short bar (<40px): anchor='end', align based on sign → outside the bar tip
 *   - Tall bar (≥40px): anchor='center', align='center' → centred inside the bar
 *
 * CFaR values are stored on chart._cfarValues[] (set after chart creation, updated
 * by updateChartSimulation) so the datalabels formatter always reads current values.
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
        // No data for this period — show placeholder, hide canvas
        emptyEl.style.display  = 'flex';
        canvasEl.style.display = 'none';
        renderPeriodInfoStrip(period || { period_var: 0, n_positions: 0, max_days: null });
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
                    // Component CFaR is shown as text labels INSIDE these bars
                    // via chartjs-plugin-datalabels (configured in options.plugins.datalabels).
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
            plugins: {
                legend: { display: false },

                // ── TOOLTIP ──────────────────────────────────────────────────
                tooltip: {
                    backgroundColor: '#1a1e25',
                    borderColor:     '#2e3440',
                    borderWidth:     1,
                    titleColor:      '#e8eaf0',
                    bodyColor:       '#6b7280',
                    padding:         12,
                    callbacks: {
                        label: ctx =>
                            `Net Exposure (${baseCcy}): ${baseCcy} ${fmtNum(ctx.parsed.y)}`,
                        afterBody: (items) => {
                            const idx = items[0]?.dataIndex;
                            if (idx === undefined) return [];
                            const c = currencies[idx];

                            // Read CFaR from chart._cfarValues so tooltip shows
                            // the simulated value (not the static pre-computed one)
                            const cfarVal = chart?._cfarValues?.[idx] ?? c.cfar;

                            // Flag zero-notional-but-nonzero-CFaR (cross-horizon case)
                            const isZeroNotional = Math.abs(c.net_notional_base) < 100;
                            const crossHorizonNote = (isZeroNotional && Math.abs(cfarVal) > 0)
                                ? ['ⓘ Zero net notional — CFaR from cross-horizon T mismatch']
                                : [];

                            return [
                                `Component CFaR:  ${baseCcy} ${fmtNum(cfarVal)}`,
                                `Direction:       ${c.net_direction.toUpperCase()}`,
                                `Spot rate:       ${c.spot_rate.toFixed(4)} ${baseCcy}/1 ${c.currency}`,
                                `Ann. vol:        ${c.annualised_vol_pct.toFixed(2)}%`,
                                `Eff. horizon:    ${c.effective_T.toFixed(0)} trading days`,
                                ...crossHorizonNote,
                                ...(deltaSpot !== 0 || deltaVol !== 0 ? ['— simulation active —'] : []),
                            ];
                        },
                    },
                },

                // ── DATALABELS — Component CFaR printed inside bars ───────────
                // chartjs-plugin-datalabels is registered globally in DOMContentLoaded.
                // Reads from chart._cfarValues[] which is set after chart creation and
                // updated by updateChartSimulation on every slider tick.
                datalabels: {
                    /**
                     * formatter: shows Component CFaR (not net notional).
                     * Always shows absolute value — negative component CFaR (hedging
                     * positions) is visually represented as zero bar height; the signed
                     * value is preserved in period_var and explained in the tooltip.
                     * Returns '' (empty) to hide the label if CFaR is effectively zero.
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
                        const cfarList = ctx.chart._cfarValues;
                        if (!cfarList) return '';
                        const cfar = cfarList[ctx.dataIndex] ?? 0;
                        if (Math.abs(cfar) < 1) return '';  // hide near-zero labels
                        const amount = Math.round(Math.abs(cfar));
                        return isNarrowChart(ctx.chart)
                            ? fmtShort(amount)
                            : `${baseCcy} ${fmtNum(amount)}`;
                    },

                    /**
                     * anchor: determines which edge of the bar the label attaches to.
                     *   'center' → label at the vertical midpoint of the bar (tall bars)
                     *   'end'    → label at the tip of the bar (short bars / zero bars)
                     * Threshold of 40px is empirically chosen for 11px font + 8px padding.
                     */
                    anchor: (ctx) => {
                        try {
                            const el  = ctx.chart.getDatasetMeta(ctx.datasetIndex).data[ctx.dataIndex];
                            const h   = Math.abs((el?.base ?? 0) - (el?.y ?? 0));
                            return h < 4 ? 'end' : h < 40 ? 'end' : 'center';
                        } catch (_) { return 'center'; }
                    },

                    /**
                     * align: which side of the anchor point the label appears on.
                     *   'center' → centred on the anchor (inside the bar)
                     *   'top'    → above the anchor (outside positive short bars / zero bars)
                     *   'bottom' → below the anchor (outside negative short bars)
                     */
                    align: (ctx) => {
                        try {
                            const el  = ctx.chart.getDatasetMeta(ctx.datasetIndex).data[ctx.dataIndex];
                            const h   = Math.abs((el?.base ?? 0) - (el?.y ?? 0));
                            const val = ctx.dataset.data[ctx.dataIndex];
                            if (h < 4)  return 'top';            // zero bar: float above baseline
                            if (h < 40) return val >= 0 ? 'top' : 'bottom';  // short bar: outside
                            return 'center';                      // tall bar: inside centre
                        } catch (_) { return 'center'; }
                    },

                    /**
                     * color: white inside normal bars; blue for zero-notional floating labels.
                     * Blue signals "this CFaR comes from cross-horizon mismatch, not net exposure"
                     * which matches the blue used for the info/link colour in the rest of the UI.
                     */
                    color: (ctx) => {
                        const val = ctx.dataset.data[ctx.dataIndex];
                        return Math.abs(val) < 100
                            ? 'rgba(96, 165, 250, 0.95)'   // blue: zero-notional label
                            : 'rgba(255, 255, 255, 0.92)'; // white: normal label inside bar
                    },

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

                    // hide label entirely if CFaR rounds to zero (nothing meaningful to show)
                    display: (ctx) => {
                        const cfarList = ctx.chart._cfarValues;
                        if (!cfarList) return false;
                        return Math.abs(cfarList[ctx.dataIndex] ?? 0) >= 1;
                    },

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
                        text:    `Net Exposure (${baseCcy}) — Component CFaR printed inside bars`,
                        color:   '#6b7280',
                        font:    { family: "'DM Mono'", size: 11 },
                    },
                },
            },
        },
    });

    // Store the initial Component CFaR values on the chart instance.
    // The datalabels formatter reads from chart._cfarValues on every render.
    // updateChartSimulation() updates this array (and calls chart.update()) so
    // the inside-bar labels always reflect the current simulation state without
    // needing to destroy and recreate the chart.
    chart._cfarValues = [...cfarValues];

    // IMPORTANT: chart._cfarValues must be set BEFORE the datalabels plugin
    // reads it. Chart.js fires its initial render synchronously during
    // new Chart(...) above, at which point _cfarValues does not yet exist,
    // so the datalabels display() function returns false and hides all labels.
    // chart.update('none') forces a second render (no animation) now that
    // _cfarValues is populated, making the CFaR labels visible immediately
    // without requiring the user to hover over any bar.
    chart.update('none');

    renderPeriodInfoStrip(period);
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
 * Vol shift (all currencies):
 *   new_cfar_long  = max(vol_term × (1 + Δ_vol) − mu_term, 0)
 *   new_cfar_short = max(vol_term × (1 + Δ_vol) + mu_term, 0)
 *   Note: mu_term does NOT scale with vol — drift is independent of vol regime.
 *         Scaling the whole cfar by (1+Δ) would incorrectly also scale mu_term.
 *
 * For period currencies, vol_term and mu_term use the exposure-weighted
 * effective_T (a per-currency approximation for multi-horizon periods).
 * For single-position currencies the formula is exact.
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
            // Flat: no exposure → zero CFaR regardless of vol
            cfar = 0;
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
 * smooth performance, skipping animation).
 *
 * Also updates the Period VaR strip to show the sum of simulated component
 * CFaRs as an approximation. (Exact period VaR needs the full covariance
 * matrix — use the pre-computed value in period.period_var for accuracy.)
 */
function updateChartSimulation() {
    if (!chart || !dashboardData) return;

    const period = dashboardData.cumulative_periods.find(p => p.key === activePeriodKey);
    if (!period) return;

    const simulated = period.currencies.map(c =>
        applySimulation(c, deltaSpot, deltaVol, activeSpotCcy)
    );

    // Push simulated net notional values into the single bar dataset.
    chart.data.datasets[0].data = simulated.map(s => s.netNotional);

    // Update the CFaR values stored on the chart instance. The datalabels formatter
    // reads from chart._cfarValues on every render, so updating this array and
    // calling chart.update() refreshes the inside-bar labels automatically.
    // There is NO second dataset — CFaR is displayed as text labels, not bars.
    chart._cfarValues = simulated.map(s => s.cfar);

    chart.update('none');   // 'none' = no animation, for a responsive feel

    // Update the Period VaR strip to the sum of current component CFaRs.
    //
    // === WHY THIS IS ALWAYS CONSISTENT WITH THE BARS ===
    //
    // The Period VaR strip always shows the column total of the bars above it.
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

/** Refreshes the % labels displayed next to both sliders. */
function updateSliderDisplays() {
    const sp = (deltaSpot * 100).toFixed(1);
    const vp = (deltaVol  * 100).toFixed(1);
    document.getElementById('dashSpotValue').textContent =
        deltaSpot >= 0 ? `+${sp}%` : `${sp}%`;
    document.getElementById('dashVolValue').textContent =
        deltaVol >= 0 ? `+${vp}%` : `${vp}%`;
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

/** Formats a number with comma separators, 0 decimal places. */
function fmtNum(n) {
    if (n === undefined || n === null || isNaN(n)) return '—';
    return Math.round(n).toLocaleString('en-US');
}

/**
 * Compact alias for fmtNum (used inside stat card innerHTML).
 * Same behaviour — exists for readability at the call site.
 */
function fmt(n) {
    return fmtNum(n);
}

/** Compact axis tick format: 1500000 → "1.5M", 25000 → "25K". */
function fmtShort(v) {
    const a = Math.abs(v);
    if (a >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
    if (a >= 1_000)     return (v / 1_000).toFixed(0) + 'K';
    return v.toFixed(0);
}

/**
 * isNarrowChart(chart) — true when the Net Exposure chart's current
 * rendered width is too narrow for the inside-bar Component CFaR labels
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
