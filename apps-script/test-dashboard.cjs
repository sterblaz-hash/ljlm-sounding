// Offline checks: node apps-script/test-dashboard.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const read = path => JSON.parse(fs.readFileSync(path, 'utf8'));
const backend = vm.createContext({});
vm.runInContext(fs.readFileSync('apps-script/Code.gs', 'utf8'), backend);
backend.PropertiesService = {getScriptProperties: () => ({getProperty: key => { assert.equal(key,'CARTO_BASEMAP_KEY'); return null; }})};
const fetched = [], cached = new Map();
backend.fetchJson_ = path => { fetched.push(path); return read(path); };
backend.CacheService = {getScriptCache: () => ({get: key => cached.get(key), put: (key, value) => cached.set(key, value)})};
backend.Utilities = {newBlob: text => ({getBytes: () => Buffer.from(text)})};
for (const key of ['7d', '30d', '90d', '1y']) {
  const result = backend.getDashboardTimeseries(key);
  assert.equal(result.records.length, read(`timeseries/ljlm/latest_${key}.json`).record_count);
  assert.equal(result.variables.length, 37);
  const count = fetched.length;
  backend.getDashboardTimeseries(key);
  assert.equal(fetched.length, count + 1); // manifest only; series from cache
}
for (const key of ['../data/latest.json', 'latest', '__proto__', null]) {
  const count = fetched.length;
  assert.throws(() => backend.getDashboardTimeseries(key));
  assert.equal(fetched.length, count);
}
backend.fetchJson_ = () => ({timeseries: {available: true, windows: {'7d': 'data/latest.json'}}});
assert.throws(() => backend.getDashboardTimeseries('7d'));
backend.fetchJson_ = path => read(path);
const latest = read('data/latest.json');
const dashboard = backend.buildDashboardView_(latest, null, null, 'data/latest.json', true);
assert.equal(dashboard.additionalDiagnostics.find(m => m.label === 'Lifted Index').value,
  latest.parameters.metpy.lifted_index_c);
assert.equal(dashboard.moistureTransport.humidity.find(m => m.label === 'q700').value,
  latest.parameters.metpy.moisture_transport.humidity_profile['700'].specific_humidity_gkg);
assert.equal(backend.buildClimatology_({parameters: {lifted_index_c: {value: -3, percentile: 5}}})[0].value, -3);

const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML: '', textContent: '', classList: {toggle() {}}, querySelectorAll: () => [], addEventListener() {}});
  return elements.get(id);
};
const calls = [];
let success, failure;
const runner = {
  withSuccessHandler(handler) { success = handler; return this; },
  withFailureHandler(handler) { failure = handler; return this; },
  getDashboardData() { calls.push({type: 'sounding', success}); },
  getDashboardTimeseries(key) { calls.push({type: 'series', key, success, failure}); }
};
const browser = vm.createContext({document: {getElementById: element, querySelectorAll: () => []}, google: {script: {run: runner}}});
const html = fs.readFileSync('apps-script/Index.html', 'utf8');
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], browser);
assert.equal(calls.length, 1);
assert.equal(calls[0].type, 'sounding'); // no initial timeseries fetch
browser.showTimeseriesView();
browser.showTimeseriesView();
assert.equal(calls.length, 2); // deduplicate in-flight requests
const first = calls[1];
const response = backend.getDashboardTimeseries('7d');
first.success(response);
assert.match(element('potekChart').innerHTML, /<svg/);
assert.equal(vm.runInContext('Object.keys(timeseriesDefinitions).length', browser), response.variables.length);
for (const key of response.variables) {
  vm.runInContext(`timeseriesParameter = '${key}'; renderTimeseries_()`, browser);
  assert.doesNotMatch(element('potekChart').innerHTML, /NaN|undefined/);
}
browser.setMainView_('sounding');
browser.showTimeseriesView();
assert.equal(calls.length, 2); // browser window cache
browser.loadTimeseriesWindow_('30d');
const request30 = calls[2];
browser.loadTimeseriesWindow_('90d');
const before = element('app').innerHTML;
request30.success(backend.getDashboardTimeseries('30d'));
assert.equal(element('app').innerHTML, before); // stale response does not replace active window
browser.setMainView_('timeseries');
calls[0].success(dashboard);
assert.equal(element('app').innerHTML, before); // initial sounding response cannot replace Potek
const points = browser.timeseriesPoints_([
  {valid_time: '2026-10-02T00:00:00Z', value_signed_fraction: null},
  {valid_time: '2026-10-01T12:00:00Z', value_signed_fraction: -0.4, processed_at: '2099-01-01'},
  {valid_time: '2026-10-02T12:00:00Z', value_signed_fraction: 0}
], 'value_signed_fraction');
assert.equal(points[0].value, -40);
assert.equal(points[1].value, null);
assert.equal(points[2].value, 0);
assert.equal(points[0].time, Date.parse('2026-10-01T12:00:00Z'));
console.log('Dashboard backend and browser regression checks passed.');
// A null sample and an absent 12-hour term must each break the line.
browser.setMainView_('timeseries');
vm.runInContext(`
  timeseriesWindow = '7d'; timeseriesParameter = 'pwat_mm';
  timeseriesCache['7d'] = {
    variables: ['pwat_mm'], startTime: '2026-10-01T00:00:00Z', endTime: '2026-10-03T12:00:00Z',
    records: [
      {valid_time: '2026-10-01T00:00:00Z', pwat_mm: -3},
      {valid_time: '2026-10-01T12:00:00Z', pwat_mm: null},
      {valid_time: '2026-10-02T00:00:00Z', pwat_mm: 0},
      {valid_time: '2026-10-02T12:00:00Z', pwat_mm: 4},
      {valid_time: '2026-10-03T12:00:00Z', pwat_mm: 5}
    ]
  }; renderTimeseries_();
`, browser);
const path = element('potekChart').innerHTML.match(/<path d="([^"]*)"/)[1];
assert.equal((path.match(/M/g) || []).length, 3);
assert.equal((path.match(/L/g) || []).length, 1);
vm.runInContext("timeseriesCache['7d'].records = []; renderTimeseries_();", browser);
assert.match(element('potekChart').textContent, /No data/);
console.log('Gap and empty-window regression checks passed.');

// Signed cross-barrier values drive the sounding cards, never clipped upslope.
for (const [signed, fraction] of [[-120, -0.4], [80, 0.25], [0, 0], [null, null]]) {
  const layer = {
    available: true, magnitude_kg_m1_s1: 300,
    signed_cross_barrier_kg_m1_s1: signed, signed_fraction: fraction,
    upslope_kg_m1_s1: 999, upslope_fraction_pct: 99,
    angle_to_upslope_normal_deg: 135, top_pressure_hpa: 850
  };
  const metpy = {moisture_transport: {orographic_cross_barrier: {
    available: true, version: 3, barriers: {dinaric_west: {
      label: 'Test barrier', upslope_normal_azimuth_deg: 45,
      representative_barrier_height_msl_m: 1500, terrain_capped_ivt: layer
    }}
  }}};
  const block = backend.buildOrographicTransport_(metpy);
  const row = block.rows[0];
  assert.equal(row.signed, signed);
  assert.equal(row.fractionPct, fraction === null ? null : fraction * 100);
  assert.equal(row.upslope, 999); // compatibility field only
  const card = browser.orographicTransportHtml({orographicTransport: block});
  assert.match(card, /cross-barrier IVT⊥/);
  assert.doesNotMatch(card, /clipped|upslope IVT⊥|999|99%/);
  assert.match(card, /Test barrier/);
  assert.match(card, /1500 m MSL/);
  assert.match(card, /45°/);
  assert.match(card, /135°/);
  assert.match(card, /850 hPa/);
  if (signed === -120) {
    assert.match(card, /signed-negative/);
    assert.match(card, /-120/);
    assert.match(card, /-40%/);
  }
  if (fraction === null) assert.match(card, /signed fraction: —/);
  // Old profiles without signed_fraction must show no fraction, not the clipped field.
  delete layer.signed_fraction;
  assert.equal(backend.buildOrographicTransport_(metpy).rows[0].fractionPct, null);
}
console.log('Signed orographic card regression checks passed.');

// Annual climatology backend validation, mappings and real compact curves.
const climateFetches = [];
backend.fetchJson_ = path => { climateFetches.push(path); return read(path); };
for (const key of ['../latest', 'q700_gkg', 'sbcape_jkg', '__proto__']) {
  const start = climateFetches.length;
  assert.throws(() => backend.getDashboardClimatology(key));
  assert.ok(climateFetches.slice(start).every(path =>
    ['data/dashboard_manifest.json', 'climatology/dashboard/index.json'].includes(path)));
}
const annualIndex = read('climatology/dashboard/index.json');
const annualResponses = new Map();
for (const definition of annualIndex.parameters) {
  const response = backend.getDashboardClimatology(definition.key);
  annualResponses.set(definition.key, response);
  assert.equal(response.days.length, 366);
  for (const day of response.days) {
    const values = ['p10', 'p25', 'p50', 'p75', 'p90'].map(key => day[key]);
    if (values.every(v => v !== null)) {
      assert.ok(values.every((v, index) => index === 0 || values[index - 1] <= v));
    }
  }
}
assert.equal(annualResponses.get('t850').currentTime, latest.nominal_date + 'T' + latest.term + ':00:00Z');
assert.equal(annualResponses.get('ivt_kg_m_s').currentValue,
  latest.parameters.metpy.moisture_transport.ivt.magnitude_kg_m1_s1);
assert.equal(annualResponses.get('wind_speed_850_ms').currentValue, latest.parameters.metpy.standard_winds['850'].speed_ms);
assert.equal(annualResponses.get('thickness_1000_500_m').currentValue, null);
assert.equal(backend.climatologyObservationValue_({}, 't850'), null);
const fetchCount = climateFetches.filter(p => p === 'climatology/dashboard/t850.json').length;
backend.getDashboardClimatology('t850');
assert.equal(climateFetches.filter(p => p === 'climatology/dashboard/t850.json').length, fetchCount);
runner.getDashboardClimatology = key => { calls.push({type: 'climatology', key, success, failure}); };
const annualBrowser = vm.createContext({document: {getElementById: element, querySelectorAll: () => []}, google: {script: {run: runner}}});
const beforeInit = calls.length;
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], annualBrowser);
assert.equal(calls.length, beforeInit + 1);
assert.equal(calls.at(-1).type, 'sounding');
annualBrowser.showClimatologyView();
const firstAnnual = calls.at(-1);
assert.equal(firstAnnual.type, 'climatology');
assert.equal(firstAnnual.key, 't850');
const afterFirst = calls.length;
annualBrowser.showClimatologyView();
assert.equal(calls.length, afterFirst); // pending deduplication
firstAnnual.success(annualResponses.get('t850'));
assert.match(element('annualChart').innerHTML, /data-annual-obs/);
assert.match(element('annualChart').innerHTML, new RegExp(latest.nominal_date));
annualBrowser.setMainView_('timeseries');
annualBrowser.showClimatologyView();
assert.equal(calls.length, afterFirst); // browser cache
for (const definition of annualIndex.parameters) {
  annualBrowser.loadAnnualParameter_(definition.key);
  if (definition.key !== 't850') calls.at(-1).success(annualResponses.get(definition.key));
  assert.doesNotMatch(element('annualChart').innerHTML, /NaN|undefined/);
  if (definition.key === 'thickness_1000_500_m') assert.doesNotMatch(element('annualChart').innerHTML, /data-annual-obs/);
}
// Use uncached keys to exercise out-of-order completion without changing data fixtures.
vm.runInContext("delete annualCache.t700; delete annualCache.t500;", annualBrowser);
annualBrowser.loadAnnualParameter_('t700');
const late = calls.at(-1);
annualBrowser.loadAnnualParameter_('t500');
const newer = calls.at(-1);
newer.success(annualResponses.get('t500'));
const rendered = element('app').innerHTML;
late.success(annualResponses.get('t700'));
assert.equal(element('app').innerHTML, rendered);
vm.runInContext("delete annualCache.pwat_mm;", annualBrowser);
annualBrowser.loadAnnualParameter_('pwat_mm');
const pendingAnnual = calls.at(-1);
annualBrowser.setMainView_('sounding');
element('app').innerHTML = 'Sounding';
pendingAnnual.success(annualResponses.get('pwat_mm'));
assert.equal(element('app').innerHTML, 'Sounding');
assert.equal(annualBrowser.annualDay_('02-29'), 59);
assert.equal(annualBrowser.annualDay_('03-01'), 60);
assert.equal(annualBrowser.annualDay_('02-30'), null);
assert.equal(annualBrowser.annualSegments_([
  {day: '02-28', p50: 1}, {day: '02-29', p50: null}, {day: '03-01', p50: 3}
], ['p50']).length, 2);
console.log('Annual climatology regression checks passed for all 25 parameters.');

// Exactly matching overlay metadata is optional and must never disable comparison.
const observed = read('data/latest.json');
const forecast = read('models/icon-d2/latest/' + observed.term + '/sounding.json');
const overlayManifest = {
  observation_sounding_id: observed.sounding_id, model_sounding_id: forecast.sounding_id,
  valid_time: forecast.valid_time, lead_hours: 12, rendered_at: '2026-10-05T02:00:00Z',
  skewt_overlay: 'verification/icon-d2/latest/' + observed.term + '/skewt_overlay.png'
};
backend.fetchJsonOptional_ = path => path.startsWith('verification/') ? null : read(path);
backend.fetchJson_ = path => read(path);
const noOverlay = backend.getSoundingComparison();
assert.equal(noOverlay.available, true);
assert.equal(noOverlay.overlay, null);
for (const field of ['observation_sounding_id', 'model_sounding_id', 'valid_time', 'lead_hours']) {
  backend.fetchJsonOptional_ = path => path.startsWith('verification/')
    ? {...overlayManifest, [field]: field === 'lead_hours' ? 6 : 'stale'} : read(path);
  assert.equal(backend.getSoundingComparison().overlay, null);
}
backend.fetchJsonOptional_ = path => path.startsWith('verification/') ? overlayManifest : read(path);
const withOverlay = backend.getSoundingComparison();
assert.match(withOverlay.overlay.url, /skewt_overlay.png\?v=2026-10-05/);
const overlayBrowser = vm.createContext({document: {getElementById: element, querySelectorAll: () => []}, google: {script: {run: runner}}});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], overlayBrowser);
overlayBrowser.setMainView_('comparison');
overlayBrowser.renderComparison(withOverlay);
assert.equal((element('app').innerHTML.match(/<img /g) || []).length, 1);
assert.match(element('app').innerHTML, /comparison-overlay/);
for (const mode of ['full', 'zoom', 'thetae', 'hodograph']) {
  vm.runInContext(`currentComparePlot = '${mode}'`, overlayBrowser);
  overlayBrowser.renderComparison(withOverlay);
  assert.equal((element('app').innerHTML.match(/<img /g) || []).length, 2);
}
vm.runInContext("currentComparePlot = 'overlay'", overlayBrowser);
overlayBrowser.renderComparison(noOverlay);
assert.equal((element('app').innerHTML.match(/<img /g) || []).length, 2);
overlayBrowser.setMainView_('climatology');
element('app').innerHTML = 'Annual';
overlayBrowser.renderComparison(withOverlay);
assert.equal(element('app').innerHTML, 'Annual');
console.log('Optional overlay metadata and comparison layout checks passed.');

// Potek heatmap uses calendar-day compact references and shares browser caches.
const heatElements = new Map();
const heatElement = id => {
  if (!heatElements.has(id)) heatElements.set(id, {
    innerHTML: '', textContent: '', classList: {toggle() {}},
    querySelectorAll(selector) {
      const attribute = selector === '[data-heat-cell]' ? 'data-heat-cell' : selector === '[data-point]' ? 'data-point' : null;
      if (!attribute) return [];
      this.nodes = [...this.innerHTML.matchAll(new RegExp('<(?:button|circle)[^>]*' + attribute + '="(\\d+)"[^>]*>', 'g'))].map(match => ({
        dataset: {heatCell: match[1]},
        getAttribute: name => (match[0].match(new RegExp(name + '="([^"]*)"')) || [])[1]
      }));
      return this.nodes;
    }
  });
  return heatElements.get(id);
};
const heatCalls = [];
let heatSuccess, heatFailure;
const heatRunner = {
  withSuccessHandler(handler) { heatSuccess = handler; return this; },
  withFailureHandler(handler) { heatFailure = handler; return this; },
  getDashboardData() {},
  getDashboardTimeseries(key) { heatCalls.push({type: 'series', key, success: heatSuccess, failure: heatFailure}); },
  getDashboardClimatology(key) { heatCalls.push({type: 'climate', key, success: heatSuccess, failure: heatFailure}); }
};
const heatBrowser = vm.createContext({document: {getElementById: heatElement, querySelectorAll: () => []}, google: {script: {run: heatRunner}}});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], heatBrowser);
heatBrowser.showTimeseriesView();
heatCalls[0].success(response);
assert.equal(heatCalls.length, 1); // Graf never requests climate
const lineMarkup = heatElement('potekChart').innerHTML;
assert.match(lineMarkup, /potek-chart-viewport/);
assert.match(lineMarkup, /min-width:680px/);
assert.match(html, /\.potek-chart-viewport \{[^}]*max-width: 1000px;[^}]*overflow-x: auto/);
assert.match(html, /\.potek-chart-inner \{[^}]*min-width: 680px/);
assert.match(html, /@media \(pointer: coarse\)[^\n]*r: 18px/);
const linePoint = heatElement('potekChart').nodes[0];
linePoint.onclick();
assert.match(heatElement('potekPoint').textContent, /UTC/);
let prevented = false;
linePoint.onkeydown({key: ' ', preventDefault() { prevented = true; }});
assert.equal(prevented, true);
vm.runInContext("timeseriesMode = 'heatmap'; renderTimeseries_(); renderTimeseries_();", heatBrowser);
const climateRequests = heatCalls.filter(call => call.type === 'climate');
assert.equal(climateRequests.length, 19); // deduplicated pending requests
assert.ok(!climateRequests.some(call => call.key.includes('dinaric')));
climateRequests.forEach(call => call.success(annualResponses.get(call.key)));
assert.doesNotMatch(heatElement('potekChart').innerHTML, /NaN|undefined|Nalaganje reference/);
const expectedGroups = [
  ['Temperature', ['t850_c', 't700_c', 't500_c', 'freezing_level_msl_m']],
  ['Moisture', ['pwat_mm', 'q_surface_gkg', 'q925_gkg', 'q850_gkg', 'ivt_kg_m1_s1']],
  ['Stability', ['lifted_index_c', 'mucape_jkg', 'lapse_rate_850_500_c_per_km', 'lapse_rate_700_500_c_per_km']],
  ['Wind / shear', ['shear_0_1km_ms', 'shear_0_3km_ms', 'shear_0_6km_ms', 'shear_sfc_700_ms']],
  ['Dynamics', ['z500_m', 'thickness_925_500_m']]
];
const mappedRows = JSON.parse(vm.runInContext('JSON.stringify(heatmapRows)', heatBrowser));
assert.deepEqual(mappedRows.map(row => row[0]), expectedGroups.flatMap(group => group[1]));
const compactMappings = {t850_c: 't850', t700_c: 't700', t500_c: 't500', ivt_kg_m1_s1: 'ivt_kg_m_s'};
for (const [group, keys] of expectedGroups) {
  for (const key of keys) {
    const row = mappedRows.find(row => row[0] === key);
    assert.equal(row[1], compactMappings[key] || key);
    assert.equal(row[2], group);
    assert.ok(response.variables.includes(key));
    assert.ok(annualIndex.parameters.some(def => def.key === row[1]));
  }
}
// These are exactly the current series variables with compact references.
assert.deepEqual(mappedRows.map(row => row[0]).sort(), response.variables.filter(key =>
  annualIndex.parameters.some(def => def.key === (compactMappings[key] || key))).sort());
const heatMarkup = heatElement('potekChart').innerHTML;
assert.deepEqual([...heatMarkup.matchAll(/class="heat-group"><span>([^<]*)/g)].map(match => match[1]),
  expectedGroups.map(group => group[0]));
assert.equal((heatMarkup.match(/class="potek-heatmap"/g) || []).length, 1);
assert.match(html, /\.heat-group \{[^}]*grid-column: 1 \/ -1/);
assert.match(html, /\.heat-label \{[^}]*position: sticky; left: 0/);
assert.doesNotMatch(html.match(/\.potek-heatmap \{([^}]*)/)[1], /height:|overflow-y:/);
const slotCount = Number(heatMarkup.match(/repeat\((\d+),/)[1]);
const renderedCells = [...heatMarkup.matchAll(/<button type="button" class="heat-cell ([^"]+)"[^>]*>/g)];
assert.equal(renderedCells.length, slotCount * 19);
// Verify every row's actual first observation is classified against its mapped day.
mappedRows.forEach((row, index) => {
  const record = response.records[0];
  const day = annualResponses.get(row[1]).days.find(day => day.day === record.valid_time.slice(5, 10));
  const band = heatBrowser.heatmapBand_(record[row[0]], day);
  const expectedClass = band === null ? 'heat-missing' : ['heat-low', 'heat-lower', 'heat-normal', 'heat-upper', 'heat-high'][band];
  assert.equal(renderedCells[index * slotCount][1], expectedClass);
  heatElement('potekChart').nodes[index * slotCount].onclick();
  const detail = heatElement('potekPoint').textContent;
  assert.match(detail, /UTC/);
  assert.match(detail, /P10:.*P50:.*P90:/);
  if (record[row[0]] === null) assert.match(detail, /Missing value/);
});

assert.match(heatElement('potekChart').innerHTML, /heat-normal/);
assert.match(heatElement('potekChart').innerHTML, /heat-missing/);
const cell = heatElement('potekChart').nodes[0];
cell.onclick();
assert.match(heatElement('potekPoint').textContent, /T850.*UTC.*°C.*P10:.*P50:.*P90:/);
cell.onfocus(); cell.onmouseenter();
const thresholds = {p10: -4, p25: -2, p50: 0, p75: 2, p90: 4};
for (const [value, band] of [[-5, 0], [-4, 1], [-2, 2], [0, 2], [2, 2], [3, 3], [4, 3], [5, 4], [null, null], [undefined, null]]) {
  assert.equal(heatBrowser.heatmapBand_(value, thresholds), band);
}
assert.equal(heatBrowser.heatmapBand_(0, {p10: 0, p25: 0, p50: 0, p75: 0, p90: 0}), 2);
assert.equal(heatBrowser.heatmapBand_(1, {...thresholds, p50: null}), null);
assert.equal(heatBrowser.heatmapBand_(1, {...thresholds, p90: -10}), null);
assert.equal(heatBrowser.heatmapBand_(-5, thresholds), 0); // LI is never inverted
assert.equal(heatBrowser.heatmapBand_(1, null), null);
for (const key of ['30d', '90d', '1y']) {
  heatBrowser.loadTimeseriesWindow_(key);
  const request = heatCalls.at(-1);
  assert.equal(request.type, 'series');
  request.success(backend.getDashboardTimeseries(key));
  assert.match(heatElement('potekChart').innerHTML, /potek-heatmap/);
  const count = heatCalls.length;
  vm.runInContext("timeseriesMode = 'graph'; renderTimeseries_();", heatBrowser);
  assert.match(heatElement('potekChart').innerHTML, /data-point/);
  vm.runInContext("timeseriesMode = 'heatmap'; renderTimeseries_();", heatBrowser);
  heatBrowser.loadTimeseriesWindow_(key);
  assert.equal(heatCalls.length, count); // same windows and references reused
}
// Missing nominal terms create empty heatmap slots, not collapsed time.
vm.runInContext(`timeseriesWindow = '7d'; timeseriesCache['7d'] = {
  variables: ['t850_c'], records: [
    {valid_time: '2026-01-01T00:00:00Z', t850_c: 0},
    {valid_time: '2026-01-02T00:00:00Z', t850_c: null}
  ]}; renderTimeseries_();`, heatBrowser);
assert.match(heatElement('potekChart').innerHTML, /repeat\(3,/);
assert.match(heatElement('potekChart').innerHTML, /2026-01-01 12:00 UTC/);
// A request completed after leaving Potek must not overwrite the current view.
vm.runInContext("delete annualCache.pwat_mm; renderTimeseries_();", heatBrowser);
const staleClimate = heatCalls.at(-1);
heatBrowser.setMainView_('sounding');
heatElement('app').innerHTML = 'Sounding';
staleClimate.success(annualResponses.get('pwat_mm'));
assert.equal(heatElement('app').innerHTML, 'Sounding');
assert.ok(climateFetches.every(path => path !== 'climatology/daily_climatology.json'));
console.log('Potek heatmap, responsive viewport, cache and interaction checks passed.');

// Main sounding climatology is a compact six-item summary; badges require numbers.
const summaryKeys = ['t850', 'pwat_mm', 'freezing_level_msl_m', 'lifted_index_c', 'shear_0_6km_ms', 'z500_m'];
const summaryFixture = {
  climatology: [...summaryKeys, 't700', 'mucape_jkg', 'ivt_kg_m1_s1'].map((key, index) => ({
    key, label: key, value: index, unit: '°C', percentile: index * 10,
    historicalMin: -99999, historicalMax: 99999
  }))
};
const summaryMarkup = browser.climatologyHtml(summaryFixture);
assert.equal((summaryMarkup.match(/class="clim-summary-item"/g) || []).length, 6);
assert.equal((summaryMarkup.match(/class="percentile-pill"/g) || []).length, 6);
for (const label of ['T850', 'PWAT', 'Freezing level', 'Lifted Index', '0–6 km shear', 'Z500']) {
  assert.ok(summaryMarkup.includes(label));
}
assert.doesNotMatch(summaryMarkup, /<table|pbar|pmark|Min \/ max|99999|mucape_jkg|ivt_kg_m1_s1|t700/);
assert.match(summaryMarkup, /onclick="showClimatologyView\(\)"[^>]*>Open Climatology →/);
const missingSummary = browser.climatologyHtml({climatology: [
  {...summaryFixture.climatology[0], value: null},
  {...summaryFixture.climatology[1], value: NaN},
  {...summaryFixture.climatology[2], value: Infinity},
  summaryFixture.climatology[3]
]});
assert.equal((missingSummary.match(/class="clim-summary-item"/g) || []).length, 1);
assert.match(browser.climatologyHtml({}), /Open Climatology →/);
for (const percentile of [null, undefined, NaN, Infinity, -Infinity, '50', 'bad']) {
  assert.equal(browser.percentileBadge_(percentile), '');
  const metric = browser.metricHtml({label: 'Total IVT', value: 42, unit: 'kg m⁻¹ s⁻¹', percentile});
  assert.doesNotMatch(metric, /percentile-pill|PNaN|Pundefined|PInfinity/);
  const summary = browser.climatologyHtml({climatology: [{...summaryFixture.climatology[0], percentile}]});
  assert.doesNotMatch(summary, /percentile-pill|PNaN|Pundefined|PInfinity/);
}
for (const [percentile, label] of [[0, 'P0'], [50.4, 'P50'], [100, 'P100']]) {
  assert.match(browser.percentileBadge_(percentile), new RegExp('>' + label + '<'));
}
assert.match(html, /\.dashboard\s*\{\s*align-items: start;/);
assert.match(html, /\.clim-summary \{[^}]*repeat\(3, minmax\(0, 1fr\)\)/);
assert.match(html, /@media \(max-width: 620px\) \{ \.clim-summary \{[^}]*repeat\(2,/);
assert.match(html, /@media \(max-width: 380px\) \{ \.clim-summary \{[^}]*grid-template-columns: 1fr/);
browser.setMainView_('sounding');
browser.render({...dashboard, climatology: summaryFixture.climatology, climatologyMeta: {referencePeriod: '1996–2025'}});
assert.match(element('app').innerHTML, /Climatological context[\s\S]*1996–2025/);
assert.match(element('app').innerHTML, /clim-summary/);
assert.doesNotMatch(element('app').innerHTML, /PNaN|class="clim-table"/);
// The summary action uses the existing top-level view and full annual renderer.
browser.showClimatologyView();
assert.equal(vm.runInContext('currentView', browser), 'climatology');
assert.equal(calls.at(-1).type, 'climatology');
calls.at(-1).success(annualResponses.get('t850'));
assert.match(element('annualChart').innerHTML, /<svg|P10–P90|P25–P75/);
assert.match(element('annualChart').innerHTML, /P10–P90/);
console.log('Compact sounding climatology, percentile safety and top alignment checks passed.');

// Independent columns keep lower content directly below Skew-T on desktop.
browser.setMainView_('sounding');
browser.render(dashboard);
const soundingMarkup = element('app').innerHTML;
const stack = [], textParents = new Map();
for (const match of soundingMarkup.matchAll(/<\/?([a-z][a-z0-9]*)\b[^>]*>|([^<]+)/gi)) {
  if (match[2]) {
    for (const label of ['Hodograph', 'Inversions', 'Recent changes', 'Current state']) {
      if (match[2].trim() === label) textParents.set(label, stack.map(entry => entry.className));
    }
    continue;
  }
  const tag = match[1].toLowerCase();
  if (match[0].startsWith('</')) {
    assert.equal(stack.pop()?.tag, tag, 'Balanced sounding markup');
  } else if (!['img', 'br', 'input', 'hr'].includes(tag)) {
    stack.push({tag, className: (match[0].match(/class="([^"]*)"/) || [])[1]});
  }
}
assert.equal(stack.length, 0);
for (const label of ['Hodograph', 'Inversions']) assert.ok(textParents.get(label).includes('left-stack'));
for (const label of ['Recent changes', 'Current state']) assert.ok(textParents.get(label).includes('right-stack'));
assert.match(html, /\.left-stack,\s*\.right-stack\s*\{\s*display: grid/);
assert.match(html, /\.lower-grid\s*\{\s*display: grid;\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
assert.match(html, /@media \(max-width: 1100px\)[\s\S]*?\.dashboard\s*\{\s*grid-template-columns: 1fr/);

const countBeforeCurrent = calls.length;
browser.setClimatologyMode_('current');
assert.equal(calls.length, countBeforeCurrent); // already loaded sounding reused
assert.match(element('app').innerHTML, /Annual cycle.*Current sounding/);
assert.match(element('app').innerHTML, /currentClimateContent/);
assert.match(element('app').innerHTML, /class="clim-table"/);
const detailMarkup = browser.detailedClimatologyHtml_({...summaryFixture,
  climatologyMeta: {referencePeriod: '1996–2025', window: '±15 calendar days', caveat: 'Historical time caveat'}});
assert.equal((detailMarkup.match(/<tr>/g) || []).length, summaryFixture.climatology.length + 1);
assert.match(detailMarkup, /pbar|pmark/);
assert.match(detailMarkup, /99,999/);
assert.match(detailMarkup, /Historical time caveat/);
assert.match(detailMarkup, /1996–2025/);
for (const percentile of [undefined, NaN, Infinity, null, '50']) {
  const detail = browser.detailedClimatologyHtml_({climatology: [{...summaryFixture.climatology[0], percentile}]});
  assert.doesNotMatch(detail, /PNaN|Pundefined|PInfinity|class="pmark"/);
}
browser.setClimatologyMode_('annual');
assert.match(element('annualChart').innerHTML, /P10–P90/);
assert.match(element('app').innerHTML, /annualParameter/);
assert.doesNotMatch(element('app').innerHTML, /class="clim-table"/);
// Initial sounding request supplies Current sounding without a second request.
const pendingStart = calls.length;
const pendingBrowser = vm.createContext({document: {getElementById: element, querySelectorAll: () => []}, google: {script: {run: runner}}});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], pendingBrowser);
const pendingSounding = calls.at(-1);
pendingBrowser.setClimatologyMode_('current');
assert.equal(calls.length, pendingStart + 1);
assert.match(element('app').innerHTML, /Loading current sounding climatology/);
pendingSounding.success(dashboard);
assert.match(element('app').innerHTML, /class="clim-table"/);
// Switching away prevents late annual results from replacing Current sounding.
vm.runInContext("delete annualCache.t500;", browser);
browser.loadAnnualParameter_('t500');
const lateAnnual = calls.at(-1);
browser.setClimatologyMode_('current');
const currentMarkup = element('app').innerHTML;
lateAnnual.success(annualResponses.get('t500'));
assert.equal(element('app').innerHTML, currentMarkup);
assert.doesNotMatch(html, /Sondaža|Klimatologija|Dodatna diagnostika|Odpri|Poskusi|Izberite|Manjkajo|Nalaganje|Nominalni|veljavni|dni|1 leto|mediana/);
assert.match(html, />Time series<\/button>/);
assert.match(html, /\? 'Graph' : 'Heatmap'/);
console.log('English UI, independent columns and climatology subview checks passed.');

// Trajectory data stays a sanitized pass-through of the extractor product.
const trajectoryPoint = (time, pressure, latitude = 46, longitude = 14) => ({
  time_s: time, pressure_hpa: pressure, height_m: time * 5, latitude, longitude
});
const trajectoryFixture = {
  available: true, start: trajectoryPoint(0, 990), end: trajectoryPoint(1800, 490, 46.2, 14.3),
  points: [trajectoryPoint(0,990), trajectoryPoint(600,850), trajectoryPoint(620,840),
    trajectoryPoint(1200,700), trajectoryPoint(1220,690), trajectoryPoint(1800,490,46.2,14.3)],
  standard_levels: {'925':trajectoryPoint(240,925), '850':trajectoryPoint(602,850), '700':trajectoryPoint(1201,700)},
  duration_s: '1800', max_height_m: 9000, raw_point_count: 901, display_point_count: 6
};
const exposed = backend.buildDashboardView_({...latest, trajectory:trajectoryFixture},null,null,'data/latest.json',true).trajectory;
assert.deepEqual(JSON.parse(JSON.stringify(exposed.points)),trajectoryFixture.points);
for (const [camel, raw] of [['durationS','duration_s'],['maxHeightM','max_height_m'],['rawPointCount','raw_point_count'],['displayPointCount','display_point_count']]) {
  assert.equal(exposed[camel],Number(trajectoryFixture[raw]));
}
assert.deepEqual(JSON.parse(JSON.stringify(exposed.start)),trajectoryFixture.start);
assert.deepEqual(JSON.parse(JSON.stringify(exposed.end)),trajectoryFixture.end);
assert.deepEqual(JSON.parse(JSON.stringify(exposed.standardLevels)),trajectoryFixture.standard_levels);
const sanitized = backend.buildTrajectory_({duration_s:Infinity, points:[{latitude:'46',longitude:NaN,height_m:''}],standard_levels:{850:{time_s:undefined}}});
assert.equal(sanitized.durationS,null);
assert.equal(sanitized.points[0].latitude,46);
assert.equal(sanitized.points[0].longitude,null);
assert.equal(sanitized.points[0].height_m,null);
assert.equal(sanitized.standardLevels[850].time_s,null);
assert.equal(backend.buildTrajectory_().available,false);
assert.match(html, /id="viewTrajectoriesBtn"[^>]*onclick="showTrajectoriesView\(\)">Trajectories/);
assert.equal(browser.trajectorySegmentPoints_(exposed,'full').length,6);
assert.deepEqual(Array.from(browser.trajectorySegmentPoints_(exposed,'10'),p=>p.time_s),[0,600]);
assert.deepEqual(Array.from(browser.trajectorySegmentPoints_(exposed,'20'),p=>p.time_s),[0,600,620,1200]);
assert.equal(browser.trajectorySegmentPoints_(exposed,'850').at(-1).time_s,600);
assert.equal(browser.trajectorySegmentPoints_(exposed,'700').at(-1).time_s,1200);
assert.equal(browser.trajectorySegmentPoints_(exposed,'500').at(-1).time_s,1800);
assert.equal(browser.trajectorySegmentPoints_({...exposed,standardLevels:{}},'850').at(-1).pressure_hpa,850);
assert.equal(browser.trajectoryDisplacement_(trajectoryPoint(0,990),trajectoryPoint(0,990)),0);
assert.ok(Math.abs(browser.trajectoryDisplacement_({latitude:0,longitude:0},{latitude:0,longitude:1})-111.195)<0.01);
assert.equal(browser.trajectoryDisplacement_(null,exposed.end),null);
const mapEvents = [], markerPopups = [];
const mapStub = {remove(){mapEvents.push('remove');},invalidateSize(){mapEvents.push('resize');},fitBounds(coords,options){mapEvents.push({coords,options});}};
const layerStub = () => ({remove(){mapEvents.push('tile-remove');},addTo(){return this;},bindTooltip(){return this;},bindPopup(label){markerPopups.push(label);return this;}});
browser.L = {map:()=>mapStub,tileLayer:(url,options)=>{assert.match(options.attribution,/OpenStreetMap/);return layerStub();},
  polyline:(coords,style)=>{mapEvents.push({polyline:coords.slice(),style});return layerStub();},circleMarker:layerStub,latLngBounds:coords=>coords};
let resizeCallback, disconnected = 0;
browser.ResizeObserver = class {constructor(callback){resizeCallback=callback;}observe(){}disconnect(){disconnected++;}};
browser.trajectoryFixture = {...dashboard,trajectory:exposed};
vm.runInContext('currentData = trajectoryFixture;',browser);
browser.showTrajectoriesView();
assert.match(element('app').innerHTML,/trajectory-map/);
assert.match(element('app').innerHTML,/Full ascent.*10 min.*20 min.*850 hPa.*700 hPa.*500 hPa/);
assert.match(element('app').innerHTML,/Displacement from launch/);
assert.ok(markerPopups.some(label=>/925 hPa.*m MSL.*since launch/.test(label)));
assert.ok(markerPopups.some(label=>/Launch/.test(label)));
assert.ok(markerPopups.some(label=>/Segment endpoint/.test(label)));
assert.equal(mapEvents.find(event=>event.polyline).polyline.length,6);
assert.equal(mapEvents.find(event=>event.coords).options.maxZoom,13);
resizeCallback();
assert.ok(mapEvents.filter(event=>event==='resize').length>=2);
markerPopups.length=0;
browser.selectTrajectorySegment_('10');
assert.ok(markerPopups.some(label=>/925 hPa/.test(label)));
assert.ok(!markerPopups.some(label=>/700 hPa/.test(label)));
assert.ok(disconnected>0);
browser.setMainView_('sounding');
assert.equal(vm.runInContext('trajectoryMap',browser),null);
browser.showTrajectoriesView(); // reopening initializes and resizes a fresh map
assert.ok(mapEvents.filter(event=>event==='resize').length>=4);
vm.runInContext('currentData = {trajectory:{available:false}};',browser);
browser.renderTrajectories_();
assert.match(element('app').innerHTML,/Trajectory not available/);
vm.runInContext('currentData = {trajectory:{available:true,points:[]}};',browser);
browser.renderTrajectories_();
assert.match(element('app').innerHTML,/Trajectory not available/);
// An initial sounding response arriving after opening Trajectories populates the view.
const trajectoryBrowser = vm.createContext({document:{getElementById:element,querySelectorAll:()=>[]},google:{script:{run:runner}}});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],trajectoryBrowser);
const initialTrajectoryRequest = calls.at(-1), beforeTrajectory = calls.length;
trajectoryBrowser.showTrajectoriesView();
assert.equal(calls.length,beforeTrajectory);
assert.match(element('app').innerHTML,/Loading trajectory/);
initialTrajectoryRequest.success({...dashboard,trajectory:exposed});
assert.match(element('app').innerHTML,/trajectory-summary/);
assert.match(element('trajectoryMap').innerHTML,/Map could not load/);
console.log('Trajectory exposure, navigation, filtering, markers, resizing and unavailable checks passed.');

// Script Properties configure browser tiles; no key is embedded in source.
assert.equal(dashboard.trajectoryViewConfig.cartoBasemapKey,'');
backend.PropertiesService = {getScriptProperties:()=>({getProperty:key=>{
  assert.equal(key,'CARTO_BASEMAP_KEY'); return ' test/key &value ';
}})};
const keyedDashboard = backend.buildDashboardView_({...latest,trajectory:trajectoryFixture},null,null,'data/latest.json',true);
assert.equal(keyedDashboard.trajectoryViewConfig.cartoBasemapKey,'test/key &value');
const lightConfig = browser.trajectoryBasemap_('light',keyedDashboard.trajectoryViewConfig);
const darkConfig = browser.trajectoryBasemap_('dark',keyedDashboard.trajectoryViewConfig);
assert.match(lightConfig.url,/\/rastertiles\/light_all\/\{z\}\/\{x\}\/\{y\}\.png\?key=test%2Fkey%20%26value$/);
assert.match(darkConfig.url,/\/rastertiles\/dark_all\//);
assert.match(lightConfig.options.attribution,/OpenStreetMap.*CARTO/);
for (const config of [undefined,{}, {cartoBasemapKey:''}]) {
  assert.equal(browser.trajectoryBasemap_('dark',config).url,'https://tile.openstreetmap.org/{z}/{x}/{y}.png');
  assert.match(browser.trajectoryBasemap_('light',config).options.attribution,/OpenStreetMap/);
}
const tileCalls = [];
browser.L.tileLayer = (url,options)=>{tileCalls.push({url,options});return layerStub();};
browser.keyedDashboard = keyedDashboard;
vm.runInContext("currentData = keyedDashboard; trajectorySegment = '20'; trajectoryBasemapMode = 'light';",browser);
browser.showTrajectoriesView();
assert.match(element('app').innerHTML,/data-trajectory-basemap="light"/);
assert.match(element('app').innerHTML,/data-trajectory-basemap="dark"/);
assert.match(tileCalls.at(-1).url,/light_all/);
const sameMap = vm.runInContext('trajectoryMap',browser);
const markupBeforeSwitch = element('app').innerHTML;
const eventsBeforeSwitch = mapEvents.length;
const markersBeforeSwitch = markerPopups.length;
browser.selectTrajectoryBasemap_('dark');
assert.equal(vm.runInContext('trajectoryMap',browser),sameMap);
assert.equal(vm.runInContext('trajectorySegment',browser),'20');
assert.equal(markerPopups.length,markersBeforeSwitch);
assert.equal(element('app').innerHTML,markupBeforeSwitch);
assert.deepEqual(mapEvents.slice(eventsBeforeSwitch),['tile-remove']); // no fitBounds, resize or map removal
assert.match(tileCalls.at(-1).url,/dark_all/);
browser.selectTrajectoryBasemap_('light');
assert.match(tileCalls.at(-1).url,/light_all/);
vm.runInContext('currentData = trajectoryFixture;',browser);
browser.renderTrajectories_();
assert.match(tileCalls.at(-1).url,/tile.openstreetmap.org/);
assert.match(element('app').innerHTML,/data-trajectory-basemap="dark"[^>]* disabled/);
const fallbackCalls = tileCalls.length;
browser.selectTrajectoryBasemap_('dark');
assert.equal(tileCalls.length,fallbackCalls);
assert.equal(vm.runInContext('trajectoryBasemapMode',browser),'light');
console.log('CARTO property configuration, Light/Dark switching and OSM fallback checks passed.');

// Compact windows enforce allowlisted paths and expose real partial coverage.
backend.fetchJson_ = path => read(path);
for (const key of ['7d','30d','90d']) {
  const window=backend.getDashboardTrajectories(key);
  assert.equal(window.records.length,read(`trajectories/ljlm/latest_${key}.json`).record_count);
  assert.ok(window.records.every(r=>r.points.every(p=>Number.isFinite(p.time_s))));
}
for (const key of ['1y','../latest','__proto__',null]) assert.throws(()=>backend.getDashboardTrajectories(key));
backend.fetchJson_ = ()=>({trajectories:{available:true,windows:{'7d':'data/latest.json'}}});
assert.throws(()=>backend.getDashboardTrajectories('7d'));
backend.fetchJson_ = path=>read(path);
const history = [
  {valid_time:'2026-10-01T00:00:00Z',term:'00',points:trajectoryFixture.points},
  {valid_time:'2026-10-02T00:00:00Z',term:'00',points:trajectoryFixture.points},
  {valid_time:'2026-10-02T12:00:00Z',term:'12',points:trajectoryFixture.points.map(p=>({...p,longitude:p.longitude+0.05}))}
];
assert.deepEqual(Array.from(browser.trajectoryDates_(history)),['2026-10-02','2026-10-01']);
assert.equal(browser.trajectoryDailyPair_(history,'2026-10-02').filter(Boolean).length,2);
assert.equal(browser.trajectoryDailyPair_(history,'2026-10-01')[1],null);
const historyRequests=[];
runner.getDashboardTrajectories=key=>historyRequests.push({key,success,failure});
const histBrowser=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[]},google:{script:{run:runner}},L:browser.L});
histBrowser.L.heatLayer=(points,options)=>{mapEvents.push({density:points,options});return layerStub();};
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],histBrowser);
histBrowser.fixture={...dashboard,trajectory:exposed};
vm.runInContext('currentData=fixture;',histBrowser);
histBrowser.showTrajectoriesView();
assert.equal(historyRequests.length,0);
histBrowser.setTrajectoryMode_('daily');
histBrowser.setTrajectoryMode_('daily');
assert.equal(historyRequests.length,1);
historyRequests[0].success({records:history});
assert.equal(vm.runInContext('trajectoryDate',histBrowser),'2026-10-02');
assert.match(element('app').innerHTML,/trajectory-date-nav/);
assert.match(element('app').innerHTML,/trajectory-overview/);
assert.match(html,/\.trajectory-overview \{[^}]*max-width:100%; overflow-x:auto/);
assert.match(element('app').innerHTML,/00 UTC endpoint.*12 UTC endpoint/);
histBrowser.navigateTrajectoryDate_(1);
assert.equal(vm.runInContext('trajectoryDate',histBrowser),'2026-10-01');
assert.match(element('app').innerHTML,/12 UTC · unavailable/);
histBrowser.navigateTrajectoryDate_(-1);
histBrowser.selectTrajectorySegment_('10');
histBrowser.history=history;
assert.ok(vm.runInContext('trajectoryFilteredRecords_(history).every(t=>t.points.at(-1).time_s===600)',histBrowser));
histBrowser.selectTrajectoryDate_('1990-01-01');
assert.equal(vm.runInContext('trajectoryDate',histBrowser),'2026-10-02');
histBrowser.setTrajectoryPeriod_('30d');
histBrowser.setTrajectoryMode_('tracks');
assert.equal(historyRequests.length,2);
historyRequests[1].success({records:history});
assert.match(element('app').innerHTML,/3 available soundings/);
assert.match(element('app').innerHTML,/Actual coverage/);
histBrowser.setTrajectoryMode_('density');
assert.equal(vm.runInContext('trajectoryPeriod',histBrowser),'30d');
assert.equal(historyRequests.length,2);
assert.ok(mapEvents.some(event=>event.density));
assert.match(element('app').innerHTML,/Low.*High density/);
assert.equal((element('app').innerHTML.match(/aria-label="Trajectory period"/g)||[]).length,1);
histBrowser.setTrajectoryMode_('latest');
assert.doesNotMatch(element('app').innerHTML,/aria-label="Trajectory period"/);
histBrowser.setTrajectoryMode_('daily');
assert.equal(vm.runInContext('trajectoryPeriod',histBrowser),'30d');
assert.equal(historyRequests.length,2);
histBrowser.setTrajectoryPeriod_('90d');
historyRequests.at(-1).success({records:[]});
assert.match(element('trajectoryMap').textContent,/Trajectory not available/);
assert.deepEqual(Array.from(histBrowser.trajectoryDensityPoints_([{points:[trajectoryPoint(0,990)]}])),[]);
assert.match(lightConfig.options.className,/trajectory-tiles-light/);
assert.match(darkConfig.options.className,/trajectory-tiles-dark/);
console.log('Daily pairing, dates, overview, shared periods, lazy archive cache, Tracks and Density checks passed.');

// Older tracks are translucent; newest is drawn last and highlighted.
histBrowser.setTrajectoryPeriod_('30d');
histBrowser.setTrajectoryMode_('tracks');
const trackEventsStart=mapEvents.length;
histBrowser.renderTrajectories_();
const drawnTracks=mapEvents.slice(trackEventsStart).filter(event=>event.polyline);
assert.equal(drawnTracks.length,3);
assert.equal(drawnTracks.at(-1).style.opacity,1);
assert.equal(drawnTracks.at(-1).style.weight,3.5);
assert.ok(drawnTracks.slice(0,-1).every(event=>event.style.opacity<0.3));
// Pending responses for a different period never replace the active map.
vm.runInContext("delete trajectoryWindows['7d']; delete trajectoryWindows['90d'];",histBrowser);
histBrowser.setTrajectoryPeriod_('7d');
const staleTrajectory=historyRequests.at(-1);
histBrowser.setTrajectoryPeriod_('90d');
const activeTrajectory=historyRequests.at(-1);
activeTrajectory.success({records:history.slice(0,1)});
const activeMarkup=element('app').innerHTML;
staleTrajectory.success({records:history});
assert.equal(element('app').innerHTML,activeMarkup);
assert.equal(vm.runInContext('trajectoryPeriod',histBrowser),'90d');
assert.match(element('app').innerHTML,/1 available soundings/);
console.log('Historical latest highlight and stale trajectory-window checks passed.');

// Shared term selection controls tracks, density inputs and Daily overlays.
assert.equal(vm.runInContext('trajectoryTerm',histBrowser),'all');
assert.equal((element('app').innerHTML.match(/aria-label="Trajectory term"/g)||[]).length,1);
vm.runInContext("trajectoryWindows['30d']={records:history}; trajectoryPeriod='30d';",histBrowser);
histBrowser.setTrajectoryMode_('tracks');
histBrowser.setTrajectoryTerm_('00');
assert.match(element('app').innerHTML,/2 available soundings/);
assert.match(element('app').innerHTML,/2026-10-01 00:00 → 2026-10-02 00:00 UTC/);
assert.deepEqual(Array.from(histBrowser.trajectoryTermRecords_(history),r=>r.term),['00','00']);
histBrowser.setTrajectoryTerm_('12');
assert.match(element('app').innerHTML,/1 available soundings/);
assert.match(element('app').innerHTML,/2026-10-02 12:00 → 2026-10-02 12:00 UTC/);
const term12=history[2], term12Endpoint=histBrowser.trajectorySegmentPoints_(term12,'10').at(-1);
assert.match(element('app').innerHTML,new RegExp(histBrowser.fmt(histBrowser.trajectoryDisplacement_(term12.points[0],term12Endpoint),1)+' km'));
const densityStart=mapEvents.length;
histBrowser.setTrajectoryMode_('density');
const densityEvent=mapEvents.slice(densityStart).find(event=>event.density);
assert.deepEqual(JSON.parse(JSON.stringify(densityEvent.density)),JSON.parse(JSON.stringify(histBrowser.trajectoryDensityPoints_(histBrowser.trajectoryFilteredRecords_([term12])))));
histBrowser.setTrajectoryMode_('daily');
histBrowser.selectTrajectoryDate_('2026-10-02');
assert.match(element('app').innerHTML,/00 UTC · filtered out/);
assert.match(element('app').innerHTML,/trajectory-day-terms/);
assert.match(element('app').innerHTML,/aria-label="00 UTC available"/);
assert.match(element('app').innerHTML,/aria-label="12 UTC unavailable"/);
assert.match(element('app').innerHTML,/Filtered out/);
const dailyTermStart=mapEvents.length;
histBrowser.renderTrajectories_();
assert.equal(mapEvents.slice(dailyTermStart).filter(event=>event.polyline).length,1);
histBrowser.selectTrajectoryDate_('2026-10-01');
assert.match(element('trajectoryMap').textContent,/Trajectory not available/);
assert.match(element('app').innerHTML,/12 UTC · unavailable/);
histBrowser.setTrajectoryTerm_('all');
histBrowser.selectTrajectoryDate_('2026-10-02');
const allDailyStart=mapEvents.length;
histBrowser.renderTrajectories_();
assert.equal(mapEvents.slice(allDailyStart).filter(event=>event.polyline).length,2);
histBrowser.setTrajectoryTerm_('00');
histBrowser.setTrajectoryMode_('latest');
assert.doesNotMatch(element('app').innerHTML,/aria-label="Trajectory term"/);
histBrowser.setTrajectoryMode_('tracks');
histBrowser.setTrajectoryPeriod_('7d');
histBrowser.selectTrajectorySegment_('20');
assert.equal(vm.runInContext('trajectoryTerm',histBrowser),'00');
// Basemap switches restyle existing historical lines without changing map bounds.
const lineStyles=[];
histBrowser.L.polyline=(coords,style)=>{
  const layer=layerStub();layer.setStyle=next=>lineStyles.push(next);
  mapEvents.push({polyline:coords,style});return layer;
};
histBrowser.keyedFixture=keyedDashboard;
vm.runInContext("currentData=keyedFixture; trajectoryBasemapMode='light';",histBrowser);
histBrowser.setTrajectoryTerm_('all');
histBrowser.setTrajectoryPeriod_('30d');
const historicalMap=vm.runInContext('trajectoryMap',histBrowser);
const switchEventsStart=mapEvents.length;
histBrowser.selectTrajectoryBasemap_('dark');
assert.equal(vm.runInContext('trajectoryMap',histBrowser),historicalMap);
assert.deepEqual(mapEvents.slice(switchEventsStart),['tile-remove']);
assert.equal(lineStyles.length,3);
assert.ok(lineStyles.slice(0,-1).every(style=>style.color==='#61b7d6' && style.opacity===0.48 && style.weight===1.2));
assert.equal(lineStyles.at(-1).opacity,1);
assert.equal(lineStyles.at(-1).weight,3.5);
assert.equal(lineStyles.at(-1).color,'#8ddfff');
histBrowser.selectTrajectoryBasemap_('light');
assert.ok(lineStyles.slice(3,-1).every(style=>style.opacity===0.22 && style.weight===1.2));
assert.equal(vm.runInContext('trajectorySegment',histBrowser),'20');
histBrowser.setTrajectoryTerm_('12');
histBrowser.selectTrajectoryBasemap_('dark');
assert.equal(vm.runInContext('trajectoryTerm',histBrowser),'12');
histBrowser.setTrajectoryTerm_('invalid');
assert.equal(vm.runInContext('trajectoryTerm',histBrowser),'12');
console.log('Shared term filtering, filtered statistics, Daily availability and Dark historical visibility checks passed.');
