/**
 * js/data/provider.js with a mocked fetch: mode resolution (auto / api / static, URL override), the request
 * URLs of both providers, caching, paging and the static composition of an anomaly detail.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ApiProvider,
  ProviderError,
  SERVICE_NAME,
  StaticProvider,
  apiUrl,
  createProvider,
  fetchJson,
  probeHealth,
  requestedMode,
  resolveMode,
  snapshotUrl,
} from '../js/data/provider.js';

const HEALTH_OK = { status: 'ok', service: SERVICE_NAME, version: '1.0.0', database: 'ok' };

function response(body, { status = 200, json = true } = {}) {
  const text = json ? JSON.stringify(body) : String(body);
  return { ok: status >= 200 && status < 300, status, text: async () => text };
}

/** fetch mock: `routes` maps a URL (or '*') to a response, a function, or an Error to throw. */
function mockFetch(routes) {
  const calls = [];
  const impl = async (url, options = {}) => {
    calls.push(String(url));
    const route = Object.prototype.hasOwnProperty.call(routes, url) ? routes[url] : routes['*'];
    if (route === undefined) return response({ detail: 'Not Found' }, { status: 404 });
    const value = typeof route === 'function' ? await route(url, options) : route;
    if (value instanceof Error) throw value;
    return value;
  };
  impl.calls = calls;
  return impl;
}

/** fetch that never answers until the request is aborted (a hanging server). */
function hangingFetch() {
  const impl = (url, { signal } = {}) =>
    new Promise((resolve, reject) => {
      impl.calls.push(String(url));
      signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    });
  impl.calls = [];
  return impl;
}

test('requestedMode: ?mode= wins over the configuration; unknown values mean auto', () => {
  assert.equal(requestedMode({ mode: 'auto' }, ''), 'auto');
  assert.equal(requestedMode({ mode: 'api' }, ''), 'api');
  assert.equal(requestedMode({ mode: 'static' }, ''), 'static');
  assert.equal(requestedMode({ mode: 'api' }, '?mode=static'), 'static');
  assert.equal(requestedMode({ mode: 'static' }, '?foo=1&mode=api'), 'api');
  assert.equal(requestedMode({ mode: 'static' }, '?mode=banana'), 'static');
  assert.equal(requestedMode({ mode: 'banana' }, ''), 'auto');
  assert.equal(requestedMode({}, ''), 'auto');
});

test('URLs are relative to the page unless a base is configured, and never end with a slash', () => {
  assert.equal(apiUrl('', 'health'), './health');
  assert.equal(apiUrl('', '/assets/BRG-001/health'), './assets/BRG-001/health');
  assert.equal(apiUrl('https://api.example.org/', 'health'), 'https://api.example.org/health');
  assert.equal(apiUrl('https://api.example.org/base//', 'meta'), 'https://api.example.org/base/meta');
  assert.equal(snapshotUrl('meta.json'), './data/snapshot/meta.json');
  assert.equal(snapshotUrl('/readings/VIB-001.json'), './data/snapshot/readings/VIB-001.json');
});

test('auto -> api when /health answers 200 JSON with the service name and status ok', async () => {
  const fetchImpl = mockFetch({ './health': response(HEALTH_OK) });
  const resolved = await resolveMode({ mode: 'auto', apiBaseUrl: '' }, { fetchImpl });
  assert.equal(resolved.mode, 'api');
  assert.equal(resolved.requested, 'auto');
  assert.equal(resolved.probe.ok, true);
  assert.deepEqual(fetchImpl.calls, ['./health']);
  const provider = await createProvider({ mode: 'auto' }, { fetchImpl });
  assert.ok(provider instanceof ApiProvider);
  assert.equal(provider.mode, 'api');
  assert.equal(provider.sourceLabel, 'Source: PostGIS API');
});

test('auto -> static when /health is an HTML page with status 200 (a static file server)', async () => {
  const fetchImpl = mockFetch({ './health': response('<!doctype html><title>Directory listing</title>', { json: false }) });
  const resolved = await resolveMode({ mode: 'auto' }, { fetchImpl });
  assert.equal(resolved.mode, 'static');
  assert.equal(resolved.probe.ok, false);
  const provider = await createProvider({ mode: 'auto' }, { fetchImpl });
  assert.ok(provider instanceof StaticProvider);
  assert.equal(provider.requestedMode, 'auto');
});

test('auto -> static on 404', async () => {
  const fetchImpl = mockFetch({ './health': response('<h1>404 Not Found</h1>', { status: 404, json: false }) });
  const resolved = await resolveMode({ mode: 'auto' }, { fetchImpl });
  assert.equal(resolved.mode, 'static');
  assert.match(resolved.probe.reason, /404/);
});

test('auto -> static on a network failure', async () => {
  const fetchImpl = mockFetch({ './health': new TypeError('Failed to fetch') });
  const resolved = await resolveMode({ mode: 'auto' }, { fetchImpl });
  assert.equal(resolved.mode, 'static');
  assert.match(resolved.probe.reason, /Could not reach/);
});

test('auto -> static when /health does not answer within the timeout', async () => {
  const fetchImpl = hangingFetch();
  const started = Date.now();
  const resolved = await resolveMode({ mode: 'auto' }, { fetchImpl, timeoutMs: 40 });
  assert.equal(resolved.mode, 'static');
  assert.match(resolved.probe.reason, /No answer/);
  assert.ok(Date.now() - started < 2000);
  assert.deepEqual(fetchImpl.calls, ['./health']);
});

test('auto -> static when another service answers, or when the API reports a degraded state', async () => {
  const wrong = mockFetch({ './health': response({ status: 'ok', service: 'some-other-service' }) });
  assert.equal((await resolveMode({ mode: 'auto' }, { fetchImpl: wrong })).mode, 'static');
  const noName = mockFetch({ './health': response({ status: 'ok' }) });
  assert.equal((await resolveMode({ mode: 'auto' }, { fetchImpl: noName })).mode, 'static');
  const degraded = mockFetch({ './health': response({ status: 'degraded', service: SERVICE_NAME, database: 'unavailable' }) });
  const resolved = await resolveMode({ mode: 'auto' }, { fetchImpl: degraded });
  assert.equal(resolved.mode, 'static');
  assert.match(resolved.probe.reason, /degraded/);
  const unavailable = mockFetch({ './health': response({ status: 'degraded', service: SERVICE_NAME }, { status: 503 }) });
  assert.equal((await resolveMode({ mode: 'auto' }, { fetchImpl: unavailable })).mode, 'static');
});

test('forced api: a failed health check is an error, never a silent switch to the snapshot', async () => {
  for (const fetchImpl of [
    mockFetch({ './health': response('<html></html>', { json: false }) }),
    mockFetch({ './health': response('nope', { status: 404, json: false }) }),
    mockFetch({ './health': new TypeError('Failed to fetch') }),
    mockFetch({ './health': response({ status: 'ok', service: 'other' }) }),
  ]) {
    await assert.rejects(
      () => createProvider({ mode: 'api' }, { fetchImpl }),
      (error) => {
        assert.ok(error instanceof ProviderError);
        assert.equal(error.kind, 'api-unavailable');
        assert.equal(error.url, './health');
        assert.ok(error.detail);
        return true;
      },
    );
  }
  // ?mode=api on a page configured for the snapshot behaves the same.
  await assert.rejects(
    () => createProvider({ mode: 'static' }, { search: '?mode=api', fetchImpl: mockFetch({}) }),
    (error) => error.kind === 'api-unavailable',
  );
  const hanging = hangingFetch();
  await assert.rejects(() => resolveMode({ mode: 'api' }, { fetchImpl: hanging, timeoutMs: 30 }), (error) => error.kind === 'api-unavailable');
});

test('forced api succeeds when the API is healthy; static never probes', async () => {
  const healthy = mockFetch({ 'https://api.example.org/health': response(HEALTH_OK) });
  const api = await createProvider({ mode: 'api', apiBaseUrl: 'https://api.example.org/' }, { fetchImpl: healthy });
  assert.equal(api.mode, 'api');
  assert.deepEqual(healthy.calls, ['https://api.example.org/health']);

  const untouched = mockFetch({ './health': response(HEALTH_OK) });
  const fromConfig = await createProvider({ mode: 'static' }, { fetchImpl: untouched });
  assert.equal(fromConfig.mode, 'static');
  const fromUrl = await createProvider({ mode: 'auto' }, { search: '?mode=static', fetchImpl: untouched });
  assert.equal(fromUrl.mode, 'static');
  assert.equal(fromUrl.requestedMode, 'static');
  assert.deepEqual(untouched.calls, []);
});

test('probeHealth never rejects', async () => {
  const probe = await probeHealth('', { fetchImpl: mockFetch({ './health': new Error('boom') }) });
  assert.deepEqual({ ok: probe.ok, url: probe.url, health: probe.health }, { ok: false, url: './health', health: null });
});

test('fetchJson classifies failures', async () => {
  const kindOf = async (fetchImpl, options = {}) => {
    try {
      await fetchJson('./x', { fetchImpl, ...options });
      return 'resolved';
    } catch (error) {
      return `${error.kind}:${error.status}`;
    }
  };
  assert.equal(await kindOf(mockFetch({ './x': response({ a: 1 }) })), 'resolved');
  assert.equal(await kindOf(mockFetch({ './x': response({ detail: 'asset not found' }, { status: 404 }) })), 'not-found:404');
  assert.equal(await kindOf(mockFetch({ './x': response({ detail: 'database unavailable' }, { status: 503 }) })), 'http:503');
  assert.equal(await kindOf(mockFetch({ './x': response('<html>', { json: false }) })), 'parse:200');
  assert.equal(await kindOf(mockFetch({ './x': new TypeError('offline') })), 'network:null');
  assert.equal(await kindOf(hangingFetch(), { timeoutMs: 20 }), 'timeout:null');
  await assert.rejects(
    () => fetchJson('./x', { fetchImpl: mockFetch({ './x': response({ detail: 'database unavailable' }, { status: 503 }) }) }),
    /database unavailable/,
  );
});

test('ApiProvider: endpoints, caching, retry after a failure, and complete paging', async () => {
  const page = (offset) => ({ total: 2500, limit: 1000, offset, items: Array.from({ length: Math.min(1000, 2500 - offset) }, (_, i) => ({ sensor_id: `S-${offset + i}` })) });
  let metaFailures = 1;
  const fetchImpl = mockFetch({
    './meta': () => (metaFailures-- > 0 ? response({ detail: 'database unavailable' }, { status: 503 }) : response({ version: '1.0.0', time: { end: 'T' }, detection_run: { finished_at: 'F' } })),
    './assets': response({ type: 'FeatureCollection', features: [] }),
    './layers/roads': response({ type: 'FeatureCollection', features: [] }),
    './layers/study-area': response({}),
    './layers/city-boundary': response({}),
    './sensors?limit=1000': response(page(0)),
    './sensors?limit=1000&offset=1000': response(page(1000)),
    './sensors?limit=1000&offset=2000': response(page(2000)),
    './anomalies?include=nearby_assets&limit=1000': response({ total: 1, items: [{ anomaly_id: 'ANM-0001' }] }),
    './spatial/clusters': response({ features: [] }),
    './spatial/risk-zones': response({ features: [] }),
    './simulation-events': response({ items: [] }),
    './playback': response({ timestamps: [] }),
    './sensor-readings?sensor_id=VIB-001&shape=columns': response({ sensor_id: 'VIB-001' }),
    './assets/BRG-001/health': response({ asset_id: 'BRG-001' }),
    './assets/BLD-0014/health': response({ detail: "asset 'BLD-0014' is not monitored" }, { status: 404 }),
    './anomalies/ANM-0001': response({ anomaly_id: 'ANM-0001', nearby_assets: [], cluster: null }),
  });
  const provider = new ApiProvider({ apiBaseUrl: '' }, { fetchImpl });

  await assert.rejects(() => provider.getMeta(), (error) => error.kind === 'http' && error.status === 503);
  const meta = await provider.getMeta();
  assert.equal(meta.version, '1.0.0');
  assert.equal(await provider.getMeta(), meta);
  assert.equal(fetchImpl.calls.filter((url) => url === './meta').length, 2, 'a failed request is not cached; a good one is');

  await Promise.all([
    provider.getAssets(), provider.getRoads(), provider.getStudyArea(), provider.getCityBoundary(), provider.getClusters(),
    provider.getRiskZones(), provider.getSimulationEvents(), provider.getPlayback(),
  ]);
  const sensors = await provider.getSensors();
  assert.equal(sensors.items.length, 2500);
  assert.equal(sensors.items[2499].sensor_id, 'S-2499');
  assert.equal((await provider.getAnomalies()).items.length, 1);
  assert.equal((await provider.getSensorReadings('VIB-001')).sensor_id, 'VIB-001');
  await provider.getSensorReadings('VIB-001');
  assert.equal(fetchImpl.calls.filter((url) => url.startsWith('./sensor-readings')).length, 1);
  assert.equal((await provider.getAssetHealth('BRG-001')).asset_id, 'BRG-001');
  await assert.rejects(() => provider.getAssetHealth('BLD-0014'), (error) => error.kind === 'not-found');
  assert.equal((await provider.getAnomalyDetail('ANM-0001')).cluster, null);
  assert.deepEqual(await provider.getManifest(), { generated_at: 'F', as_of: 'T', api_version: '1.0.0', files: [] });

  for (const url of fetchImpl.calls) {
    assert.ok(url.startsWith('./'), `${url} is relative to the page`);
    assert.ok(!url.split('?')[0].endsWith('/'), `${url} has no trailing slash`);
  }
});

test('StaticProvider: snapshot files, and an anomaly detail composed like GET /anomalies/{id}', async () => {
  const fetchImpl = mockFetch({
    './data/snapshot/meta.json': response({ version: '1.0.0' }),
    './data/snapshot/manifest.json': response({ generated_at: '2026-10-05T04:44:35Z', files: [] }),
    './data/snapshot/anomalies.json': response({
      total: 2,
      items: [
        { anomaly_id: 'ANM-0001', cluster_id: 7, nearby_assets: [{ asset_id: 'RD-0001', distance_m: 4.5 }] },
        { anomaly_id: 'ANM-0002', cluster_id: null },
      ],
    }),
    './data/snapshot/clusters.geojson': response({ features: [{ properties: { cluster_id: 7, n_anomalies: 3, anomaly_ids: ['ANM-0001'] } }] }),
    './data/snapshot/readings/VIB-001.json': response({ sensor_id: 'VIB-001' }),
    './data/snapshot/health/BRG-001.json': response({ asset_id: 'BRG-001' }),
  });
  const provider = new StaticProvider({}, { fetchImpl });
  assert.equal(provider.mode, 'static');
  assert.equal((await provider.getMeta()).version, '1.0.0');
  assert.equal((await provider.getManifest()).generated_at, '2026-10-05T04:44:35Z');
  assert.equal((await provider.getSensorReadings('VIB-001')).sensor_id, 'VIB-001');
  assert.equal((await provider.getAssetHealth('BRG-001')).asset_id, 'BRG-001');
  // An unmonitored asset has no exported health file: the same 'not-found' as the API.
  await assert.rejects(() => provider.getAssetHealth('BLD-0014'), (error) => error.kind === 'not-found' && error.status === 404);

  const first = await provider.getAnomalyDetail('ANM-0001');
  assert.deepEqual(first.nearby_assets, [{ asset_id: 'RD-0001', distance_m: 4.5 }]);
  assert.deepEqual(first.cluster, { cluster_id: 7, n_anomalies: 3, anomaly_ids: ['ANM-0001'] });
  const second = await provider.getAnomalyDetail('ANM-0002');
  assert.deepEqual(second.nearby_assets, []);
  assert.equal(second.cluster, null);
  await assert.rejects(() => provider.getAnomalyDetail('ANM-9999'), (error) => error.kind === 'not-found');
  assert.equal(fetchImpl.calls.filter((url) => url.endsWith('anomalies.json')).length, 1, 'the list is read once');
  for (const url of fetchImpl.calls) assert.ok(url.startsWith('./data/snapshot/'));
});
