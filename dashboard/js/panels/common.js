/**
 * Pieces shared by the three panel tabs (Assets, Anomalies, Sensors):
 *
 *   createListDetailPanel(container, ctx, spec)   the list <-> detail life cycle of a tab, with focus handling
 *                                                 (Back returns the focus to the originating row)
 *   lazyRegion(host, dom, options)                a lazily loaded resource: skeleton on first load only, inline
 *                                                 error with Retry on failure
 *   mountSensorChart(host, ctx, sensor, options)  sensor history chart (loads the readings lazily)
 *   localHoursOf(data)                            hour of the day of every grid index (cached per data set)
 *   stackedRow(dom, options)                      list row as a <button> with a headline and detail lines
 *   detectionMethodWords(method)                  "threshold+robust_zscore" -> words
 *   sensorTypeLabel(meta, type), placementLabel(placement), assetLink(ctx, assetId), optionsOfAssets(...)
 */

import { createChart, sensorChartConfig, windowAround } from '../ui/chart.js';

const hoursCache = new WeakMap();

/** Hour of the day (0-23, study-area zone) of every index of the time grid. Computed once per data set. */
export function localHoursOf(data) {
  let hours = hoursCache.get(data);
  if (!hours) {
    hours = new Int8Array(data.count);
    for (let i = 0; i < data.count; i += 1) hours[i] = data.format.hourOfDay(data.times[i]);
    hoursCache.set(data, hours);
  }
  return hours;
}

export function sensorTypeLabel(meta, sensorType) {
  const entry = meta.sensor_types ? meta.sensor_types[sensorType] : null;
  if (entry && entry.label) return entry.label;
  const words = String(sensorType || '').replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** "name (ID)" of an asset, or just the ID when it has no name. */
export function assetNameWithId(properties) {
  if (!properties) return '—';
  return properties.name ? `${properties.name} (${properties.asset_id})` : properties.asset_id;
}

/** Options of an asset <select>, ordered by display name: [{value, label}]. */
export function assetOptions(features, allLabel) {
  const options = features
    .map((feature) => ({ value: feature.properties.asset_id, label: assetNameWithId(feature.properties) }))
    .sort((a, b) => a.label.localeCompare(b.label, 'en', { numeric: true }));
  return [{ value: '', label: allLabel }, ...options];
}

const METHOD_WORDS = Object.freeze({
  threshold: 'critical-threshold breach',
  robust_zscore: 'robust z-score',
  rolling_median: 'rolling median of the z-score',
});
const CORROBORATING = 'isolation_forest';

/**
 * Detection method in words. The Isolation Forest never creates an event; it is named as corroborating
 * evidence (build contract 8.4).
 */
export function detectionMethodWords(method) {
  const parts = String(method || '')
    .split('+')
    .map((part) => part.trim())
    .filter(Boolean);
  const primary = parts.filter((part) => part !== CORROBORATING).map((part) => METHOD_WORDS[part] || part.replace(/_/g, ' '));
  const list =
    primary.length <= 1
      ? primary.join('')
      : `${primary.slice(0, -1).join(', ')} and ${primary[primary.length - 1]}`;
  const sentences = [];
  if (list) sentences.push(`Flagged by ${list}.`);
  if (parts.includes(CORROBORATING)) sentences.push('Corroborated by Isolation Forest (corroborating evidence only).');
  return sentences.join(' ') || '—';
}

/**
 * List row as a <button>: a headline (lead, title, trailing value) and detail lines underneath. Used where a
 * single line cannot hold a status pill, a long name and a value in a 300 px panel.
 *
 * @param {object} dom the DOM kit
 * @param {{id: string, lead?: Node, title: string, trail?: Node|string|Array, lines?: Array<Node|string|Array>,
 *          ariaLabel?: string, onClick: function}} options  each entry of `lines` is one line (text, node or
 *          an array placed left and right)
 */
export function stackedRow(dom, { id, lead, title, trail, lines = [], ariaLabel, onClick }) {
  const { h } = dom;
  return h(
    'button',
    { class: 'row row--stacked', type: 'button', dataset: { id }, 'aria-label': ariaLabel, on: { click: onClick } },
    h(
      'span',
      { class: 'row__top' },
      lead || null,
      h('span', { class: 'row__title', text: title, title }),
      trail ? h('span', { class: 'row__top-trail' }, trail) : null,
    ),
    lines.map((line) => h('span', { class: 'row__line' }, Array.isArray(line) ? line.map((part) => (typeof part === 'string' ? h('span', { text: part }) : part)) : line)),
  );
}

/**
 * Show a lazily loaded resource inside `host`.
 * First load: a skeleton. Failure: an inline error with Retry. The provider caches answers, so a resource that
 * was loaded before appears without a skeleton frame (the Promise is already settled before the next paint).
 *
 * @param {HTMLElement} host
 * @param {object} dom
 * @param {{load: function(): Promise, render: function(value, HTMLElement): (function|void), label: string,
 *          errorTitle: string, skeleton?: object}} options  `render` may return a clean-up function
 * @returns {{cancel: function}} cancel: ignore a pending answer and run the clean-up
 */
export function lazyRegion(host, dom, { load, render, label, errorTitle, skeleton = { rows: 2 } }) {
  let cancelled = false;
  let cleanup = null;

  function fail(error) {
    if (cancelled) return;
    host.replaceChildren(dom.errorState(errorTitle, { detail: dom.describeError(error), onRetry: start }));
  }

  function start() {
    host.replaceChildren(dom.skeleton({ ...skeleton, label }));
    Promise.resolve()
      .then(load)
      .then((value) => {
        if (cancelled) return;
        host.replaceChildren();
        try {
          cleanup = render(value, host) || null;
        } catch (error) {
          console.error(`[panel] ${label}: render failed`, error);
          fail(error);
        }
      }, fail);
  }

  start();
  return {
    cancel() {
      cancelled = true;
      if (typeof cleanup === 'function') cleanup();
      cleanup = null;
    },
  };
}

/**
 * Sensor history chart with lazily loaded readings.
 *
 * @param {HTMLElement} host
 * @param {object} ctx panel context
 * @param {object} sensor sensor item
 * @param {{variant?: 'full'|'compact', around?: object, legend?: boolean}} [options]
 *        `around`: an anomaly - the chart shows 48 h before its start to 48 h after its end and emphasises it
 * @returns {{setCursor: function(number), destroy: function}}
 */
export function mountSensorChart(host, ctx, sensor, { variant = 'full', around = null, legend = true } = {}) {
  const { data, dom, provider, store } = ctx;
  let chart = null;
  const typeLabel = sensorTypeLabel(data.meta, sensor.sensor_type);
  const region = lazyRegion(host, dom, {
    label: `Loading the readings of ${sensor.sensor_id}`,
    errorTitle: `The readings of ${sensor.sensor_id} could not be loaded.`,
    skeleton: { rows: 0, block: true },
    load: () => provider.getSensorReadings(sensor.sensor_id),
    render(readings, target) {
      const stepsPerHour = 60 / data.stepMinutes;
      const window = around
        ? windowAround(around.start_idx, around.end_idx, data.lastIndex, undefined, stepsPerHour)
        : { from: 0, to: data.lastIndex };
      chart = createChart(
        target,
        sensorChartConfig({
          readings,
          sensor,
          anomalies: data.anomaliesBySensor(sensor.sensor_id),
          events: data.simulationEvents,
          data,
          localHours: localHoursOf(data),
          from: window.from,
          to: window.to,
          cursor: store.getState().t,
          variant,
          highlightAnomalyId: around ? around.anomaly_id : null,
          typeLabel,
          legend,
          title: around
            ? `${sensor.sensor_id} · ${typeLabel} (${readings.unit || sensor.unit}) — 48 h before to 48 h after the event`
            : undefined,
        }),
      );
      return () => {
        if (chart) chart.destroy();
        chart = null;
      };
    },
  });
  return {
    setCursor(t) {
      if (chart) chart.setCursor(t);
    },
    destroy() {
      region.cancel();
    },
  };
}

/**
 * The list <-> detail life cycle shared by the three tabs.
 *
 * @param {HTMLElement} container the tab panel
 * @param {object} ctx panel context
 * @param {object} spec
 *   kind                               'asset' | 'sensor' | 'anomaly': the selection kind this tab shows in detail
 *   renderList(container, state)       -> {refresh(state, changedKeys), rowFor(id) -> HTMLElement|null, destroy?()}
 *   renderDetail(container, id, state) -> {refresh(state, changedKeys), tick?(t), focusTarget?: HTMLElement,
 *                                          destroy?()}
 * @returns {{update: function(Set<string>), tick: function(number), destroy: function}} the panel module object
 *
 * Focus (build contract 12.8): opening a detail from a row moves the focus to the detail's Back control;
 * leaving the detail (Back, Esc) returns it to the row the detail was opened from. The focus is only moved
 * when it was inside this panel (or lost), so a selection made on the map never steals it.
 */
export function createListDetailPanel(container, ctx, spec) {
  const { store } = ctx;
  let view = null;
  let listScrollTop = 0;

  const selectedId = (state) => (state.selection.kind === spec.kind ? state.selection.id : null);

  function teardown() {
    if (view && typeof view.api.destroy === 'function') view.api.destroy();
    view = null;
  }

  function update(changed) {
    const state = store.getState();
    const id = selectedId(state);
    const kind = id ? 'detail' : 'list';
    if (view && view.kind === kind && view.id === id) {
      view.api.refresh(state, changed);
      return;
    }
    const previous = view;
    const active = document.activeElement;
    const focusWasHere = previous !== null && (active === null || active === document.body || container.contains(active));
    if (previous && previous.kind === 'list') listScrollTop = container.scrollTop;
    teardown();
    container.replaceChildren();
    const api = id ? spec.renderDetail(container, id, state) : spec.renderList(container, state);
    view = { kind, id, api };
    if (kind === 'detail') {
      container.scrollTop = 0;
      if (focusWasHere && api.focusTarget) api.focusTarget.focus({ preventScroll: true });
      return;
    }
    const cameFromDetail = previous !== null && previous.kind === 'detail';
    container.scrollTop = cameFromDetail ? listScrollTop : 0;
    if (focusWasHere) {
      const row = cameFromDetail && typeof api.rowFor === 'function' ? api.rowFor(previous.id) : null;
      (row || container).focus({ preventScroll: !row });
      if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
    }
  }

  return {
    update,
    tick(t) {
      if (view && typeof view.api.tick === 'function') view.api.tick(t);
    },
    destroy: teardown,
  };
}

/** True when a refresh must re-read everything that depends on the clock (a tick, or the tab was re-opened). */
export function clockChanged(changed) {
  return changed.has('t') || changed.has('tab');
}
