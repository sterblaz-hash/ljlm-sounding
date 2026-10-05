// Offline checks: node apps-script/test-dashboard.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const read = path => JSON.parse(fs.readFileSync(path, 'utf8'));
const backend = vm.createContext({});
vm.runInContext(fs.readFileSync('apps-script/Code.gs', 'utf8'), backend);
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
  if (!elements.has(id)) elements.set(id, {innerHTML: '', textContent: '', classList: {toggle() {}}, querySelectorAll: () => []});
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
assert.match(element('potekChart').textContent, /ni podatkov/);
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
