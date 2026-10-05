/**
 * Right-panel tabs: Assets (default) · Anomalies · Sensors (build contract 12.2, 12.5, 12.8).
 *
 * This module owns the tab strip and the life cycle of the three panel modules. Each panel is an ES module
 * next to this file, imported on first use:
 *
 *     js/panels/assets.js      js/panels/anomalies.js      js/panels/sensors.js
 *
 * =====================================================================================================
 * PANEL MODULE INTERFACE
 * =====================================================================================================
 *
 *   export function mount(container, ctx)  ->  { update(changedKeys), tick?(t), destroy() }
 *
 *   container    the tab's <div role="tabpanel"> (scrolls vertically; flex column with a 12 px gap). The panel
 *                owns everything inside it. `mount` may be async (return a Promise of the object).
 *
 *   update(changedKeys)
 *                Re-render what depends on the changed state keys. `changedKeys` is a Set of state key names
 *                ('t', 'selection', 'filters', 'tab', 'playing', 'layers', ...). It is called
 *                  - once right after mount and every time the tab becomes the active one, with EVERY state
 *                    key in the set (render from scratch);
 *                  - at once for any change other than a playback tick;
 *                  - for playback ticks (only 't' changed while `state.playing`) at most every 500 ms, and
 *                    at once when playback pauses (contract 12.7: list panels).
 *                Read the state with `ctx.store.getState()`; do not subscribe to the store yourself.
 *
 *   tick(t)      Optional. Called on EVERY change of the clock, playing or not, before `update`. Keep it
 *                cheap: text nodes, a chart cursor - no list rebuilds (contract 12.7 step 7: health, latest
 *                readings and chart cursor of an open detail view).
 *
 *   destroy()    Remove listeners and timers, abort pending requests. The container is emptied by the caller.
 *
 *   Only the active tab's panel receives calls. When `mount`, `update` or `tick` throws (or the module
 *   cannot be imported) the tab shows an error state with Retry, which imports and mounts the module again.
 *
 * ctx
 *   store        js/state/store.js instance - getState(), set(patch), subscribe(keys, fn). State keys are
 *                documented at the top of that file. Prefer the actions below over `set`.
 *   data         index built by js/data/index.js `buildIndex` (full list in its JSDoc):
 *                  lookups        assetById(id) sensorById(id) anomalyById(id) clusterById(id)
 *                                 sensorsByAsset(assetId) anomaliesByAsset(assetId) anomaliesBySensor(sensorId)
 *                  lists          assets[] (GeoJSON features) monitoredAssets[] sensors[] anomalies[]
 *                                 (with start_idx, end_idx, peak_idx, start_day, cell_id) simulationEvents[]
 *                  time           timestamps[] times[] (epoch ms) lastIndex days[] indexOfTime(v) timeAt(t)
 *                  state at t     assetStatusAt(id, t) assetHealthAt(id, t) sensorStatusAt(id, t)
 *                                 sensorValueAt(id, t) statsAt(t) activeAnomaliesAt(t) startedAnomaliesAt(t)
 *                                 anomalyStatusAt(anomaly, t) activeAnomalyCountByAsset(t)
 *                                 needsAttention(t, limit) attentionCountAt(t)
 *                  meta, playback, manifest
 *   helpers      pure functions of js/data/index.js: filterAnomalies(anomalies, filters, t),
 *                sortAnomaliesForList(anomalies, t), filterSensors(sensors, {sensorType, assetId}),
 *                isActiveAt(anomaly, t), assetDisplayName(properties), assetTypeLabel(properties, labels),
 *                riskLevelOf(score, levels), indexOfTime(times, value)
 *   provider     ApiProvider | StaticProvider (identical methods, cached): getSensorReadings(sensorId)
 *                (columnar: value[], expected[], expected_low[], expected_high[], robust_z[], flagged[idx],
 *                thresholds{}), getAssetHealth(assetId) (columnar penalties; rejects with error.kind
 *                'not-found' for an unmonitored asset), getAnomalyDetail(anomalyId) (item + nearby_assets[] +
 *                cluster|null). `provider.mode` is 'api' or 'static'.
 *   format       time formatter bound to the study-area zone - dateTime(v) "Sep 1, 12:00 AM CDT",
 *                dateTimeLong(v), dateTimeShort(v), date(v), dateWithWeekday(v), dateLong(v), hour(v),
 *                zoneName(v), dayKey(v), hourOfDay(v) - plus the number helpers of js/ui/format.js:
 *                formatNumber, formatInteger, formatValue(value, unit), formatDelta, formatScore, formatZ,
 *                formatPercent, formatLatLon(lat, lon), formatDuration(hours), formatDistance(m), plural,
 *                humanize, valueDigits. Never format a time any other way.
 *   tokens       js/ui/tokens.js (colours, labels, orders, glyphs; `withAlpha`, `labelOf`, `colorOf`)
 *   dom          js/ui/dom.js: h, clear, replace, icon, pill(kind, value), tag, simulatedTag, sourceKindTag,
 *                sensorGlyph, emptyState, errorState, describeError, skeleton, setBusy, keyValue, block,
 *                rowButton, linkButton, select, chipGroup, segmented, panelHead. CSS: css/components.css
 *                (.block, .kv, .list, .row, .pill, .tag, .glyph, .chips, .seg, .filters, .data-table,
 *                .figure-caption, .empty, .error-state, .skeleton, .is-busy).
 *   actions      select(kind, id, {origin})   kind 'asset' | 'sensor' | 'anomaly'. Sets the selection and the
 *                                             matching tab; an anomaly outside its window moves the clock
 *                                             to its peak; origin 'list' (default) also flies the map to
 *                                             the feature, origin 'map' leaves the camera alone.
 *                clearSelection()             back to the list of the current tab
 *                flyTo({kind, id} | {lon, lat, zoom})
 *                setTime(t) stepTime(delta) play() pause() togglePlay()
 *                setTab(tab)
 *                setFilter(patch)             merge into state.filters ({status, assetId, dateFrom, dateTo,
 *                                             severity: Set, sensorType: Set})
 *                toggleFilterValue(key, value, on)   add/remove one value of the 'severity' / 'sensorType' Set
 *                clearFilters()
 *                setLayer(id, on) setHexMetric('risk' | 'count') openAbout()
 *
 * Wording the panels must keep (contract 12.1): the banned words of contract section 2 never appear in a UI
 * string; every anomaly card and detail carries `dom.simulatedTag()`; the Anomalies tab subtitle is
 * "Prototype Anomaly Detection"; every sensor chart caption is "Simulated Sensor Data".
 */

import { describeError, errorState, h, skeleton } from '../ui/dom.js';

export const TABS = Object.freeze([
  { id: 'assets', label: 'Assets', module: './assets.js' },
  { id: 'anomalies', label: 'Anomalies', module: './anomalies.js' },
  { id: 'sensors', label: 'Sensors', module: './sensors.js' },
]);
export const LIST_THROTTLE_MS = 500;
const TICK_ORDER = 40;

/**
 * @param {HTMLElement} container the <aside class="side-panel">
 * @param {object} ctx the context handed to every panel (see above)
 * @param {{importModule?: function(string): Promise<object>}} [options] replacement for `import()` (tests)
 */
export function mountTabs(container, ctx, { importModule } = {}) {
  const { store } = ctx;
  const load = importModule || ((specifier) => import(specifier));
  const entries = new Map();

  const tablist = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Assets, anomalies and sensors' });
  const panels = [];
  for (const tab of TABS) {
    const button = h('button', {
      class: 'tab',
      type: 'button',
      role: 'tab',
      id: `tab-${tab.id}`,
      'aria-controls': `tabpanel-${tab.id}`,
      text: tab.label,
      on: { click: () => ctx.actions.setTab(tab.id) },
    });
    const panel = h('div', {
      class: 'tabpanel',
      role: 'tabpanel',
      id: `tabpanel-${tab.id}`,
      'aria-labelledby': `tab-${tab.id}`,
      tabindex: '0',
      hidden: true,
    });
    tablist.append(button);
    panels.push(panel);
    entries.set(tab.id, { tab, button, panel, instance: null, state: 'idle', attempts: 0, pendingTick: false, timer: null, lastList: 0 });
  }
  container.replaceChildren(tablist, ...panels);
  container.removeAttribute('aria-busy');

  // Arrow keys move between tabs (tabs activate on focus); Home/End jump to the first/last tab.
  tablist.addEventListener('keydown', (event) => {
    const order = TABS.map((tab) => tab.id);
    const current = order.indexOf(store.getState().tab);
    let next = null;
    if (event.key === 'ArrowRight') next = (current + 1) % order.length;
    else if (event.key === 'ArrowLeft') next = (current - 1 + order.length) % order.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = order.length - 1;
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    ctx.actions.setTab(order[next]);
    entries.get(order[next]).button.focus();
  });

  const allKeys = () => new Set(Object.keys(store.getState()));

  function fail(entry, title, error) {
    console.error(`[tabs] ${entry.tab.label} panel: ${title}`, error);
    if (entry.timer !== null) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
    if (entry.instance && typeof entry.instance.destroy === 'function') {
      try {
        entry.instance.destroy();
      } catch (destroyError) {
        console.error(`[tabs] ${entry.tab.label} panel: destroy failed`, destroyError);
      }
    }
    entry.instance = null;
    entry.state = 'error';
    entry.panel.replaceChildren(
      errorState(title, {
        detail: describeError(error),
        onRetry: () => mountPanel(entry),
      }),
    );
  }

  /** Run a panel callback; a throw turns the tab into its error state. */
  function guarded(entry, what, fn) {
    try {
      fn();
      return true;
    } catch (error) {
      fail(entry, `The ${entry.tab.label} panel stopped while ${what}.`, error);
      return false;
    }
  }

  async function mountPanel(entry) {
    if (entry.state === 'loading') return;
    entry.state = 'loading';
    entry.attempts += 1;
    entry.panel.replaceChildren(skeleton({ rows: 5, label: `Loading the ${entry.tab.label} panel` }));
    // A module that failed to load stays failed in the module map; a retry needs a new specifier.
    const specifier = entry.attempts > 1 ? `${entry.tab.module}?retry=${entry.attempts}` : entry.tab.module;
    let module;
    try {
      module = await load(specifier);
    } catch (error) {
      fail(entry, `The ${entry.tab.label} panel could not be loaded.`, error);
      return;
    }
    if (!module || typeof module.mount !== 'function') {
      fail(entry, `The ${entry.tab.label} panel could not be loaded.`, new Error(`${entry.tab.module} does not export mount(container, ctx)`));
      return;
    }
    entry.panel.replaceChildren();
    try {
      const instance = await module.mount(entry.panel, ctx);
      if (!instance || typeof instance.update !== 'function') {
        throw new Error(`${entry.tab.module}: mount() must return {update(changedKeys), destroy()}`);
      }
      entry.instance = instance;
      entry.state = 'ready';
    } catch (error) {
      fail(entry, `The ${entry.tab.label} panel could not start.`, error);
      return;
    }
    if (store.getState().tab === entry.tab.id) {
      entry.lastList = performance.now();
      guarded(entry, 'rendering', () => entry.instance.update(allKeys()));
    }
  }

  function flushList(entry) {
    if (entry.timer !== null) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
    if (!entry.pendingTick || entry.state !== 'ready') return;
    entry.pendingTick = false;
    entry.lastList = performance.now();
    guarded(entry, 'following the clock', () => entry.instance.update(new Set(['t'])));
  }

  function showTab(tabId) {
    for (const entry of entries.values()) {
      const active = entry.tab.id === tabId;
      entry.button.setAttribute('aria-selected', active ? 'true' : 'false');
      entry.button.tabIndex = active ? 0 : -1;
      entry.panel.hidden = !active;
      if (!active && entry.timer !== null) {
        clearTimeout(entry.timer);
        entry.timer = null;
        entry.pendingTick = false;
      }
    }
    const entry = entries.get(tabId);
    if (!entry) return;
    if (entry.state === 'idle') {
      mountPanel(entry);
    } else if (entry.state === 'ready') {
      entry.pendingTick = false;
      entry.lastList = performance.now();
      guarded(entry, 'rendering', () => entry.instance.update(allKeys()));
    }
  }

  const unsubscribe = store.subscribe(
    '*',
    (state, changed) => {
      if (changed.has('tab')) {
        // The newly active panel renders from scratch, which covers every other key of this change.
        showTab(state.tab);
        return;
      }
      const entry = entries.get(state.tab);
      if (!entry || entry.state !== 'ready') return;

      // (7) open detail panel: every tick, cheap.
      if (changed.has('t') && typeof entry.instance.tick === 'function') {
        if (!guarded(entry, 'following the clock', () => entry.instance.tick(state.t))) return;
      }

      const onlyTick = changed.size === 1 && changed.has('t');
      if (onlyTick && state.playing) {
        // List panels re-render at most every 500 ms while playing.
        entry.pendingTick = true;
        const wait = LIST_THROTTLE_MS - (performance.now() - entry.lastList);
        if (wait <= 0) flushList(entry);
        else if (entry.timer === null) entry.timer = setTimeout(() => flushList(entry), wait);
        return;
      }

      // Anything else (and every tick while paused) renders at once; a pending tick is folded in.
      const keys = new Set(changed);
      keys.delete('tab');
      if (entry.pendingTick) keys.add('t');
      entry.pendingTick = false;
      if (entry.timer !== null) {
        clearTimeout(entry.timer);
        entry.timer = null;
      }
      if (keys.size === 0) return;
      entry.lastList = performance.now();
      guarded(entry, 'rendering', () => entry.instance.update(keys));
    },
    { order: TICK_ORDER },
  );

  showTab(store.getState().tab);

  return {
    /** 'idle' | 'loading' | 'ready' | 'error' for a tab id (diagnostics). */
    stateOf: (tabId) => (entries.get(tabId) ? entries.get(tabId).state : null),
    destroy() {
      unsubscribe();
      for (const entry of entries.values()) {
        if (entry.timer !== null) clearTimeout(entry.timer);
        if (entry.instance && typeof entry.instance.destroy === 'function') entry.instance.destroy();
      }
    },
  };
}
