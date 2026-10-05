/**
 * The 3D map (build contract 12.3, 12.6).
 *
 * - The basemap style is fetched with a 4 s timeout BEFORE the map is created. When it cannot be fetched, the
 *   map is built on an inline style (plain background, no glyphs, no sprite) and a notice says so; basemap
 *   errors that arrive after loading raise the same notice.
 * - Project sources and layers are part of the style object handed to MapLibre, so they never wait for a
 *   basemap event and never depend on a basemap layer id.
 * - Per simulated hour the map only updates what changed: feature-state for assets, sensors and risk cells
 *   whose value differs from the one applied, and `setFilter` for anomaly rings and cluster hulls when the
 *   set of visible features changed. No setData per tick, no animation loop.
 * - The map talks to the rest of the application only through the store.
 */

import { assetDisplayName, assetTypeLabel, boundsOf } from '../data/index.js';
import { describeError, h, icon } from '../ui/dom.js';
import { formatValue, plural } from '../ui/format.js';
import {
  ASSET_STATUS_LABEL,
  ASSET_TYPE_LABEL,
  RISK_LEVEL_LABEL,
  SENSOR_STATUS_LABEL,
  SEVERITY_LABEL,
  labelOf,
} from '../ui/tokens.js';
import {
  EMPTY_COLLECTION,
  IMAGERY_TILE_URL,
  INTERACTIVE_LAYERS,
  LAYER_ENTITY,
  LAYER_GROUPS,
  activeAtFilter,
  buildProjectStyle,
  circlePolygon,
  clusterAtFilter,
  fallbackBasemapStyle,
  lineHaloFilter,
  riskZonePaint,
  selectedAnomalyFilter,
  sensorGlyphLayer,
} from './layers.js';

export const BASEMAP_TIMEOUT_MS = 4000;
export const BASEMAP_NOTICE = 'Basemap unavailable — showing project data only';
const IMAGERY_NOTICE = 'Imagery service unavailable — the Imagery view is switched off';
const HOME_VIEW = Object.freeze({ padding: 40, pitch: 50, bearing: -20 });
const FLY_ZOOM = 17;
const HIT_BOX_PX = 4;
const NARROW_VIEWPORT_PX = 900;
const TICK_ORDER = 30;

const prefersReducedMotion = () =>
  typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function absolutize(value, base) {
  if (typeof value !== 'string' || /^[a-z][a-z0-9+.-]*:/i.test(value)) return value;
  try {
    return new URL(value, base).href.replace(/%7B/gi, '{').replace(/%7D/gi, '}');
  } catch {
    return value;
  }
}

/**
 * Fetch and prepare the basemap style: relative URLs are made absolute and the basemap's own building layers
 * are hidden (the project draws its own extrusions). Rejects when the style cannot be used.
 */
export async function loadBasemapStyle(url, { timeoutMs = BASEMAP_TIMEOUT_MS, fetchImpl = globalThis.fetch } = {}) {
  if (!url) throw new Error('no basemap style configured');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, { signal: controller.signal });
    if (!response.ok) throw new Error(`basemap style answered HTTP ${response.status}`);
    const style = await response.json();
    if (!style || style.version !== 8 || !Array.isArray(style.layers) || typeof style.sources !== 'object') {
      throw new Error('basemap style is not a MapLibre style document');
    }
    const base = new URL(url, window.location.href).href;
    if (typeof style.sprite === 'string') style.sprite = absolutize(style.sprite, base);
    if (typeof style.glyphs === 'string') style.glyphs = absolutize(style.glyphs, base);
    for (const source of Object.values(style.sources)) {
      if (typeof source.url === 'string') source.url = absolutize(source.url, base);
      if (Array.isArray(source.tiles)) source.tiles = source.tiles.map((tile) => absolutize(tile, base));
    }
    for (const layer of style.layers) {
      if (layer['source-layer'] === 'building' || /building/i.test(layer.id)) {
        layer.layout = { ...(layer.layout || {}), visibility: 'none' };
      }
    }
    return style;
  } catch (error) {
    if (controller.signal.aborted) throw new Error(`basemap style did not answer within ${timeoutMs / 1000} s`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Camera that fits `bounds` inside the padded viewport at the given pitch and bearing.
 *
 * MapLibre's own fit ignores the pitch: for a tilted view it answers with a zoom at which the area fills only
 * part of the screen. This starts from that answer and refines it by projecting the four corners (the map
 * camera is moved without rendering and put back before returning).
 */
export function cameraForPitchedBounds(map, bounds, { padding, pitch, bearing }) {
  const flat = map.cameraForBounds(bounds, { padding, bearing });
  if (!flat) return null;
  const container = map.getContainer();
  const width = container.clientWidth - 2 * padding;
  const height = container.clientHeight - 2 * padding;
  if (!pitch || width <= 0 || height <= 0) return { center: flat.center, zoom: flat.zoom, pitch: pitch || 0, bearing };
  const before = { center: map.getCenter(), zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
  const [[west, south], [east, north]] = bounds;
  const corners = [[west, south], [east, south], [east, north], [west, north]];
  let camera = { center: flat.center, zoom: flat.zoom, pitch, bearing };
  for (let pass = 0; pass < 6; pass += 1) {
    map.jumpTo(camera);
    const points = corners.map((corner) => map.project(corner));
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const spanX = Math.max(...xs) - Math.min(...xs);
    const spanY = Math.max(...ys) - Math.min(...ys);
    if (!(spanX > 0) || !(spanY > 0)) break;
    const scale = Math.min(width / spanX, height / spanY);
    const middle = map.unproject([(Math.max(...xs) + Math.min(...xs)) / 2, (Math.max(...ys) + Math.min(...ys)) / 2]);
    camera = {
      center: middle,
      zoom: camera.zoom + Math.max(-2, Math.min(2, Math.log2(scale))),
      pitch,
      bearing,
    };
  }
  map.jumpTo(before);
  return camera;
}

/** First font stack used by a basemap label layer (for the sensor letter layer), or null. */
function basemapFontStack(style) {
  if (!style.glyphs) return null;
  for (const layer of style.layers) {
    const font = layer.type === 'symbol' && layer.layout ? layer.layout['text-font'] : null;
    if (Array.isArray(font) && font.every((entry) => typeof entry === 'string')) return font;
  }
  return null;
}

/** Tile of the imagery service that covers [lon, lat] at zoom z (to probe the service once). */
function imageryProbeUrl(lon, lat, z) {
  const n = 2 ** z;
  const x = Math.floor(((lon + 180) / 360) * n);
  const rad = (lat * Math.PI) / 180;
  const y = Math.floor(((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * n);
  return IMAGERY_TILE_URL.replace('{z}', z).replace('{y}', y).replace('{x}', x);
}

/**
 * Create the map inside `elements.map` and connect it to the store.
 *
 * @param {object} options
 * @param {object} options.config        runtime config ({basemapStyleUrl})
 * @param {object} options.data          index from js/data/index.js
 * @param {object|null} options.cityBoundary GeoJSON or null
 * @param {object} options.store
 * @param {object} options.actions       shell actions ({select, clearSelection, setLayer})
 * @param {object} options.elements      {map, skeleton, tooltip, notices, viewButtons}
 * @returns {Promise<{map: object, basemap: string, destroy: function}>}
 */
export async function createMap({ config, data, cityBoundary, store, actions, elements }) {
  const maplibregl = window.maplibregl;
  if (!maplibregl || typeof maplibregl.Map !== 'function') {
    throw new Error('The map library (vendor/maplibre-gl) did not load.');
  }
  const { meta } = data;
  const bbox = meta.study_area.bbox;
  const studyBounds = [[bbox[0], bbox[1]], [bbox[2], bbox[3]]];

  // ---- basemap ---------------------------------------------------------------------------------------------
  let basemapStyle;
  let basemapProblem = null;
  try {
    basemapStyle = await loadBasemapStyle(config.basemapStyleUrl);
  } catch (error) {
    basemapProblem = describeError(error);
    basemapStyle = fallbackBasemapStyle();
  }
  const fallback = basemapProblem !== null;
  const basemapSourceIds = new Set(Object.keys(basemapStyle.sources));

  // ---- style = basemap + project layers --------------------------------------------------------------------
  const initial = store.getState();
  const project = buildProjectStyle({
    data,
    cityBoundary,
    layerState: initial.layers,
    hexMetric: initial.hexMetric,
    t: initial.t,
  });
  const fontStack = fallback ? null : basemapFontStack(basemapStyle);
  const baseLayers = basemapStyle.layers.filter((layer) => layer.type !== 'symbol');
  const labelLayers = basemapStyle.layers.filter((layer) => layer.type === 'symbol');
  const overlay = project.overlay.slice();
  if (fontStack) {
    const at = overlay.findIndex((layer) => layer.id === 'sensors-circle');
    overlay.splice(at + 1, 0, sensorGlyphLayer(fontStack, initial.layers));
  }
  const style = {
    ...basemapStyle,
    sources: { ...basemapStyle.sources, ...project.sources },
    layers: [...baseLayers, ...project.underlay, ...project.ground, ...labelLayers, ...overlay],
  };
  const projectLayerIds = new Set([...project.underlay, ...project.ground, ...overlay].map((layer) => layer.id));

  const narrow = window.innerWidth < NARROW_VIEWPORT_PX;
  const map = new maplibregl.Map({
    container: elements.map,
    style,
    bounds: studyBounds,
    fitBoundsOptions: { padding: HOME_VIEW.padding, bearing: HOME_VIEW.bearing },
    maxPitch: 70,
    minZoom: 9,
    attributionControl: false,
    cooperativeGestures: narrow,
    canvasContextAttributes: { antialias: true },
    fadeDuration: prefersReducedMotion() ? 0 : 200,
  });
  map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
  if (narrow) {
    // Small screens: the credits start folded behind their (i) button, as usual on phones.
    const credits = elements.map.querySelector('.maplibregl-ctrl-attrib');
    if (credits) {
      credits.classList.remove('maplibregl-compact-show');
      credits.removeAttribute('open');
    }
  }
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric', maxWidth: 110 }), 'bottom-right');
  elements.map.setAttribute('role', 'application');

  const moveDuration = (ms) => (prefersReducedMotion() ? 0 : ms);
  // True while a camera is being computed or applied by this module: pitch events are then not user gestures.
  let cameraBusy = false;
  /** Fit `bounds` with the home padding, pitched (50 deg, bearing -20) or flat; `duration` 0 jumps. */
  function fitTo(bounds, pitched, duration) {
    cameraBusy = true;
    let camera;
    try {
      camera = cameraForPitchedBounds(map, bounds, {
        padding: HOME_VIEW.padding,
        pitch: pitched ? HOME_VIEW.pitch : 0,
        bearing: pitched ? HOME_VIEW.bearing : 0,
      });
      if (camera && duration === 0) map.jumpTo(camera);
    } finally {
      cameraBusy = false;
    }
    if (camera && duration > 0) map.flyTo({ ...camera, duration, essential: false });
  }
  // Initial camera = Home: the study-area bbox fitted with padding 40 at pitch 50, bearing -20 (never coordinates).
  fitTo(studyBounds, initial.pitched, 0);

  // ---- notices ---------------------------------------------------------------------------------------------
  const shownNotices = new Map();
  function showNotice(key, text) {
    if (shownNotices.has(key)) return;
    const node = h(
      'div',
      { class: 'map-notice' },
      icon('alert', { size: 14 }),
      h('span', { text }),
      h('button', { class: 'btn btn--ghost btn--icon', type: 'button', 'aria-label': 'Dismiss this notice', on: { click: () => node.remove() } }, icon('close', { size: 12 })),
    );
    shownNotices.set(key, node);
    elements.notices.append(node);
  }
  if (fallback) {
    console.warn(`[map] ${BASEMAP_NOTICE} (${basemapProblem})`);
    showNotice('basemap', BASEMAP_NOTICE);
  }
  store.set({ basemap: fallback ? 'fallback' : 'ok' });

  // ---- view buttons (3D · Home · Imagery · City limits) ----------------------------------------------------
  const pitchButton = h('button', { class: 'btn', type: 'button', title: 'Switch between the 3D and the 2D view', on: { click: () => store.set({ pitched: !store.getState().pitched }) } }, icon('cube'), h('span', { class: 'btn__label', text: '3D' }));
  const homeButton = h('button', { class: 'btn', type: 'button', title: 'Return to the study area', on: { click: goHome } }, icon('home'), h('span', { class: 'btn__label', text: 'Home' }));
  const imageryButton = h('button', { class: 'btn', type: 'button', title: 'USGS orthoimagery under the project layers', on: { click: () => actions.setLayer('imagery', !store.getState().layers.imagery) } }, icon('imagery'), h('span', { class: 'btn__label', text: 'Imagery' }));
  const cityButton = h('button', { class: 'btn', type: 'button', title: 'Show the city limits (context outline) and zoom out to them', on: { click: () => actions.setLayer('city_boundary', !store.getState().layers.city_boundary) } }, icon('boundary'), h('span', { class: 'btn__label', text: 'City limits' }));
  const cityBounds = cityBoundary ? boundsOf(cityBoundary) : null;
  cityButton.hidden = !cityBounds;
  elements.viewButtons.replaceChildren(pitchButton, homeButton, imageryButton, cityButton);

  function syncViewButtons(state) {
    pitchButton.setAttribute('aria-pressed', state.pitched ? 'true' : 'false');
    imageryButton.setAttribute('aria-pressed', state.layers.imagery ? 'true' : 'false');
    cityButton.setAttribute('aria-pressed', state.layers.city_boundary ? 'true' : 'false');
    imageryButton.hidden = state.imagery === 'failed';
  }
  syncViewButtons(initial);

  let homing = false;
  function goHome() {
    homing = true;
    try {
      if (!store.getState().pitched) store.set({ pitched: true });
    } finally {
      homing = false;
    }
    fitTo(studyBounds, true, moveDuration(900));
  }

  // ---- imagery availability --------------------------------------------------------------------------------
  function imageryFailed() {
    if (store.getState().imagery === 'failed') return;
    const patch = { imagery: 'failed' };
    if (store.getState().layers.imagery) patch.layers = { ...store.getState().layers, imagery: false };
    store.set(patch);
    if (patch.layers) showNotice('imagery', IMAGERY_NOTICE);
  }
  const probe = new Image();
  probe.onload = () => {
    if (store.getState().imagery === 'unknown') store.set({ imagery: 'ok' });
  };
  probe.onerror = imageryFailed;
  probe.referrerPolicy = 'no-referrer';
  probe.src = imageryProbeUrl(meta.study_area.center[0], meta.study_area.center[1], 14);

  // A basemap layer may name a sprite image its sprite does not contain: supply a transparent pixel so the
  // basemap draws nothing there (and MapLibre has nothing to complain about).
  map.on('styleimagemissing', (event) => {
    if (!map.hasImage(event.id)) map.addImage(event.id, { width: 1, height: 1, data: new Uint8Array(4) });
  });

  // ---- errors after loading --------------------------------------------------------------------------------
  let basemapTilesSeen = 0;
  let basemapErrors = 0;
  let glyphLayerPresent = Boolean(fontStack);
  map.on('sourcedata', (event) => {
    if (event.tile && basemapSourceIds.has(event.sourceId)) basemapTilesSeen += 1;
  });
  map.on('error', (event) => {
    const sourceId = event.sourceId || (event.source && event.source.id) || null;
    const message = event.error && event.error.message ? event.error.message : String(event.error || 'unknown error');
    if (sourceId === 'imagery') {
      imageryFailed();
      return;
    }
    if (sourceId && !basemapSourceIds.has(sourceId)) {
      console.warn(`[map] problem with project source "${sourceId}": ${message}`);
      return;
    }
    // A basemap source, its glyphs or its sprite failed after the style was accepted.
    basemapErrors += 1;
    if (fallback) return;
    if (basemapTilesSeen === 0 || basemapErrors >= 5) {
      if (!shownNotices.has('basemap')) console.warn(`[map] ${BASEMAP_NOTICE} (${message})`);
      showNotice('basemap', BASEMAP_NOTICE);
      store.set({ basemap: 'degraded' });
      if (glyphLayerPresent && map.getLayer('sensors-glyph')) {
        map.removeLayer('sensors-glyph');
        glyphLayerPresent = false;
      }
    }
  });

  // ---- wait for the style ----------------------------------------------------------------------------------
  await new Promise((resolve) => {
    if (map.isStyleLoaded()) resolve();
    else map.once('style.load', resolve);
  });

  const hasLayer = (id) => Boolean(map.getLayer(id));
  const setVisibility = (groupId, on) => {
    const group = LAYER_GROUPS.find((entry) => entry.id === groupId);
    if (!group) return;
    for (const id of group.layers) {
      if (hasLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
    }
  };

  // ---- time: changed-only feature-state --------------------------------------------------------------------
  const applied = { assets: new Map(), sensors: new Map(), risk: new Map(), count: new Map() };
  let appliedAnomalyVersion = -1;
  let appliedClusterVersion = -1;
  const assetIds = Object.keys(data.playback.assets);
  const sensorIds = Object.keys(data.playback.sensors);
  const zoneIds = data.riskZones.map((zone) => zone.properties.cell_id);

  function applyTime(t) {
    let updates = 0;
    // (3) asset status
    for (const id of assetIds) {
      const status = data.assetStatusAt(id, t);
      if (applied.assets.get(id) !== status) {
        applied.assets.set(id, status);
        map.setFeatureState({ source: 'assets', id }, { status });
        updates += 1;
      }
    }
    // (4) sensor status
    for (const id of sensorIds) {
      const status = data.sensorStatusAt(id, t);
      if (applied.sensors.get(id) !== status) {
        applied.sensors.set(id, status);
        map.setFeatureState({ source: 'sensors', id }, { status });
        updates += 1;
      }
    }
    // (5) anomaly markers and cluster hulls: only when the visible set changed
    if (data.anomalySetVersion[t] !== appliedAnomalyVersion) {
      appliedAnomalyVersion = data.anomalySetVersion[t];
      const filter = activeAtFilter(t);
      map.setFilter('anomaly-rings', filter);
      map.setFilter('anomaly-rings-casing', filter);
      updates += 1;
    }
    if (data.clusterSetVersion[t] !== appliedClusterVersion) {
      appliedClusterVersion = data.clusterSetVersion[t];
      const filter = clusterAtFilter(t);
      map.setFilter('cluster-hulls-fill', filter);
      map.setFilter('cluster-hulls-line', filter);
      updates += 1;
    }
    // (6) risk cells
    for (const id of zoneIds) {
      const risk = data.zoneRiskAt(id, t);
      const count = data.zoneCountAt(id, t);
      if (applied.risk.get(id) !== risk || applied.count.get(id) !== count) {
        applied.risk.set(id, risk);
        applied.count.set(id, count);
        map.setFeatureState({ source: 'risk-zones', id }, { risk, count });
        updates += 1;
      }
    }
    return updates;
  }
  applyTime(store.getState().t);

  // ---- selection -------------------------------------------------------------------------------------------
  let marks = [];
  function mark(source, id, key) {
    map.setFeatureState({ source, id }, { [key]: true });
    marks.push({ source, id, key });
  }
  function applySelection(selection) {
    for (const entry of marks) map.setFeatureState({ source: entry.source, id: entry.id }, { [entry.key]: false });
    marks = [];
    let ring = EMPTY_COLLECTION;
    let anomalyId = null;
    if (selection.kind === 'asset' && data.assetById(selection.id)) {
      mark('assets', selection.id, 'selected');
      for (const sensor of data.sensorsByAsset(selection.id)) mark('sensors', sensor.sensor_id, 'selected');
    } else if (selection.kind === 'sensor' && data.sensorById(selection.id)) {
      const sensor = data.sensorById(selection.id);
      mark('sensors', sensor.sensor_id, 'selected');
      mark('assets', sensor.asset_id, 'nearby');
    } else if (selection.kind === 'anomaly' && data.anomalyById(selection.id)) {
      const anomaly = data.anomalyById(selection.id);
      anomalyId = anomaly.anomaly_id;
      mark('sensors', anomaly.sensor_id, 'selected');
      mark('assets', anomaly.asset_id, 'nearby');
      for (const near of anomaly.nearby_assets || []) mark('assets', near.asset_id, 'nearby');
      const radius = meta.spatial ? meta.spatial.proximity_radius_m : null;
      if (radius) ring = { type: 'FeatureCollection', features: [circlePolygon(anomaly.lon, anomaly.lat, radius)] };
    }
    map.setFilter('anomaly-selected', selectedAnomalyFilter(anomalyId));
    map.getSource('selection-ring').setData(ring);
  }
  applySelection(store.getState().selection);

  // ---- hover tooltip and click -----------------------------------------------------------------------------
  let hovered = null;
  const tooltip = elements.tooltip;
  const tooltipTitle = h('div', { class: 'map-tooltip__title' });
  const tooltipSub = h('div', { class: 'map-tooltip__sub' });
  tooltip.replaceChildren(tooltipTitle, tooltipSub);

  function topHit(point) {
    const layers = INTERACTIVE_LAYERS.filter((id) => hasLayer(id) && map.getLayoutProperty(id, 'visibility') !== 'none');
    if (layers.length === 0) return null;
    const box = [[point.x - HIT_BOX_PX, point.y - HIT_BOX_PX], [point.x + HIT_BOX_PX, point.y + HIT_BOX_PX]];
    const features = map.queryRenderedFeatures(box, { layers });
    let best = null;
    let bestRank = Infinity;
    for (const feature of features) {
      const rank = INTERACTIVE_LAYERS.indexOf(feature.layer.id);
      if (rank < bestRank) {
        best = feature;
        bestRank = rank;
      }
    }
    if (!best) return null;
    if (LAYER_ENTITY[best.layer.id] === 'sensor') {
      // Sensors that share an asset or a street sit a few metres apart and overlap at city zoom: take the one
      // nearest to the pointer, and among (almost) equally near ones the one whose status needs attention,
      // so an offline or anomalous sensor is never hidden behind a normal neighbour.
      const t = store.getState().t;
      const urgency = { anomaly: 0, warning: 1, offline: 2, normal: 3 };
      const scored = features
        .filter((feature) => feature.layer.id === best.layer.id && feature.geometry && feature.geometry.type === 'Point')
        .map((feature) => {
          const at = map.project(feature.geometry.coordinates);
          const distance = Math.hypot(at.x - point.x, at.y - point.y);
          return { feature, bucket: Math.round(distance / HIT_BOX_PX), urgency: urgency[data.sensorStatusAt(feature.id, t)] ?? 3 };
        })
        .sort((a, b) => a.bucket - b.bucket || a.urgency - b.urgency);
      if (scored.length > 0) best = scored[0].feature;
    }
    return { entity: LAYER_ENTITY[best.layer.id], id: best.id, source: best.source, properties: best.properties };
  }

  /** Tooltip text: name · type · status (status as words, at the current simulated hour). */
  function describe(hit) {
    const t = store.getState().t;
    if (hit.entity === 'asset') {
      const feature = data.assetById(hit.id);
      if (!feature) return null;
      const props = feature.properties;
      const status = data.assetStatusAt(hit.id, t);
      const health = data.assetHealthAt(hit.id, t);
      const parts = [assetTypeLabel(props, ASSET_TYPE_LABEL), labelOf(ASSET_STATUS_LABEL, status)];
      if (health !== null) parts.push(`health ${health}`);
      return { title: assetDisplayName(props), sub: parts.join(' · ') };
    }
    if (hit.entity === 'sensor') {
      const sensor = data.sensorById(hit.id);
      if (!sensor) return null;
      const typeLabel = meta.sensor_types[sensor.sensor_type] ? meta.sensor_types[sensor.sensor_type].label : sensor.sensor_type;
      const status = data.sensorStatusAt(hit.id, t);
      const value = formatValue(data.sensorValueAt(hit.id, t), sensor.unit);
      return {
        title: `${sensor.sensor_id} · ${typeLabel} sensor (simulated)`,
        sub: `${labelOf(SENSOR_STATUS_LABEL, status)} · ${value} · ${sensor.asset_name || sensor.asset_id}`,
      };
    }
    if (hit.entity === 'anomaly') {
      const anomaly = data.anomalyById(hit.id);
      if (!anomaly) return null;
      return {
        title: `${anomaly.anomaly_label} (simulated)`,
        sub: `${labelOf(SEVERITY_LABEL, anomaly.severity)} severity · ${anomaly.asset_name || anomaly.asset_id} · ${anomaly.sensor_id}`,
      };
    }
    if (hit.entity === 'zone') {
      const risk = data.zoneRiskAt(hit.id, t);
      const count = data.zoneCountAt(hit.id, t);
      if (risk === 0 && count === 0) return null;
      return {
        title: 'Risk zone (derived from simulated anomalies)',
        sub: `Risk score ${risk} · ${labelOf(RISK_LEVEL_LABEL, data.zoneLevelAt(hit.id, t))} · ${plural(count, 'active anomaly', 'active anomalies')}`,
      };
    }
    if (hit.entity === 'cluster') {
      const cluster = data.clusterById(hit.id);
      if (!cluster) return null;
      const props = cluster.properties;
      return {
        title: 'Co-occurrence cluster — descriptive, not a causal finding',
        sub: `${plural(props.n_anomalies, 'anomaly', 'anomalies')} on ${plural(props.n_sensors, 'sensor')} · highest severity ${labelOf(SEVERITY_LABEL, props.max_severity).toLowerCase()}`,
      };
    }
    return null;
  }

  function setHover(next) {
    const same = hovered && next && hovered.source === next.source && hovered.id === next.id;
    if (same) return;
    if (hovered && hovered.stateful) map.setFeatureState({ source: hovered.source, id: hovered.id }, { hover: false });
    hovered = next;
    if (hovered && hovered.stateful) map.setFeatureState({ source: hovered.source, id: hovered.id }, { hover: true });
  }

  function hideTooltip() {
    tooltip.hidden = true;
    setHover(null);
    map.getCanvas().style.cursor = '';
  }

  map.on('mousemove', (event) => {
    const hit = topHit(event.point);
    const text = hit ? describe(hit) : null;
    if (!hit || !text) {
      hideTooltip();
      return;
    }
    const selectable = hit.entity === 'asset' || hit.entity === 'sensor' || hit.entity === 'anomaly';
    setHover({ source: hit.source, id: hit.id, stateful: hit.entity === 'asset' || hit.entity === 'sensor' });
    map.getCanvas().style.cursor = selectable ? 'pointer' : '';
    tooltipTitle.textContent = text.title;
    tooltipSub.textContent = text.sub;
    tooltip.hidden = false;
    const width = elements.map.clientWidth;
    const height = elements.map.clientHeight;
    const boxWidth = tooltip.offsetWidth;
    const boxHeight = tooltip.offsetHeight;
    const x = event.point.x + 14 + boxWidth > width ? event.point.x - 14 - boxWidth : event.point.x + 14;
    const y = event.point.y + 14 + boxHeight > height ? event.point.y - 10 - boxHeight : event.point.y + 14;
    tooltip.style.transform = `translate(${Math.max(4, x)}px, ${Math.max(4, y)}px)`;
  });
  map.on('mouseout', hideTooltip);
  map.on('movestart', () => {
    tooltip.hidden = true;
  });

  map.on('click', (event) => {
    const hit = topHit(event.point);
    if (hit && (hit.entity === 'asset' || hit.entity === 'sensor' || hit.entity === 'anomaly')) {
      // Map-initiated selection: the camera stays where the user put it.
      actions.select(hit.entity, hit.id, { origin: 'map' });
    } else if (store.getState().selection.kind) {
      actions.clearSelection();
    }
  });

  // Keep the 3D button in step with a pitch gesture (no camera move is triggered by this).
  map.on('pitchend', () => {
    if (cameraBusy) return;
    const pitched = map.getPitch() > 1;
    if (pitched !== store.getState().pitched) store.set({ pitched });
  });

  // ---- store subscriptions ---------------------------------------------------------------------------------
  const unsubscribe = [];
  unsubscribe.push(store.subscribe(['t'], (state) => applyTime(state.t), { order: TICK_ORDER }));

  unsubscribe.push(
    store.subscribe(['layers'], (state, changed, previous) => {
      for (const group of LAYER_GROUPS) {
        if (state.layers[group.id] !== previous.layers[group.id]) setVisibility(group.id, state.layers[group.id]);
      }
      map.setFilter('asset-line-halo', lineHaloFilter(state.layers));
      if (state.layers.city_boundary !== previous.layers.city_boundary && cityBounds) {
        if (state.layers.city_boundary) {
          fitTo([[cityBounds[0], cityBounds[1]], [cityBounds[2], cityBounds[3]]], state.pitched, moveDuration(900));
        } else {
          fitTo(studyBounds, state.pitched, moveDuration(900));
        }
      }
      syncViewButtons(state);
    }),
  );

  unsubscribe.push(
    store.subscribe(['hexMetric'], (state) => {
      const paint = riskZonePaint(state.hexMetric, meta.risk.levels);
      map.setPaintProperty('risk-zones-fill', 'fill-color', paint.fillColor);
      map.setPaintProperty('risk-zones-fill', 'fill-opacity', paint.fillOpacity);
      map.setPaintProperty('risk-zones-outline', 'line-color', paint.lineColor);
      map.setPaintProperty('risk-zones-outline', 'line-opacity', paint.lineOpacity);
    }),
  );

  unsubscribe.push(store.subscribe(['selection'], (state) => applySelection(state.selection)));

  unsubscribe.push(
    store.subscribe(['flyTo'], (state) => {
      const request = state.flyTo;
      if (!request || !Number.isFinite(request.lon) || !Number.isFinite(request.lat)) return;
      // essential: false - with "prefers-reduced-motion" MapLibre jumps instead of flying.
      map.flyTo({ center: [request.lon, request.lat], zoom: request.zoom || FLY_ZOOM, essential: false });
    }),
  );

  unsubscribe.push(
    store.subscribe(['pitched'], (state) => {
      syncViewButtons(state);
      if (homing || state.pitched === map.getPitch() > 1) return;
      map.easeTo({
        pitch: state.pitched ? HOME_VIEW.pitch : 0,
        bearing: state.pitched ? HOME_VIEW.bearing : 0,
        duration: moveDuration(700),
        essential: false,
      });
    }),
  );

  unsubscribe.push(store.subscribe(['imagery'], (state) => syncViewButtons(state)));

  if (elements.skeleton) elements.skeleton.hidden = true;

  return {
    map,
    basemap: fallback ? 'fallback' : 'ok',
    projectLayerIds,
    /** Number of feature-state / filter updates the given hour needs right now (diagnostics). */
    applyTime,
    destroy() {
      unsubscribe.forEach((off) => off());
      map.remove();
    },
  };
}
