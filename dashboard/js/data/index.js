/**
 * In-memory index of the loaded data: lookups and the pure helpers that answer "what is the state at hour t"
 * (build contract 12.4). Lists are loaded once and filtered here; everything that varies with time comes from
 * the playback bundle, so the API and the static snapshot give the same picture.
 *
 * `t` is always an index into `playback.timestamps`; times are compared as epoch milliseconds.
 *
 * Pure helpers (exported, Node-tested):
 *   indexOfTime(times, value)                  hour index of a timestamp (floored to the grid, clamped)
 *   indexAnomalies(items, times, formatter)    copies with start_idx / end_idx / peak_idx / start_day
 *   statusAt(entry, t, kind)                   status word of a playback entry ('sensor' | 'asset')
 *   anomalyStatusAt(anomaly, t)                'active' | 'resolved' | 'upcoming'
 *   isActiveAt(anomaly, t)                     start_idx <= t <= end_idx
 *   activeAnomaliesAt(anomalies, t)            anomalies active at t
 *   startedAnomaliesAt(anomalies, t)           anomalies with start_idx <= t (visible in lists)
 *   filterAnomalies(anomalies, filters, t)     list filter of the Anomalies tab
 *   sortAnomaliesForList(anomalies, t)         active first, then severity, then newest
 *   filterSensors(sensors, {sensorType, assetId})
 *   riskLevelOf(score, levels)                 'low' | 'moderate' | 'high' | 'very_high'
 *   trendOf(current, previous, options)        rising | falling | steady, with a dead-band
 *   trendDeadband(sensorType, runParams)       dead-band of a sensor type from the detection run's noise floors
 *   pointInPolygon(point, polygonCoordinates)
 *   buildIndex(raw)                            the `data` object handed to every module (documented below)
 *
 * No DOM access; importable from Node tests.
 */

import { createFormatter, toEpochMs } from '../ui/format.js';
import { ASSET_STATUS_BY_CHAR, SENSOR_STATUS_BY_CHAR, SEVERITY_ORDER } from '../ui/tokens.js';

const HOUR_MS = 3600 * 1000;
/** Cluster hulls stay visible this long after their last anomaly ended (build contract 12.6). */
export const CLUSTER_LINGER_HOURS = 48;
/** The "Needs attention" ranking shows at most this many assets (build contract 12.5). */
export const ATTENTION_LIMIT = 12;

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/**
 * Index of the hour that contains `value` on the grid `times` (ascending epoch ms, constant step).
 * Values before the window give 0, values after it the last index; NaN gives -1.
 */
export function indexOfTime(times, value) {
  const ms = toEpochMs(value);
  if (!Number.isFinite(ms) || times.length === 0) return -1;
  if (times.length === 1) return 0;
  const step = times[1] - times[0];
  return clamp(Math.floor((ms - times[0]) / step), 0, times.length - 1);
}

/**
 * Copies of the anomaly items with integer positions on the time grid:
 *   start_idx, end_idx, peak_idx   hour indexes of started_at / ended_at / peak_at
 *   start_day                      calendar day of started_at in the study-area zone ("YYYY-MM-DD")
 *   severity_rank                  position in the severity scale (higher = more severe)
 */
export function indexAnomalies(items, times, formatter, severityLevels = SEVERITY_ORDER) {
  return items.map((item) => ({
    ...item,
    start_idx: indexOfTime(times, item.started_at),
    end_idx: indexOfTime(times, item.ended_at),
    peak_idx: indexOfTime(times, item.peak_at),
    start_day: formatter ? formatter.dayKey(item.started_at) : '',
    severity_rank: severityLevels.indexOf(item.severity),
  }));
}

/**
 * Status word of a playback entry at hour t.
 * @param {{status: string}|undefined} entry `playback.sensors[id]` or `playback.assets[id]`
 * @param {'sensor'|'asset'} kind
 * @returns {string|null} sensor: normal | warning | anomaly | offline; asset: normal | watch | at_risk | critical;
 *                        null when the entry or the hour does not exist
 */
export function statusAt(entry, t, kind = 'sensor') {
  if (!entry || typeof entry.status !== 'string') return null;
  const char = entry.status.charAt(t);
  const table = kind === 'asset' ? ASSET_STATUS_BY_CHAR : SENSOR_STATUS_BY_CHAR;
  return table[char] || null;
}

/** Active at t <=> started_at <= t <= ended_at (build contract 10.1), in grid indexes. */
export function isActiveAt(anomaly, t) {
  return anomaly.start_idx <= t && t <= anomaly.end_idx;
}

/** 'upcoming' (not started yet at t), 'active' or 'resolved'. */
export function anomalyStatusAt(anomaly, t) {
  if (anomaly.start_idx > t) return 'upcoming';
  return anomaly.end_idx >= t ? 'active' : 'resolved';
}

export function activeAnomaliesAt(anomalies, t) {
  return anomalies.filter((anomaly) => isActiveAt(anomaly, t));
}

export function startedAnomaliesAt(anomalies, t) {
  return anomalies.filter((anomaly) => anomaly.start_idx <= t);
}

/**
 * Anomalies tab filter. Only anomalies that have started by t are listed.
 * @param {object} filters {severity: Set, sensorType: Set, status: 'all'|'active'|'resolved', assetId,
 *                          dateFrom, dateTo} - dateFrom/dateTo are day keys compared with `start_day`
 */
export function filterAnomalies(anomalies, filters, t) {
  return anomalies.filter((anomaly) => {
    if (anomaly.start_idx > t) return false;
    if (filters.severity && !filters.severity.has(anomaly.severity)) return false;
    if (filters.sensorType && !filters.sensorType.has(anomaly.sensor_type)) return false;
    if (filters.status === 'active' && anomaly.end_idx < t) return false;
    if (filters.status === 'resolved' && anomaly.end_idx >= t) return false;
    if (filters.assetId && anomaly.asset_id !== filters.assetId) return false;
    if (filters.dateFrom && anomaly.start_day < filters.dateFrom) return false;
    if (filters.dateTo && anomaly.start_day > filters.dateTo) return false;
    return true;
  });
}

/** Order of the anomaly list: active at t first, then higher severity, then the newest start. */
export function sortAnomaliesForList(anomalies, t) {
  return anomalies.slice().sort((a, b) => {
    const activeA = isActiveAt(a, t) ? 1 : 0;
    const activeB = isActiveAt(b, t) ? 1 : 0;
    if (activeA !== activeB) return activeB - activeA;
    if (a.severity_rank !== b.severity_rank) return b.severity_rank - a.severity_rank;
    if (a.start_idx !== b.start_idx) return b.start_idx - a.start_idx;
    return a.anomaly_id < b.anomaly_id ? -1 : a.anomaly_id > b.anomaly_id ? 1 : 0;
  });
}

/** Sensors tab filter: `sensorType` and `assetId` are optional (null/''/'all' = no restriction). */
export function filterSensors(sensors, { sensorType = null, assetId = null } = {}) {
  const anyType = !sensorType || sensorType === 'all';
  const anyAsset = !assetId || assetId === 'all';
  return sensors.filter(
    (sensor) => (anyType || sensor.sensor_type === sensorType) && (anyAsset || sensor.asset_id === assetId),
  );
}

/** Risk level of a 0-100 score for the thresholds of `meta.risk.levels` ({low: 0, moderate: 25, ...}). */
export function riskLevelOf(score, levels) {
  const ordered = Object.entries(levels).sort((a, b) => a[1] - b[1]);
  let level = ordered.length ? ordered[0][0] : 'low';
  for (const [name, minimum] of ordered) {
    if (score >= minimum) level = name;
  }
  return level;
}

/** The trend of a sensor compares the reading at t with the reading this many hours earlier (build contract 12.5). */
export const TREND_LOOKBACK_HOURS = 24;
/** Dead-band used when the detection run does not publish a noise floor: 5 % of the earlier value. */
export const DEFAULT_TREND_DEADBAND = Object.freeze({ deadband: 0.05, relative: true });

/**
 * Direction of change between two readings, with a dead-band so that noise is not reported as a trend.
 *
 * @param {number|null} current   reading at t
 * @param {number|null} previous  reading at t - 24 h
 * @param {{deadband?: number, relative?: boolean}} [options]
 *        relative = false: steady while |current - previous| <= deadband (same unit as the readings)
 *        relative = true:  steady while |ln(current / previous)| <= deadband (about a relative change; used for
 *                          vibration, which the detector also analyses on a log scale). Falls back to the
 *                          absolute difference when a reading is not positive.
 * @returns {{direction: 'rising'|'falling'|'steady', delta: number}|null} null when either reading is missing
 */
export function trendOf(current, previous, { deadband = 0, relative = false } = {}) {
  const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);
  if (!isNumber(current) || !isNumber(previous)) return null;
  const delta = current - previous;
  const band = Math.max(0, Number(deadband) || 0);
  const change = relative && current > 0 && previous > 0 ? Math.abs(Math.log(current / previous)) : Math.abs(delta);
  if (change <= band || delta === 0) return { direction: 'steady', delta };
  return { direction: delta > 0 ? 'rising' : 'falling', delta };
}

/**
 * Dead-band of a sensor type: the noise floor the detection run used for it (`meta.detection_run.params.baseline`:
 * `scale_floors` per type, on a log scale for `log_domain_types`). A change smaller than one floor is "steady".
 */
export function trendDeadband(sensorType, runParams) {
  const baseline = runParams && runParams.baseline ? runParams.baseline : null;
  const floor = baseline && baseline.scale_floors ? baseline.scale_floors[sensorType] : null;
  if (typeof floor !== 'number' || !Number.isFinite(floor) || floor <= 0) return { ...DEFAULT_TREND_DEADBAND };
  const relative = Array.isArray(baseline.log_domain_types) && baseline.log_domain_types.includes(sensorType);
  return { deadband: floor, relative };
}

/** Even-odd test of [lon, lat] against GeoJSON Polygon coordinates (outer ring + holes). */
export function pointInPolygon(point, rings) {
  const [x, y] = point;
  let inside = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
  }
  return inside;
}

/** [west, south, east, north] of any GeoJSON geometry or feature collection. */
export function boundsOf(geojson) {
  const box = [Infinity, Infinity, -Infinity, -Infinity];
  const visit = (coordinates) => {
    if (typeof coordinates[0] === 'number') {
      box[0] = Math.min(box[0], coordinates[0]);
      box[1] = Math.min(box[1], coordinates[1]);
      box[2] = Math.max(box[2], coordinates[0]);
      box[3] = Math.max(box[3], coordinates[1]);
    } else {
      coordinates.forEach(visit);
    }
  };
  const walk = (node) => {
    if (!node) return;
    if (node.type === 'FeatureCollection') node.features.forEach(walk);
    else if (node.type === 'Feature') walk(node.geometry);
    else if (node.type === 'GeometryCollection') node.geometries.forEach(walk);
    else if (node.coordinates) visit(node.coordinates);
  };
  walk(geojson);
  return Number.isFinite(box[0]) ? box : null;
}

/** Display name of an asset: its name, or the asset id when it has none (build contract 5). */
export function assetDisplayName(properties) {
  if (!properties) return '—';
  return properties.name || properties.asset_id;
}

/** Type wording of an asset: bridges recorded as culverts say so; simulated mains are labelled simulated. */
export function assetTypeLabel(properties, labels) {
  if (!properties) return '—';
  if (properties.asset_type === 'bridge' && properties.structure_kind === 'culvert') return 'Culvert';
  if (properties.asset_type === 'road' && properties.highway_class) {
    const road = String(properties.highway_class).replace(/_/g, ' ');
    return `Road (${road})`;
  }
  return labels[properties.asset_type] || properties.asset_type;
}

function groupBy(items, keyOf) {
  const groups = new Map();
  for (const item of items) {
    const key = keyOf(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return groups;
}

/**
 * Build the `data` object from the raw API/snapshot documents.
 *
 * @param {object} raw {meta, assets, sensors, anomalies, clusters, riskZones, simulationEvents, playback,
 *                      manifest, roads, studyArea}
 * @returns {object} data:
 *   meta, manifest, playback, roads, studyArea
 *   format                       formatter bound to meta.study_area.timezone (js/ui/format.js)
 *   timestamps[], times[] (epoch ms), count, lastIndex, stepMinutes
 *   days[]                       {key, label, firstIndex, lastIndex, hours: [{index, label}]} per calendar day
 *   dayIndexOf(t)                position in days[] of hour t
 *   assets[] (GeoJSON features), sensors[], anomalies[] (indexed copies), clusters[] (features),
 *   riskZones[] (features), simulationEvents[]
 *   monitoredAssets[]            features with at least one sensor
 *   assetById(id), sensorById(id), anomalyById(id), clusterById(id)
 *   sensorsByAsset(assetId), anomaliesByAsset(assetId), anomaliesBySensor(sensorId)
 *   indexOfTime(value), timeAt(t) (ISO string)
 *   assetStatusAt(assetId, t)    normal | watch | at_risk | critical | not_monitored
 *   assetHealthAt(assetId, t)    0-100 or null (not monitored)
 *   sensorStatusAt(sensorId, t)  normal | warning | anomaly | offline
 *   sensorValueAt(sensorId, t)   number or null (no reading that hour)
 *   statsAt(t)                   KPI figures of playback.stats at t + the static counts of meta.counts
 *   activeAnomaliesAt(t), startedAnomaliesAt(t), anomalyStatusAt(anomaly, t)
 *   activeAnomalyCountByAsset(t) Map assetId -> number of anomalies active at t
 *   assetsWithActiveAnomalies(t) number of distinct assets with an anomaly active at t
 *   needsAttention(t, limit)     monitored assets with health below the "normal" band, lowest first
 *   attentionCountAt(t)          how many assets that ranking would hold without the limit
 *   zoneRiskAt(cellId, t)        0-100 integer
 *   zoneCountAt(cellId, t)       anomalies active at t whose point lies in the cell
 *   zoneLevelAt(cellId, t)       low | moderate | high | very_high
 *   anomalySetVersion[t], clusterSetVersion[t]   change counters: equal values at two hours mean the same
 *                                set of visible anomaly markers / cluster hulls (lets the map skip setFilter)
 */
export function buildIndex(raw) {
  const { meta, playback } = raw;
  const format = createFormatter(meta.study_area.timezone);
  const timestamps = playback.timestamps;
  const times = timestamps.map((iso) => Date.parse(iso));
  const count = timestamps.length;
  const lastIndex = count - 1;
  const stepMinutes = meta.time && meta.time.step_minutes ? meta.time.step_minutes : 60;
  const stepsPerHour = 60 / stepMinutes;

  // ---- calendar days of the window, in the study-area zone -------------------------------------------------
  const days = [];
  const dayOfIndex = new Uint16Array(count);
  for (let i = 0; i < count; i += 1) {
    const key = format.dayKey(times[i]);
    let day = days[days.length - 1];
    if (!day || day.key !== key) {
      day = { key, label: format.dateWithWeekday(times[i]), firstIndex: i, lastIndex: i, hours: [] };
      days.push(day);
    }
    day.lastIndex = i;
    day.hours.push({ index: i, label: format.hour(times[i]) });
    dayOfIndex[i] = days.length - 1;
  }

  // ---- entities -----------------------------------------------------------------------------------------
  const assets = raw.assets.features;
  const sensors = raw.sensors.items;
  const anomalies = indexAnomalies(raw.anomalies.items, times, format, meta.severity_levels);
  const clusters = (raw.clusters ? raw.clusters.features : []).map((feature) => {
    const first = indexOfTime(times, feature.properties.first_started_at);
    const last = indexOfTime(times, feature.properties.last_ended_at);
    const linger = Math.round(CLUSTER_LINGER_HOURS * stepsPerHour);
    return {
      ...feature,
      properties: {
        ...feature.properties,
        start_idx: first,
        end_idx: last,
        visible_until_idx: Math.min(lastIndex, last + linger),
      },
    };
  });
  const riskZones = raw.riskZones ? raw.riskZones.features : [];
  const simulationEvents = raw.simulationEvents ? raw.simulationEvents.items : [];

  const assetsById = new Map(assets.map((feature) => [feature.properties.asset_id, feature]));
  const sensorsById = new Map(sensors.map((sensor) => [sensor.sensor_id, sensor]));
  const anomaliesById = new Map(anomalies.map((anomaly) => [anomaly.anomaly_id, anomaly]));
  const clustersById = new Map(clusters.map((feature) => [feature.properties.cluster_id, feature]));
  const sensorsByAssetId = groupBy(sensors, (sensor) => sensor.asset_id);
  const anomaliesByAssetId = groupBy(anomalies, (anomaly) => anomaly.asset_id);
  const anomaliesBySensorId = groupBy(anomalies, (anomaly) => anomaly.sensor_id);
  const monitoredAssets = assets.filter((feature) => feature.properties.monitored);
  const EMPTY = Object.freeze([]);

  // ---- risk cells: which cell holds each anomaly point, and the hourly count per cell ----------------------
  const zoneCounts = new Map();
  for (const zone of riskZones) zoneCounts.set(zone.properties.cell_id, new Uint8Array(count));
  for (const anomaly of anomalies) {
    const cell = riskZones.find((zone) => pointInPolygon([anomaly.lon, anomaly.lat], zone.geometry.coordinates));
    anomaly.cell_id = cell ? cell.properties.cell_id : null;
    if (!cell) continue;
    const counts = zoneCounts.get(anomaly.cell_id);
    for (let i = Math.max(0, anomaly.start_idx); i <= anomaly.end_idx && i < count; i += 1) counts[i] += 1;
  }
  const riskLevels = meta.risk && meta.risk.levels ? meta.risk.levels : { low: 0 };

  // ---- change counters for filter-driven map layers --------------------------------------------------------
  const versionOf = (starts, ends) => {
    const marks = new Uint8Array(count + 1);
    starts.forEach((index) => {
      if (index >= 0 && index < count) marks[index] = 1;
    });
    ends.forEach((index) => {
      if (index + 1 >= 0 && index + 1 < count) marks[index + 1] = 1;
    });
    const version = new Uint16Array(count);
    let current = 0;
    for (let i = 0; i < count; i += 1) {
      if (marks[i]) current += 1;
      version[i] = current;
    }
    return version;
  };
  const anomalySetVersion = versionOf(
    anomalies.map((anomaly) => anomaly.start_idx),
    anomalies.map((anomaly) => anomaly.end_idx),
  );
  const clusterSetVersion = versionOf(
    clusters.map((feature) => feature.properties.start_idx),
    clusters.map((feature) => feature.properties.visible_until_idx),
  );

  // ---- hourly number of monitored assets below the "normal" band -------------------------------------------
  const normalBand = meta.health && meta.health.bands ? meta.health.bands.normal : 90;
  const attentionCounts = new Uint16Array(count);
  for (const entry of Object.values(playback.assets)) {
    const health = entry.health;
    for (let i = 0; i < count; i += 1) {
      if (health[i] !== null && health[i] < normalBand) attentionCounts[i] += 1;
    }
  }

  const stats = playback.stats;
  const counts = meta.counts;
  const clampIndex = (t) => clamp(Math.round(t), 0, lastIndex);

  const data = {
    meta,
    manifest: raw.manifest || null,
    playback,
    roads: raw.roads || null,
    studyArea: raw.studyArea || null,
    format,
    timestamps,
    times,
    count,
    lastIndex,
    stepMinutes,
    days,
    assets,
    sensors,
    anomalies,
    clusters,
    riskZones,
    simulationEvents,
    monitoredAssets,
    anomalySetVersion,
    clusterSetVersion,
    normalBand,

    dayIndexOf: (t) => dayOfIndex[clampIndex(t)],
    indexOfTime: (value) => indexOfTime(times, value),
    timeAt: (t) => timestamps[clampIndex(t)],
    clampIndex,

    assetById: (id) => assetsById.get(id) || null,
    sensorById: (id) => sensorsById.get(id) || null,
    anomalyById: (id) => anomaliesById.get(id) || null,
    clusterById: (id) => clustersById.get(id) || null,
    sensorsByAsset: (assetId) => sensorsByAssetId.get(assetId) || EMPTY,
    anomaliesByAsset: (assetId) => anomaliesByAssetId.get(assetId) || EMPTY,
    anomaliesBySensor: (sensorId) => anomaliesBySensorId.get(sensorId) || EMPTY,

    assetStatusAt(assetId, t) {
      return statusAt(playback.assets[assetId], t, 'asset') || 'not_monitored';
    },
    assetHealthAt(assetId, t) {
      const entry = playback.assets[assetId];
      if (!entry) return null;
      const value = entry.health[t];
      return value === undefined ? null : value;
    },
    sensorStatusAt(sensorId, t) {
      return statusAt(playback.sensors[sensorId], t, 'sensor') || 'offline';
    },
    sensorValueAt(sensorId, t) {
      const entry = playback.sensors[sensorId];
      if (!entry) return null;
      const value = entry.values[t];
      return value === undefined ? null : value;
    },

    statsAt(t) {
      const i = clampIndex(t);
      return {
        index: i,
        as_of: timestamps[i],
        total_assets: counts.assets,
        real_assets: counts.real_assets,
        simulated_assets: counts.simulated_assets,
        monitored_assets: counts.monitored_assets,
        total_sensors: counts.sensors,
        active_sensors: stats.active_sensors[i],
        offline_sensors: stats.offline_sensors[i],
        warning_sensors: stats.warning_sensors[i],
        active_anomalies: stats.active_anomalies[i],
        critical_alerts: stats.critical_alerts[i],
        assets_at_risk: stats.assets_at_risk[i],
      };
    },

    activeAnomaliesAt: (t) => activeAnomaliesAt(anomalies, t),
    startedAnomaliesAt: (t) => startedAnomaliesAt(anomalies, t),
    anomalyStatusAt,

    activeAnomalyCountByAsset(t) {
      const result = new Map();
      for (const anomaly of anomalies) {
        if (isActiveAt(anomaly, t)) result.set(anomaly.asset_id, (result.get(anomaly.asset_id) || 0) + 1);
      }
      return result;
    },
    assetsWithActiveAnomalies(t) {
      return data.activeAnomalyCountByAsset(t).size;
    },

    needsAttention(t, limit = ATTENTION_LIMIT) {
      const active = data.activeAnomalyCountByAsset(t);
      const rows = [];
      for (const feature of monitoredAssets) {
        const id = feature.properties.asset_id;
        const health = data.assetHealthAt(id, t);
        if (health === null || health >= normalBand) continue;
        rows.push({
          asset: feature,
          assetId: id,
          health,
          status: data.assetStatusAt(id, t),
          activeAnomalies: active.get(id) || 0,
        });
      }
      rows.sort(
        (a, b) =>
          a.health - b.health ||
          b.activeAnomalies - a.activeAnomalies ||
          (a.assetId < b.assetId ? -1 : a.assetId > b.assetId ? 1 : 0),
      );
      return Number.isFinite(limit) ? rows.slice(0, limit) : rows;
    },
    attentionCountAt: (t) => attentionCounts[clampIndex(t)],

    zoneRiskAt(cellId, t) {
      const zone = playback.zones[cellId];
      return zone ? zone.risk[t] || 0 : 0;
    },
    zoneCountAt(cellId, t) {
      const cell = zoneCounts.get(cellId);
      return cell ? cell[t] : 0;
    },
    zoneLevelAt(cellId, t) {
      return riskLevelOf(data.zoneRiskAt(cellId, t), riskLevels);
    },
  };
  return data;
}
