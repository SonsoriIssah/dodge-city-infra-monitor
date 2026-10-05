/**
 * Design tokens shared by the map, the charts and the panels.
 *
 * The hex values mirror `css/tokens.css` one to one (build contract 12.6): MapLibre paint expressions and
 * canvas drawing cannot read CSS custom properties, so the same values are kept here. Change both files together.
 *
 * Status and severity are never shown by colour alone: every vocabulary below carries a text label, and the
 * map adds a form (size, stroke, hollow ring) on top of the colour.
 *
 * Pure module: no DOM access, importable from Node tests.
 */

export const SURFACE = Object.freeze({
  bg: '#0b1016',
  panel: '#111821',
  card: '#151e29',
  line: '#233142',
  text: '#e6edf5',
  muted: '#8a9bb0',
  accent: '#38bdf8',
});

/** Derived Asset Health Score bands (asset status). */
export const ASSET_STATUS = Object.freeze({
  normal: '#2dd4bf',
  watch: '#f5b942',
  at_risk: '#fb8c3c',
  critical: '#ef5a45',
  not_monitored: '#2f3b4a',
});

/** Sensor status at a point of the playback. */
export const SENSOR_STATUS = Object.freeze({
  normal: '#2dd4bf',
  warning: '#f5b942',
  anomaly: '#ef5a45',
  offline: '#64748b',
});

/** Anomaly severity. */
export const SEVERITY = Object.freeze({
  low: '#8fb8de',
  medium: '#f5b942',
  high: '#fb8c3c',
  critical: '#ef5a45',
});

/** Risk-zone levels use the severity ramp. */
export const RISK_LEVEL = Object.freeze({
  low: SEVERITY.low,
  moderate: SEVERITY.medium,
  high: SEVERITY.high,
  very_high: SEVERITY.critical,
});

/** Neutral used for context-only line and point assets (rail, power, street lights) so they stay legible. */
export const CONTEXT_FEATURE = '#6b7c93';

export const ASSET_STATUS_ORDER = Object.freeze(['normal', 'watch', 'at_risk', 'critical', 'not_monitored']);
export const SENSOR_STATUS_ORDER = Object.freeze(['normal', 'warning', 'anomaly', 'offline']);
export const SEVERITY_ORDER = Object.freeze(['low', 'medium', 'high', 'critical']);
export const RISK_LEVEL_ORDER = Object.freeze(['low', 'moderate', 'high', 'very_high']);

export const ASSET_STATUS_LABEL = Object.freeze({
  normal: 'Normal',
  watch: 'Watch',
  at_risk: 'At risk',
  critical: 'Critical',
  not_monitored: 'Not monitored',
});

export const SENSOR_STATUS_LABEL = Object.freeze({
  normal: 'Normal',
  warning: 'Warning',
  anomaly: 'Anomaly',
  offline: 'Offline',
});

export const SEVERITY_LABEL = Object.freeze({
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  critical: 'Critical',
});

export const RISK_LEVEL_LABEL = Object.freeze({
  low: 'Low',
  moderate: 'Moderate',
  high: 'High',
  very_high: 'Very high',
});

export const ANOMALY_STATUS_LABEL = Object.freeze({
  active: 'Active',
  resolved: 'Resolved',
  upcoming: 'Not started',
});

/** Playback bundle encoding: one character per hour (build contract 10.4). */
export const ASSET_STATUS_BY_CHAR = Object.freeze({ n: 'normal', w: 'watch', r: 'at_risk', c: 'critical' });
export const SENSOR_STATUS_BY_CHAR = Object.freeze({ n: 'normal', w: 'warning', a: 'anomaly', o: 'offline' });

/** Sensor type is encoded by a letter, never by hue. */
export const SENSOR_TYPE_GLYPH = Object.freeze({
  temperature: 'T',
  vibration: 'V',
  moisture: 'M',
  pressure: 'P',
});

export const ASSET_TYPE_LABEL = Object.freeze({
  building: 'Building',
  road: 'Road',
  bridge: 'Bridge',
  rail: 'Rail',
  power: 'Power',
  street_light: 'Street light',
  water_main: 'Simulated water main',
});

/** Provenance tags of `meta.data_sources[].kind`. */
export const SOURCE_KIND_LABEL = Object.freeze({
  real: 'REAL',
  simulated: 'SIMULATED',
  derived: 'DERIVED',
});

/** Marker geometry on the map, in CSS pixels (build contract 12.6). */
export const SENSOR_RADIUS = Object.freeze({ normal: 5, warning: 7, anomaly: 9, offline: 5 });
export const ANOMALY_RING_RADIUS = Object.freeze({ low: 10, medium: 13, high: 16, critical: 20 });
export const ANOMALY_RING_WIDTH = 2.5;

/** Colour of a vocabulary entry; `fallback` when the key is unknown. */
export function colorOf(vocabulary, key, fallback = SURFACE.muted) {
  return Object.prototype.hasOwnProperty.call(vocabulary, key) ? vocabulary[key] : fallback;
}

/** Text of a vocabulary entry; an unknown key is turned into words ("very_high" -> "Very high"). */
export function labelOf(labels, key) {
  if (key === null || key === undefined || key === '') return '—';
  if (Object.prototype.hasOwnProperty.call(labels, key)) return labels[key];
  const words = String(key).replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** "#rrggbb" + alpha -> "rgba(r, g, b, a)" for canvas and inline SVG drawing. */
export function withAlpha(hex, alpha) {
  const value = hex.replace('#', '');
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
