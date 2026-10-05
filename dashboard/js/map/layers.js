/**
 * Map layer catalogue and style builder (build contract 12.3 and 12.6).
 *
 * LAYER_GROUPS is the list of things a user can switch on and off (`state.layers[id]`); each group owns one or
 * more MapLibre layers. `buildProjectStyle` returns the project's sources and layers, which js/map/map.js
 * appends to the basemap style (or to the inline fallback style) before the map is created. Nothing here
 * depends on a basemap layer id, sprite or glyph: the only symbol layer (sensor type letters) is added by the
 * caller, and only when the basemap provides glyphs.
 *
 * Time-varying appearance:
 *   feature-state `status`   assets and sensors (colour, width, radius, form)
 *   feature-state `risk`, `count`   risk hexagons
 *   feature-state `selected`, `hover`, `nearby`   interaction
 *   setFilter on integer `start_idx` / `end_idx`   anomaly rings and cluster hulls
 * There is no per-tick setData.
 *
 * Pure module (no DOM, no MapLibre import): importable from Node tests.
 */

import {
  ANOMALY_RING_RADIUS,
  ANOMALY_RING_WIDTH,
  ASSET_STATUS,
  CONTEXT_FEATURE,
  RISK_LEVEL,
  SENSOR_RADIUS,
  SENSOR_STATUS,
  SENSOR_TYPE_GLYPH,
  SEVERITY,
  SURFACE,
} from '../ui/tokens.js';

export const OSM_COPYRIGHT_URL = 'https://www.openstreetmap.org/copyright';
export const IMAGERY_TILE_URL =
  'https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}';
export const IMAGERY_MAX_ZOOM = 16;
export const RISK_FILL_OPACITY = 0.35;
const RISK_LOW_FILL_OPACITY = 0.14;
const LOW_ZOOM_MARKER_SCALE = 0.6;
const WHITE = '#ffffff';
const HOVER_CONTEXT_BUILDING = '#56697f';
const NEARBY_CONTEXT_BUILDING = '#3d6480';

/**
 * Layer groups in display order. `section`: 'assets' | 'monitoring' | 'context'.
 * `assetType` links a group to the asset registry (count shown in the layer list; a selected asset's group is
 * switched on so the selection is visible). `control: 'view'` groups are toggled from the view buttons.
 */
export const LAYER_GROUPS = Object.freeze([
  { id: 'buildings', section: 'assets', label: 'Buildings (3D extrusions)', defaultOn: true, assetType: 'building', layers: ['buildings-context', 'buildings-monitored'] },
  { id: 'roads', section: 'assets', label: 'Roads (asset registry)', defaultOn: true, assetType: 'road', layers: ['asset-roads-casing', 'asset-roads'] },
  { id: 'bridges', section: 'assets', label: 'Bridges and culverts', defaultOn: true, assetType: 'bridge', layers: ['asset-bridges-casing', 'asset-bridges', 'asset-bridge-points'] },
  { id: 'rail', section: 'assets', label: 'Rail', defaultOn: false, assetType: 'rail', layers: ['asset-rail-casing', 'asset-rail'] },
  { id: 'power', section: 'assets', label: 'Power', defaultOn: false, assetType: 'power', layers: ['asset-power-lines', 'asset-power-points'] },
  { id: 'street_lights', section: 'assets', label: 'Street lights', defaultOn: false, assetType: 'street_light', layers: ['asset-street-lights'] },
  { id: 'water_mains', section: 'assets', label: 'Simulated water network (not a record of real utilities)', labelFromMeta: 'water_network', defaultOn: false, assetType: 'water_main', layers: ['asset-water-mains-casing', 'asset-water-mains'] },
  { id: 'sensors', section: 'monitoring', label: 'Simulated sensors', defaultOn: true, layers: ['sensors-halo', 'sensors-circle', 'sensors-glyph'] },
  { id: 'anomalies', section: 'monitoring', label: 'Active anomalies', defaultOn: true, layers: ['anomaly-rings-casing', 'anomaly-rings', 'anomaly-selected'] },
  { id: 'risk_zones', section: 'monitoring', label: 'Risk zones — derived from simulated anomalies', defaultOn: true, layers: ['risk-zones-fill', 'risk-zones-outline'] },
  { id: 'clusters', section: 'monitoring', label: 'Anomaly clusters (co-occurrence)', defaultOn: false, layers: ['cluster-hulls-fill', 'cluster-hulls-line'] },
  { id: 'study_area', section: 'context', label: 'Study area outline', defaultOn: true, layers: ['study-area-outline'] },
  { id: 'roads_base', section: 'context', label: 'Roads (base map)', defaultOn: true, layers: ['roads-base'] },
  { id: 'city_boundary', section: 'context', label: 'City limits', defaultOn: false, control: 'view', layers: ['city-boundary-line'] },
  { id: 'imagery', section: 'context', label: 'Imagery', defaultOn: false, control: 'view', layers: ['imagery'] },
]);

const GROUP_BY_ID = new Map(LAYER_GROUPS.map((group) => [group.id, group]));
const GROUP_BY_ASSET_TYPE = new Map(LAYER_GROUPS.filter((group) => group.assetType).map((group) => [group.assetType, group]));

export function layerGroup(id) {
  return GROUP_BY_ID.get(id) || null;
}

/** Layer group that draws assets of this type (`building` -> the 'buildings' group). */
export function layerGroupForAssetType(assetType) {
  return GROUP_BY_ASSET_TYPE.get(assetType) || null;
}

/** Initial `state.layers` (build contract 12.3). */
export function defaultLayerState() {
  const state = {};
  for (const group of LAYER_GROUPS) state[group.id] = group.defaultOn;
  return state;
}

/** Layers a pointer can hit, most specific first: markers, points, lines, buildings, then areas. */
export const INTERACTIVE_LAYERS = Object.freeze([
  'anomaly-rings',
  'sensors-circle',
  'asset-bridge-points',
  'asset-power-points',
  'asset-street-lights',
  'asset-bridges',
  'asset-water-mains',
  'asset-roads',
  'asset-rail',
  'asset-power-lines',
  'buildings-monitored',
  'buildings-context',
  'cluster-hulls-fill',
  'risk-zones-fill',
]);

/** What a hit on a layer selects or describes. */
export const LAYER_ENTITY = Object.freeze({
  'anomaly-rings': 'anomaly',
  'sensors-circle': 'sensor',
  'asset-bridge-points': 'asset',
  'asset-power-points': 'asset',
  'asset-street-lights': 'asset',
  'asset-bridges': 'asset',
  'asset-water-mains': 'asset',
  'asset-roads': 'asset',
  'asset-rail': 'asset',
  'asset-power-lines': 'asset',
  'buildings-monitored': 'asset',
  'buildings-context': 'asset',
  'cluster-hulls-fill': 'cluster',
  'risk-zones-fill': 'zone',
});

/** Asset types drawn as lines, with the layer group that shows them (used for the highlight halo). */
const LINE_GROUPS = Object.freeze({ roads: 'road', bridges: 'bridge', rail: 'rail', power: 'power', water_mains: 'water_main' });

// ---- expressions -------------------------------------------------------------------------------------------
const assetStatus = ['coalesce', ['feature-state', 'status'], 'not_monitored'];
const sensorStatus = ['coalesce', ['feature-state', 'status'], 'normal'];
const isSelected = ['boolean', ['feature-state', 'selected'], false];
const isHover = ['boolean', ['feature-state', 'hover'], false];
const isNearby = ['boolean', ['feature-state', 'nearby'], false];
const riskValue = ['coalesce', ['feature-state', 'risk'], 0];
const countValue = ['coalesce', ['feature-state', 'count'], 0];
const isLine = ['==', ['geometry-type'], 'LineString'];
const isPoint = ['==', ['geometry-type'], 'Point'];
const ofType = (assetType) => ['==', ['get', 'asset_type'], assetType];

/** Shared colour of every asset layer: the Derived Asset Health Score band held in feature-state `status`. */
export function assetStatusColor(neutral = ASSET_STATUS.not_monitored) {
  return [
    'match',
    assetStatus,
    'normal', ASSET_STATUS.normal,
    'watch', ASSET_STATUS.watch,
    'at_risk', ASSET_STATUS.at_risk,
    'critical', ASSET_STATUS.critical,
    neutral,
  ];
}

/** Extra line width for assets that are at risk or critical (form on top of colour). */
const elevatedExtra = ['match', assetStatus, ['at_risk', 'critical'], 2, 0];

function lineWidth(atLowZoom, atHighZoom, extra = 0) {
  return [
    'interpolate', ['linear'], ['zoom'],
    13, ['+', atLowZoom, elevatedExtra, extra],
    17, ['+', atHighZoom, elevatedExtra, extra],
  ];
}

/** Filter of the anomaly rings: active at hour t <=> start_idx <= t <= end_idx. */
export function activeAtFilter(t) {
  return ['all', ['<=', ['get', 'start_idx'], t], ['>=', ['get', 'end_idx'], t]];
}

/** Filter of the cluster hulls: from the first anomaly until 48 h after the last one ended. */
export function clusterAtFilter(t) {
  return ['all', ['<=', ['get', 'start_idx'], t], ['>=', ['get', 'visible_until_idx'], t]];
}

/** Filter of the ring around the selected anomaly. */
export function selectedAnomalyFilter(anomalyId) {
  return ['==', ['get', 'anomaly_id'], anomalyId || ''];
}

/** Filter of the line halo: only the line asset types whose layer group is visible. */
export function lineHaloFilter(layerState) {
  const types = Object.entries(LINE_GROUPS)
    .filter(([groupId]) => layerState[groupId])
    .map(([, assetType]) => assetType);
  return ['all', isLine, ['in', ['get', 'asset_type'], ['literal', types]]];
}

/** Paint of the risk hexagons for the chosen metric ('risk' score classes or 'count' of active anomalies). */
export function riskZonePaint(metric, levels) {
  const moderate = levels.moderate;
  const high = levels.high;
  const veryHigh = levels.very_high;
  if (metric === 'count') {
    const color = ['step', countValue, RISK_LEVEL.low, 2, RISK_LEVEL.moderate, 3, RISK_LEVEL.high, 4, RISK_LEVEL.very_high];
    return {
      fillColor: color,
      fillOpacity: ['case', ['>', countValue, 0], RISK_FILL_OPACITY, 0],
      lineColor: color,
      lineOpacity: ['case', ['>', countValue, 0], 0.7, 0],
    };
  }
  const color = ['step', riskValue, RISK_LEVEL.low, moderate, RISK_LEVEL.moderate, high, RISK_LEVEL.high, veryHigh, RISK_LEVEL.very_high];
  return {
    fillColor: color,
    // 35 % from the "moderate" level up; below it the wash stays faint, so a trace of risk does not tint the
    // whole map.
    fillOpacity: ['case', ['>=', riskValue, moderate], RISK_FILL_OPACITY, ['interpolate', ['linear'], riskValue, 0, 0, moderate, RISK_LOW_FILL_OPACITY]],
    lineColor: color,
    lineOpacity: ['case', ['>=', riskValue, moderate], 0.7, ['interpolate', ['linear'], riskValue, 0, 0, moderate, 0.3]],
  };
}

/** Classes of the "Anomaly count" metric, for the legend. */
export const COUNT_CLASSES = Object.freeze([
  { label: '1', color: RISK_LEVEL.low },
  { label: '2', color: RISK_LEVEL.moderate },
  { label: '3', color: RISK_LEVEL.high },
  { label: '4+', color: RISK_LEVEL.very_high },
]);

/** Polygon approximating a circle of `radiusM` metres around [lon, lat] (for the proximity ring). */
export function circlePolygon(lon, lat, radiusM, steps = 72) {
  const dLat = radiusM / 110540;
  const dLon = radiusM / (111320 * Math.cos((lat * Math.PI) / 180));
  const ring = [];
  for (let i = 0; i <= steps; i += 1) {
    const angle = (i / steps) * 2 * Math.PI;
    ring.push([lon + dLon * Math.cos(angle), lat + dLat * Math.sin(angle)]);
  }
  return { type: 'Feature', properties: { radius_m: radiusM }, geometry: { type: 'Polygon', coordinates: [ring] } };
}

export const EMPTY_COLLECTION = Object.freeze({ type: 'FeatureCollection', features: [] });

function attributionOf(meta, sourceId) {
  const source = (meta.data_sources || []).find((entry) => entry.source_id === sourceId);
  return source ? source.attribution_text : null;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[char]);
}

/** Attribution strings of the project sources, taken from `meta.data_sources` so credit survives fallback mode. */
export function projectAttributions(meta) {
  const osmText = attributionOf(meta, 'osm') || '© OpenStreetMap contributors';
  const osm = `<a href="${OSM_COPYRIGHT_URL}" target="_blank" rel="noopener">${escapeHtml(osmText)}</a>`;
  const nbi = attributionOf(meta, 'nbi');
  const lidar = attributionOf(meta, 'usgs_3dep');
  const tiger = attributionOf(meta, 'tiger');
  const imagery = attributionOf(meta, 'imagery');
  return {
    osm,
    assets: [osm, nbi ? escapeHtml(nbi) : null, lidar ? escapeHtml(lidar) : null].filter(Boolean).join(' | '),
    tiger: tiger ? escapeHtml(tiger) : null,
    imagery: imagery ? escapeHtml(imagery) : null,
  };
}

function sensorFeatures(sensors) {
  return {
    type: 'FeatureCollection',
    features: sensors.map((sensor) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [sensor.lon, sensor.lat] },
      properties: {
        sensor_id: sensor.sensor_id,
        sensor_type: sensor.sensor_type,
        asset_id: sensor.asset_id,
        glyph: SENSOR_TYPE_GLYPH[sensor.sensor_type] || '',
      },
    })),
  };
}

function anomalyFeatures(anomalies) {
  return {
    type: 'FeatureCollection',
    features: anomalies.map((anomaly) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [anomaly.lon, anomaly.lat] },
      properties: {
        anomaly_id: anomaly.anomaly_id,
        severity: anomaly.severity,
        sensor_id: anomaly.sensor_id,
        asset_id: anomaly.asset_id,
        start_idx: anomaly.start_idx,
        end_idx: anomaly.end_idx,
      },
    })),
  };
}

/**
 * Sources and layers of the project.
 *
 * @param {object} options
 * @param {object} options.data          the index built by js/data/index.js
 * @param {object|null} options.cityBoundary GeoJSON of the city limits (null when it could not be loaded)
 * @param {object} options.layerState    `state.layers`
 * @param {'risk'|'count'} options.hexMetric
 * @param {number} options.t             hour index for the initial filters
 * @returns {{sources: object, underlay: object[], ground: object[], overlay: object[]}} layers in three bands:
 *   underlay (imagery; goes below the basemap labels), ground (areas, lines, extrusions), overlay (points and
 *   markers; goes above the basemap labels)
 */
export function buildProjectStyle({ data, cityBoundary, layerState, hexMetric, t }) {
  const { meta } = data;
  const credit = projectAttributions(meta);
  const visible = (groupId) => (layerState[groupId] ? 'visible' : 'none');
  const riskPaint = riskZonePaint(hexMetric, meta.risk.levels);
  const ringRadius = ['match', ['get', 'severity'], 'low', ANOMALY_RING_RADIUS.low, 'medium', ANOMALY_RING_RADIUS.medium, 'high', ANOMALY_RING_RADIUS.high, 'critical', ANOMALY_RING_RADIUS.critical, ANOMALY_RING_RADIUS.medium];
  const ringColor = ['match', ['get', 'severity'], 'low', SEVERITY.low, 'medium', SEVERITY.medium, 'high', SEVERITY.high, 'critical', SEVERITY.critical, SEVERITY.medium];
  const sensorBase = ['match', sensorStatus, 'warning', SENSOR_RADIUS.warning, 'anomaly', SENSOR_RADIUS.anomaly, 'offline', SENSOR_RADIUS.offline, SENSOR_RADIUS.normal];
  // Marker sizes are the contract's pixel sizes from zoom 14 up; below that (small screens, city-limits view)
  // they shrink so that markers do not cover the study area.
  const zoomScaled = (size, extra = 0) => ['interpolate', ['linear'], ['zoom'], 12, ['+', ['*', LOW_ZOOM_MARKER_SCALE, size], extra], 14, ['+', size, extra]];
  const sensorRadius = zoomScaled(sensorBase);

  const sources = {
    imagery: {
      type: 'raster',
      tiles: [IMAGERY_TILE_URL],
      tileSize: 256,
      maxzoom: IMAGERY_MAX_ZOOM,
      attribution: credit.imagery || 'USDA, USGS The National Map: Orthoimagery',
    },
    'risk-zones': { type: 'geojson', data: { type: 'FeatureCollection', features: data.riskZones }, promoteId: 'cell_id' },
    'study-area': { type: 'geojson', data: data.studyArea || EMPTY_COLLECTION },
    'city-boundary': {
      type: 'geojson',
      data: cityBoundary || EMPTY_COLLECTION,
      ...(credit.tiger ? { attribution: credit.tiger } : {}),
    },
    roads: { type: 'geojson', data: data.roads || EMPTY_COLLECTION, attribution: credit.osm },
    assets: {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: data.assets },
      promoteId: 'asset_id',
      attribution: credit.assets,
    },
    clusters: { type: 'geojson', data: { type: 'FeatureCollection', features: data.clusters }, promoteId: 'cluster_id' },
    'selection-ring': { type: 'geojson', data: EMPTY_COLLECTION },
    sensors: { type: 'geojson', data: sensorFeatures(data.sensors), promoteId: 'sensor_id' },
    anomalies: { type: 'geojson', data: anomalyFeatures(data.anomalies), promoteId: 'anomaly_id' },
  };

  const underlay = [
    { id: 'imagery', type: 'raster', source: 'imagery', layout: { visibility: visible('imagery') }, paint: { 'raster-opacity': 0.9, 'raster-fade-duration': 0 } },
  ];

  const casing = (id, groupId, filter, low, high, extra = 0) => ({
    id,
    type: 'line',
    source: 'assets',
    filter,
    layout: { visibility: visible(groupId), 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': SURFACE.bg, 'line-width': lineWidth(low + 4, high + 4, extra), 'line-opacity': 0.9 },
  });

  const ground = [
    {
      id: 'risk-zones-fill',
      type: 'fill',
      source: 'risk-zones',
      layout: { visibility: visible('risk_zones') },
      paint: { 'fill-color': riskPaint.fillColor, 'fill-opacity': riskPaint.fillOpacity, 'fill-antialias': false },
    },
    {
      id: 'risk-zones-outline',
      type: 'line',
      source: 'risk-zones',
      layout: { visibility: visible('risk_zones') },
      paint: { 'line-color': riskPaint.lineColor, 'line-opacity': riskPaint.lineOpacity, 'line-width': 1 },
    },
    {
      id: 'study-area-outline',
      type: 'line',
      source: 'study-area',
      layout: { visibility: visible('study_area') },
      paint: { 'line-color': SURFACE.accent, 'line-width': 1.5, 'line-opacity': 0.85, 'line-dasharray': [4, 3] },
    },
    {
      id: 'city-boundary-line',
      type: 'line',
      source: 'city-boundary',
      layout: { visibility: visible('city_boundary') },
      paint: { 'line-color': SURFACE.text, 'line-width': 1.5, 'line-opacity': 0.8 },
    },
    {
      id: 'roads-base',
      type: 'line',
      source: 'roads',
      layout: { visibility: visible('roads_base'), 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#2b3848',
        'line-width': [
          'interpolate', ['linear'], ['zoom'],
          12, ['match', ['get', 'highway_class'], ['motorway', 'trunk', 'primary', 'primary_link'], 1.6, ['secondary', 'tertiary'], 1.1, 0.5],
          17, ['match', ['get', 'highway_class'], ['motorway', 'trunk', 'primary', 'primary_link'], 7, ['secondary', 'tertiary'], 5, ['residential', 'unclassified'], 3.5, 1.5],
        ],
      },
    },
    {
      id: 'asset-line-halo',
      type: 'line',
      source: 'assets',
      filter: lineHaloFilter(layerState),
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': ['case', isSelected, WHITE, SURFACE.accent],
        'line-width': ['interpolate', ['linear'], ['zoom'], 13, 9, 17, 14],
        'line-opacity': ['case', isSelected, 0.9, isHover, 0.6, isNearby, 0.45, 0],
        'line-blur': 1,
      },
    },
    casing('asset-rail-casing', 'rail', ofType('rail'), 1.2, 2.4),
    {
      id: 'asset-rail',
      type: 'line',
      source: 'assets',
      filter: ofType('rail'),
      layout: { visibility: visible('rail') },
      paint: { 'line-color': assetStatusColor(CONTEXT_FEATURE), 'line-width': lineWidth(1.2, 2.4), 'line-dasharray': [3, 1.5] },
    },
    {
      id: 'asset-power-lines',
      type: 'line',
      source: 'assets',
      filter: ['all', ofType('power'), isLine],
      layout: { visibility: visible('power') },
      paint: { 'line-color': assetStatusColor(CONTEXT_FEATURE), 'line-width': lineWidth(1, 2), 'line-dasharray': [1, 2] },
    },
    casing('asset-roads-casing', 'roads', ofType('road'), 1.6, 5),
    {
      id: 'asset-roads',
      type: 'line',
      source: 'assets',
      filter: ofType('road'),
      layout: { visibility: visible('roads'), 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': assetStatusColor(), 'line-width': lineWidth(1.6, 5) },
    },
    casing('asset-water-mains-casing', 'water_mains', ofType('water_main'), 1.6, 3.5),
    {
      id: 'asset-water-mains',
      type: 'line',
      source: 'assets',
      filter: ofType('water_main'),
      layout: { visibility: visible('water_mains') },
      paint: { 'line-color': assetStatusColor(), 'line-width': lineWidth(1.6, 3.5), 'line-dasharray': [2, 1.6] },
    },
    casing('asset-bridges-casing', 'bridges', ['all', ofType('bridge'), isLine], 3.5, 8),
    {
      id: 'asset-bridges',
      type: 'line',
      source: 'assets',
      filter: ['all', ofType('bridge'), isLine],
      layout: { visibility: visible('bridges'), 'line-cap': 'butt', 'line-join': 'round' },
      paint: { 'line-color': assetStatusColor(), 'line-width': lineWidth(3.5, 8) },
    },
    {
      id: 'cluster-hulls-fill',
      type: 'fill',
      source: 'clusters',
      filter: clusterAtFilter(t),
      layout: { visibility: visible('clusters') },
      paint: { 'fill-color': SURFACE.text, 'fill-opacity': 0.06 },
    },
    {
      id: 'cluster-hulls-line',
      type: 'line',
      source: 'clusters',
      filter: clusterAtFilter(t),
      layout: { visibility: visible('clusters') },
      paint: { 'line-color': SURFACE.text, 'line-width': 2, 'line-dasharray': [2.5, 2], 'line-opacity': 0.9 },
    },
    {
      id: 'selection-ring-fill',
      type: 'fill',
      source: 'selection-ring',
      paint: { 'fill-color': SURFACE.accent, 'fill-opacity': 0.06 },
    },
    {
      id: 'selection-ring-line',
      type: 'line',
      source: 'selection-ring',
      paint: { 'line-color': SURFACE.accent, 'line-width': 1.5, 'line-dasharray': [3, 2] },
    },
    {
      id: 'buildings-context',
      type: 'fill-extrusion',
      source: 'assets',
      filter: ['all', ofType('building'), ['!=', ['get', 'monitored'], true]],
      layout: { visibility: visible('buildings') },
      paint: {
        'fill-extrusion-color': ['case', isSelected, WHITE, isHover, HOVER_CONTEXT_BUILDING, isNearby, NEARBY_CONTEXT_BUILDING, ASSET_STATUS.not_monitored],
        'fill-extrusion-height': ['get', 'height_m'],
        'fill-extrusion-base': 0,
        'fill-extrusion-opacity': 0.6,
        'fill-extrusion-vertical-gradient': true,
      },
    },
    {
      id: 'buildings-monitored',
      type: 'fill-extrusion',
      source: 'assets',
      filter: ['all', ofType('building'), ['==', ['get', 'monitored'], true]],
      layout: { visibility: visible('buildings') },
      paint: {
        'fill-extrusion-color': ['case', isSelected, WHITE, assetStatusColor()],
        'fill-extrusion-height': ['get', 'height_m'],
        'fill-extrusion-base': 0,
        'fill-extrusion-opacity': 0.95,
        'fill-extrusion-vertical-gradient': true,
      },
    },
  ];

  const donut = (id, groupId, assetType, radius, neutral) => ({
    id,
    type: 'circle',
    source: 'assets',
    filter: ['all', ofType(assetType), isPoint],
    layout: { visibility: visible(groupId) },
    paint: {
      'circle-radius': ['+', radius, ['case', isHover, 1, 0]],
      'circle-color': SURFACE.bg,
      'circle-stroke-color': ['case', isSelected, WHITE, isNearby, SURFACE.accent, assetStatusColor(neutral)],
      'circle-stroke-width': ['case', isSelected, 4, 3],
    },
  });

  const overlay = [
    donut('asset-bridge-points', 'bridges', 'bridge', 8, ASSET_STATUS.not_monitored),
    donut('asset-power-points', 'power', 'power', 7, CONTEXT_FEATURE),
    {
      id: 'asset-street-lights',
      type: 'circle',
      source: 'assets',
      filter: ofType('street_light'),
      layout: { visibility: visible('street_lights') },
      paint: {
        'circle-radius': ['case', isSelected, 6, isHover, 5, 3.5],
        'circle-color': ['case', isSelected, WHITE, CONTEXT_FEATURE],
        'circle-stroke-color': SURFACE.bg,
        'circle-stroke-width': 1,
      },
    },
    {
      id: 'sensors-halo',
      type: 'circle',
      source: 'sensors',
      layout: { visibility: visible('sensors') },
      paint: {
        'circle-radius': zoomScaled(sensorBase, 5),
        'circle-color': SURFACE.accent,
        'circle-opacity': ['case', isSelected, 0.18, 0],
        'circle-stroke-color': ['case', isSelected, WHITE, SURFACE.accent],
        'circle-stroke-width': 2,
        'circle-stroke-opacity': ['case', isSelected, 1, isHover, 0.9, 0],
      },
    },
    {
      id: 'sensors-circle',
      type: 'circle',
      source: 'sensors',
      layout: { visibility: visible('sensors') },
      paint: {
        // Status by colour AND form: normal r5, warning r7, anomaly r9 with a white stroke, offline hollow grey ring.
        'circle-radius': sensorRadius,
        'circle-color': ['match', sensorStatus, 'normal', SENSOR_STATUS.normal, 'warning', SENSOR_STATUS.warning, 'anomaly', SENSOR_STATUS.anomaly, SENSOR_STATUS.offline],
        'circle-opacity': ['match', sensorStatus, 'offline', 0, 1],
        'circle-stroke-color': ['match', sensorStatus, 'anomaly', WHITE, 'offline', SENSOR_STATUS.offline, SURFACE.bg],
        'circle-stroke-width': ['match', sensorStatus, ['anomaly', 'offline'], 2, 1],
      },
    },
    {
      id: 'anomaly-rings-casing',
      type: 'circle',
      source: 'anomalies',
      filter: activeAtFilter(t),
      layout: { visibility: visible('anomalies') },
      paint: {
        'circle-radius': zoomScaled(ringRadius),
        'circle-color': SURFACE.bg,
        'circle-opacity': 0,
        'circle-stroke-color': SURFACE.bg,
        'circle-stroke-width': ANOMALY_RING_WIDTH + 2.5,
        'circle-stroke-opacity': 0.65,
      },
    },
    {
      id: 'anomaly-rings',
      type: 'circle',
      source: 'anomalies',
      filter: activeAtFilter(t),
      layout: { visibility: visible('anomalies') },
      paint: {
        // Static ring: transparent fill, severity by colour AND radius. No pulse, no animation loop.
        'circle-radius': zoomScaled(ringRadius),
        'circle-color': ringColor,
        'circle-opacity': 0,
        'circle-stroke-color': ringColor,
        'circle-stroke-width': ANOMALY_RING_WIDTH,
      },
    },
    {
      id: 'anomaly-selected',
      type: 'circle',
      source: 'anomalies',
      filter: selectedAnomalyFilter(null),
      layout: { visibility: visible('anomalies') },
      paint: {
        'circle-radius': zoomScaled(ringRadius, 5),
        'circle-color': WHITE,
        'circle-opacity': 0,
        'circle-stroke-color': WHITE,
        'circle-stroke-width': 2,
      },
    },
  ];

  return { sources, underlay, ground, overlay };
}

/** Symbol layer with the sensor type letter; the caller adds it only when the basemap provides glyphs. */
export function sensorGlyphLayer(fontStack, layerState) {
  return {
    id: 'sensors-glyph',
    type: 'symbol',
    source: 'sensors',
    minzoom: 15.5,
    layout: {
      visibility: layerState.sensors ? 'visible' : 'none',
      'text-field': ['get', 'glyph'],
      'text-font': fontStack,
      'text-size': 11,
      'text-offset': [0, -1.5],
      'text-allow-overlap': true,
      'text-ignore-placement': true,
    },
    paint: { 'text-color': SURFACE.text, 'text-halo-color': SURFACE.bg, 'text-halo-width': 1.5 },
  };
}

/** Inline style used when the basemap cannot be fetched: project data on a plain background, no glyphs/sprite. */
export function fallbackBasemapStyle() {
  return {
    version: 8,
    name: 'Project data only',
    sources: {},
    layers: [{ id: 'fallback-background', type: 'background', paint: { 'background-color': SURFACE.bg } }],
  };
}
