/**
 * Pure helpers of js/data/index.js: status at t, active anomalies at t, the anomaly filters in every
 * combination, list order, the "Needs attention" ranking, the trend dead-band - on a small hand-made data set
 * and on the committed snapshot.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  ATTENTION_LIMIT,
  activeAnomaliesAt,
  anomalyStatusAt,
  assetDisplayName,
  assetTypeLabel,
  buildIndex,
  filterAnomalies,
  filterSensors,
  indexAnomalies,
  indexOfTime,
  isActiveAt,
  riskLevelOf,
  sortAnomaliesForList,
  startedAnomaliesAt,
  statusAt,
  trendDeadband,
  trendOf,
} from '../js/data/index.js';
import { createFormatter } from '../js/ui/format.js';

const HOUR = 3600 * 1000;
const START = Date.parse('2026-09-01T05:00:00Z');
const times = Array.from({ length: 48 }, (_, i) => START + i * HOUR);
const iso = (index) => new Date(START + index * HOUR).toISOString().replace('.000Z', 'Z');
const format = createFormatter('America/Chicago');

/** Anomaly item on the 48-hour grid. */
function anomaly(id, start, end, severity, sensorType, assetId, sensorId = `S-${id}`) {
  return {
    anomaly_id: id,
    started_at: iso(start),
    ended_at: iso(end),
    peak_at: iso(Math.round((start + end) / 2)),
    severity,
    sensor_type: sensorType,
    asset_id: assetId,
    sensor_id: sensorId,
    lon: -100.0,
    lat: 37.75,
  };
}

const RAW_ANOMALIES = [
  anomaly('A1', 2, 5, 'low', 'vibration', 'X'), // day 1, resolved early
  anomaly('A2', 10, 30, 'critical', 'pressure', 'Y'), // spans the day boundary
  anomaly('A3', 24, 24, 'medium', 'vibration', 'X'), // one hour: index 24 is local midnight of day 2
  anomaly('A4', 25, 47, 'high', 'moisture', 'Z'), // active at the end
  anomaly('A5', 40, 44, 'critical', 'temperature', 'Y'),
];
const anomalies = indexAnomalies(RAW_ANOMALIES, times, format);
const ids = (list) => list.map((item) => item.anomaly_id);
const ALL = { severity: new Set(['low', 'medium', 'high', 'critical']), sensorType: new Set(['vibration', 'pressure', 'moisture', 'temperature']), status: 'all', assetId: null, dateFrom: null, dateTo: null };
const withFilters = (patch) => ({ ...ALL, ...patch });

test('indexOfTime floors to the grid and clamps to the window', () => {
  assert.equal(indexOfTime(times, iso(0)), 0);
  assert.equal(indexOfTime(times, START + 3 * HOUR + 59 * 60 * 1000), 3);
  assert.equal(indexOfTime(times, START - 5 * HOUR), 0);
  assert.equal(indexOfTime(times, START + 500 * HOUR), 47);
  assert.equal(indexOfTime(times, 'not a date'), -1);
  assert.equal(indexOfTime([], iso(0)), -1);
});

test('indexAnomalies adds grid positions, the local start day and the severity rank', () => {
  const a2 = anomalies[1];
  assert.deepEqual([a2.start_idx, a2.end_idx, a2.peak_idx], [10, 30, 20]);
  assert.equal(anomalies[0].start_day, '2026-09-01');
  // Index 24 = 2026-09-02T00:00 in America/Chicago (05:00Z).
  assert.equal(anomalies[2].start_day, '2026-09-02');
  assert.deepEqual(anomalies.map((item) => item.severity_rank), [0, 3, 1, 2, 3]);
});

test('statusAt decodes one character per hour for sensors and assets', () => {
  const sensor = { status: 'nwao' };
  assert.deepEqual([0, 1, 2, 3].map((t) => statusAt(sensor, t, 'sensor')), ['normal', 'warning', 'anomaly', 'offline']);
  const asset = { status: 'nwrc' };
  assert.deepEqual([0, 1, 2, 3].map((t) => statusAt(asset, t, 'asset')), ['normal', 'watch', 'at_risk', 'critical']);
  assert.equal(statusAt(sensor, 9, 'sensor'), null);
  assert.equal(statusAt(undefined, 0, 'sensor'), null);
  assert.equal(statusAt({ status: 'x' }, 0, 'asset'), null);
});

test('an anomaly is active at t exactly while started_at <= t <= ended_at', () => {
  const a2 = anomalies[1];
  assert.equal(isActiveAt(a2, 9), false);
  assert.equal(isActiveAt(a2, 10), true);
  assert.equal(isActiveAt(a2, 30), true);
  assert.equal(isActiveAt(a2, 31), false);
  assert.equal(anomalyStatusAt(a2, 9), 'upcoming');
  assert.equal(anomalyStatusAt(a2, 10), 'active');
  assert.equal(anomalyStatusAt(a2, 30), 'active');
  assert.equal(anomalyStatusAt(a2, 31), 'resolved');
});

test('activeAnomaliesAt and startedAnomaliesAt follow the clock', () => {
  assert.deepEqual(ids(activeAnomaliesAt(anomalies, 0)), []);
  assert.deepEqual(ids(activeAnomaliesAt(anomalies, 4)), ['A1']);
  assert.deepEqual(ids(activeAnomaliesAt(anomalies, 20)), ['A2']);
  assert.deepEqual(ids(activeAnomaliesAt(anomalies, 24)), ['A2', 'A3']);
  assert.deepEqual(ids(activeAnomaliesAt(anomalies, 42)), ['A4', 'A5']);
  assert.deepEqual(ids(activeAnomaliesAt(anomalies, 47)), ['A4']);
  assert.deepEqual(ids(startedAnomaliesAt(anomalies, 1)), []);
  assert.deepEqual(ids(startedAnomaliesAt(anomalies, 20)), ['A1', 'A2']);
  assert.deepEqual(ids(startedAnomaliesAt(anomalies, 24)), ['A1', 'A2', 'A3']);
  assert.deepEqual(ids(startedAnomaliesAt(anomalies, 47)), ['A1', 'A2', 'A3', 'A4', 'A5']);
});

test('filterAnomalies: each filter on its own', () => {
  assert.deepEqual(ids(filterAnomalies(anomalies, ALL, 47)), ['A1', 'A2', 'A3', 'A4', 'A5']);
  // Only anomalies that have started by t are listed.
  assert.deepEqual(ids(filterAnomalies(anomalies, ALL, 12)), ['A1', 'A2']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ severity: new Set(['critical']) }), 47)), ['A2', 'A5']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ severity: new Set() }), 47)), []);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ sensorType: new Set(['vibration']) }), 47)), ['A1', 'A3']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ status: 'active' }), 42)), ['A4', 'A5']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ status: 'resolved' }), 42)), ['A1', 'A2', 'A3']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ assetId: 'Y' }), 47)), ['A2', 'A5']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ dateFrom: '2026-09-02' }), 47)), ['A3', 'A4', 'A5']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ dateTo: '2026-09-01' }), 47)), ['A1', 'A2']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ dateFrom: '2026-09-02', dateTo: '2026-09-02' }), 47)), ['A3', 'A4', 'A5']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ dateFrom: '2026-09-03' }), 47)), []);
});

test('filterAnomalies: status is evaluated at t, not at the end of the window', () => {
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ status: 'active' }), 24)), ['A2', 'A3']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ status: 'resolved' }), 24)), ['A1']);
  assert.deepEqual(ids(filterAnomalies(anomalies, withFilters({ status: 'active' }), 47)), ['A4']);
});

test('filterAnomalies: every combination of the five filters equals the intersection of the single filters', () => {
  const options = {
    severity: [ALL.severity, new Set(['critical']), new Set(['low', 'medium'])],
    sensorType: [ALL.sensorType, new Set(['vibration']), new Set(['pressure', 'temperature'])],
    status: ['all', 'active', 'resolved'],
    assetId: [null, 'X', 'Y'],
    date: [[null, null], ['2026-09-02', null], [null, '2026-09-01'], ['2026-09-01', '2026-09-01']],
  };
  let combinations = 0;
  for (const t of [4, 24, 42, 47]) {
    for (const severity of options.severity) {
      for (const sensorType of options.sensorType) {
        for (const status of options.status) {
          for (const assetId of options.assetId) {
            for (const [dateFrom, dateTo] of options.date) {
              const combined = ids(filterAnomalies(anomalies, { severity, sensorType, status, assetId, dateFrom, dateTo }, t));
              const single = [
                filterAnomalies(anomalies, withFilters({ severity }), t),
                filterAnomalies(anomalies, withFilters({ sensorType }), t),
                filterAnomalies(anomalies, withFilters({ status }), t),
                filterAnomalies(anomalies, withFilters({ assetId }), t),
                filterAnomalies(anomalies, withFilters({ dateFrom, dateTo }), t),
              ].map((list) => new Set(ids(list)));
              const expected = ids(anomalies).filter((id) => single.every((set) => set.has(id)));
              assert.deepEqual(combined, expected);
              combinations += 1;
            }
          }
        }
      }
    }
  }
  assert.equal(combinations, 4 * 3 * 3 * 3 * 3 * 4);
});

test('sortAnomaliesForList: active first, then severity, then the newest start', () => {
  assert.deepEqual(ids(sortAnomaliesForList(anomalies, 42)), ['A5', 'A4', 'A2', 'A3', 'A1']);
  assert.deepEqual(ids(sortAnomaliesForList(anomalies, 24)), ['A2', 'A3', 'A5', 'A4', 'A1']);
  // Same activity and severity: the newer start comes first.
  assert.deepEqual(ids(sortAnomaliesForList(anomalies, 0)), ['A5', 'A2', 'A4', 'A3', 'A1']);
  // The input is not reordered.
  assert.deepEqual(ids(anomalies), ['A1', 'A2', 'A3', 'A4', 'A5']);
});

test('filterSensors narrows by sensor type and by asset', () => {
  const sensors = [
    { sensor_id: 'V1', sensor_type: 'vibration', asset_id: 'X' },
    { sensor_id: 'V2', sensor_type: 'vibration', asset_id: 'Y' },
    { sensor_id: 'T1', sensor_type: 'temperature', asset_id: 'X' },
  ];
  const got = (filters) => filterSensors(sensors, filters).map((sensor) => sensor.sensor_id);
  assert.deepEqual(got({}), ['V1', 'V2', 'T1']);
  assert.deepEqual(got({ sensorType: 'all', assetId: '' }), ['V1', 'V2', 'T1']);
  assert.deepEqual(got({ sensorType: 'vibration' }), ['V1', 'V2']);
  assert.deepEqual(got({ assetId: 'X' }), ['V1', 'T1']);
  assert.deepEqual(got({ sensorType: 'vibration', assetId: 'X' }), ['V1']);
  assert.deepEqual(got({ sensorType: 'pressure', assetId: 'X' }), []);
});

test('riskLevelOf uses the thresholds of meta.risk.levels', () => {
  const levels = { low: 0, moderate: 25, high: 50, very_high: 75 };
  assert.deepEqual([0, 24.9, 25, 49, 50, 74, 75, 100].map((score) => riskLevelOf(score, levels)), ['low', 'low', 'moderate', 'moderate', 'high', 'high', 'very_high', 'very_high']);
});

test('asset wording: name or id, culverts and road classes', () => {
  const labels = { building: 'Building', bridge: 'Bridge', road: 'Road', water_main: 'Simulated water main' };
  assert.equal(assetDisplayName({ asset_id: 'BLD-0007', name: null }), 'BLD-0007');
  assert.equal(assetDisplayName({ asset_id: 'BRG-001', name: '2nd. Avenue over Arkansas River' }), '2nd. Avenue over Arkansas River');
  assert.equal(assetTypeLabel({ asset_type: 'bridge', structure_kind: 'culvert' }, labels), 'Culvert');
  assert.equal(assetTypeLabel({ asset_type: 'bridge', structure_kind: 'bridge' }, labels), 'Bridge');
  assert.equal(assetTypeLabel({ asset_type: 'road', highway_class: 'primary_link' }, labels), 'Road (primary link)');
  assert.equal(assetTypeLabel({ asset_type: 'water_main' }, labels), 'Simulated water main');
});

test('trendOf: rising, falling, and steady inside the dead-band', () => {
  assert.deepEqual(trendOf(21.0, 20.0, { deadband: 0.5 }), { direction: 'rising', delta: 1 });
  assert.equal(trendOf(19.0, 20.0, { deadband: 0.5 }).direction, 'falling');
  assert.equal(trendOf(20.4, 20.0, { deadband: 0.5 }).direction, 'steady');
  assert.equal(trendOf(19.6, 20.0, { deadband: 0.5 }).direction, 'steady');
  // The band itself is still steady; just outside it is a trend.
  assert.equal(trendOf(20.5, 20.0, { deadband: 0.5 }).direction, 'steady');
  assert.equal(trendOf(20.51, 20.0, { deadband: 0.5 }).direction, 'rising');
  assert.equal(trendOf(20, 20, { deadband: 0 }).direction, 'steady');
  assert.equal(trendOf(20.001, 20, {}).direction, 'rising');
});

test('trendOf: relative dead-band on a log scale (vibration)', () => {
  const band = { deadband: 0.1, relative: true };
  assert.equal(trendOf(1.05, 1.0, band).direction, 'steady'); // ln(1.05) = 0.049
  assert.equal(trendOf(1.2, 1.0, band).direction, 'rising'); // ln(1.2) = 0.182
  assert.equal(trendOf(0.8, 1.0, band).direction, 'falling');
  // The same absolute change is a trend for a quiet sensor and noise for a loud one.
  assert.equal(trendOf(0.13, 0.1, band).direction, 'rising');
  assert.equal(trendOf(5.03, 5.0, band).direction, 'steady');
  // A reading of zero cannot be compared on a log scale: the absolute difference is used.
  assert.equal(trendOf(0.5, 0, band).direction, 'rising');
});

test('trendOf: no trend without both readings', () => {
  assert.equal(trendOf(null, 1, { deadband: 0.5 }), null);
  assert.equal(trendOf(1, null, { deadband: 0.5 }), null);
  assert.equal(trendOf(undefined, undefined), null);
  assert.equal(trendOf(Number.NaN, 1), null);
});

test('trendDeadband comes from the noise floors of the detection run', () => {
  const params = { baseline: { scale_floors: { temperature: 0.5, vibration: 0.1 }, log_domain_types: ['vibration'] } };
  assert.deepEqual(trendDeadband('temperature', params), { deadband: 0.5, relative: false });
  assert.deepEqual(trendDeadband('vibration', params), { deadband: 0.1, relative: true });
  assert.deepEqual(trendDeadband('pressure', params), { deadband: 0.05, relative: true });
  assert.deepEqual(trendDeadband('pressure', null), { deadband: 0.05, relative: true });
});

// ---- buildIndex on a hand-made data set --------------------------------------------------------------------------
function smallRaw() {
  const count = 4;
  const timestamps = Array.from({ length: count }, (_, i) => iso(i));
  const asset = (id, monitored, name = null) => ({ type: 'Feature', geometry: null, properties: { asset_id: id, asset_type: 'building', name, monitored, centroid: [-100, 37.75] } });
  return {
    meta: {
      study_area: { timezone: 'America/Chicago' },
      time: { step_minutes: 60 },
      severity_levels: ['low', 'medium', 'high', 'critical'],
      sensor_types: { vibration: { unit: 'mm/s' } },
      health: { bands: { normal: 90, watch: 70, at_risk: 45, critical: 0 } },
      risk: { levels: { low: 0, moderate: 25, high: 50, very_high: 75 } },
      counts: { assets: 5, real_assets: 5, simulated_assets: 0, monitored_assets: 4, sensors: 4 },
    },
    assets: { features: [asset('A', true, 'Alpha'), asset('B', true), asset('C', true), asset('D', true), asset('E', false)] },
    sensors: { items: ['A', 'B', 'C', 'D'].map((id) => ({ sensor_id: `V-${id}`, asset_id: id, sensor_type: 'vibration', unit: 'mm/s' })) },
    anomalies: {
      items: [
        { anomaly_id: 'N1', sensor_id: 'V-A', asset_id: 'A', sensor_type: 'vibration', severity: 'high', started_at: iso(1), ended_at: iso(3), peak_at: iso(2), lon: -100, lat: 37.75 },
        { anomaly_id: 'N2', sensor_id: 'V-B', asset_id: 'B', sensor_type: 'vibration', severity: 'low', started_at: iso(2), ended_at: iso(2), peak_at: iso(2), lon: -100, lat: 37.75 },
        { anomaly_id: 'N3', sensor_id: 'V-B', asset_id: 'B', sensor_type: 'vibration', severity: 'low', started_at: iso(2), ended_at: iso(3), peak_at: iso(3), lon: -100, lat: 37.75 },
      ],
    },
    clusters: { features: [] },
    riskZones: { features: [] },
    simulationEvents: { items: [] },
    playback: {
      timestamps,
      sensors: {
        'V-A': { values: [0.2, 0.9, null, 1.1], status: 'naoa' },
        'V-B': { values: [0.3, 0.3, 0.6, 0.6], status: 'nnaa' },
        'V-C': { values: [0.1, 0.1, 0.1, 0.1], status: 'nnnn' },
        'V-D': { values: [0.1, 0.1, 0.1, 0.1], status: 'nwnn' },
      },
      assets: {
        A: { health: [100, 62, 60, 60], status: 'nrrr' },
        B: { health: [100, 100, 60, 89], status: 'nnrw' },
        C: { health: [100, 100, 90, 60], status: 'nnnr' },
        D: { health: [100, 95, 44, 100], status: 'nncn' },
      },
      zones: {},
      stats: { active_sensors: [4, 4, 3, 4], offline_sensors: [0, 0, 1, 0], warning_sensors: [0, 1, 0, 0], active_anomalies: [0, 1, 3, 2], critical_alerts: [0, 0, 0, 0], assets_at_risk: [0, 1, 3, 2] },
    },
    manifest: null,
  };
}

test('buildIndex: status, health and values at t come from the playback bundle', () => {
  const data = buildIndex(smallRaw());
  assert.equal(data.lastIndex, 3);
  assert.equal(data.sensorStatusAt('V-A', 2), 'offline');
  assert.equal(data.sensorValueAt('V-A', 2), null);
  assert.equal(data.sensorValueAt('V-A', 3), 1.1);
  assert.equal(data.sensorStatusAt('missing', 0), 'offline');
  assert.equal(data.assetStatusAt('A', 1), 'at_risk');
  assert.equal(data.assetStatusAt('E', 1), 'not_monitored');
  assert.equal(data.assetHealthAt('E', 1), null);
  assert.equal(data.assetHealthAt('D', 2), 44);
  assert.deepEqual(data.monitoredAssets.map((feature) => feature.properties.asset_id), ['A', 'B', 'C', 'D']);
  assert.deepEqual(data.statsAt(2), {
    index: 2, as_of: iso(2), total_assets: 5, real_assets: 5, simulated_assets: 0, monitored_assets: 4, total_sensors: 4,
    active_sensors: 3, offline_sensors: 1, warning_sensors: 0, active_anomalies: 3, critical_alerts: 0, assets_at_risk: 3,
  });
  assert.equal(data.activeAnomaliesAt(2).length, 3);
  assert.deepEqual([...data.activeAnomalyCountByAsset(2)], [['A', 1], ['B', 2]]);
  assert.equal(data.assetsWithActiveAnomalies(3), 2);
});

test('needsAttention: monitored assets below the normal band, lowest health first', () => {
  const data = buildIndex(smallRaw());
  const ranking = (t, limit) => data.needsAttention(t, limit).map((row) => `${row.assetId}:${row.health}:${row.status}:${row.activeAnomalies}`);
  assert.deepEqual(ranking(0), []);
  assert.deepEqual(ranking(1), ['A:62:at_risk:1']);
  // Ties on health: more active anomalies first, then the asset id. 90 is normal and not listed.
  assert.deepEqual(ranking(2), ['D:44:critical:0', 'B:60:at_risk:2', 'A:60:at_risk:1']);
  assert.deepEqual(ranking(3), ['A:60:at_risk:1', 'C:60:at_risk:0', 'B:89:watch:1']);
  assert.deepEqual(ranking(2, 2), ['D:44:critical:0', 'B:60:at_risk:2']);
  assert.deepEqual([0, 1, 2, 3].map((t) => data.attentionCountAt(t)), [0, 1, 3, 3]);
  assert.equal(ATTENTION_LIMIT, 12);
});

test('buildIndex: days and hours map back to grid indexes', () => {
  const raw = smallRaw();
  const timestamps = Array.from({ length: 48 }, (_, i) => iso(i));
  raw.playback.timestamps = timestamps;
  for (const entry of Object.values(raw.playback.sensors)) {
    entry.values = Array.from({ length: 48 }, () => 0.1);
    entry.status = 'n'.repeat(48);
  }
  for (const entry of Object.values(raw.playback.assets)) {
    entry.health = Array.from({ length: 48 }, () => 100);
    entry.status = 'n'.repeat(48);
  }
  for (const key of Object.keys(raw.playback.stats)) raw.playback.stats[key] = Array.from({ length: 48 }, () => 0);
  const data = buildIndex(raw);
  assert.deepEqual(data.days.map((day) => [day.key, day.firstIndex, day.lastIndex, day.hours.length]), [
    ['2026-09-01', 0, 23, 24],
    ['2026-09-02', 24, 47, 24],
  ]);
  assert.equal(data.days[0].label, 'Tue, Sep 1');
  assert.equal(data.days[0].hours[0].label, '12:00 AM');
  assert.equal(data.days[1].hours[13].label, '1:00 PM');
  // Every (day, hour) option maps back to exactly its index.
  const seen = [];
  data.days.forEach((day, position) => {
    day.hours.forEach((hour) => {
      assert.equal(data.dayIndexOf(hour.index), position);
      seen.push(hour.index);
    });
  });
  assert.deepEqual(seen, Array.from({ length: 48 }, (_, i) => i));
  assert.equal(data.indexOfTime(timestamps[30]), 30);
  assert.equal(data.timeAt(30), timestamps[30]);
});

// ---- the committed snapshot ------------------------------------------------------------------------------------
const snapshot = (name) => JSON.parse(readFileSync(new URL(`../data/snapshot/${name}`, import.meta.url), 'utf8'));

test('snapshot: the index agrees with the playback bundle at the end of the simulation', () => {
  const playback = snapshot('playback.json');
  const data = buildIndex({
    meta: snapshot('meta.json'),
    assets: snapshot('assets.geojson'),
    sensors: snapshot('sensors.json'),
    anomalies: snapshot('anomalies.json'),
    clusters: snapshot('clusters.geojson'),
    riskZones: snapshot('risk-zones.geojson'),
    simulationEvents: snapshot('simulation-events.json'),
    playback,
    manifest: snapshot('manifest.json'),
  });
  const t = data.lastIndex;
  assert.equal(data.count, data.meta.time.count);
  assert.equal(data.timestamps[t], data.meta.time.end);

  // KPI figures at the last hour.
  const stats = data.statsAt(t);
  assert.equal(stats.active_anomalies, data.activeAnomaliesAt(t).length);
  assert.equal(stats.critical_alerts, data.activeAnomaliesAt(t).filter((item) => item.severity === 'critical').length);
  assert.equal(stats.active_sensors + stats.offline_sensors, stats.total_sensors);
  assert.equal(stats.offline_sensors, data.sensors.filter((sensor) => data.sensorStatusAt(sensor.sensor_id, t) === 'offline').length);
  const atRiskBelow = data.meta.health.at_risk_below;
  assert.equal(stats.assets_at_risk, Object.values(playback.assets).filter((entry) => entry.health[t] < atRiskBelow).length);

  // The list values of /assets and /anomalies are the values at the end of the simulation.
  for (const feature of data.monitoredAssets) {
    assert.equal(data.assetHealthAt(feature.properties.asset_id, t), feature.properties.health_score);
    assert.equal(data.assetStatusAt(feature.properties.asset_id, t), feature.properties.status);
  }
  for (const item of data.anomalies) assert.equal(data.anomalyStatusAt(item, t), item.status);

  // Ranking: every monitored asset below the normal band, lowest first, at most the limit.
  const below = Object.entries(playback.assets).filter(([, entry]) => entry.health[t] < data.normalBand);
  const ranking = data.needsAttention(t);
  assert.equal(ranking.length, Math.min(ATTENTION_LIMIT, below.length));
  assert.equal(data.attentionCountAt(t), below.length);
  assert.ok(ranking.length > 0, 'the default data set ends with assets that need attention');
  ranking.forEach((row, position) => {
    assert.equal(row.health, playback.assets[row.assetId].health[t]);
    if (position > 0) assert.ok(ranking[position - 1].health <= row.health);
  });
  assert.equal(ranking[0].health, Math.min(...below.map(([, entry]) => entry.health[t])));

  // At the first hour nothing has happened yet (quiet lead-in of the simulation).
  assert.equal(data.needsAttention(0).length, 0);
  assert.equal(data.startedAnomaliesAt(0).length, 0);
});
