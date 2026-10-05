/**
 * Pure helpers of the panel modules (no DOM needed): wording of recorded attributes, the choice of sensors for
 * the small multiples, the detection method in words, the trend sentence, and the throttling of list panels
 * during playback (js/panels/tabs.js with stand-in panels).
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { trendDeadband } from '../js/data/index.js';
import {
  EMPTY_RANKING,
  MAX_SENSOR_CHARTS,
  NOT_MONITORED,
  RECORDED_SOURCE_NAME,
  RECORDED_TITLE,
  SIMULATED_TITLE,
  addressTags,
  heightText,
  nbiRatingText,
  sensorsForCharts,
} from '../js/panels/assets.js';
import { EMPTY_LIST, SUBTITLE } from '../js/panels/anomalies.js';
import { assetNameWithId, assetOptions, detectionMethodWords, sensorTypeLabel } from '../js/panels/common.js';
import { describeTrend } from '../js/panels/sensors.js';
import { LIST_THROTTLE_MS, TABS } from '../js/panels/tabs.js';
import { formatDelta, formatNumber, formatValue } from '../js/ui/format.js';

test('the fixed wording of the panels (contract 12.1, 12.5)', () => {
  assert.equal(`${RECORDED_TITLE}${RECORDED_SOURCE_NAME.osm} / ${RECORDED_SOURCE_NAME.nbi}`, 'Recorded attributes — source: OpenStreetMap / FHWA National Bridge Inventory');
  assert.equal(SIMULATED_TITLE, "Simulated monitoring — Derived Asset Health Score, simulated sensors; not an assessment of this structure's real condition");
  assert.equal(NOT_MONITORED, 'Not monitored — no simulated sensors on this asset');
  assert.equal(EMPTY_RANKING, 'All monitored assets are normal at this time');
  assert.equal(EMPTY_LIST, 'No anomalies match these filters at the selected time');
  assert.equal(SUBTITLE, 'Prototype Anomaly Detection');
  assert.deepEqual(TABS.map((tab) => tab.label), ['Assets', 'Anomalies', 'Sensors']);
  assert.equal(LIST_THROTTLE_MS, 500);
  assert.equal(MAX_SENSOR_CHARTS, 4);
});

test('building height names how it was obtained', () => {
  assert.equal(heightText({ height_m: 8.2, height_source: 'lidar_3dep' }, formatNumber), '8.2 m (measured from USGS 3DEP lidar)');
  assert.equal(heightText({ height_m: 7.2, height_source: 'osm_levels' }, formatNumber), '7.2 m (OSM levels)');
  assert.equal(heightText({ height_m: 4.5, height_source: 'estimated' }, formatNumber), '4.5 m (estimated)');
  assert.equal(heightText({ height_m: 12, height_source: 'osm_height' }, formatNumber), '12 m (OSM height tag)');
  assert.equal(heightText({ height_m: null, height_source: 'estimated' }, formatNumber), null);
});

test('NBI condition codes are given with their meaning as text', () => {
  assert.equal(nbiRatingText('7'), '7 — Good condition');
  assert.equal(nbiRatingText(6), '6 — Satisfactory condition');
  assert.equal(nbiRatingText('5'), '5 — Fair condition');
  assert.equal(nbiRatingText('4'), '4 — Poor condition');
  assert.equal(nbiRatingText('N'), 'N — not applicable to this structure');
  assert.equal(nbiRatingText('n'), 'N — not applicable to this structure');
  assert.equal(nbiRatingText(null), null);
  assert.equal(nbiRatingText(''), null);
  assert.equal(nbiRatingText('X'), 'X');
  for (let code = 0; code <= 9; code += 1) assert.match(nbiRatingText(code), new RegExp(`^${code} — .+`));
});

test('OSM address tags are listed one by one, never composed into an address', () => {
  const tags = addressTags({
    asset_id: 'BLD-0014',
    name: 'Taco Bell',
    'addr:street': 'West Wyatt Earp Boulevard',
    'addr:housenumber': '708',
    'addr:city': 'Dodge City',
    'addr:postcode': '67801',
    'addr:state': null,
  });
  assert.deepEqual(tags, [
    ['addr:city', 'Dodge City'],
    ['addr:housenumber', '708'],
    ['addr:postcode', '67801'],
    ['addr:street', 'West Wyatt Earp Boulevard'],
  ]);
  assert.deepEqual(addressTags({ asset_id: 'BLD-0007', name: null }), []);
});

test('small multiples: at most four sensors, those with the most anomalies first', () => {
  const sensors = ['MST-031', 'TMP-001', 'TMP-002', 'VIB-001', 'VIB-002'].map((sensor_id) => ({ sensor_id }));
  const counts = { 'VIB-002': 2, 'TMP-001': 1, 'VIB-001': 1 };
  const chosen = sensorsForCharts(sensors, (id) => counts[id] || 0);
  assert.deepEqual(chosen.map((sensor) => sensor.sensor_id), ['VIB-002', 'TMP-001', 'VIB-001', 'MST-031']);
  assert.equal(sensorsForCharts(sensors.slice(0, 2), () => 0).length, 2);
  assert.deepEqual(sensorsForCharts(sensors, () => 0, 3).map((sensor) => sensor.sensor_id), ['MST-031', 'TMP-001', 'TMP-002']);
  // The input list is not reordered.
  assert.equal(sensors[0].sensor_id, 'MST-031');
});

test('detection method in words: Isolation Forest is corroborating evidence, never the detector', () => {
  assert.equal(
    detectionMethodWords('threshold+robust_zscore+rolling_median+isolation_forest'),
    'Flagged by critical-threshold breach, robust z-score and rolling median of the z-score. Corroborated by Isolation Forest (corroborating evidence only).',
  );
  assert.equal(detectionMethodWords('robust_zscore+isolation_forest'), 'Flagged by robust z-score. Corroborated by Isolation Forest (corroborating evidence only).');
  assert.equal(detectionMethodWords('robust_zscore+rolling_median'), 'Flagged by robust z-score and rolling median of the z-score.');
  assert.equal(detectionMethodWords('some_new_detector'), 'Flagged by some new detector.');
  assert.equal(detectionMethodWords(''), '—');
  assert.equal(detectionMethodWords(null), '—');
});

test('asset names in links and selects', () => {
  assert.equal(assetNameWithId({ asset_id: 'BRG-001', name: '2nd. Avenue over Arkansas River' }), '2nd. Avenue over Arkansas River (BRG-001)');
  assert.equal(assetNameWithId({ asset_id: 'BLD-0033', name: null }), 'BLD-0033');
  const options = assetOptions(
    [
      { properties: { asset_id: 'RD-0041', name: '10th Avenue' } },
      { properties: { asset_id: 'BLD-0033', name: null } },
      { properties: { asset_id: 'RD-0119', name: '1st Avenue' } },
    ],
    'All monitored assets',
  );
  assert.deepEqual(options.map((option) => option.label), ['All monitored assets', '1st Avenue (RD-0119)', '10th Avenue (RD-0041)', 'BLD-0033']);
  assert.equal(options[0].value, '');
  assert.equal(sensorTypeLabel({ sensor_types: { vibration: { label: 'Vibration' } } }, 'vibration'), 'Vibration');
  assert.equal(sensorTypeLabel({ sensor_types: {} }, 'soil_gas'), 'Soil gas');
});

test('trend sentence: arrow, word and the difference against the reading 24 h earlier', () => {
  const run = { baseline: { scale_floors: { vibration: 0.1, pressure: 0.5 }, log_domain_types: ['vibration'] } };
  const say = (current, previous, unit, type) => describeTrend({ current, previous, unit, deadband: trendDeadband(type, run), formatDelta, formatValue });
  assert.deepEqual(say(1.08, 1.392, 'mm/s', 'vibration'), { arrow: '▼', word: 'falling', detail: '−0.312 mm/s against 24 h earlier (1.39 mm/s)' });
  assert.deepEqual(say(68.4, 66.1, 'psi', 'pressure'), { arrow: '▲', word: 'rising', detail: '+2.3 psi against 24 h earlier (66.1 psi)' });
  assert.equal(say(66.4, 66.1, 'psi', 'pressure').word, 'steady');
  assert.equal(say(66.4, 66.1, 'psi', 'pressure').arrow, '▬');
  assert.equal(say(0.31, 0.3, 'mm/s', 'vibration').word, 'steady');
  assert.deepEqual(say(null, 66.1, 'psi', 'pressure'), { arrow: '–', word: 'no trend', detail: 'no reading at this time' });
  assert.deepEqual(say(66.1, null, 'psi', 'pressure'), { arrow: '–', word: 'no trend', detail: 'no reading 24 h earlier' });
});

// ---- js/panels/tabs.js: the contract between the shell and a panel module -------------------------------------
class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = { setProperty() {} };
    this.listeners = {};
    this.hidden = false;
    this.tabIndex = 0;
    this.className = '';
    this.textContent = '';
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }
  focus() {}
}

async function withFakeDom(run) {
  const saved = { document: globalThis.document, Node: globalThis.Node };
  globalThis.Node = FakeElement;
  globalThis.document = {
    createElement: (tag) => new FakeElement(tag),
    createElementNS: (ns, tag) => new FakeElement(tag),
    createTextNode: (text) => ({ textContent: text }),
  };
  try {
    return await run();
  } finally {
    globalThis.document = saved.document;
    globalThis.Node = saved.Node;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('tabs: list panels re-render at most every 500 ms while playing, at once on pause; tick runs every hour', async () => {
  const { mountTabs } = await import('../js/panels/tabs.js');
  const { createStore } = await import('../js/state/store.js');
  await withFakeDom(async () => {
    const store = createStore({ t: 0, playing: false, tab: 'assets', selection: { kind: null, id: null } });
    const calls = { assets: [], anomalies: [], ticks: [] };
    const modules = {
      './assets.js': { mount: () => ({ update: (changed) => calls.assets.push({ at: performance.now(), keys: [...changed].sort().join(',') }), tick: (t) => calls.ticks.push(t), destroy() {} }) },
      './anomalies.js': { mount: () => ({ update: (changed) => calls.anomalies.push([...changed].sort().join(',')), destroy() {} }) },
      './sensors.js': {},
    };
    const ctx = { store, actions: { setTab: (tab) => store.set({ tab }) } };
    const tabs = mountTabs(new FakeElement('aside'), ctx, { importModule: async (specifier) => modules[specifier.split('?')[0]] });
    await sleep(10);
    assert.equal(tabs.stateOf('assets'), 'ready');
    // First render: every state key.
    assert.equal(calls.assets.length, 1);
    assert.equal(calls.assets[0].keys, 'playing,selection,t,tab');

    // Paused: every change of the clock renders at once.
    store.set({ t: 1 });
    store.set({ t: 2 });
    assert.deepEqual(calls.assets.slice(1).map((call) => call.keys), ['t', 't']);
    assert.deepEqual(calls.ticks, [1, 2]);

    // Playing: 30 ticks in about 600 ms.
    calls.assets.length = 0;
    calls.ticks.length = 0;
    store.set({ playing: true });
    const started = performance.now();
    for (let t = 3; t < 33; t += 1) {
      store.set({ t });
      await sleep(20);
    }
    const whilePlaying = calls.assets.filter((call) => call.keys === 't');
    assert.equal(calls.ticks.length, 30, 'tick(t) runs for every hour');
    assert.ok(whilePlaying.length >= 1 && whilePlaying.length <= Math.ceil((performance.now() - started) / LIST_THROTTLE_MS) + 1, `${whilePlaying.length} list renders`);
    for (let i = 1; i < whilePlaying.length; i += 1) {
      assert.ok(whilePlaying[i].at - whilePlaying[i - 1].at >= LIST_THROTTLE_MS - 5, 'two list renders are at least 500 ms apart');
    }

    // Pause right after a tick: the pending tick is rendered immediately.
    store.set({ t: 40 });
    const before = calls.assets.length;
    store.set({ playing: false });
    assert.equal(calls.assets.length, before + 1);
    assert.ok(calls.assets[calls.assets.length - 1].keys.includes('t'));

    // Another tab: mounted on first use and rendered from scratch; the hidden tab gets no more calls.
    const assetCalls = calls.assets.length;
    store.set({ tab: 'anomalies' });
    await sleep(10);
    assert.equal(tabs.stateOf('anomalies'), 'ready');
    assert.deepEqual(calls.anomalies, ['playing,selection,t,tab']);
    store.set({ t: 41 });
    assert.equal(calls.assets.length, assetCalls);
    assert.deepEqual(calls.anomalies, ['playing,selection,t,tab', 't']);

    // A module without mount() becomes an error state with Retry, not an exception.
    const original = console.error;
    console.error = () => {};
    try {
      store.set({ tab: 'sensors' });
      await sleep(10);
    } finally {
      console.error = original;
    }
    assert.equal(tabs.stateOf('sensors'), 'error');
    tabs.destroy();
  });
});
