/**
 * js/state/store.js: shallow merge, change detection, subscribe by keys, ordered notification, re-entrant
 * `set`, and the filter defaults.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { NO_SELECTION, createStore, defaultFilters, filtersAreDefault } from '../js/state/store.js';

test('set merges shallowly, freezes the state and returns the keys that changed', () => {
  const store = createStore({ t: 0, playing: false, tab: 'assets' });
  assert.deepEqual([...store.set({ t: 1, tab: 'assets' })], ['t']);
  assert.deepEqual(store.getState(), { t: 1, playing: false, tab: 'assets' });
  assert.ok(Object.isFrozen(store.getState()));
  assert.equal(store.set({ t: 1 }).size, 0);
  assert.equal(store.set(null).size, 0);
  assert.equal(store.set(undefined).size, 0);
  assert.deepEqual([...store.set({ extra: 'x' })], ['extra']);
});

test('subscribe by keys: a subscriber only hears about the keys it asked for', () => {
  const store = createStore({ t: 0, playing: false, tab: 'assets', selection: NO_SELECTION });
  const heard = { t: [], tabOrSelection: [], one: [], all: [] };
  store.subscribe(['t'], (state, changed) => heard.t.push([state.t, [...changed].sort().join(',')]));
  store.subscribe(['tab', 'selection'], (state, changed) => heard.tabOrSelection.push([...changed].sort().join(',')));
  store.subscribe('playing', (state) => heard.one.push(state.playing));
  store.subscribe('*', (state, changed) => heard.all.push([...changed].sort().join(',')));

  store.set({ t: 5 });
  store.set({ playing: true });
  store.set({ tab: 'anomalies', t: 6 });
  store.set({ t: 6 }); // no change: nobody is called
  store.set({ selection: { kind: 'asset', id: 'BRG-001' } });

  assert.deepEqual(heard.t, [[5, 't'], [6, 't,tab']]);
  assert.deepEqual(heard.tabOrSelection, ['t,tab', 'selection']);
  assert.deepEqual(heard.one, [true]);
  assert.deepEqual(heard.all, ['t', 'playing', 't,tab', 'selection']);
});

test('subscribers receive the previous state; values are compared by identity', () => {
  const store = createStore({ filters: { status: 'all' }, t: 3 });
  const seen = [];
  store.subscribe(['filters', 't'], (state, changed, previous) => seen.push([previous.t, state.t, previous.filters === state.filters]));
  const same = store.getState().filters;
  store.set({ filters: same, t: 4 });
  store.set({ filters: { status: 'all' } }); // equal content, new object: a change
  assert.deepEqual(seen, [[3, 4, true], [4, 4, false]]);
});

test('subscribers run in ascending order, then in subscription order', () => {
  const store = createStore({ t: 0 });
  const order = [];
  store.subscribe(['t'], () => order.push('panels'), { order: 40 });
  store.subscribe(['t'], () => order.push('default-a'));
  store.subscribe(['t'], () => order.push('timeline'), { order: 10 });
  store.subscribe(['t'], () => order.push('map'), { order: 30 });
  store.subscribe(['t'], () => order.push('kpis'), { order: 20 });
  store.subscribe(['t'], () => order.push('default-b'));
  store.set({ t: 1 });
  assert.deepEqual(order, ['timeline', 'kpis', 'map', 'panels', 'default-a', 'default-b']);
});

test('unsubscribe stops the calls, also when done from inside a subscriber', () => {
  const store = createStore({ t: 0 });
  const calls = [];
  let offSecond = () => {};
  const offFirst = store.subscribe(['t'], () => {
    calls.push('first');
    offSecond();
  });
  offSecond = store.subscribe(['t'], () => calls.push('second'));
  store.set({ t: 1 });
  assert.deepEqual(calls, ['first']);
  offFirst();
  store.set({ t: 2 });
  assert.deepEqual(calls, ['first']);
});

test('a set made inside a subscriber is applied at once and notified after the current round', () => {
  const store = createStore({ t: 0, playing: true });
  const log = [];
  store.subscribe(
    ['t'],
    (state) => {
      log.push(`a:t=${state.t}:playing=${state.playing}`);
      if (state.t === 5) {
        store.set({ playing: false });
        assert.equal(store.getState().playing, false);
      }
    },
    { order: 1 },
  );
  store.subscribe(['t'], (state) => log.push(`b:t=${state.t}`), { order: 2 });
  store.subscribe(['playing'], (state) => log.push(`c:playing=${state.playing}`), { order: 3 });
  store.set({ t: 5 });
  assert.deepEqual(log, ['a:t=5:playing=true', 'b:t=5', 'c:playing=false']);
});

test('a failing subscriber does not stop the others', () => {
  const store = createStore({ t: 0 });
  const calls = [];
  const original = console.error;
  const errors = [];
  console.error = (...args) => errors.push(args);
  try {
    store.subscribe(['t'], () => {
      throw new Error('broken module');
    });
    store.subscribe(['t'], () => calls.push('still called'));
    store.set({ t: 1 });
  } finally {
    console.error = original;
  }
  assert.deepEqual(calls, ['still called']);
  assert.equal(errors.length, 1);
  assert.throws(() => store.subscribe(['t'], 'not a function'), TypeError);
});

test('filter defaults come from the metadata; filtersAreDefault detects every restriction', () => {
  const meta = { severity_levels: ['low', 'medium', 'high', 'critical'], sensor_types: { temperature: {}, vibration: {}, moisture: {}, pressure: {} } };
  const base = defaultFilters(meta);
  assert.deepEqual([...base.severity], ['low', 'medium', 'high', 'critical']);
  assert.deepEqual([...base.sensorType].sort(), ['moisture', 'pressure', 'temperature', 'vibration']);
  assert.deepEqual([base.status, base.assetId, base.dateFrom, base.dateTo], ['all', null, null, null]);
  assert.notEqual(defaultFilters(meta).severity, base.severity, 'every call builds new Sets');
  assert.equal(filtersAreDefault(base, meta), true);
  assert.equal(filtersAreDefault({ ...base, status: 'active' }, meta), false);
  assert.equal(filtersAreDefault({ ...base, assetId: 'BRG-001' }, meta), false);
  assert.equal(filtersAreDefault({ ...base, dateFrom: '2026-09-02' }, meta), false);
  assert.equal(filtersAreDefault({ ...base, dateTo: '2026-09-02' }, meta), false);
  assert.equal(filtersAreDefault({ ...base, severity: new Set(['critical']) }, meta), false);
  assert.equal(filtersAreDefault({ ...base, sensorType: new Set(['vibration']) }, meta), false);
  assert.deepEqual(NO_SELECTION, { kind: null, id: null });
});
