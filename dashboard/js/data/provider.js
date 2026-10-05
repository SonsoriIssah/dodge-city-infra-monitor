/**
 * Data access (build contract 12.4). Two providers with identical methods:
 *
 *   ApiProvider     reads the FastAPI service (PostGIS behind it)
 *   StaticProvider  reads the committed snapshot of the same API responses under ./data/snapshot
 *
 * Methods (all return a Promise; results are cached in memory, a failed request is not cached):
 *   getMeta() getAssets() getRoads() getStudyArea() getCityBoundary() getSensors() getAnomalies() getClusters()
 *   getRiskZones() getSimulationEvents() getPlayback() getSensorReadings(sensorId) getAssetHealth(assetId)
 *   getAnomalyDetail(anomalyId) getManifest()
 *
 * Mode: `window.DCIM_CONFIG.mode` ('auto' | 'api' | 'static'), overridden by `?mode=static|api`.
 *   auto    probe `${apiBaseUrl}/health` for 3 s; use the API only for HTTP 200 JSON with
 *           service === 'dodge-city-infra-monitor' and status === 'ok', otherwise the static snapshot
 *   api     the same probe must succeed; a failure raises ProviderError('api-unavailable') so the shell can
 *           show the error state with "Retry" and "Use static snapshot" - never a silent switch
 *   static  no probe
 *
 * Every URL is relative to the page ("./health", "./data/snapshot/meta.json") unless `apiBaseUrl` is set, so
 * the dashboard works under a sub-path (GitHub Pages). No request ends with a slash.
 *
 * Pure module: `fetch` is injectable, no DOM access, importable from Node tests.
 */

export const SERVICE_NAME = 'dodge-city-infra-monitor';
export const DEFAULT_CONFIG = Object.freeze({
  mode: 'auto',
  apiBaseUrl: '',
  basemapStyleUrl: 'https://tiles.openfreemap.org/styles/dark',
});
export const HEALTH_TIMEOUT_MS = 3000;
export const REQUEST_TIMEOUT_MS = 20000;
export const SNAPSHOT_BASE = './data/snapshot';
const MODES = ['auto', 'api', 'static'];
const PAGE_SIZE = 1000;

/**
 * Error of a data request.
 *   kind    'timeout' | 'network' | 'http' | 'not-found' | 'parse' | 'api-unavailable'
 *   url     the request that failed
 *   status  HTTP status when there was a response
 *   detail  the API's `{"detail": ...}` text, or the reason of a failed probe
 */
export class ProviderError extends Error {
  constructor(message, { kind = 'network', url = null, status = null, detail = null, cause = undefined } = {}) {
    super(message);
    this.name = 'ProviderError';
    this.kind = kind;
    this.url = url;
    this.status = status;
    this.detail = detail;
    if (cause !== undefined) this.cause = cause;
  }
}

function resolveFetch(fetchImpl) {
  const impl = fetchImpl || globalThis.fetch;
  if (typeof impl !== 'function') throw new ProviderError('fetch is not available in this environment');
  return impl;
}

/**
 * GET a JSON document with a timeout (AbortController).
 * Rejects with ProviderError: timeout, network failure, non-2xx status, or a body that is not JSON.
 */
export async function fetchJson(url, { timeoutMs = REQUEST_TIMEOUT_MS, fetchImpl } = {}) {
  const doFetch = resolveFetch(fetchImpl);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await doFetch(url, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      cache: 'no-cache',
    });
  } catch (cause) {
    clearTimeout(timer);
    if (controller.signal.aborted) {
      throw new ProviderError(`No answer from ${url} within ${Math.round(timeoutMs / 1000)} s`, {
        kind: 'timeout',
        url,
        cause,
      });
    }
    throw new ProviderError(`Could not reach ${url}`, { kind: 'network', url, cause });
  }
  let text;
  try {
    text = await response.text();
  } catch (cause) {
    clearTimeout(timer);
    const kind = controller.signal.aborted ? 'timeout' : 'network';
    throw new ProviderError(`The answer from ${url} was interrupted`, { kind, url, cause });
  }
  clearTimeout(timer);
  let body;
  let parsed = true;
  try {
    body = JSON.parse(text);
  } catch {
    parsed = false;
  }
  if (!response.ok) {
    const detail = parsed && body && typeof body.detail === 'string' ? body.detail : null;
    throw new ProviderError(detail || `${url} answered HTTP ${response.status}`, {
      kind: response.status === 404 ? 'not-found' : 'http',
      url,
      status: response.status,
      detail,
    });
  }
  if (!parsed) {
    throw new ProviderError(`${url} did not return JSON`, { kind: 'parse', url, status: response.status });
  }
  return body;
}

function trimBase(apiBaseUrl) {
  return typeof apiBaseUrl === 'string' ? apiBaseUrl.trim().replace(/\/+$/, '') : '';
}

/** URL of an API path ("health", "assets/BRG-001/health"): relative to the page unless a base is configured. */
export function apiUrl(apiBaseUrl, path) {
  const base = trimBase(apiBaseUrl);
  const clean = String(path).replace(/^\/+/, '');
  return base ? `${base}/${clean}` : `./${clean}`;
}

/** URL of a snapshot file ("meta.json", "readings/VIB-001.json"). */
export function snapshotUrl(path, base = SNAPSHOT_BASE) {
  return `${base}/${String(path).replace(/^\/+/, '')}`;
}

/**
 * Probe the API. Resolves (never rejects) with `{ok, reason, health}`; `ok` only for HTTP 200 JSON with the
 * expected service name and status "ok" - an HTML 404 page or a degraded service is not the API.
 */
export async function probeHealth(apiBaseUrl, { timeoutMs = HEALTH_TIMEOUT_MS, fetchImpl } = {}) {
  const url = apiUrl(apiBaseUrl, 'health');
  try {
    const health = await fetchJson(url, { timeoutMs, fetchImpl });
    if (!health || typeof health !== 'object' || health.service !== SERVICE_NAME) {
      return { ok: false, url, reason: 'the address answered, but it is not the monitoring API', health: null };
    }
    if (health.status !== 'ok') {
      return { ok: false, url, reason: `the API reports status "${health.status}"`, health };
    }
    return { ok: true, url, reason: null, health };
  } catch (error) {
    return { ok: false, url, reason: error instanceof Error ? error.message : String(error), health: null };
  }
}

/** Mode asked for: `?mode=static|api` wins over the configuration; anything unknown means 'auto'. */
export function requestedMode(config = {}, search = '') {
  let override = null;
  try {
    override = new URLSearchParams(search || '').get('mode');
  } catch {
    override = null;
  }
  if (override === 'static' || override === 'api') return override;
  return MODES.includes(config.mode) ? config.mode : 'auto';
}

/**
 * Decide between the API and the static snapshot.
 * @returns {Promise<{mode: 'api'|'static', requested: 'auto'|'api'|'static', probe: object|null}>}
 * @throws {ProviderError} kind 'api-unavailable' when the API was asked for explicitly and the probe failed
 */
export async function resolveMode(config = {}, { search = '', fetchImpl, timeoutMs = HEALTH_TIMEOUT_MS } = {}) {
  const requested = requestedMode(config, search);
  if (requested === 'static') return { mode: 'static', requested, probe: null };
  const probe = await probeHealth(config.apiBaseUrl, { timeoutMs, fetchImpl });
  if (probe.ok) return { mode: 'api', requested, probe };
  if (requested === 'api') {
    throw new ProviderError(`The monitoring API is not available: ${probe.reason}.`, {
      kind: 'api-unavailable',
      url: probe.url,
      detail: probe.reason,
    });
  }
  return { mode: 'static', requested, probe };
}

/** Shared caching: one Promise per resource key; a rejected Promise is forgotten so a retry asks again. */
class CachingProvider {
  constructor({ fetchImpl, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    this._fetch = fetchImpl;
    this._timeoutMs = timeoutMs;
    this._cache = new Map();
  }

  _cached(key, load) {
    if (this._cache.has(key)) return this._cache.get(key);
    const promise = Promise.resolve()
      .then(load)
      .catch((error) => {
        this._cache.delete(key);
        throw error;
      });
    this._cache.set(key, promise);
    return promise;
  }

  _get(url) {
    return fetchJson(url, { timeoutMs: this._timeoutMs, fetchImpl: this._fetch });
  }

  /** Forget every cached answer (used by "Retry"). */
  clearCache() {
    this._cache.clear();
  }
}

/** Provider over the FastAPI service. */
export class ApiProvider extends CachingProvider {
  constructor(config = {}, options = {}) {
    super(options);
    this.mode = 'api';
    this.apiBaseUrl = trimBase(config.apiBaseUrl);
    this.sourceLabel = 'Source: PostGIS API';
  }

  _api(path) {
    return this._get(apiUrl(this.apiBaseUrl, path));
  }

  /** Read a paged `{total, items}` list completely. */
  async _allPages(path) {
    const join = path.includes('?') ? '&' : '?';
    const first = await this._api(`${path}${join}limit=${PAGE_SIZE}`);
    const items = Array.isArray(first.items) ? first.items.slice() : [];
    const total = Number.isFinite(first.total) ? first.total : items.length;
    while (items.length < total) {
      const page = await this._api(`${path}${join}limit=${PAGE_SIZE}&offset=${items.length}`);
      if (!Array.isArray(page.items) || page.items.length === 0) break;
      items.push(...page.items);
    }
    return { ...first, items, total, limit: items.length, offset: 0 };
  }

  getMeta() {
    return this._cached('meta', () => this._api('meta'));
  }

  getAssets() {
    return this._cached('assets', () => this._api('assets'));
  }

  getRoads() {
    return this._cached('roads', () => this._api('layers/roads'));
  }

  getStudyArea() {
    return this._cached('study-area', () => this._api('layers/study-area'));
  }

  getCityBoundary() {
    return this._cached('city-boundary', () => this._api('layers/city-boundary'));
  }

  getSensors() {
    return this._cached('sensors', () => this._allPages('sensors'));
  }

  getAnomalies() {
    return this._cached('anomalies', () => this._allPages('anomalies?include=nearby_assets'));
  }

  getClusters() {
    return this._cached('clusters', () => this._api('spatial/clusters'));
  }

  getRiskZones() {
    return this._cached('risk-zones', () => this._api('spatial/risk-zones'));
  }

  getSimulationEvents() {
    return this._cached('simulation-events', () => this._api('simulation-events'));
  }

  getPlayback() {
    return this._cached('playback', () => this._api('playback'));
  }

  /** Columnar readings of one sensor for the whole window. */
  getSensorReadings(sensorId) {
    const id = encodeURIComponent(sensorId);
    return this._cached(`readings:${sensorId}`, () => this._api(`sensor-readings?sensor_id=${id}&shape=columns`));
  }

  /** Columnar health history of one monitored asset; rejects with kind 'not-found' for an unmonitored one. */
  getAssetHealth(assetId) {
    const id = encodeURIComponent(assetId);
    return this._cached(`health:${assetId}`, () => this._api(`assets/${id}/health`));
  }

  /** Anomaly item + `nearby_assets[]` + `cluster` (object or null). */
  getAnomalyDetail(anomalyId) {
    const id = encodeURIComponent(anomalyId);
    return this._cached(`anomaly:${anomalyId}`, () => this._api(`anomalies/${id}`));
  }

  /** The API has no export manifest: the same shape is derived from /meta (no file list). */
  getManifest() {
    return this._cached('manifest', async () => {
      const meta = await this.getMeta();
      return {
        generated_at: meta.detection_run ? meta.detection_run.finished_at : null,
        as_of: meta.time ? meta.time.end : null,
        api_version: meta.version,
        files: [],
      };
    });
  }
}

/** Provider over the exported snapshot (GitHub Pages, or any static file server). */
export class StaticProvider extends CachingProvider {
  constructor(config = {}, options = {}) {
    super(options);
    this.mode = 'static';
    this.base = options.snapshotBase || SNAPSHOT_BASE;
    this.sourceLabel = 'Source: static snapshot';
  }

  _file(path) {
    return this._get(snapshotUrl(path, this.base));
  }

  getMeta() {
    return this._cached('meta', () => this._file('meta.json'));
  }

  getAssets() {
    return this._cached('assets', () => this._file('assets.geojson'));
  }

  getRoads() {
    return this._cached('roads', () => this._file('roads.geojson'));
  }

  getStudyArea() {
    return this._cached('study-area', () => this._file('study-area.geojson'));
  }

  getCityBoundary() {
    return this._cached('city-boundary', () => this._file('city-boundary.geojson'));
  }

  getSensors() {
    return this._cached('sensors', () => this._file('sensors.json'));
  }

  getAnomalies() {
    return this._cached('anomalies', () => this._file('anomalies.json'));
  }

  getClusters() {
    return this._cached('clusters', () => this._file('clusters.geojson'));
  }

  getRiskZones() {
    return this._cached('risk-zones', () => this._file('risk-zones.geojson'));
  }

  getSimulationEvents() {
    return this._cached('simulation-events', () => this._file('simulation-events.json'));
  }

  getPlayback() {
    return this._cached('playback', () => this._file('playback.json'));
  }

  getSensorReadings(sensorId) {
    const id = encodeURIComponent(sensorId);
    return this._cached(`readings:${sensorId}`, () => this._file(`readings/${id}.json`));
  }

  /** Exported for monitored assets only; rejects with kind 'not-found' for an unmonitored one, like the API. */
  getAssetHealth(assetId) {
    const id = encodeURIComponent(assetId);
    return this._cached(`health:${assetId}`, () => this._file(`health/${id}.json`));
  }

  /** Same shape as GET /anomalies/{id}: the list item (which carries nearby_assets) + its cluster or null. */
  getAnomalyDetail(anomalyId) {
    return this._cached(`anomaly:${anomalyId}`, async () => {
      const [anomalies, clusters] = await Promise.all([this.getAnomalies(), this.getClusters()]);
      const item = (anomalies.items || []).find((entry) => entry.anomaly_id === anomalyId);
      if (!item) {
        throw new ProviderError(`anomaly '${anomalyId}' not found`, {
          kind: 'not-found',
          url: snapshotUrl('anomalies.json', this.base),
          status: 404,
        });
      }
      const feature =
        item.cluster_id === null || item.cluster_id === undefined
          ? null
          : (clusters.features || []).find((entry) => entry.properties.cluster_id === item.cluster_id);
      return {
        ...item,
        nearby_assets: Array.isArray(item.nearby_assets) ? item.nearby_assets : [],
        cluster: feature ? { ...feature.properties } : null,
      };
    });
  }

  getManifest() {
    return this._cached('manifest', () => this._file('manifest.json'));
  }
}

/**
 * Resolve the mode and build the matching provider.
 *
 * @param {object} config  `window.DCIM_CONFIG` ({mode, apiBaseUrl, basemapStyleUrl})
 * @param {object} [options]
 * @param {string} [options.search]     the page's query string (for `?mode=`)
 * @param {function} [options.fetchImpl] replacement for `fetch` (tests)
 * @param {'api'|'static'} [options.mode] skip the resolution and use this mode
 * @returns {Promise<ApiProvider|StaticProvider>} with `.mode`, `.requestedMode` and `.probe`
 * @throws {ProviderError} kind 'api-unavailable' in explicit API mode when /health does not answer correctly
 */
export async function createProvider(config = {}, options = {}) {
  const merged = { ...DEFAULT_CONFIG, ...config };
  const resolved = options.mode
    ? { mode: options.mode, requested: options.mode, probe: null }
    : await resolveMode(merged, { search: options.search, fetchImpl: options.fetchImpl });
  const provider =
    resolved.mode === 'api' ? new ApiProvider(merged, options) : new StaticProvider(merged, options);
  provider.requestedMode = resolved.requested;
  provider.probe = resolved.probe;
  return provider;
}
