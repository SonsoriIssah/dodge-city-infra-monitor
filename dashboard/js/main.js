/**
 * Boot of the dashboard (build contract 12.3, 12.4, 12.8).
 *
 *   1. resolve the data source (API or static snapshot) - js/data/provider.js
 *   2. load the lists once (meta, assets, sensors, anomalies, layers, playback bundle)
 *   3. build the in-memory index and the store; the clock starts at the end of the simulation
 *   4. mount the modules in the order a clock tick must run through them:
 *      timeline (10) -> KPI cards and status sentence (20) -> map (30) -> layer card and panel tabs (40)
 *
 * States: loading skeleton until step 3; a full error state with Retry when the data cannot be loaded (plus
 * "Use static snapshot" when the API was the source); the map zone has its own error state so the panels
 * keep working without WebGL.
 */

import * as helpers from './data/index.js';
import { DEFAULT_CONFIG, createProvider } from './data/provider.js';
import { createMap } from './map/map.js';
import { defaultLayerState, layerGroupForAssetType } from './map/layers.js';
import { mountAbout } from './panels/about.js';
import { introSeen, mountIntro } from './panels/intro.js';
import { mountKpis } from './panels/kpis.js';
import { mountLayers } from './panels/layers.js';
import { TABS, mountTabs } from './panels/tabs.js';
import { NO_SELECTION, createStore, defaultFilters } from './state/store.js';
import { DEFAULT_SPEED, mountTimeline } from './timeline/timeline.js';
import * as dom from './ui/dom.js';
import * as numberFormat from './ui/format.js';
import * as tokens from './ui/tokens.js';

const WIDE_VIEWPORT_PX = 1600;
const FLY_ZOOM = 17;
const PERF_SAMPLES = 2000;

const $ = (id) => document.getElementById(id);
const config = { ...DEFAULT_CONFIG, ...(window.DCIM_CONFIG || {}) };

/** Durations of playback ticks (store.set of one hour, all subscribers included), for diagnostics. */
function createPerfRecorder() {
  let samples = [];
  return {
    record(ms) {
      samples.push(ms);
      if (samples.length > PERF_SAMPLES) samples = samples.slice(-PERF_SAMPLES);
    },
    reset() {
      samples = [];
    },
    summary() {
      if (samples.length === 0) return { count: 0, median: null, p95: null, max: null };
      const sorted = samples.slice().sort((a, b) => a - b);
      const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
      return { count: sorted.length, median: at(0.5), p95: at(0.95), max: sorted[sorted.length - 1] };
    },
  };
}

// ---- full-page states ------------------------------------------------------------------------------------------
function showFatal(error, { offerStatic }) {
  const overlay = $('app-overlay');
  const actions = [
    dom.h('button', { class: 'btn btn--primary', type: 'button', on: { click: () => window.location.reload() } }, dom.icon('refresh'), 'Retry'),
  ];
  if (offerStatic) {
    actions.push(
      dom.h('button', {
        class: 'btn',
        type: 'button',
        text: 'Use static snapshot',
        on: {
          click: () => {
            const url = new URL(window.location.href);
            url.searchParams.set('mode', 'static');
            window.location.assign(url.href);
          },
        },
      }),
    );
  }
  const apiDown = error && error.kind === 'api-unavailable';
  overlay.replaceChildren(
    dom.h(
      'div',
      { class: 'app-overlay__box' },
      dom.h('h2', { text: apiDown ? 'The monitoring API is not available' : 'The dashboard data could not be loaded' }),
      dom.h('p', {
        text: apiDown
          ? 'This page is set to read from the PostGIS API, which did not answer its health check. Start the API (and its database) and retry, or open the exported static snapshot instead.'
          : 'A data file or API response is missing or unreadable. Check the connection and retry.',
      }),
      dom.h('div', { class: 'app-overlay__detail', text: [dom.describeError(error), error && error.url ? `Request: ${error.url}` : null].filter(Boolean).join('\n') }),
      dom.h('div', { class: 'app-overlay__actions' }, actions),
    ),
  );
  overlay.hidden = false;
  $('source-badge').textContent = 'Source: not available';
  document.body.classList.remove('is-loading');
  document.body.classList.add('has-error');
}

function showMapError(error) {
  const skeleton = $('map-skeleton');
  skeleton.hidden = false;
  skeleton.replaceChildren(
    dom.h(
      'div',
      { class: 'map-skeleton__box' },
      dom.errorState('The map could not be displayed.', {
        detail: `${dom.describeError(error)} The panels and the timeline still work.`,
        onRetry: () => window.location.reload(),
      }),
    ),
  );
}

// ---- actions: the only way modules change the state ----------------------------------------------------------------
function createActions(store, data, about) {
  let flySequence = 0;
  const clampTime = (t) => Math.min(data.lastIndex, Math.max(0, Math.round(t)));

  function locate(target) {
    if (!target) return null;
    if (Number.isFinite(target.lon) && Number.isFinite(target.lat)) return target;
    if (target.kind === 'asset') {
      const feature = data.assetById(target.id);
      const centroid = feature ? feature.properties.centroid : null;
      return centroid ? { lon: centroid[0], lat: centroid[1] } : null;
    }
    if (target.kind === 'sensor') {
      const sensor = data.sensorById(target.id);
      return sensor ? { lon: sensor.lon, lat: sensor.lat } : null;
    }
    if (target.kind === 'anomaly') {
      const anomaly = data.anomalyById(target.id);
      return anomaly ? { lon: anomaly.lon, lat: anomaly.lat } : null;
    }
    return null;
  }

  function flyRequest(target) {
    const place = locate(target);
    if (!place) return null;
    flySequence += 1;
    return { lon: place.lon, lat: place.lat, zoom: target.zoom || FLY_ZOOM, seq: flySequence };
  }

  /** Layers that must be visible for the selected entity to be seen on the map. */
  function layersFor(kind, id, layers) {
    const needed = [];
    if (kind === 'asset') {
      const feature = data.assetById(id);
      const group = feature ? layerGroupForAssetType(feature.properties.asset_type) : null;
      if (group) needed.push(group.id);
    } else if (kind === 'sensor') {
      needed.push('sensors');
    } else if (kind === 'anomaly') {
      needed.push('anomalies', 'sensors');
    }
    const missing = needed.filter((groupId) => !layers[groupId]);
    if (missing.length === 0) return layers;
    const next = { ...layers };
    missing.forEach((groupId) => {
      next[groupId] = true;
    });
    return next;
  }

  const actions = {
    select(kind, id, { origin = 'list' } = {}) {
      const exists =
        (kind === 'asset' && data.assetById(id)) ||
        (kind === 'sensor' && data.sensorById(id)) ||
        (kind === 'anomaly' && data.anomalyById(id));
      if (!exists) return;
      const state = store.getState();
      const patch = {
        selection: { kind, id },
        tab: kind === 'asset' ? 'assets' : kind === 'sensor' ? 'sensors' : 'anomalies',
        layers: layersFor(kind, id, state.layers),
      };
      if (kind === 'anomaly') {
        // Outside the anomaly's own window the clock moves to its peak, so the map and the detail agree.
        const anomaly = data.anomalyById(id);
        if (state.t < anomaly.start_idx || state.t > anomaly.end_idx) patch.t = anomaly.peak_idx;
      }
      if (origin === 'list') {
        const request = flyRequest({ kind, id });
        if (request) patch.flyTo = request;
      }
      store.set(patch);
    },
    clearSelection() {
      if (store.getState().selection.kind) store.set({ selection: NO_SELECTION });
    },
    flyTo(target) {
      const request = flyRequest(target);
      if (request) store.set({ flyTo: request });
    },
    setTime(t) {
      if (Number.isFinite(t)) store.set({ t: clampTime(t) });
    },
    stepTime(delta) {
      store.set({ t: clampTime(store.getState().t + delta) });
    },
    play() {
      const state = store.getState();
      if (state.playing) return;
      // Play at the last hour restarts from the first hour.
      store.set(state.t >= data.lastIndex ? { t: 0, playing: true } : { playing: true });
    },
    pause() {
      store.set({ playing: false });
    },
    togglePlay() {
      if (store.getState().playing) actions.pause();
      else actions.play();
    },
    setTab(tab) {
      if (TABS.some((entry) => entry.id === tab)) store.set({ tab });
    },
    setFilter(patch) {
      const current = store.getState().filters;
      const next = { ...current, ...patch };
      next.severity = new Set(patch.severity !== undefined ? patch.severity : current.severity);
      next.sensorType = new Set(patch.sensorType !== undefined ? patch.sensorType : current.sensorType);
      store.set({ filters: next });
    },
    toggleFilterValue(key, value, on) {
      if (key !== 'severity' && key !== 'sensorType') return;
      const values = new Set(store.getState().filters[key]);
      if (on) values.add(value);
      else values.delete(value);
      actions.setFilter({ [key]: values });
    },
    clearFilters() {
      store.set({ filters: defaultFilters(data.meta) });
    },
    setLayer(id, on) {
      const layers = store.getState().layers;
      if (!(id in layers) || layers[id] === Boolean(on)) return;
      store.set({ layers: { ...layers, [id]: Boolean(on) } });
    },
    setHexMetric(metric) {
      if (metric === 'risk' || metric === 'count') store.set({ hexMetric: metric });
    },
    /** KPI shortcut: Anomalies tab with the given status and severities (all other filters at default). */
    showAnomalies({ status = 'all', severity } = {}) {
      const filters = defaultFilters(data.meta);
      filters.status = status;
      if (severity) filters.severity = new Set(severity);
      store.set({ tab: 'anomalies', selection: NO_SELECTION, filters });
    },
    /** KPI shortcut: the "Needs attention" ranking of the Assets tab. */
    showAssetsRanking() {
      store.set({ tab: 'assets', selection: NO_SELECTION });
    },
    /** KPI shortcut: the sensor list. */
    showSensors() {
      store.set({ tab: 'sensors', selection: NO_SELECTION });
    },
    openAbout() {
      about.open();
    },
  };
  return actions;
}

// ---- header -------------------------------------------------------------------------------------------------------
function renderHeader(provider, data) {
  const { meta, manifest, format } = data;
  $('study-area-name').textContent = `${meta.study_area.name} · research prototype`;
  const badge = $('source-badge');
  if (provider.mode === 'api') {
    badge.replaceChildren('Source: PostGIS API');
    badge.title = 'Data is read from the FastAPI service backed by PostgreSQL/PostGIS.';
  } else {
    const exported = manifest && manifest.generated_at ? format.dateLong(manifest.generated_at) : null;
    badge.replaceChildren(
      'Source: static snapshot',
      exported ? dom.h('span', { class: 'badge__extra', text: ` (exported ${exported})` }) : '',
    );
    badge.title = exported
      ? `Data is read from files exported from the API on ${exported}.`
      : 'Data is read from files exported from the API.';
  }
}

// ---- boot ---------------------------------------------------------------------------------------------------------
async function boot() {
  if (window.DCIM_FILE_PROTOCOL) return;

  let provider;
  try {
    provider = await createProvider(config, { search: window.location.search });
  } catch (error) {
    showFatal(error, { offerStatic: error && error.kind === 'api-unavailable' });
    return;
  }

  let raw;
  // The city limits are context only: the dashboard starts without them when they cannot be loaded.
  const cityBoundaryRequest = provider.getCityBoundary().catch((error) => {
    console.warn('[boot] city limits not available:', dom.describeError(error));
    return null;
  });
  try {
    const [meta, assets, roads, studyArea, sensors, anomalies, clusters, riskZones, simulationEvents, playback, manifest] =
      await Promise.all([
        provider.getMeta(),
        provider.getAssets(),
        provider.getRoads(),
        provider.getStudyArea(),
        provider.getSensors(),
        provider.getAnomalies(),
        provider.getClusters(),
        provider.getRiskZones(),
        provider.getSimulationEvents(),
        provider.getPlayback(),
        provider.getManifest(),
      ]);
    raw = { meta, assets, roads, studyArea, sensors, anomalies, clusters, riskZones, simulationEvents, playback, manifest };
  } catch (error) {
    showFatal(error, { offerStatic: provider.mode === 'api' });
    return;
  }

  let data;
  try {
    data = helpers.buildIndex(raw);
  } catch (error) {
    showFatal(error, { offerStatic: provider.mode === 'api' });
    return;
  }

  const store = createStore({
    t: data.lastIndex,
    playing: false,
    speed: DEFAULT_SPEED,
    selection: NO_SELECTION,
    tab: TABS[0].id,
    filters: defaultFilters(data.meta),
    layers: defaultLayerState(),
    pitched: true,
    hexMetric: 'risk',
    flyTo: null,
    layersOpen: window.innerWidth >= WIDE_VIEWPORT_PX,
    introOpen: !introSeen(),
    basemap: 'pending',
    imagery: 'unknown',
  });
  const perf = createPerfRecorder();
  const about = mountAbout($('about-dialog'), { data });
  const actions = createActions(store, data, about);
  const format = { ...numberFormat, ...data.format };
  const ctx = { store, data, provider, format, tokens, dom, helpers, actions };

  renderHeader(provider, data);
  $('btn-about').addEventListener('click', () => about.open());
  $('btn-intro').addEventListener('click', () => store.set({ introOpen: !store.getState().introOpen }));

  // Mount order = order of a clock tick (contract 12.7).
  mountTimeline($('timeline'), { store, data, actions, perf });
  mountKpis({ strip: $('kpi-strip'), sentence: $('status-sentence') }, { store, data, actions });
  mountLayers({ card: $('layers-card'), legend: $('legend') }, { store, data, actions });
  mountIntro($('intro-card'), { store, data });
  mountTabs($('panel'), ctx);

  // Esc: close the intro card first, then clear the selection. (An open <dialog> handles Esc itself.)
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (document.querySelector('dialog[open]')) return;
    const tagName = event.target && event.target.tagName ? event.target.tagName.toLowerCase() : '';
    if (['input', 'select', 'textarea'].includes(tagName)) return;
    const state = store.getState();
    if (state.introOpen) store.set({ introOpen: false });
    else if (state.selection.kind) actions.clearSelection();
  });

  document.body.classList.remove('is-loading');
  window.DCIM = { config, provider, data, store, actions, perf, map: null };

  const cityBoundary = await cityBoundaryRequest;
  try {
    const controller = await createMap({
      config,
      data,
      cityBoundary,
      store,
      actions,
      elements: {
        map: $('map'),
        skeleton: $('map-skeleton'),
        tooltip: $('map-tooltip'),
        notices: $('map-notices'),
        viewButtons: $('view-buttons'),
      },
    });
    window.DCIM.map = controller;
  } catch (error) {
    console.error('[boot] the map could not be created', error);
    showMapError(error);
  }
}

boot().catch((error) => {
  console.error('[boot] unexpected failure', error);
  showFatal(error, { offerStatic: false });
});
