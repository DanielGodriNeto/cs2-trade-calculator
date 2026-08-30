// web/static/js/views/legacy.js
//
// Host for the three screens NOT rebuilt in this redesign pass: Rare Patterns (PAT),
// Simulation Lab (LAB) and Connections (CONN). Their markup is lifted VERBATIM from the
// pre-redesign index.html (commit 0897283) -- lab lines 84-483, patterns 787-876,
// connections 878-968 -- with only the wrapper <div> changed (data-view + hidden dropped,
// class="legacy" added, id kept). Their render logic still lives in the classic /app.js;
// loadOldApp() pulls it in once as a plain <script> and renderLegacy() calls the loader
// functions it exposes on window. LEGACY_LOADERS is copied from that file's VIEW_LOADERS
// map (its "ml-lab" key is renamed "lab" to match the HUD nav route #lab).

const LEGACY_LOADERS = {
  patterns: ["loadPatternsStatus", "loadPatternOpportunities"],
  lab: [
    "loadThirdpartyStats", "loadModelStatus", "loadRetrainLoopStatus", "loadOutcomes",
    "loadPriceScanStatus", "loadRoundRobinStatus", "loadSimPopulationStatus", "loadConvergenceStatus",
    "loadVerifyQueueStatus", "loadDeadSkins", "loadInvalidSkins", "loadMispricedSkins",
    "loadWearAnomalies", "loadMarketReliability", "loadSkinBlacklist", "loadPriceOverrides",
  ],
  connections: ["loadBackupStatus", "loadConnectionsStatus"],
};

// The classic /app.js wires ~60 top-level listeners against the PRE-redesign DOM. Under the
// new shell only one legacy block is mounted at a time, so it hits a missing element and
// aborts partway through its top-level code. That is mostly harmless: every load* / render* /
// wireCopyButtons it defines is a hoisted `function` declaration, so all are on `window` by
// then, and its own 3s poll loop (near the end of the file) sits past the abort point, so it
// never starts and never competes with app.js's poll.
//
// /app.js also registers a `hashchange` listener BEFORE the abort point:
//   () => { applyView(); pollStatus(); }
// Both halves misbehave under the new shell:
//  - pollStatus() throws (its catch touches #status-value, gone now) -> "Uncaught (in
//    promise)" on every SPA navigation.
//  - applyView() sets hidden=true on #view-ml-lab whenever the route is "lab" (its VIEWS
//    key is "ml-lab"), racing the router and blanking the LAB screen.
// Once /app.js has loaded we overwrite both globals with no-ops. They are classic-script
// function declarations, so the global IS the binding that stale listener resolves -> it
// goes quiet. The new js/app.js owns the telemetry poll; this module owns legacy visibility.
//
// A `/app.js`-scoped error + unhandledrejection swallow stays installed for the life of the
// page: it catches the synchronous top-level TypeError at the abort point, and closes the
// brief window (script parsed far enough to register the hashchange listener, `load` not yet
// fired) where a fast double-click could still fire the un-neutralised pollStatus. It only
// suppresses exceptions whose origin is /app.js -- anything thrown by the new js/ modules
// surfaces normally.
// Matches the classic script at exactly "/app.js" — never the new router at "/js/app.js".
// Handles both a bare URL (error event `filename`) and a multi-line stack blob.
const OLD_APP_IN_STACK = /:\/\/[^/\s]+\/app\.js(?=[:?)\s]|$)/;
export function __swallowMatchesOldApp(s) {
  if (typeof s !== "string") return false;
  try { if (new URL(s, location.origin).pathname === "/app.js") return true; } catch { /* not a bare URL */ }
  return OLD_APP_IN_STACK.test(s);
}

let _oldApp = null;
function _installOldAppSwallow() {
  window.addEventListener("error", (e) => {
    if (__swallowMatchesOldApp(e.filename) || __swallowMatchesOldApp(e.error && e.error.stack)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  window.addEventListener("unhandledrejection", (e) => {
    if (__swallowMatchesOldApp(e.reason && e.reason.stack)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
}
function loadOldApp() {
  if (_oldApp) return _oldApp;
  _installOldAppSwallow();
  _oldApp = new Promise((resolve) => {
    const done = () => {
      if (typeof window.pollStatus === "function") window.pollStatus = function legacyPollNeutralised() {};
      if (typeof window.applyView === "function") window.applyView = function legacyApplyViewNeutralised() {};
      resolve();
    };
    const s = document.createElement("script");
    s.src = "/app.js";
    s.onload = done;
    s.onerror = done;
    document.body.appendChild(s);
  });
  return _oldApp;
}

const MARKUP = {
  lab: `<div class="legacy" id="view-ml-lab">

<details class="methodology" id="methodology-section">
  <summary><h2>How this works &amp; what the numbers mean</h2></summary>
  <div class="methodology-body">

    <h3>1. The trade-up contract</h3>
    <p>A trade-up contract consumes 10 skins of the same rarity and the same collection, and
    returns 1 random skin of the next rarity tier up, drawn uniformly from that collection's
    pool at that tier. If a collection has <em>n</em> possible outputs at the target rarity,
    each one has a <strong>1/n</strong> chance. Steam takes a cut when you resell the output --
    currently modeled here as <strong><span id="const-fee">&hellip;</span>%</strong>
    (<code>STEAM_SELL_FEE</code> in <code>tradeup.py</code>). Worked example: 10 inputs costing
    $1.00 each = $10 cost; if the expected resale value is $12, after the fee that's
    $12 &times; (1 &minus; fee) &asymp; $10.44, for roughly a 4% ROI.</p>
    <p>When multiple collections are mixed into one contract (see "filler" strategies below),
    the Oct-2025 rules make odds <strong>group-based</strong>: a group's share of the 10 inputs
    is its total chance, split evenly across its own outputs (<code>oddsmath.group_odds()</code>).</p>

    <h3>2. Float manipulation</h3>
    <p>The output's float (wear condition) is the average of the 10 input floats, each
    normalized into <em>its own</em> skin's min/max range, then re-applied to the
    <em>output</em> skin's own range. Buying a deliberate mix of wears -- instead of whatever's
    cheapest -- can steer the result into a more valuable wear bracket. Steam's public API
    doesn't expose exact per-listing floats, so this tool approximates each wear by the
    midpoint of its standard range, shown below:</p>
    <div class="chart-wrap chart-wrap-wide"><canvas id="wear-bracket-chart"></canvas></div>

    <h3>3. Third-party drought detection</h3>
    <p>Steam's own listing supply for a specific skin+wear can run temporarily thin, pushing
    its Steam price well above what the same item sells for elsewhere. This tool cross-checks
    against Skinport (and CSFloat, if configured) and flags a "drought" when Steam is priced
    <strong><span id="const-drought">&hellip;</span>%</strong> or more above the cheapest
    verified alternative -- an annotation only, never a change to the cost/ROI numbers.</p>

    <h3>4. ML-assisted ROI calibration</h3>
    <p>Once you've logged <strong><span id="const-min-samples">&hellip;</span>+</strong> real
    trade-up outcomes (see "Learning from real trades" below), a
    <code>HistGradientBoostingRegressor</code> (scikit-learn) is trained on features like the
    wear-mix composition, whether a filler collection was used, StatTrak, cost, and the
    formula ROI at the time, to predict realized ROI. It's consulted only as a
    <strong>tiebreaker</strong> between near-identical formula-ROI candidates, never to
    override a clear formula winner. It retrains automatically once
    <strong><span id="const-retrain-delta">&hellip;</span></strong> new outcomes accumulate.
    Once trained, its feature importances are shown here:</p>
    <div class="chart-wrap" id="feature-importance-wrap" style="display:none;"><canvas id="feature-importance-chart"></canvas></div>
    <p id="feature-importance-empty" class="empty-state">(not trained yet)</p>
    <p class="disclaimer" id="candidate-comparison"></p>

    <p>The honest test of whether this calibration is worth anything is whether its predictions
    actually track reality. Every point below is one <em>sold</em> outcome where the scan had an
    ML prediction at the time (use "&#128203; Log this" on an ML-scored result, then confirm the
    real sale price) -- x is what the model predicted, y is what actually happened. Points on the
    dashed line would mean perfect calibration; scatter above/below it shows where the model
    over- or under-estimates.</p>
    <div class="chart-wrap" id="calibration-wrap" style="display:none;"><canvas id="calibration-chart"></canvas></div>
    <p id="calibration-empty" class="empty-state">(no ML-predicted outcomes logged yet)</p>

    <h3>5. Price history &amp; prediction</h3>
    <p>Steam's public API has <strong>no history endpoint</strong> -- this tool starts
    recording a price history itself, one snapshot per fresh lookup, from whenever you first
    use this feature. It is <em>not</em> retroactive. Below
    <strong><span id="const-min-history">&hellip;</span></strong> recorded points, no
    prediction is shown at all. Above that, a simple linear-regression trend line is fit and
    extrapolated 24h ahead -- a naive, fully-explainable baseline suitable for a coursework
    demonstration, not a real forecasting model or financial advice.</p>

    <h3>Glossary</h3>
    <dl class="glossary">
      <dt>ROI</dt><dd>Return on investment: profit &divide; cost &times; 100, after the Steam fee.</dd>
      <dt>Float</dt><dd>A number from 0.00 (cleanest) to 1.00 (most worn) determining a skin's exact wear within its bracket.</dd>
      <dt>Wear</dt><dd>The five named condition brackets a float falls into -- see the chart above.</dd>
      <dt>StatTrak</dt><dd>A variant that tracks kills; priced and traded separately from normal copies.</dd>
      <dt>Collection vs. case</dt><dd>Regular trade-ups group skins by collection; the Covert-&gt;knife/glove trade-up groups by weapon case instead.</dd>
      <dt>Drought</dt><dd>A temporary Steam-specific supply shortage that inflates Steam's price relative to other markets.</dd>
    </dl>
  </div>
</details>

<section class="network-health" id="thirdparty-health" title="Diagnostics for the 'Verify 3rd-party prices' feature -- distinguishes a genuine 'no listings found' from the source being unreachable/blocked, which otherwise look identical.">
  <div class="health-headline" id="thirdparty-headline">3rd-party price checks: not used yet this session</div>
</section>

<section id="ml-picks-section" class="ml-picks">
  <h2>&#129302; Learning Machine &mdash; top picks <span class="count" id="ml-picks-count"></span></h2>
  <p class="disclaimer" id="ml-picks-explainer">Trade-ups the trained ML model rates most highly, ranked by its own predicted ROI (a tiebreaker signal calibrated on your logged outcomes -- never a replacement for the formula ROI shown elsewhere).</p>
  <table id="ml-picks-table" style="display:none;">
    <thead><tr>
      <th>Skin</th><th>Collection</th><th>From</th><th>To</th>
      <th>Formula ROI %</th><th>ML-predicted ROI %</th><th>ML edge</th><th>Confidence</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <div id="ml-picks-empty" class="empty-state"></div>
</section>

<section id="learning-section">
  <h2>ML training &amp; simulation
    <button class="export-btn" id="export-data-btn" title="Download a .zip of everything accumulated in data/ (outcomes, price history, trained model) -- for backup, or to carry your training data to another machine">Export data archive</button>
  </h2>
  <p class="disclaimer">Logs your actual trade-up outcomes so the tool can compare winners vs. losers over time, plus a background loop that retrains the ML ROI model automatically as you log more outcomes. Everything here lives in <code>data/</code> -- see the README for training across multiple machines (git, or <code>export-data</code>/<code>import-data</code>).</p>

  <div class="learning-panel">
    <h3>ML retrain-watch loop</h3>
    <div class="learning-status" id="retrain-loop-status">loading&hellip;</div>
    <div class="learning-controls">
      <button id="retrain-loop-start-btn">Start watching</button>
      <button id="retrain-loop-stop-btn" class="secondary">Stop watching</button>
      <button id="train-now-btn" class="secondary">Train now</button>
      <button id="reset-model-btn" class="secondary" title="Deletes the trained ROI-calibration model so it stops giving ML predictions until retrained. Your logged outcomes (data/outcomes.jsonl) are never touched, so you can just hit Train now afterward to rebuild it from the same history.">Reset model</button>
    </div>
  </div>

  <div class="learning-panel">
    <h3>Bulk price scan</h3>
    <p class="disclaimer">Separate mode from the tradeups/covert scan on the Trade-Up Calculator page: walks every tradeable skin/wear/StatTrak combo and just warms the Steam price cache + price history -- no trade-up math. Run this before the guided random simulation below so it isn't working off a mostly-empty cache. Don't run this at the same time as another scan/simulation -- they'd compete for the same rate-limited Steam requests.</p>
    <div class="learning-controls">
      <label>Collection filter <input type="text" id="price-scan-collection" placeholder="e.g. Dead Hand (blank = all)"></label>
    </div>
    <div class="learning-status" id="price-scan-status">loading&hellip;</div>
    <div class="learning-controls">
      <button id="price-scan-start-btn">Start</button>
      <button id="price-scan-pause-btn" class="secondary">Pause</button>
      <button id="price-scan-stop-btn" class="secondary">Stop</button>
    </div>
  </div>

  <div class="learning-panel">
    <h3>Multi-agent simulation (6 agents: 4 specialists + 2 meta)</h3>
    <p class="disclaimer">Generates totally random, plausible trade-up candidates (random skins/collections/floats spanning multiple collections, respecting each skin's real float cap), simulates each one's expected ROI, and logs every trial to its own <code>data/sons/son_N/simulations.jsonl</code>. Four independent named agents run in parallel, each with a distinct search personality (8 behavioral parameters -- exploration, risk tolerance, collection-mixing bias, wear-optimization bias, liquidity importance, ROI weight, mutation strength, memory weight) and its own separately-trained search-heuristic ML model. Five of those parameters <strong>self-tune automatically</strong>: every few rounds an agent A/B-tests a small tweak of its own search preferences and keeps it only if it beat the previous window on an ROI-first reward that also pays a bonus for exploring under-tried collections (and penalizes unpriceable candidates) -- so preferences chase real ROI without ever giving up on new skins/collections; candidate selection additionally carries an uncertainty bonus for collections an agent has rarely simulated, so a powerful trade-up hiding in unexplored territory can't be starved out of the budget. The risk/liquidity admission gates and each agent's ROI trust weight stay fixed so an agent can never "improve" its reward by lowering its own quality bar. The radar chart below shows the LIVE (self-tuned) parameter values, drifting in real time. Because the four agents search independently, if one plateaus the others keep making progress, and a plateaued ("stuck") one can be individually Nudged (a temporary push back toward exploring, keeps its data/model) or Fully reset (wipes that one agent's data/model and starts it over). An anti-convergence watcher automatically nudges agents that settle on the same strategy for too long. This is a THEORETICAL, formula-based estimate from candidates nobody has bought or sold -- a search heuristic, not a market oracle -- and is kept entirely separate from the real ROI-calibration model above (trained only on outcomes you actually logged). See the <a href="#results">Results</a> page for its curated finds, the full archive, and leaderboards.</p>

    <h4>Population health at a glance</h4>
    <div id="son-health-wrap">
      <table id="son-health-table">
        <thead><tr>
          <th>Agent</th><th>Status</th><th class="num">Round</th><th class="num">Simulated</th>
          <th class="num">Invalid</th><th class="num">Invalid %</th><th>Flag</th><th class="num">Temp</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="chart-wrap chart-wrap-wide" id="agent-radar-wrap"><canvas id="agent-radar-chart"></canvas></div>

    <h4>Simulated bankroll <span class="muted-inline">(if every trade every agent proposed had actually been bought)</span></h4>
    <p class="disclaimer">Purely hypothetical -- nothing here is ever bought or sold, this tool is read-only. Sums each agent's own expected cost/profit (the same exact-EV math used everywhere else in this app, never a random sampled outcome) across EVERY valid candidate it has ever simulated, not just its curated best-known finds, so it also reflects the ones that turned out to be bad ideas.</p>
    <div class="kpi-grid" id="sim-bankroll-grid">
      <div class="kpi-card"><div class="kpi-label">Total spent</div><div class="kpi-value" id="bankroll-spent">&mdash;</div></div>
      <div class="kpi-card"><div class="kpi-label">Net if bought everything</div><div class="kpi-value" id="bankroll-net">&mdash;</div></div>
      <div class="kpi-card"><div class="kpi-label">Blended ROI %</div><div class="kpi-value" id="bankroll-roi">&mdash;</div></div>
    </div>
    <div id="sim-bankroll-per-agent" class="sim-bankroll-per-agent"></div>
    <p class="disclaimer">The graph below overlays every agent on one axis so you can see at a glance which one would have made (or lost) the most money if you'd bought every trade it proposed: running net expected profit (cumulative expected profit &minus; nothing is subtracted twice, it's already net of cost) versus round number. The line highest at the right edge is the current front-runner. Click an agent in the legend to hide/show its line.</p>
    <div class="chart-wrap chart-wrap-wide" id="sim-bankroll-chart-wrap"><canvas id="sim-bankroll-chart"></canvas></div>

    <h4>Agent stats over rounds</h4>
    <div class="learning-controls">
      <label>Stat
        <select id="agent-stat-select">
          <option value="best_roi_ever">Best ROI % so far</option>
          <option value="avg_roi_pct">Avg ROI % this round</option>
          <option value="avg_input_float">Preferred input float (normalized, 0=cleanest/1=worst)</option>
          <option value="avg_confidence">Avg confidence score</option>
          <option value="cumulative_expected_profit">Cumulative expected profit (if bought all)</option>
          <option value="n_simulated">Simulated trials so far (cumulative)</option>
        </select>
      </label>
    </div>
    <p class="disclaimer">Click an agent's name in the legend below to toggle its line on/off.</p>
    <div class="chart-wrap chart-wrap-wide" id="agent-stats-wrap"><canvas id="agent-stats-chart"></canvas></div>

    <h4>Search-heuristic model quality over time (MAE) <span class="muted-inline">(per agent, lower is better)</span>
      <button class="export-btn collapse-btn" id="collapse-sim-mae-btn" data-target="sim-mae-wrap">Hide</button>
    </h4>
    <p class="disclaimer">Each agent's own sim-model validation MAE (mean absolute error in expected-ROI percentage points, cross-validated) every time it retrains -- automatically, every <span id="const-sim-retrain-delta">&hellip;</span> new simulated trials. <code>sim_roi_model_meta.json</code> only ever stores the CURRENT model's MAE, not a history of past trainings, so this trend is sampled live as the dashboard polls and only accumulates while this page stays open -- it resets on reload, same as the rest of this dashboard's in-memory state. An agent below the minimum training-sample threshold hasn't trained a model yet and won't appear until it does.</p>
    <div id="sim-mae-wrap">
      <div class="chart-wrap chart-wrap-wide" id="sim-mae-chart-wrap"><canvas id="sim-mae-chart"></canvas></div>
      <p id="sim-mae-empty" class="empty-state">No agent has trained a search-heuristic model yet in this browser session -- keep this page open while simulations run to start building the trend.</p>
    </div>

    <h4>Training data volume &amp; validity by agent
      <button class="export-btn collapse-btn" id="collapse-sim-data-quality-btn" data-target="sim-data-quality-wrap">Hide</button>
    </h4>
    <p class="disclaimer">One chart per agent, all-time cumulative totals, broken into three categories: <strong>Trainable</strong> (priced, and liquid enough that <code>sim_learning.py</code>'s <code>train()</code> actually fits on it), <strong>Low-volume</strong> (priced fine, but thin-liquidity enough that training excludes it -- see <code>SimulationRecord.low_volume_warning</code>), and <strong>Invalid</strong> (no Steam/third-party price at all -- never even becomes a <code>SimulationRecord</code>, see <code>InvalidSimulation</code>). A high Low-volume or Invalid share means an agent is mostly hitting untradeable/illiquid skins instead of producing usable training data.</p>
    <div id="sim-data-quality-wrap" class="sim-feature-grid"></div>

    <h4>Search-heuristic feature importance by agent
      <button class="export-btn collapse-btn" id="collapse-sim-feature-btn" data-target="sim-feature-wrap">Hide</button>
    </h4>
    <p class="disclaimer">Same permutation-importance method as the real ROI-calibration model's chart above (Methodology section), run per agent against its own sim-model (<code>src/sim_learning.py</code>, trained only on theoretical simulated trials -- never mixed with or compared against the real model). Shows which candidate features (rarity, wear mix, filler fraction, StatTrak, cost, collection's mean simulated ROI, ...) each agent's search heuristic actually leans on. An agent still below the minimum training-sample threshold shows "not trained yet" instead of an empty/broken chart.</p>
    <div id="sim-feature-wrap" class="sim-feature-grid"></div>

    <div id="convergence-banner" class="convergence-banner" style="display:none;"></div>
    <div class="round-robin-controls">
      <button id="round-robin-start-btn" title="Starts every agent, one at a time: each runs exactly one round then hands off to the next (wrapping around), instead of all four running at once">Run all (one round each, in turn)</button>
      <button id="round-robin-stop-btn" class="secondary" disabled>Stop running all</button>
      <button id="reset-all-btn" class="danger" title="Wipes EVERY agent's logged simulations, trained models and progress -- a from-scratch restart for the whole population. Cannot be undone.">Restart all training &amp; data</button>
      <span class="muted-inline" id="round-robin-status"></span>
    </div>
    <div id="sim-population-container" class="sim-population-container"></div>
  </div>

  <div class="learning-panel">
    <h3>Automatic Stage-2 verification queue</h3>
    <p class="disclaimer">The moment an agent's find clears a real bar (any <strong>positive ROI</strong>, not dead-skin-flagged, high confidence), it's queued automatically for a live Skinport/<strong>Buff163</strong>/CSFloat cross-check covering <strong>every input skin and every output</strong> of the trade-up -- one at a time, through the same rate-limited path every other live request in this app uses. Pausable if it ever needs to be.</p>
    <div class="learning-status" id="verify-queue-status">loading&hellip;</div>
    <div class="learning-controls">
      <button id="verify-queue-start-btn">Start</button>
      <button id="verify-queue-stop-btn" class="secondary">Pause</button>
    </div>
    <table id="verify-queue-table">
      <thead><tr><th>Agent</th><th>Collection</th><th>Verified ROI %</th><th>Confidence</th><th>When</th><th>Error</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>


  <div class="learning-panel">
    <h3>Log a trade-up outcome <span class="count" id="outcomes-count"></span></h3>
    <p class="disclaimer">Record what actually happened after you executed a trade-up manually (this tool never auto-buys/sells). One line per input wear: <code>skin name:wear:count:unit price paid</code> -- for "wear" you can type either a bracket name (<code>Field-Tested</code>) or the exact float you saw on the listing (e.g. <code>0.18</code>), rounded to 2 decimals; a float is automatically mapped to its wear bracket and the precise value is kept alongside it for training data.</p>
    <form id="outcome-form">
      <label>Collection/case <input type="text" id="oc-collection" required></label>
      <label>Kind <select id="oc-kind"><option value="collection">Collection</option><option value="case">Case</option></select></label>
      <label><input type="checkbox" id="oc-stattrak"> StatTrak</label>
      <label>From rarity
        <select id="oc-from-rarity">
          <option>Consumer Grade</option><option>Industrial Grade</option><option>Mil-Spec Grade</option>
          <option>Restricted</option><option>Classified</option><option>Covert</option>
        </select>
      </label>
      <label>To rarity
        <select id="oc-to-rarity">
          <option>Industrial Grade</option><option>Mil-Spec Grade</option><option>Restricted</option>
          <option>Classified</option><option>Covert</option><option>Extraordinary</option>
        </select>
      </label>
      <label>Currency <select id="oc-currency"></select></label>
      <label class="wide">Input mix (one per line: skin:wear-or-float:count:price)
        <textarea id="oc-input-mix" rows="3" placeholder="AK-47 | Redline:0.18:7:0.42&#10;AK-47 | Redline:0.95:3:0.31" required></textarea>
      </label>
      <label>Filler (optional: skin:wear-or-float:count:price) <input type="text" id="oc-filler" placeholder="blank if none"></label>
      <label>Filler collection <input type="text" id="oc-filler-collection" placeholder="blank if none"></label>
      <label>Output skin <input type="text" id="oc-output-skin" required></label>
      <label>Output float <input type="number" id="oc-output-wear" step="any" min="0" max="1" placeholder="0.18" title="Exact float, 0.00 (cleanest) - 1.00 (worst) -- rounded to 2 decimals and automatically mapped to its wear bracket" required></label>
      <label>Status
        <select id="oc-status"><option value="sold">Sold</option><option value="held">Held</option><option value="kept">Kept</option></select>
      </label>
      <label>Sale price <input type="number" id="oc-sale-price" step="0.01"></label>
      <label>Predicted ROI % (optional) <input type="number" id="oc-predicted-roi" step="0.1"></label>
      <input type="hidden" id="oc-ml-predicted-roi">
      <label class="wide">Notes <input type="text" id="oc-notes"></label>
      <button type="submit">Log outcome</button>
    </form>
    <div id="outcome-form-result"></div>
    <div class="chart-wrap" id="cumulative-profit-wrap" style="display:none;"><canvas id="cumulative-profit-chart"></canvas></div>
    <table id="outcomes-table">
      <thead><tr>
        <th>Logged</th><th>Collection</th><th>From</th><th>To</th><th>Status</th>
        <th>Cost</th><th>Profit</th><th>ROI %</th><th>Win?</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>

<section id="market-intel-section">
  <details class="methodology" id="market-intel-details">
    <summary><h2>Market intelligence &amp; risk registries <span class="count" id="market-intel-badge"></span></h2></summary>
    <div class="methodology-body">
  <p class="disclaimer">Diagnostic registries -- dead/invalid/mispriced skins, wear anomalies, reliability overrides, your permanent blacklist and price overrides. Collapsed by default since these are reference tables you check occasionally, not every session; nothing here was removed, just tucked away. Skins flagged below are temporary scores (reviewed weekly -- never a permanent blacklist, see <code>src/dead_skin.py</code>) and drop off on their own once market activity looks healthy again.</p>
  <h3>Dead skins (thin/unreliable market data) <button class="export-btn collapse-btn" id="collapse-dead-skins-btn" data-target="dead-skins-table,dead-skins-empty">Hide</button></h3>
  <table id="dead-skins-table">
    <thead><tr><th>Skin</th><th>Score</th><th>Reasons</th><th>Last seen</th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="dead-skins-empty" class="empty-state"></div>

  <h3>Mispriced skins (verified over/undervalued) <button class="export-btn collapse-btn" id="collapse-mispriced-skins-btn" data-target="mispriced-skins-table,mispriced-skins-empty">Hide</button></h3>
  <p class="disclaimer">Skins whose Steam price a <em>live</em> Stage-2 verification (the automatic queue, a "Verify prices" button, or "Verify top 50" on the Results page) showed to sit 20%+ away from the cheapest independent market price (Skinport/Buff163/CSFloat). An <strong>overvalued</strong> Steam price inflates a trade-up's expected revenue (fake ROI). While a mark is active, the agents' new simulations of that skin score as unreliable &mdash; excluded from ML training and strict agents' best lists &mdash; so the bots learn to route around it. Marks expire on their own after 24h and clear immediately if a later verification shows the price back in line.</p>
  <button id="mispriced-skins-clear-btn" type="button">Erase mispriced marks</button>
  <table id="mispriced-skins-table">
    <thead><tr><th>Skin</th><th>Direction</th><th>Gap %</th><th>Steam</th><th>Cheapest verified</th><th>State</th><th>Marked</th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="mispriced-skins-empty" class="empty-state"></div>

  <h3>Wear-order anomalies (better condition priced cheaper) <button class="export-btn collapse-btn" id="collapse-wear-anomalies-btn" data-target="wear-anomalies-table,wear-anomalies-empty">Hide</button></h3>
  <p class="disclaimer">A skin's price should never <em>rise</em> as its condition gets worse &mdash; a Field-Tested copy "worth" more than its own Factory New means the higher price is a bad data point (a lone troll listing, or a souvenir-only wear conflated under the plain name), not real revenue. Every positive-ROI scan result now verifies its output prices against their own better wears for free (same bulk price dump, no extra requests); a failed check drops that price from the trade-up's expected revenue and excludes the skin+wear from all scans and strategy searches while the mark is active. Marks expire after 24h and clear immediately when a re-check finds the wear ladder back in order (see <code>src/wear_anomaly.py</code>).</p>
  <button id="wear-anomalies-clear-btn" type="button">Erase wear-anomaly marks</button>
  <table id="wear-anomalies-table">
    <thead><tr><th>Skin</th><th>Flagged price</th><th>Better wear (cheaper)</th><th>Inversion %</th><th>State</th><th>Marked</th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="wear-anomalies-empty" class="empty-state"></div>

  <h3>Market Reliability Engine <button class="export-btn collapse-btn" id="collapse-market-reliability-btn" data-target="market-reliability-table,market-reliability-empty">Hide</button></h3>
  <p class="disclaimer">Unifies the dead-skin score, verified mispriced-skin gap, and a rolling price-history Z-score into one Healthy/Suspicious/Excluded state per skin (see <code>src/market_reliability.py</code>). <strong>Excluded</strong> skins are removed from ML training, ranking's reward score, and a son's best-known list -- never a permanent ban: every skin is re-evaluated from current signals on each recheck, and a manual override always wins over the computed state.</p>
  <table id="market-reliability-table">
    <thead><tr><th>Skin</th><th>State</th><th>Score</th><th>Reasons</th><th>Rolling Z</th><th>Last checked</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="market-reliability-empty" class="empty-state"></div>

  <h3>Invalid skins (no price on any source) <button class="export-btn collapse-btn" id="collapse-invalid-skins-btn" data-target="invalid-skins-table,invalid-skins-empty">Hide</button></h3>
  <p class="disclaimer">Skins where no price could be obtained from Steam <em>or</em> any third-party site (Skinport/CSFloat). These are excluded from trade-up candidate generation entirely, so the agents never propose -- or waste a simulation on -- a trade-up built out of one. Temporary and self-healing: each becomes re-checkable on its own and drops off once it prices again (see <code>src/invalid_skins.py</code>).</p>
  <button id="invalid-skins-clear-btn" type="button">Erase invalid skins</button>
  <table id="invalid-skins-table">
    <thead><tr><th>Skin</th><th>Reason</th><th>State</th><th>Marked</th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="invalid-skins-empty" class="empty-state"></div>

  <h3>Skin blacklist <button class="export-btn collapse-btn" id="collapse-skin-blacklist-btn" data-target="skin-blacklist-table,skin-blacklist-empty">Hide</button></h3>
  <p class="disclaimer">Unlike every self-healing registry above, this list <strong>never re-checks a skin and never revives an entry on its own</strong> -- it changes only when you add or remove entries (see <code>src/user_overrides.py</code>). A blacklisted skin is banned everywhere, as an input <em>and</em> as a possible output: agents stop proposing trade-ups with it, the scans skip it, the manual builder rejects it, and every existing trade-up opportunity containing it is removed immediately. Use it for skins whose price data you've personally confirmed is garbage -- e.g. the one behind an absurd ROI figure.</p>
  <p class="disclaimer"><strong>Duration</strong> defaults to <em>Forever</em>: the entry stands until you delete it. Pick a number of days instead when you only want the skin gone while a bad price works itself out -- the entry then stops applying and drops off this list by itself, and nothing re-checks the skin in the meantime. Blacklisting a skin that is already listed replaces its duration, so you can extend one or make it permanent by adding it again.</p>
  <form id="skin-blacklist-form" autocomplete="off">
    <label class="wide" style="position:relative;">Skin name
      <input type="text" id="bl-skin" placeholder="Start typing a skin name...">
      <div id="bl-suggestions" class="suggestions" style="display:none;"></div>
    </label>
    <label>Duration
      <select id="bl-duration" title="Forever = the entry stands until you remove it (the default). Any other choice makes it stop applying, and drop off the list, on its own after that long.">
        <option value="" selected>Forever</option>
        <option value="1">1 day</option>
        <option value="7">7 days</option>
        <option value="30">30 days</option>
        <option value="90">90 days</option>
      </select>
    </label>
    <label class="wide">Note (optional)
      <input type="text" id="bl-note" placeholder="why, e.g. bogus mirror price">
    </label>
    <button type="submit">Blacklist skin</button>
  </form>
  <div id="skin-blacklist-msg" class="empty-state" style="display:none;"></div>
  <table id="skin-blacklist-table">
    <thead><tr><th>Skin</th><th>Note</th><th>Added</th><th>Expires</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="skin-blacklist-empty" class="empty-state"></div>

  <h3>Manual price overrides <button class="export-btn collapse-btn" id="collapse-price-overrides-btn" data-target="price-overrides-table,price-overrides-empty">Hide</button></h3>
  <p class="disclaimer">Set your own price for a specific skin+wear -- it <strong>wins over every other source</strong> (price cache, bulk mirror, live fetches) until you remove it, and every existing trade-up opportunity containing the skin has its cost/revenue/profit/<strong>ROI recomputed</strong> with your number, so a Min-ROI filter catches bugged trade-ups immediately. Rows repriced this way carry a "repriced" badge in the opportunities table. Permanent like the blacklist: only you change it (see <code>src/user_overrides.py</code>).</p>
  <form id="price-override-form" autocomplete="off">
    <label class="wide" style="position:relative;">Skin name
      <input type="text" id="po-skin" placeholder="Start typing a skin name...">
      <div id="po-suggestions" class="suggestions" style="display:none;"></div>
    </label>
    <label>Wear
      <select id="po-wear">
        <option>Factory New</option><option>Minimal Wear</option><option selected>Field-Tested</option>
        <option>Well-Worn</option><option>Battle-Scarred</option>
      </select>
    </label>
    <label><input type="checkbox" id="po-stattrak"> StatTrak</label>
    <label>Price
      <input type="number" id="po-price" min="0.01" step="0.01" placeholder="e.g. 1500.00">
    </label>
    <label>Currency <select id="po-currency"></select></label>
    <button type="submit">Set price</button>
  </form>
  <div id="price-override-msg" class="empty-state" style="display:none;"></div>
  <table id="price-overrides-table">
    <thead><tr><th>Market name</th><th>Price</th><th>Currency</th><th>Added</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
  <div id="price-overrides-empty" class="empty-state"></div>
    </div>
  </details>
</section>

<section id="price-history-section">
  <h2>Skin price history &amp; prediction</h2>
  <p class="disclaimer">Steam's public API has no history endpoint -- this only shows what this tool has itself recorded since you started using this feature (not retroactive), plus a naive linear-trend extrapolation once there's enough data. Not financial advice.</p>
  <form id="price-history-form" autocomplete="off">
    <label class="wide" style="position:relative;">Skin name
      <input type="text" id="ph-skin" placeholder="Start typing a skin name..." required>
      <div id="ph-suggestions" class="suggestions" style="display:none;"></div>
    </label>
    <label>Wear
      <select id="ph-wear">
        <option>Factory New</option><option>Minimal Wear</option><option selected>Field-Tested</option>
        <option>Well-Worn</option><option>Battle-Scarred</option>
      </select>
    </label>
    <label><input type="checkbox" id="ph-stattrak"> StatTrak</label>
    <label>Currency <select id="ph-currency"></select></label>
    <button type="submit">Look up</button>
  </form>
  <div id="ph-result" style="display:none;">
    <div class="ph-header">
      <img id="ph-image" class="skin-thumb-large" style="display:none;">
      <span id="ph-name"></span>
    </div>
    <div class="chart-wrap"><canvas id="price-history-chart"></canvas></div>
    <div id="ph-prediction" class="learning-status"></div>
  </div>
</section>

</div><!-- /view-ml-lab -->`,
  patterns: `<div class="legacy" id="view-patterns">

<section id="patterns-section">
  <h2>Rare Pattern Opportunities <span class="muted-inline">(Collector Arbitrage -- independent from the trade-up optimizer)</span></h2>
  <details class="methodology">
    <summary>How this works</summary>
    <div class="methodology-body">
  <p class="disclaimer">Scans <strong>Steam Market listings directly</strong> (the only listing source -- no third-party listings API involved) for items whose seller priced a <strong>documented collector pattern</strong> (Blue Gems, Case Hardened tiers, Heat Treated seeds, Doppler phases, rare fades, Katowice/Crown sticker crafts&hellip;). <strong>Every matched listing is shown here, profitable or not</strong> -- nothing is hidden behind a hardcoded profit bar; use the filters and "Sort by" below (expected profit, price, confidence&hellip;) to find the ones worth buying. Detection is <strong>knowledge-driven, never ML-driven</strong>: a listing only ever becomes an opportunity by matching the versioned Pattern Knowledge Database below (public collector consensus, every entry links its source) -- no model can invent a pattern. Valuation is <strong>Steam-first</strong>: the base price a pattern's premium multiplies is the skin's own Steam median-sale price when it exists, and only falls back to the Steam/Skinport/Buff163 consensus (with confidence demoted) when Steam has no sale evidence. Each listing's float + paint seed are <strong>decoded from its inspect link locally</strong> -- no API call, no key, no rate limit (modern CS2 inspect links carry the item data themselves; this is the same trick the fast market extensions use). <strong>Honest caveat:</strong> Steam rate-limits anonymous listing requests hard -- often refusing the very first one -- so a scan can spend real time sitting out cooldowns rather than finding listings. That render call is now the scanner's <em>only</em> network cost, so saving your Steam session cookie under <a href="#connections">Connections</a> is the single biggest thing you can do for scan speed; the scanner status below reports every cooldown plainly rather than hiding it. As always: read-only, nothing is ever bought -- verify float/seed on the listing itself before spending money.</p>
    </div>
  </details>

  <div class="learning-panel">
    <h3>Marketplace scanner</h3>
    <div class="learning-status" id="patterns-scan-status">loading&hellip;</div>
    <div class="learning-controls">
      <button id="patterns-scan-start-btn">Start scanning</button>
      <button id="patterns-scan-pause-btn" class="secondary">Pause</button>
    </div>
  </div>

  <h3>Live opportunities <span class="count" id="patterns-opps-count"></span>
    <span class="view-toggle" role="group" aria-label="Result view">
      <button id="pat-view-cards" class="view-toggle-btn is-active" aria-pressed="true">Cards</button>
      <button id="pat-view-table" class="view-toggle-btn" aria-pressed="false">Table</button>
    </span>
    <button class="export-btn" id="export-patterns-btn">Export CSV</button>
  </h3>
  <div class="opportunity-filters">
    <label>Weapon <input type="text" id="pat-weapon" placeholder="e.g. AK-47"></label>
    <label>Pattern type
      <select id="pat-type">
        <option value="">any</option>
        <option value="paint_seed">Paint seed (Blue Gem / Case Hardened / Heat Treated...)</option>
        <option value="paint_index">Paint index (Doppler phases / Ruby / Sapphire...)</option>
        <option value="sticker_craft">Sticker craft (Katowice / Crown...)</option>
      </select>
    </label>
    <label>Marketplace
      <select id="pat-marketplace"><option value="">any</option><option value="steam">Steam Market</option></select>
    </label>
    <label>Min expected profit <input type="number" id="pat-min-profit" step="1" placeholder="any"></label>
    <label>Min confidence
      <select id="pat-min-confidence"><option value="">any</option><option value="low">low+</option><option value="medium">medium+</option><option value="high">high</option></select>
    </label>
    <label>Float min <input type="number" id="pat-min-float" step="0.01" min="0" max="1" placeholder="0"></label>
    <label>Float max <input type="number" id="pat-max-float" step="0.01" min="0" max="1" placeholder="1"></label>
    <label>Pattern tier
      <select id="pat-tier"><option value="">any</option><option value="1">Tier 1</option><option value="2">Tier 2</option><option value="3">Tier 3</option></select>
    </label>
    <label>Sort by
      <select id="pat-sort">
        <option value="score">Best opportunity</option>
        <option value="profit">Expected profit</option>
        <option value="price">Price (cheapest first)</option>
        <option value="premium">Est. collector value</option>
        <option value="confidence">Confidence</option>
        <option value="newest">Newly listed</option>
      </select>
    </label>
  </div>
  <div id="patterns-cards" class="pattern-cards"></div>
  <table id="patterns-table" hidden>
    <thead><tr>
      <th>Item</th><th>Pattern</th><th class="num">Float</th><th class="num">Seed / Index</th>
      <th class="num">Ask</th><th class="num">Est. collector value</th><th class="num">Est. profit</th>
      <th class="num">Edge %</th><th>Confidence</th><th>Marketplace</th><th>Listing</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <div id="patterns-empty" class="empty-state" style="display:none;">No opportunities match. If the scanner has never run, start it above. Scans Steam Market listings directly, no API key required. If the status line above shows repeated rate-limit cooldowns, save your Steam session cookie under <a href="#connections">Connections</a> -- anonymous scanning is throttled hard by Steam.</div>
</section>

<section id="pattern-db-section">
  <h2>Pattern Knowledge Database <span class="muted-inline">(centralized, versioned -- nothing hardcoded)</span></h2>
  <p class="disclaimer">The single source of truth the scanner matches against. Seeded from documented public collector resources (every entry links its source), re-verified automatically every 24h (new knowledge shipped with code updates merges in without overwriting local edits; every change is versioned in <code>data/pattern_db_history.jsonl</code>). Live scraping of CSBlueGem/FloatDB is <strong>not</strong> wired up -- no stable public API -- so tier/premium changes from the community land via updates to the seed knowledge or your own edits; the status line below says exactly this rather than pretending otherwise.</p>
  <div class="learning-status" id="pattern-db-status">loading&hellip;</div>
  <div class="learning-controls">
    <button id="patterns-refresh-btn" class="secondary" title="Re-validate the live database against the bundled seed knowledge now (also runs automatically every 24h)">Refresh knowledge now</button>
    <span id="patterns-refresh-result" class="muted-inline"></span>
  </div>
  <table id="pattern-db-table">
    <thead><tr>
      <th>Pattern</th><th>Weapon</th><th>Skin</th><th>Type</th><th class="num">Tier</th>
      <th class="num">Premium &times;</th><th>Confidence</th><th>Float req.</th><th>Source</th><th>Notes</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</section>

</div><!-- /view-patterns -->`,
  connections: `<div class="legacy" id="view-connections">

<section id="connections-section">
  <h2>Connections</h2>
  <p class="disclaimer">API keys and integrations this dashboard talks to. Everything below is stored locally in <code>connections.local.json</code> at the project root -- deliberately kept outside <code>data/</code> so it's never swept into the GitHub backup archive below or pushed anywhere.</p>

  <div class="learning-panel" id="csfloat-panel">
    <h3>CSFloat API key <span class="conn-badge" id="csfloat-badge">&hellip;</span></h3>
    <p class="disclaimer">Optional, free (csfloat.com). <strong>The Rare Patterns scanner no longer needs this at all</strong> -- float and paint seed are decoded from the inspect link locally, with no API call. It is still used for third-party price verification elsewhere in the app. Leaving it blank works fine. To speed up rare-pattern scanning, set the <strong>Steam session</strong> below instead -- that is the real bottleneck.</p>
    <form id="csfloat-config-form" class="learning-controls" autocomplete="off">
      <label>API key <input type="password" id="csfloat-key-input" placeholder="paste key, or leave blank to keep current" size="34"></label>
      <button type="submit">Save</button>
    </form>
    <div id="csfloat-config-msg" class="empty-state" style="display:none;"></div>
    <div class="learning-status" id="csfloat-status"></div>
  </div>

  <div class="learning-panel" id="steam-session-panel">
    <h3>Steam session <span class="conn-badge" id="steam-session-badge">&hellip;</span> <span class="muted-inline">(raises the Rare Patterns scan rate limit)</span></h3>
    <p class="disclaimer"><strong>This is the single biggest lever on scan speed.</strong> Steam rate-limits <em>anonymous</em> listing requests hard -- often refusing the very first one -- so without this the Rare Patterns scanner spends most of its time sitting out cooldowns instead of finding listings. Sending your own browser's Steam session cookie gets the same friendlier limit any logged-in browser gets.</p>
    <p class="disclaimer warn-strong"><strong>Read this before pasting.</strong> <code>steamLoginSecure</code> is <strong>not an API key -- it is your Steam login session</strong>. Anyone who holds it can act as your Steam account on Steam's web endpoints without your password or Steam Guard. Only ever paste it into software you run yourself, like this dashboard on your own machine. It is stored in <code>connections.local.json</code> (gitignored, and deliberately kept outside <code>data/</code> so the 6-hourly GitHub backup below can never push it), is never written to logs, and is never sent back to this page in full -- only the last 4 characters. <strong>If you ever share your screen, a screenshot, or this repo, clear it first.</strong> It expires on its own when you log out of Steam in your browser; clicking "Log out of all devices" on Steam invalidates it immediately.</p>
    <p class="disclaimer easiest-way"><strong>Easiest way &mdash; one click, no copy-paste:</strong> install the bundled helper extension in <code>extension/</code>, then click its icon while logged into Steam. It reads the cookie for you and re-syncs itself whenever Steam rotates it. <em>Why an extension?</em> <code>steamLoginSecure</code> is HttpOnly, so no page script or bookmarklet can read it &mdash; devtools or an extension are the only two options. It is <strong>not</strong> a Steam login: it never sees your password.</p>
    <details class="methodology">
      <summary>Install the helper extension (recommended)</summary>
      <div class="methodology-body">
        <ol class="howto-list">
          <li>Open <code>chrome://extensions</code> (Edge: <code>edge://extensions</code>).</li>
          <li>Turn on <strong>Developer mode</strong>, top right.</li>
          <li>Click <strong>Load unpacked</strong> and pick the <code>extension/</code> folder in this project.</li>
          <li>Pin the green gem icon, make sure you're logged into <a href="https://steamcommunity.com/market/" target="_blank" rel="noopener noreferrer">steamcommunity.com</a>, and click it.</li>
          <li>A green <strong>OK</strong> badge means done &mdash; this panel will show the masked value. Full notes in <code>extension/README.md</code>.</li>
        </ol>
      </div>
    </details>
    <details class="methodology">
      <summary>Or paste it manually (Chrome / Edge / Firefox)</summary>
      <div class="methodology-body">
        <ol class="howto-list">
          <li>Open <a href="https://steamcommunity.com/market/" target="_blank" rel="noopener noreferrer">steamcommunity.com/market</a> and make sure you're logged in.</li>
          <li>Press <kbd>F12</kbd> to open developer tools.</li>
          <li>Go to <strong>Application</strong> (Chrome/Edge) or <strong>Storage</strong> (Firefox) &rarr; <strong>Cookies</strong> &rarr; <code>https://steamcommunity.com</code>.</li>
          <li>Find the row named <code>steamLoginSecure</code> and copy its <strong>Value</strong>.</li>
          <li>Paste it below. Pasting the whole <code>steamLoginSecure=...</code> pair works too.</li>
        </ol>
      </div>
    </details>
    <form id="steam-session-form" class="learning-controls" autocomplete="off">
      <label>steamLoginSecure <input type="password" id="steam-session-input" placeholder="paste value, or leave blank to keep current" size="34" autocomplete="off"></label>
      <button type="submit">Save</button>
      <button type="button" id="steam-session-clear-btn" class="secondary" title="Remove the stored session cookie from connections.local.json">Clear</button>
    </form>
    <div id="steam-session-msg" class="empty-state" style="display:none;"></div>
    <div class="learning-status" id="steam-session-status"></div>
  </div>

  <div class="learning-panel" id="github-backup-panel">
    <h3>GitHub backup <span class="conn-badge" id="backup-badge">&hellip;</span> <span class="muted-inline">(delta-aware, every 6h)</span></h3>
    <p class="disclaimer">A background worker wakes every 6 hours, SHA-256-hashes the mutable telemetry files (<code>outcomes.jsonl</code>, <code>price_history.jsonl</code>, <code>dead_skin_scores.json</code>) and compares them against the last successful backup. Unchanged &rarr; logs "No telemetry mutations detected" and sleeps. Changed &rarr; wraps <code>data/</code> into <code>archive_YYYYMMDD_HHMMSS_[skins]skins_[outcomes]outcomes.zip</code> via the same export-data pipeline as the Simulation Lab's export button, then commits + pushes it to the git repository configured below. Point <code>repo_dir</code> at a local clone of your backup GitHub repo (this project folder itself isn't a git repo). A failed push still commits locally and retries next cycle -- no archive is ever lost.</p>
    <div class="learning-status" id="backup-status">loading&hellip;</div>
    <form id="backup-config-form" class="learning-controls">
      <label><input type="checkbox" id="backup-enabled"> Enabled</label>
      <label>Local clone path <input type="text" id="backup-repo-dir" placeholder="e.g. C:\\Users\\me\\cs2-backups" size="34"></label>
      <label>Remote <input type="text" id="backup-remote" placeholder="origin" size="8"></label>
      <label>Branch <input type="text" id="backup-branch" placeholder="main" size="8"></label>
      <button type="submit">Save config</button>
      <button type="button" id="backup-run-now-btn" class="secondary" title="Runs a full archive + commit + push cycle immediately, skipping the delta check">Run backup now</button>
    </form>
    <div id="backup-run-result" class="muted-inline"></div>
    <table id="backup-log-table">
      <thead><tr><th>Event</th><th>Archive / message</th><th>When</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="learning-panel" id="ntfy-panel">
    <h3>NTFY push alerts <span class="conn-badge" id="ntfy-badge">&hellip;</span></h3>
    <p class="disclaimer">Free phone push notifications (<a href="https://ntfy.sh" target="_blank" rel="noopener">ntfy.sh</a> -- no account needed) the moment a high-ROI trade-up or underpriced rare-pattern listing turns up, without keeping the dashboard open. Install the ntfy app, pick a private hard-to-guess topic name, subscribe to it in the app, then enter the same topic below. Checked every 5 minutes against data the dashboard has already scanned -- no extra Steam/CSFloat requests.</p>
    <form id="ntfy-config-form" class="learning-controls" autocomplete="off">
      <label><input type="checkbox" id="ntfy-enabled"> Enabled</label>
      <label>Topic <input type="text" id="ntfy-topic" placeholder="e.g. cs2-alerts-x7f2q" size="20"></label>
      <label>Server <input type="text" id="ntfy-server" placeholder="https://ntfy.sh" size="20"></label>
      <label>Min trade-up ROI % <input type="number" id="ntfy-roi-threshold" step="1" value="100"></label>
      <label>Min pattern edge % <input type="number" id="ntfy-pattern-threshold" step="1" value="100"></label>
      <button type="submit">Save</button>
      <button type="button" id="ntfy-test-btn" class="secondary">Send test notification</button>
    </form>
    <div id="ntfy-config-msg" class="empty-state" style="display:none;"></div>
  </div>
</section>

</div><!-- /view-connections -->`,
};

export async function renderLegacy(root, view) {
  root.innerHTML = MARKUP[view] || `<div class="legacy">Unknown legacy view "${view}".</div>`;
  await loadOldApp();
  // /app.js's applyView() runs on load and on its stale hashchange listener, and sets
  // hidden=true on #view-ml-lab whenever the route is "lab" (its own key is "ml-lab") -- which
  // blanks the LAB screen. The router owns visibility now. Do this BEFORE the loaders so a
  // slow/hanging loader can't leave the screen blank.
  const show = () => root.querySelectorAll(".legacy").forEach((el) => { el.hidden = false; });
  show();
  for (const fn of (LEGACY_LOADERS[view] || [])) {
    try { await window[fn]?.(); } catch (e) { console.warn("legacy loader", fn, e); }
  }
  try { window.wireCopyButtons?.(root); } catch (e) { /* copy-buttons are cosmetic */ }
  show();
}
