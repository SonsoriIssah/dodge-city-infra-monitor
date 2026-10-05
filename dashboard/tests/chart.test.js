/**
 * Pure helpers of js/ui/chart.js: scales, ticks (values and time), gaps, paths, windows, and the chart
 * configurations built from readings.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ANOMALY_CONTEXT_HOURS,
  BENIGN_EVENT_LABEL,
  CHART_CAPTION,
  FUTURE_OPACITY,
  bandPath,
  extentOf,
  formatTick,
  healthChartConfig,
  linePath,
  linearScale,
  niceStep,
  niceTicks,
  runsOf,
  sensorChartConfig,
  stepDecimals,
  thresholdLines,
  timeTicks,
  valueDomain,
  windowAround,
} from '../js/ui/chart.js';
import { createFormatter } from '../js/ui/format.js';

const near = (actual, expected, tolerance = 1e-9) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not ${expected}`);

test('niceStep picks 1, 2 or 5 times a power of ten', () => {
  near(niceStep(100, 4), 50);
  near(niceStep(10, 4), 5);
  near(niceStep(7, 4), 2);
  near(niceStep(3.2, 4), 1);
  near(niceStep(0.9, 4), 0.5);
  near(niceStep(0.31, 4), 0.1);
  near(niceStep(0.07, 4), 0.02);
  near(niceStep(0, 4), 1);
  near(niceStep(Number.NaN, 4), 1);
});

test('stepDecimals gives the decimals a tick label needs', () => {
  assert.deepEqual([50, 5, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.005].map(stepDecimals), [0, 0, 0, 1, 1, 1, 2, 2, 3]);
});

test('niceTicks covers the data on round numbers', () => {
  const pressure = niceTicks(12.1, 71.4, 4);
  assert.deepEqual(pressure.ticks, [0, 20, 40, 60, 80]);
  assert.deepEqual([pressure.min, pressure.max, pressure.step, pressure.decimals], [0, 80, 20, 0]);
  assert.deepEqual(niceTicks(15.2, 29.8, 4).ticks, [15, 20, 25, 30]);
  assert.deepEqual(niceTicks(-3, 7, 4).ticks, [-5, 0, 5, 10]);
  // A flat series still gets an axis around its value.
  const flat = niceTicks(20, 20, 4);
  assert.ok(flat.min < 20 && flat.max > 20);
  assert.deepEqual(niceTicks(Number.NaN, 1).ticks, [0, 1]);
  // Reversed bounds are accepted.
  assert.deepEqual(niceTicks(7, -3, 4).ticks, [-5, 0, 5, 10]);
});

test('vibration axes are labelled with two or three decimals', () => {
  const building = niceTicks(0.052, 0.191, 4);
  assert.deepEqual(building.ticks, [0.05, 0.1, 0.15, 0.2]);
  assert.equal(building.decimals, 2);
  assert.deepEqual(building.ticks.map((value) => formatTick(value, building.decimals)), ['0.05', '0.10', '0.15', '0.20']);
  const quiet = niceTicks(0.0512, 0.0668, 4);
  assert.equal(quiet.decimals, 3);
  assert.deepEqual(quiet.ticks.map((value) => formatTick(value, quiet.decimals)), ['0.050', '0.055', '0.060', '0.065', '0.070']);
  const deck = niceTicks(0.3, 6.4, 4);
  assert.deepEqual(deck.ticks.map((value) => formatTick(value, deck.decimals)), ['0', '2', '4', '6', '8']);
  // Ticks carry no binary noise.
  for (const tick of niceTicks(0.1, 0.7, 4).ticks) assert.equal(tick, Number(tick.toFixed(3)));
});

test('formatTick: fixed decimals, thousands separators, no negative zero', () => {
  assert.equal(formatTick(1.5, 2), '1.50');
  assert.equal(formatTick(12000, 0), '12,000');
  assert.equal(formatTick(-0.0000001, 2), '0.00');
  assert.equal(formatTick(Number.NaN, 2), '');
});

test('linearScale maps both ways', () => {
  const x = linearScale([0, 720], [40, 400]);
  near(x(0), 40);
  near(x(720), 400);
  near(x(360), 220);
  near(x.invert(220), 360);
  const y = linearScale([0, 100], [200, 20]); // screen y grows downwards
  near(y(0), 200);
  near(y(100), 20);
  near(y.invert(110), 50);
  const flat = linearScale([5, 5], [0, 100]);
  near(flat(5), 50);
  near(flat.invert(70), 5);
});

test('extentOf and valueDomain look only at the window and skip missing readings', () => {
  const values = [5, null, 9, 3, undefined, Number.NaN, 20];
  assert.deepEqual(extentOf([values], 0, 6), [3, 20]);
  assert.deepEqual(extentOf([values], 0, 3), [3, 9]);
  assert.deepEqual(extentOf([values, [1, 1, 1, 30]], 0, 3), [1, 30]);
  assert.equal(extentOf([[null, null]], 0, 1), null);
  assert.equal(extentOf([undefined], 0, 5), null);
  const domain = valueDomain([[10, 20]], 0, 1);
  near(domain[0], 9.4);
  near(domain[1], 20.6);
  // Readings that are never negative are not padded below zero.
  assert.equal(valueDomain([[0.01, 2]], 0, 1)[0], 0);
  assert.ok(valueDomain([[-1, 2]], 0, 1)[0] < -1);
  assert.equal(valueDomain([[null]], 0, 0), null);
});

test('runsOf finds the runs of readings between the gaps', () => {
  assert.deepEqual(runsOf([1, 2, null, null, 3, null, 4, 5, 6], 0, 8), [[0, 1], [4, 4], [6, 8]]);
  assert.deepEqual(runsOf([1, 2, null, null, 3, null, 4, 5, 6], 1, 7), [[1, 1], [4, 4], [6, 7]]);
  assert.deepEqual(runsOf([null, null], 0, 1), []);
  assert.deepEqual(runsOf([1, 2, 3], 0, 99), [[0, 2]]);
});

test('linePath breaks at missing readings and draws a lone reading as a dot', () => {
  const x = (index) => index * 10;
  const y = (value) => 100 - value;
  assert.equal(linePath([1, 2, null, 4, 5], 0, 4, x, y), 'M0.0 99.0L10.0 98.0M30.0 96.0L40.0 95.0');
  assert.equal(linePath([1, null, 3, null, 5], 0, 4, x, y), 'M0.0 99.0h0.01M20.0 97.0h0.01M40.0 95.0h0.01');
  assert.equal(linePath([null, null], 0, 1, x, y), '');
  assert.equal((linePath([1, 2, 3, 4, 5], 1, 3, x, y).match(/[ML]/g) || []).length, 3);
});

test('bandPath closes one area per run where both bounds exist', () => {
  const x = (index) => index * 10;
  const y = (value) => 100 - value;
  assert.equal(bandPath([1, 1, 1], [3, 3, 3], 0, 2, x, y), 'M0.0 97.0L10.0 97.0L20.0 97.0L20.0 99.0L10.0 99.0L0.0 99.0Z');
  assert.equal((bandPath([1, 1, null, 1, 1], [3, 3, 3, 3, 3], 0, 4, x, y).match(/Z/g) || []).length, 2);
  assert.equal(bandPath(null, [1, 2], 0, 1, x, y), '');
  assert.equal(bandPath([1, null, 1], [2, 2, 2], 0, 2, x, y), '');
});

test('timeTicks: dates on local midnights for long windows, hours for short ones', () => {
  // 30 days starting at local midnight.
  const month = Array.from({ length: 720 }, (_, index) => index % 24);
  const weekly = timeTicks(0, 719, 5, month);
  assert.deepEqual(weekly.map((tick) => tick.index), [0, 168, 336, 504, 672]);
  assert.ok(weekly.every((tick) => tick.midnight));
  assert.deepEqual(timeTicks(0, 719, 3, month).map((tick) => tick.index), [0, 336, 672]);
  assert.equal(timeTicks(0, 719, 40, month).length, 30);
  // 97 hours around an event; index 0 is 20:00 local time.
  const offset = Array.from({ length: 200 }, (_, index) => (index + 20) % 24);
  assert.deepEqual(timeTicks(0, 96, 5, offset).map((tick) => tick.index), [4, 28, 52, 76]);
  assert.deepEqual(
    timeTicks(0, 96, 9, offset).map((tick) => [tick.index, tick.midnight]),
    [[4, true], [16, false], [28, true], [40, false], [52, true], [64, false], [76, true], [88, false]],
  );
  assert.deepEqual(timeTicks(7, 7, 5, offset), [{ index: 7, midnight: false }]);
  // Never more ticks than fit.
  for (const max of [2, 3, 4, 6, 8]) assert.ok(timeTicks(0, 719, max, month).length <= max);
});

test('windowAround an anomaly: 48 h before the start to 48 h after the end, inside the grid', () => {
  assert.equal(ANOMALY_CONTEXT_HOURS, 48);
  assert.deepEqual(windowAround(300, 320, 719), { from: 252, to: 368 });
  assert.deepEqual(windowAround(10, 12, 719), { from: 0, to: 60 });
  assert.deepEqual(windowAround(692, 719, 719), { from: 644, to: 719 });
  assert.deepEqual(windowAround(100, 100, 719, 6), { from: 94, to: 106 });
  // A 15-minute grid: 48 h are 192 steps.
  assert.deepEqual(windowAround(1000, 1010, 5000, 48, 4), { from: 808, to: 1202 });
});

test('thresholdLines lists only the limits a placement has', () => {
  assert.deepEqual(
    thresholdLines({ warn_low: 40, warn_high: 90, crit_low: 20, crit_high: 110 }, 'psi').map((line) => [line.key, line.value, line.tone, line.label]),
    [
      ['crit_high', 110, 'crit', 'Critical high 110 psi'],
      ['warn_high', 90, 'warn', 'Warning high 90 psi'],
      ['warn_low', 40, 'warn', 'Warning low 40 psi'],
      ['crit_low', 20, 'crit', 'Critical low 20 psi'],
    ],
  );
  assert.deepEqual(
    thresholdLines({ warn_low: null, warn_high: 1, crit_low: null, crit_high: 3 }, 'mm/s').map((line) => line.label),
    ['Critical high 3 mm/s', 'Warning high 1 mm/s'],
  );
  assert.deepEqual(thresholdLines(null, 'psi'), []);
});

// ---- chart configurations -------------------------------------------------------------------------------------
const HOUR = 3600 * 1000;
const START = Date.parse('2026-09-01T05:00:00Z');
const times = Array.from({ length: 72 }, (_, index) => START + index * HOUR);
const at = (index) => new Date(START + index * HOUR).toISOString();
const data = {
  times,
  lastIndex: 71,
  format: createFormatter('America/Chicago'),
  indexOfTime: (value) => Math.max(0, Math.min(71, Math.floor((Date.parse(value) - START) / HOUR))),
};
const localHours = times.map((ms) => data.format.hourOfDay(ms));
const readings = {
  unit: '%',
  value: times.map((_, index) => (index === 5 ? null : 20 + (index % 3))),
  expected: times.map(() => 21),
  expected_low: times.map(() => 19.5),
  expected_high: times.map(() => 22.5),
  robust_z: times.map((_, index) => (index === 40 ? 7.25 : 0.1)),
  flagged: [40, 41],
  thresholds: { warn_low: null, warn_high: 35, crit_low: null, crit_high: 42 },
};
const sensor = { sensor_id: 'MST-001', sensor_type: 'moisture', unit: '%' };
const events = [
  { is_anomaly: false, sensor_type: 'moisture', started_at: at(10), ended_at: at(20), description: 'Simulated regional rain' },
  { is_anomaly: false, sensor_type: 'temperature', started_at: at(10), ended_at: at(20) },
  { is_anomaly: true, sensor_type: 'moisture', sensor_id: 'MST-001', started_at: at(38), ended_at: at(45) },
];
const anomalies = [{ anomaly_id: 'ANM-0007', anomaly_label: 'Unusual moisture increase', severity: 'high', start_idx: 39, end_idx: 44 }];

test('sensorChartConfig: one series, expected band, thresholds, anomaly windows and benign events by sensor type', () => {
  const config = sensorChartConfig({ readings, sensor, anomalies, events, data, localHours, cursor: 50, typeLabel: 'Moisture' });
  assert.equal(config.caption, 'Simulated Sensor Data');
  assert.equal(config.caption, CHART_CAPTION);
  assert.equal(config.title, 'MST-001 · Moisture (%)');
  assert.deepEqual([config.from, config.to, config.cursor], [0, 71, 50]);
  assert.deepEqual(Object.keys(config.series), ['values', 'expected', 'low', 'high']);
  assert.deepEqual(config.refLines.map((line) => line.label), ['Critical high 42 %', 'Warning high 35 %']);
  // Only the benign event of the sensor's own type is shaded; injected events are never drawn from ground truth.
  assert.deepEqual(config.windows.map((entry) => [entry.tone, entry.from, entry.to]), [['event', 10, 20], ['anomaly', 39, 44]]);
  assert.equal(config.windows[0].label, 'Simulated regional event (benign — not flagged)');
  assert.equal(config.windows[0].label, BENIGN_EVENT_LABEL);
  assert.equal(config.windows[1].severity, 'high');
  assert.deepEqual(config.legend.map((item) => item.label), ['Observed', 'Expected', 'Expected range', 'Anomaly window', BENIGN_EVENT_LABEL]);
  assert.match(config.ariaLabel, /Moisture readings of simulated sensor MST-001 in %/);
  assert.match(config.ariaLabel, /Sep 1, 12:00 AM CDT to Sep 3, 11:00 PM CDT/);
  assert.match(config.ariaLabel, /1 shaded anomaly window, 1 benign simulated regional event/);
});

test('sensorChartConfig: the tooltip names time, value, expected, z and the flag; the table twin has the same rows', () => {
  const config = sensorChartConfig({ readings, sensor, anomalies, events, data, localHours, typeLabel: 'Moisture' });
  const at40 = config.describe(40);
  assert.equal(at40.title, 'Sep 2, 4:00 PM CDT');
  assert.deepEqual(at40.rows, [
    ['Observed', '21 %', true],
    ['Expected', '21 %'],
    ['Expected range', '19.5 – 22.5 %'],
    ['Robust z', '+7.25'],
    ['Flagged', 'Yes'],
  ]);
  assert.deepEqual(at40.notes, ['Unusual moisture increase (ANM-0007, High)']);
  const gap = config.describe(5);
  assert.equal(gap.rows[0][1], 'No reading');
  assert.equal(gap.rows[4][1], 'No');
  assert.deepEqual(config.describe(12).notes, [BENIGN_EVENT_LABEL]);
  assert.deepEqual(
    config.table.columns.map((column) => column.label),
    ['Time (CDT)', 'Observed (%)', 'Expected', 'Expected range', 'Robust z', 'Flagged', 'Window'],
  );
  assert.deepEqual(config.table.rowAt(40), ['Sep 2, 4:00 PM', '21', '21', '19.5 – 22.5', '+7.25', 'Yes', 'Unusual moisture increase (ANM-0007, High)']);
  assert.equal(config.table.rowAt(5)[1], 'No reading');
});

test('sensorChartConfig: a window around an anomaly emphasises it and keeps the legend honest', () => {
  const config = sensorChartConfig({ readings, sensor, anomalies, events, data, localHours, from: 30, to: 71, highlightAnomalyId: 'ANM-0007', legend: true });
  assert.equal(config.windows.find((entry) => entry.tone === 'anomaly').emphasised, true);
  // The benign event (hours 10-20) lies outside this window: it is not in the legend.
  assert.deepEqual(config.legend.map((item) => item.label), ['Observed', 'Expected', 'Expected range', 'Anomaly window']);
  assert.equal(sensorChartConfig({ readings, sensor, anomalies, events, data, localHours, legend: false }).legend, null);
});

test('healthChartConfig: fixed 0-100 axis, the at-risk line, penalties in the tooltip when loaded', () => {
  const health = times.map((_, index) => (index < 40 ? 100 : 58));
  const bare = healthChartConfig({ health, data, localHours, cursor: 71, atRiskBelow: 70, assetName: 'Bridge' });
  assert.deepEqual(bare.yDomain, [0, 100]);
  assert.deepEqual(bare.refLines.map((line) => [line.value, line.label]), [[70, 'At risk below 70']]);
  assert.equal(bare.variant, 'spark');
  assert.deepEqual(bare.describe(50).rows, [['Health score', '58 of 100', true]]);
  const history = {
    frequency_penalty: times.map(() => 9.3),
    severity_penalty: times.map(() => 30.6),
    reading_penalty: times.map(() => 2),
    sensor_penalty: times.map(() => 0),
  };
  const full = healthChartConfig({ health, history, data, localHours, atRiskBelow: 70, assetName: 'Bridge' });
  assert.deepEqual(full.describe(50).rows.map((row) => row[1]), ['58 of 100', '9.3', '30.6', '2', '0']);
  assert.deepEqual(full.table.rowAt(50), ['Sep 3, 2:00 AM', '58', '9.3', '30.6', '2', '0']);
  assert.equal(FUTURE_OPACITY, 0.35);
});
