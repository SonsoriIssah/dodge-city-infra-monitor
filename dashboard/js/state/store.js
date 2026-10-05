/**
 * Application state (build contract 12.4). Modules never call each other: they read the state, change it
 * with `set`, and react to changes with `subscribe`.
 *
 * State keys
 *   t            index into `playback.timestamps` (the simulated clock)
 *   playing      playback running
 *   speed        simulated hours per second (6 | 12 | 24)
 *   selection    {kind: 'asset' | 'sensor' | 'anomaly' | null, id: string | null}
 *   tab          'assets' | 'anomalies' | 'sensors'
 *   filters      {severity: Set, sensorType: Set, status: 'all' | 'active' | 'resolved', assetId: string | null,
 *                 dateFrom: 'YYYY-MM-DD' | null, dateTo: 'YYYY-MM-DD' | null}   (anomaly list; day keys are
 *                 calendar days in the study-area time zone, null = no bound)
 *   layers       {layerId: boolean} visibility of the map layer groups (ids in js/map/layers.js)
 *   pitched      true = 3D camera, false = 2D
 *   hexMetric    'risk' | 'count' - what the risk hexagons show
 * Keys added by the shell
 *   flyTo        {lon, lat, zoom, seq} camera request raised by list-initiated selections; the map consumes it
 *   layersOpen   "Layers & legend" card expanded
 *   introOpen    "How to read this dashboard" card shown
 *   basemap      'pending' | 'ok' | 'fallback' | 'degraded'
 *   imagery      'unknown' | 'ok' | 'failed' - availability of the orthoimagery service
 *
 * Values are replaced, never mutated: to change a filter build a new `filters` object (with new Sets), so a
 * subscriber can compare the previous and the next state by identity.
 *
 * Pure module: no DOM access, importable from Node tests.
 */

const DEFAULT_ORDER = 100;

/**
 * @param {object} initialState
 * @returns {{getState: function(): object, set: function(object): Set<string>,
 *            subscribe: function((string[]|string), function, {order?: number}=): function(): void}}
 */
export function createStore(initialState = {}) {
  let state = Object.freeze({ ...initialState });
  let sequence = 0;
  let subscribers = [];
  let notifying = false;
  const queue = [];

  function getState() {
    return state;
  }

  function flush() {
    notifying = true;
    try {
      while (queue.length > 0) {
        const { changed, previous } = queue.shift();
        // A snapshot of the list: a subscriber may subscribe or unsubscribe while it runs.
        for (const subscriber of subscribers.slice()) {
          if (!subscriber.active) continue;
          if (subscriber.keys !== null && !subscriber.keys.some((key) => changed.has(key))) continue;
          try {
            subscriber.fn(state, changed, previous);
          } catch (error) {
            // One failing module must not stop the others from following the clock.
            console.error('[store] a subscriber failed', error);
          }
        }
      }
    } finally {
      notifying = false;
    }
  }

  /**
   * Shallow-merge `patch` into the state and notify the subscribers of the keys whose value changed
   * (compared with Object.is). A `set` made from inside a subscriber is applied at once and its
   * notification runs after the current round.
   *
   * @returns {Set<string>} the keys that changed
   */
  function set(patch) {
    const changed = new Set();
    if (!patch || typeof patch !== 'object') return changed;
    for (const key of Object.keys(patch)) {
      if (!Object.is(state[key], patch[key])) changed.add(key);
    }
    if (changed.size === 0) return changed;
    const previous = state;
    state = Object.freeze({ ...state, ...patch });
    queue.push({ changed, previous });
    if (!notifying) flush();
    return changed;
  }

  /**
   * Call `fn(state, changedKeys, previousState)` after every `set` that changed one of `keys`.
   * `keys`: array of state keys, one key, or '*' for every change.
   * `options.order`: subscribers run in ascending order (default 100), then in subscription order. The shell
   * uses it for the per-tick sequence of contract 12.7: timeline 10, KPIs 20, map 30, panels 40.
   *
   * @returns {function(): void} unsubscribe
   */
  function subscribe(keys, fn, options = {}) {
    if (typeof fn !== 'function') throw new TypeError('subscribe needs a function');
    const list = keys === '*' || keys === undefined || keys === null ? null : [].concat(keys);
    const subscriber = {
      keys: list,
      fn,
      order: Number.isFinite(options.order) ? options.order : DEFAULT_ORDER,
      sequence: sequence++,
      active: true,
    };
    subscribers = subscribers
      .concat(subscriber)
      .sort((a, b) => a.order - b.order || a.sequence - b.sequence);
    return function unsubscribe() {
      subscriber.active = false;
      subscribers = subscribers.filter((entry) => entry !== subscriber);
    };
  }

  return { getState, set, subscribe };
}

/** Every anomaly filter at its default: all severities and sensor types, any status, any asset, whole window. */
export function defaultFilters(meta) {
  return {
    severity: new Set(meta.severity_levels),
    sensorType: new Set(Object.keys(meta.sensor_types)),
    status: 'all',
    assetId: null,
    dateFrom: null,
    dateTo: null,
  };
}

/** True when `filters` restricts nothing (used to enable "Clear filters"). */
export function filtersAreDefault(filters, meta) {
  const base = defaultFilters(meta);
  return (
    filters.status === base.status &&
    filters.assetId === null &&
    filters.dateFrom === null &&
    filters.dateTo === null &&
    filters.severity.size === base.severity.size &&
    filters.sensorType.size === base.sensorType.size
  );
}

/** Selection value for "nothing selected". */
export const NO_SELECTION = Object.freeze({ kind: null, id: null });
