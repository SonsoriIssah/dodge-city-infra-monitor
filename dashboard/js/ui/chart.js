/**
 * Dependency-free SVG time-series chart (build contract 12.5, 12.6).
 *
 * One chart = one measure on one y-axis. Several sensors are shown as small multiples (one chart each),
 * never as a second axis. The observed series is the only accent-coloured mark; the expected value and its
 * band stay in the muted colour; status colours are used for status only (threshold lines, anomaly windows)
 * and always come with a text label.
 *
 *   createChart(host, config)      the chart: legend, plot, hover crosshair + tooltip, keyboard reading,
 *                                  "View as table" twin, caption. Returns {setCursor, update, destroy}.
 *   sensorChartConfig(options)     config of a sensor history chart (observed, expected + band, thresholds,
 *                                  anomaly windows, benign simulated regional events)
 *   healthChartConfig(options)     config of the compact health-score sparkline
 *   legendRow(items)               a legend shared by a group of small multiples
 *
 * Pure helpers (exported, Node-tested; nothing below touches the DOM at import time):
 *   niceStep, stepDecimals, niceTicks, formatTick, linearScale, extentOf, valueDomain, runsOf, linePath,
 *   bandPath, timeTicks, windowAround
 *
 * The clock cursor: everything after hour t is drawn at 35 % opacity. Moving the cursor changes two clip
 * rectangles and one line - the chart is not rebuilt, so playback does not flicker.
 */

import { h } from './dom.js';
import { formatInteger, formatNumber, formatValue, formatZ } from './format.js';
import { SEVERITY_LABEL, labelOf } from './tokens.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
/** Opacity of the part of the series that lies after the clock (build contract 12.5). */
export const FUTURE_OPACITY = 0.35;
/** Half-width, in hours, of the window drawn around an anomaly. */
export const ANOMALY_CONTEXT_HOURS = 48;
/** Candidate distances between x-axis ticks, in hours. */
export const TICK_STEPS_HOURS = Object.freeze([6, 12, 24, 48, 72, 168, 336, 720]);
export const CHART_CAPTION = 'Simulated Sensor Data';
export const BENIGN_EVENT_LABEL = 'Simulated regional event (benign — not flagged)';

const TICK_FONT_PX = 12;
const TICK_CHAR_PX = 6.7;
const HEIGHTS = Object.freeze({ full: 190, compact: 112, spark: 78 });

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

// ---- pure helpers ----------------------------------------------------------------------------------------------

/** A "nice" distance between ticks (1, 2 or 5 times a power of ten) for about `targetCount` intervals. */
export function niceStep(span, targetCount = 4) {
  if (!(span > 0) || !Number.isFinite(span)) return 1;
  const raw = span / Math.max(1, targetCount);
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

/** Decimals needed to print multiples of `step` exactly: 5 -> 0, 0.5 -> 1, 0.05 -> 2, 0.002 -> 3. */
export function stepDecimals(step) {
  if (!(step > 0) || !Number.isFinite(step)) return 0;
  return Math.max(0, Math.min(6, -Math.floor(Math.log10(step) + 1e-9)));
}

/**
 * Tick values covering [min, max] on nice numbers.
 * @returns {{min: number, max: number, step: number, decimals: number, ticks: number[]}} `min`/`max` are the
 *          first and last tick (the axis domain)
 */
export function niceTicks(min, max, targetCount = 4) {
  let low = Number(min);
  let high = Number(max);
  if (!Number.isFinite(low) || !Number.isFinite(high)) return { min: 0, max: 1, step: 1, decimals: 0, ticks: [0, 1] };
  if (low > high) [low, high] = [high, low];
  if (low === high) {
    const pad = Math.abs(low) > 0 ? Math.abs(low) * 0.1 : 1;
    low -= pad;
    high += pad;
  }
  const step = niceStep(high - low, targetCount);
  const decimals = stepDecimals(step);
  const first = Math.floor(low / step + 1e-9);
  const last = Math.ceil(high / step - 1e-9);
  const ticks = [];
  for (let i = first; i <= last; i += 1) ticks.push(Number((i * step).toFixed(decimals + 2)));
  return { min: ticks[0], max: ticks[ticks.length - 1], step, decimals, ticks };
}

/** Tick label with a fixed number of decimals and thousands separators: (1.5, 2) -> "1.50". */
export function formatTick(value, decimals = 0) {
  if (!isNumber(value)) return '';
  const rounded = Math.abs(value) < 10 ** -(decimals + 1) ? 0 : value;
  return rounded.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** Linear map of `domain` onto `range`; `.invert(pixel)` goes back. A zero-width domain maps to the range middle. */
export function linearScale([d0, d1], [r0, r1]) {
  const span = d1 - d0;
  const scale = (value) => (span === 0 ? (r0 + r1) / 2 : r0 + ((value - d0) / span) * (r1 - r0));
  scale.invert = (pixel) => (r1 === r0 ? d0 : d0 + ((pixel - r0) / (r1 - r0)) * span);
  return scale;
}

/** [min, max] of the finite numbers of several arrays between two indexes (inclusive); null when there is none. */
export function extentOf(arrays, from, to) {
  let min = Infinity;
  let max = -Infinity;
  for (const values of arrays) {
    if (!values) continue;
    const end = Math.min(to, values.length - 1);
    for (let i = Math.max(0, from); i <= end; i += 1) {
      const value = values[i];
      if (!isNumber(value)) continue;
      if (value < min) min = value;
      if (value > max) max = value;
    }
  }
  return min <= max ? [min, max] : null;
}

/**
 * Padded value domain of the given series for a window; null when the window holds no number.
 * A series that is never negative is not padded below zero.
 */
export function valueDomain(arrays, from, to, { padding = 0.06 } = {}) {
  const extent = extentOf(arrays, from, to);
  if (!extent) return null;
  const [min, max] = extent;
  const span = max - min;
  const pad = span > 0 ? span * padding : Math.abs(max) > 0 ? Math.abs(max) * 0.1 : 1;
  const low = min >= 0 ? Math.max(0, min - pad) : min - pad;
  return [low, max + pad];
}

/** Runs of consecutive readings: [[start, end], ...] (inclusive indexes) - the gaps are missing readings. */
export function runsOf(values, from, to) {
  const runs = [];
  let start = -1;
  const end = Math.min(to, values.length - 1);
  for (let i = Math.max(0, from); i <= end; i += 1) {
    if (isNumber(values[i])) {
      if (start < 0) start = i;
    } else if (start >= 0) {
      runs.push([start, i - 1]);
      start = -1;
    }
  }
  if (start >= 0) runs.push([start, end]);
  return runs;
}

const point = (xValue, yValue) => `${xValue.toFixed(1)} ${yValue.toFixed(1)}`;

/** SVG path of a series: one sub-path per run, so missing readings stay visible as gaps. A lone reading is a dot. */
export function linePath(values, from, to, x, y) {
  let d = '';
  for (const [start, end] of runsOf(values, from, to)) {
    d += `M${point(x(start), y(values[start]))}`;
    if (start === end) d += 'h0.01';
    for (let i = start + 1; i <= end; i += 1) d += `L${point(x(i), y(values[i]))}`;
  }
  return d;
}

/** SVG path of the area between two series (the expected band), split where either bound is missing. */
export function bandPath(low, high, from, to, x, y) {
  if (!low || !high) return '';
  const both = [];
  const end = Math.min(to, low.length - 1, high.length - 1);
  for (let i = 0; i <= end; i += 1) both.push(isNumber(low[i]) && isNumber(high[i]) ? 1 : null);
  let d = '';
  for (const [start, stop] of runsOf(both, from, to)) {
    if (start === stop) continue;
    d += `M${point(x(start), y(high[start]))}`;
    for (let i = start + 1; i <= stop; i += 1) d += `L${point(x(i), y(high[i]))}`;
    for (let i = stop; i >= start; i -= 1) d += `L${point(x(i), y(low[i]))}`;
    d += 'Z';
  }
  return d;
}

/**
 * Indexes of the x-axis ticks of a window.
 * @param {number} from first index, @param {number} to last index (inclusive)
 * @param {number} maxTicks how many labels fit
 * @param {ArrayLike<number>} localHours hour of the day (0-23, study-area zone) of every index
 * @param {number} [stepsPerHour] grid steps per hour
 * @returns {Array<{index: number, midnight: boolean}>} ticks on local midnights (labelled with the date) and,
 *          for short windows, on local hours that are a multiple of 6 or 12
 */
export function timeTicks(from, to, maxTicks, localHours, stepsPerHour = 1) {
  if (!(to > from)) return [{ index: from, midnight: localHours[from] === 0 }];
  const hours = (to - from) / stepsPerHour;
  const limit = Math.max(2, maxTicks);
  const stepHours =
    TICK_STEPS_HOURS.find((candidate) => Math.floor(hours / candidate) + 1 <= limit) ||
    TICK_STEPS_HOURS[TICK_STEPS_HOURS.length - 1];
  const onTheHour = (index) => index % stepsPerHour === 0;
  const ticks = [];
  if (stepHours < 24) {
    for (let i = from; i <= to; i += 1) {
      if (onTheHour(i) && localHours[i] % stepHours === 0) ticks.push({ index: i, midnight: localHours[i] === 0 });
    }
    return ticks;
  }
  const everyDays = stepHours / 24;
  let seen = 0;
  for (let i = from; i <= to; i += 1) {
    if (!onTheHour(i) || localHours[i] !== 0) continue;
    if (seen % everyDays === 0) ticks.push({ index: i, midnight: true });
    seen += 1;
  }
  return ticks;
}

/** Window of `hours` before the start and after the end of an interval, clamped to the grid. */
export function windowAround(startIndex, endIndex, lastIndex, hours = ANOMALY_CONTEXT_HOURS, stepsPerHour = 1) {
  const margin = Math.round(hours * stepsPerHour);
  return {
    from: Math.max(0, Math.min(startIndex, endIndex) - margin),
    to: Math.min(lastIndex, Math.max(startIndex, endIndex) + margin),
  };
}

// ---- chart configurations --------------------------------------------------------------------------------------

const THRESHOLD_LINES = Object.freeze([
  ['crit_high', 'Critical high', 'crit'],
  ['warn_high', 'Warning high', 'warn'],
  ['warn_low', 'Warning low', 'warn'],
  ['crit_low', 'Critical low', 'crit'],
]);

/** Threshold lines of a placement as `[{value, label, tone}]` (only the limits that exist). */
export function thresholdLines(thresholds, unit) {
  if (!thresholds) return [];
  return THRESHOLD_LINES.filter(([key]) => isNumber(thresholds[key])).map(([key, label, tone]) => ({
    key,
    value: thresholds[key],
    tone,
    label: `${label} ${formatValue(thresholds[key], unit)}`,
  }));
}

/**
 * Config of a sensor history chart.
 *
 * @param {object} options
 * @param {object} options.readings     columnar readings (provider.getSensorReadings)
 * @param {object} options.sensor       sensor item
 * @param {Array}  options.anomalies    anomalies of this sensor (with start_idx / end_idx)
 * @param {Array}  options.events       simulation events; the benign ones of the sensor's type are shaded grey
 * @param {object} options.data         the data index (times, format, lastIndex, indexOfTime)
 * @param {ArrayLike<number>} options.localHours
 * @param {number} [options.from] / [options.to]   window (defaults to the whole series)
 * @param {number|null} [options.cursor]
 * @param {'full'|'compact'} [options.variant]
 * @param {string} [options.title]
 * @param {string|null} [options.highlightAnomalyId]  the anomaly the window was opened for
 * @param {string} [options.typeLabel]  "Vibration"
 */
export function sensorChartConfig({
  readings,
  sensor,
  anomalies = [],
  events = [],
  data,
  localHours,
  from = 0,
  to = data.lastIndex,
  cursor = null,
  variant = 'full',
  title,
  highlightAnomalyId = null,
  typeLabel,
  legend = true,
}) {
  const { times, format } = data;
  const unit = readings.unit || sensor.unit;
  const flagged = new Set(readings.flagged || []);
  const name = typeLabel || labelOf({}, sensor.sensor_type);
  const windows = [];
  for (const event of events) {
    if (event.is_anomaly || event.sensor_type !== sensor.sensor_type) continue;
    windows.push({
      tone: 'event',
      from: data.indexOfTime(event.started_at),
      to: data.indexOfTime(event.ended_at),
      label: BENIGN_EVENT_LABEL,
      detail: event.description || null,
    });
  }
  for (const anomaly of anomalies) {
    windows.push({
      tone: 'anomaly',
      from: anomaly.start_idx,
      to: anomaly.end_idx,
      severity: anomaly.severity,
      emphasised: anomaly.anomaly_id === highlightAnomalyId,
      label: `${anomaly.anomaly_label} (${anomaly.anomaly_id}, ${labelOf(SEVERITY_LABEL, anomaly.severity)})`,
    });
  }
  const inWindow = (entry) => entry.to >= from && entry.from <= to;
  const visibleAnomalies = windows.filter((entry) => entry.tone === 'anomaly' && inWindow(entry)).length;
  const visibleEvents = windows.filter((entry) => entry.tone === 'event' && inWindow(entry)).length;
  const notesAt = (index) =>
    windows.filter((entry) => entry.from <= index && index <= entry.to).map((entry) => entry.label);

  const legendItems = [
    { swatch: 'observed', label: 'Observed' },
    { swatch: 'expected', label: 'Expected' },
    { swatch: 'band', label: 'Expected range' },
  ];
  if (visibleAnomalies > 0) legendItems.push({ swatch: 'anomaly', label: 'Anomaly window' });
  if (visibleEvents > 0) legendItems.push({ swatch: 'event', label: BENIGN_EVENT_LABEL });

  const range = (index) =>
    isNumber(readings.expected_low[index]) && isNumber(readings.expected_high[index])
      ? `${formatValue(readings.expected_low[index], null)} – ${formatValue(readings.expected_high[index], unit)}`
      : '—';

  return {
    variant,
    title: title || `${sensor.sensor_id} · ${name} (${unit})`,
    caption: CHART_CAPTION,
    legend: legend ? legendItems : null,
    times,
    localHours,
    format,
    from,
    to,
    cursor,
    unit,
    series: {
      values: readings.value,
      expected: readings.expected,
      low: readings.expected_low,
      high: readings.expected_high,
    },
    refLines: thresholdLines(readings.thresholds, unit),
    windows,
    emptyText: 'No readings in this period',
    ariaLabel:
      `${name} readings of simulated sensor ${sensor.sensor_id} in ${unit}, ` +
      `${format.dateTime(times[from])} to ${format.dateTime(times[to])}: observed and expected values` +
      `${visibleAnomalies ? `, ${visibleAnomalies} shaded anomaly ${visibleAnomalies === 1 ? 'window' : 'windows'}` : ''}` +
      `${visibleEvents ? `, ${visibleEvents} benign simulated regional ${visibleEvents === 1 ? 'event' : 'events'}` : ''}.`,
    describe(index) {
      return {
        title: format.dateTime(times[index]),
        rows: [
          ['Observed', formatValue(readings.value[index], unit), true],
          ['Expected', formatValue(readings.expected[index], unit, { missing: '—' })],
          ['Expected range', range(index)],
          ['Robust z', formatZ(readings.robust_z[index])],
          ['Flagged', flagged.has(index) ? 'Yes' : 'No'],
        ],
        notes: notesAt(index),
      };
    },
    table: {
      caption: `${sensor.sensor_id} readings (${unit}), times in ${format.zoneName(times[from])}`,
      columns: [
        { label: `Time (${format.zoneName(times[from])})` },
        { label: `Observed (${unit})`, num: true },
        { label: 'Expected', num: true },
        { label: 'Expected range', num: true },
        { label: 'Robust z', num: true },
        { label: 'Flagged' },
        { label: 'Window' },
      ],
      rowAt(index) {
        return [
          format.dateTimeShort(times[index]),
          formatValue(readings.value[index], null),
          formatValue(readings.expected[index], null, { missing: '—' }),
          isNumber(readings.expected_low[index]) && isNumber(readings.expected_high[index])
            ? `${formatValue(readings.expected_low[index], null)} – ${formatValue(readings.expected_high[index], null)}`
            : '—',
          formatZ(readings.robust_z[index]),
          flagged.has(index) ? 'Yes' : 'No',
          notesAt(index).join('; '),
        ];
      },
    },
  };
}

/**
 * Config of the health-score sparkline of a monitored asset.
 * @param {object} options {health: number[], penalties?: columnar /assets/{id}/health, data, localHours, cursor,
 *                          atRiskBelow, assetName}
 */
export function healthChartConfig({ health, history = null, data, localHours, cursor = null, atRiskBelow, assetName }) {
  const { times, format } = data;
  const penalty = (key, index) => (history && history[key] ? formatNumber(history[key][index], 1) : '—');
  return {
    variant: 'spark',
    title: 'Health score history',
    caption: 'Derived Asset Health Score (from simulated sensors)',
    legend: null,
    times,
    localHours,
    format,
    from: 0,
    to: data.lastIndex,
    cursor,
    unit: '',
    series: { values: health },
    yDomain: [0, 100],
    yTicks: [0, 50, 100],
    refLines: isNumber(atRiskBelow) ? [{ value: atRiskBelow, tone: 'neutral', label: `At risk below ${atRiskBelow}` }] : [],
    windows: [],
    emptyText: 'No health score in this period',
    ariaLabel:
      `Derived Asset Health Score of ${assetName}, 0 to 100, ` +
      `${format.dateTime(times[0])} to ${format.dateTime(times[data.lastIndex])}.`,
    describe(index) {
      const rows = [['Health score', isNumber(health[index]) ? `${formatInteger(health[index])} of 100` : '—', true]];
      if (history) {
        rows.push(
          ['Frequency penalty', penalty('frequency_penalty', index)],
          ['Severity penalty', penalty('severity_penalty', index)],
          ['Reading penalty', penalty('reading_penalty', index)],
          ['Sensor penalty', penalty('sensor_penalty', index)],
        );
      }
      return { title: format.dateTime(times[index]), rows, notes: [] };
    },
    table: {
      caption: `Derived Asset Health Score of ${assetName}, times in ${format.zoneName(times[0])}`,
      columns: [
        { label: `Time (${format.zoneName(times[0])})` },
        { label: 'Health', num: true },
        { label: 'Frequency', num: true },
        { label: 'Severity', num: true },
        { label: 'Reading', num: true },
        { label: 'Sensor', num: true },
      ],
      rowAt(index) {
        return [
          format.dateTimeShort(times[index]),
          isNumber(health[index]) ? formatInteger(health[index]) : '—',
          penalty('frequency_penalty', index),
          penalty('severity_penalty', index),
          penalty('reading_penalty', index),
          penalty('sensor_penalty', index),
        ];
      },
    },
  };
}

// ---- DOM -------------------------------------------------------------------------------------------------------

function svgNode(tag, attributes = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child) node.append(child);
  }
  return node;
}

/** Legend of line and area keys (never filled boxes for lines). Shared by a group of small multiples. */
export function legendRow(items) {
  return h(
    'ul',
    { class: 'chart-legend', 'aria-label': 'Chart legend' },
    items.map((item) =>
      h(
        'li',
        { class: 'chart-legend__item' },
        h('span', { class: 'chart-legend__key', dataset: { swatch: item.swatch }, 'aria-hidden': 'true' }),
        h('span', { text: item.label }),
      ),
    ),
  );
}

let chartSequence = 0;

/**
 * Build a chart inside `host`.
 *
 * @param {HTMLElement} host
 * @param {object} config
 *   variant        'full' | 'compact' | 'spark'
 *   title, caption, ariaLabel, emptyText
 *   legend         [{swatch, label}] or null
 *   times[]        epoch ms of every grid index; localHours[] hour of the day per index; format (time formatter)
 *   from, to       window (inclusive indexes); cursor (index of the clock, or null)
 *   series         {values[], expected?[], low?[], high?[]}
 *   yDomain        [min, max] to fix the axis (default: from the data of the window); yTicks optional tick values
 *   refLines       [{value, label, tone: 'warn' | 'crit' | 'neutral'}] - drawn only inside the y-range
 *   windows        [{from, to, tone: 'anomaly' | 'event', severity?, emphasised?, label}]
 *   describe(i)    {title, rows: [[label, value, strong?]], notes: [text]} for the tooltip
 *   table          {caption, columns: [{label, num?}], rowAt(i) -> [text]}
 * @returns {{element: HTMLElement, setCursor: function(number|null), update: function(object), destroy: function}}
 */
export function createChart(host, config) {
  let cfg = { variant: 'full', legend: null, refLines: [], windows: [], cursor: null, ...config };
  chartSequence += 1;
  const uid = `chart-${chartSequence}`;
  let geometry = null;
  let hoverIndex = null;
  let tableOpen = false;
  let tableBuiltFor = null;
  let destroyed = false;

  const titleNode = h('h4', { class: 'chart__title', id: `${uid}-title` });
  const tableButton = h('button', {
    class: 'link-btn chart__toggle',
    type: 'button',
    'aria-expanded': 'false',
    'aria-controls': `${uid}-table`,
    text: 'View as table',
  });
  const legendHost = h('div', { class: 'chart__legend' });
  const plot = h('div', {
    class: 'chart__plot',
    tabindex: '0',
    role: 'group',
    'aria-describedby': `${uid}-help`,
    dataset: { ownKeys: 'true' },
  });
  const help = h('span', {
    class: 'visually-hidden',
    id: `${uid}-help`,
    text: 'Use the left and right arrow keys to read the values hour by hour; Home and End jump to the ends of the chart.',
  });
  const tooltip = h('div', { class: 'chart__tooltip', role: 'presentation', hidden: true });
  const announcer = h('div', { class: 'visually-hidden', 'aria-live': 'polite' });
  const tableHost = h('div', { class: 'table-scroll chart__table', id: `${uid}-table`, hidden: true, tabindex: '0' });
  const captionNode = h('figcaption', { class: 'figure-caption' });
  const element = h(
    'figure',
    { class: 'chart', 'aria-labelledby': `${uid}-title` },
    h('div', { class: 'chart__head' }, titleNode, tableButton),
    legendHost,
    plot,
    tableHost,
    captionNode,
    help,
    announcer,
  );
  host.append(element);

  const refs = { pastClip: null, futureClip: null, cursorLine: null, hoverLine: null, hoverDot: null };

  function renderFrame() {
    element.className = `chart chart--${cfg.variant}`;
    titleNode.textContent = cfg.title || '';
    captionNode.textContent = cfg.caption || '';
    captionNode.hidden = !cfg.caption;
    legendHost.replaceChildren(cfg.legend && cfg.legend.length ? legendRow(cfg.legend) : '');
    legendHost.hidden = !(cfg.legend && cfg.legend.length);
    plot.setAttribute('aria-label', cfg.ariaLabel || cfg.title || 'Chart');
    tableButton.hidden = !cfg.table;
  }

  function yAxis(height) {
    if (cfg.yDomain) {
      const ticks = cfg.yTicks || niceTicks(cfg.yDomain[0], cfg.yDomain[1], 2).ticks;
      const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
      return { min: cfg.yDomain[0], max: cfg.yDomain[1], ticks, decimals: stepDecimals(step) };
    }
    const { values, expected, low, high } = cfg.series;
    const domain = valueDomain([values, expected, low, high], cfg.from, cfg.to);
    if (!domain) return null;
    return niceTicks(domain[0], domain[1], height < 130 ? 2 : 4);
  }

  function render() {
    if (destroyed) return;
    const width = Math.floor(plot.clientWidth);
    if (width < 60) return;
    const height = cfg.height || HEIGHTS[cfg.variant] || HEIGHTS.full;
    const axis = yAxis(height);
    hideHover();
    if (!axis) {
      geometry = null;
      plot.replaceChildren(h('div', { class: 'chart__empty', role: 'status', text: cfg.emptyText || 'No data in this period' }), tooltip);
      return;
    }
    const labels = axis.ticks.map((value) => formatTick(value, axis.decimals));
    const left = Math.ceil(Math.max(...labels.map((label) => label.length)) * TICK_CHAR_PX) + 10;
    const right = 10;
    const top = 8;
    const bottom = 22;
    const plotWidth = Math.max(10, width - left - right);
    const plotHeight = Math.max(10, height - top - bottom);
    const x = linearScale([cfg.from, cfg.to], [left, left + plotWidth]);
    const y = linearScale([axis.min, axis.max], [top + plotHeight, top]);
    const hourWidth = cfg.to > cfg.from ? plotWidth / (cfg.to - cfg.from) : plotWidth;
    geometry = { width, height, left, right, top, plotWidth, plotHeight, x, y, axis };

    const root = svgNode('svg', {
      class: 'chart__svg',
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      role: 'img',
      'aria-label': cfg.ariaLabel || cfg.title || 'Chart',
      focusable: 'false',
    });

    // Clip rectangles: what lies before the clock, and what lies after it.
    refs.pastClip = svgNode('rect', { x: left - 2, y: 0, width: plotWidth + 4, height });
    refs.futureClip = svgNode('rect', { x: left + plotWidth + 2, y: 0, width: 0, height });
    root.append(
      svgNode(
        'defs',
        {},
        svgNode('clipPath', { id: `${uid}-past` }, refs.pastClip),
        svgNode('clipPath', { id: `${uid}-future` }, refs.futureClip),
        svgNode('clipPath', { id: `${uid}-plot` }, svgNode('rect', { x: left, y: top, width: plotWidth, height: plotHeight })),
      ),
    );

    // Recessive solid hairlines and the value axis labels.
    const grid = svgNode('g', { class: 'chart__grid' });
    const yLabels = svgNode('g', { class: 'chart__ticks' });
    axis.ticks.forEach((value, index) => {
      const py = Math.round(y(value)) + 0.5;
      grid.append(svgNode('line', { x1: left, x2: left + plotWidth, y1: py, y2: py }));
      const label = svgNode('text', { x: left - 6, y: py, 'text-anchor': 'end', 'dominant-baseline': 'middle' });
      label.textContent = labels[index];
      yLabels.append(label);
    });
    root.append(grid);

    // Shaded windows: benign simulated regional events (grey) under anomaly windows (severity colour + top rule).
    const shading = svgNode('g', { class: 'chart__windows', 'clip-path': `url(#${uid}-plot)` });
    const ordered = cfg.windows.slice().sort((a, b) => (a.tone === 'event' ? 0 : 1) - (b.tone === 'event' ? 0 : 1));
    for (const entry of ordered) {
      if (entry.to < cfg.from || entry.from > cfg.to) continue;
      const x0 = x(Math.max(cfg.from, entry.from)) - hourWidth / 2;
      const x1 = x(Math.min(cfg.to, entry.to)) + hourWidth / 2;
      const rectX = Math.max(left, x0);
      const rectWidth = Math.max(2, Math.min(left + plotWidth, x1) - rectX);
      const rect = svgNode('rect', {
        class: 'chart__window',
        'data-tone': entry.tone,
        'data-severity': entry.severity,
        'data-emphasised': entry.emphasised ? 'true' : null,
        x: rectX.toFixed(1),
        y: top,
        width: rectWidth.toFixed(1),
        height: plotHeight,
      });
      const tip = svgNode('title');
      tip.textContent = entry.label;
      rect.append(tip);
      shading.append(rect);
      if (entry.tone === 'anomaly') {
        shading.append(
          svgNode('rect', {
            class: 'chart__window-rule',
            'data-severity': entry.severity,
            x: rectX.toFixed(1),
            y: top,
            width: rectWidth.toFixed(1),
            height: entry.emphasised ? 3 : 2,
          }),
        );
      }
    }
    root.append(shading);

    // Data marks, drawn twice: before the clock at full strength, after it at 35 %.
    const { values, expected, low, high } = cfg.series;
    const band = bandPath(low, high, cfg.from, cfg.to, x, y);
    const expectedLine = expected ? linePath(expected, cfg.from, cfg.to, x, y) : '';
    const observedLine = linePath(values, cfg.from, cfg.to, x, y);
    const marks = (clipId, opacity) => {
      const group = svgNode('g', { class: 'chart__marks', 'clip-path': `url(#${clipId})`, opacity });
      const inner = svgNode('g', { 'clip-path': `url(#${uid}-plot)` });
      if (band) inner.append(svgNode('path', { class: 'chart__band', d: band }));
      if (expectedLine) inner.append(svgNode('path', { class: 'chart__expected', d: expectedLine }));
      if (observedLine) inner.append(svgNode('path', { class: 'chart__observed', d: observedLine }));
      group.append(inner);
      return group;
    };
    root.append(marks(`${uid}-past`, null), marks(`${uid}-future`, FUTURE_OPACITY));

    // Threshold and reference lines, only when they fall inside the value range. Each carries its text label.
    const references = svgNode('g', { class: 'chart__refs' });
    let lastLabelY = -Infinity;
    cfg.refLines
      .filter((line) => isNumber(line.value) && line.value >= axis.min && line.value <= axis.max)
      .sort((a, b) => b.value - a.value)
      .forEach((line) => {
        const py = Math.round(y(line.value)) + 0.5;
        references.append(svgNode('line', { class: 'chart__ref', 'data-tone': line.tone, x1: left, x2: left + plotWidth, y1: py, y2: py }));
        let labelY = py - 4 < top + TICK_FONT_PX ? py + TICK_FONT_PX + 1 : py - 4;
        if (labelY - lastLabelY < TICK_FONT_PX + 1) labelY = lastLabelY + TICK_FONT_PX + 1;
        if (labelY > top + plotHeight - 2) return;
        lastLabelY = labelY;
        const label = svgNode('text', { class: 'chart__ref-label', 'data-tone': line.tone, x: left + plotWidth - 4, y: labelY, 'text-anchor': 'end' });
        label.textContent = line.label;
        references.append(label);
      });
    root.append(references, yLabels);

    // Time axis: dates on local midnights; hours for short windows.
    const maxTicks = Math.max(2, Math.floor(plotWidth / 62));
    const xLabels = svgNode('g', { class: 'chart__ticks' });
    let lastRight = -Infinity;
    for (const tick of timeTicks(cfg.from, cfg.to, maxTicks, cfg.localHours)) {
      const px = x(tick.index);
      const text = tick.midnight ? cfg.format.date(cfg.times[tick.index]) : cfg.format.hour(cfg.times[tick.index]).replace(':00', '');
      const textWidth = text.length * TICK_CHAR_PX;
      let anchor = 'middle';
      let start = px - textWidth / 2;
      if (start < left - 4) {
        anchor = 'start';
        start = px;
      } else if (px + textWidth / 2 > width - 2) {
        anchor = 'end';
        start = px - textWidth;
      }
      if (start < lastRight + 8) continue;
      lastRight = start + textWidth;
      xLabels.append(svgNode('line', { class: 'chart__tickmark', x1: px, x2: px, y1: top + plotHeight, y2: top + plotHeight + 4 }));
      const label = svgNode('text', { x: px, y: top + plotHeight + 16, 'text-anchor': anchor });
      label.textContent = text;
      xLabels.append(label);
    }
    root.append(svgNode('line', { class: 'chart__baseline', x1: left, x2: left + plotWidth, y1: top + plotHeight + 0.5, y2: top + plotHeight + 0.5 }), xLabels);

    refs.cursorLine = svgNode('line', { class: 'chart__cursor', y1: top - 2, y2: top + plotHeight });
    refs.hoverLine = svgNode('line', { class: 'chart__crosshair', y1: top, y2: top + plotHeight, visibility: 'hidden' });
    refs.hoverDot = svgNode('circle', { class: 'chart__dot', r: 4, visibility: 'hidden' });
    root.append(refs.cursorLine, refs.hoverLine, refs.hoverDot);

    plot.replaceChildren(root, tooltip);
    applyCursor();
  }

  function applyCursor() {
    if (!geometry) return;
    const { left, plotWidth, x } = geometry;
    const cursor = cfg.cursor;
    if (cursor === null || cursor === undefined) {
      refs.pastClip.setAttribute('width', String(plotWidth + 4));
      refs.futureClip.setAttribute('width', '0');
      refs.cursorLine.setAttribute('visibility', 'hidden');
      return;
    }
    const clamped = Math.min(cfg.to, Math.max(cfg.from, cursor));
    const px = cursor < cfg.from ? left - 2 : cursor > cfg.to ? left + plotWidth + 2 : x(clamped);
    refs.pastClip.setAttribute('width', String(Math.max(0, px - (left - 2)).toFixed(1)));
    refs.futureClip.setAttribute('x', px.toFixed(1));
    refs.futureClip.setAttribute('width', String(Math.max(0, left + plotWidth + 2 - px).toFixed(1)));
    const inside = cursor >= cfg.from && cursor <= cfg.to;
    refs.cursorLine.setAttribute('visibility', inside ? 'visible' : 'hidden');
    if (inside) {
      const lineX = (Math.round(px) + 0.5).toFixed(1);
      refs.cursorLine.setAttribute('x1', lineX);
      refs.cursorLine.setAttribute('x2', lineX);
    }
  }

  // ---- hover crosshair, tooltip, keyboard reading ------------------------------------------------------------
  function hideHover() {
    hoverIndex = null;
    tooltip.hidden = true;
    if (refs.hoverLine) refs.hoverLine.setAttribute('visibility', 'hidden');
    if (refs.hoverDot) refs.hoverDot.setAttribute('visibility', 'hidden');
  }

  function showHover(index, { announce = false } = {}) {
    if (!geometry || !cfg.describe) return;
    const clamped = Math.min(cfg.to, Math.max(cfg.from, Math.round(index)));
    hoverIndex = clamped;
    const { x, y, left, plotWidth, width } = geometry;
    const px = x(clamped);
    refs.hoverLine.setAttribute('x1', px.toFixed(1));
    refs.hoverLine.setAttribute('x2', px.toFixed(1));
    refs.hoverLine.setAttribute('visibility', 'visible');
    const value = cfg.series.values[clamped];
    if (isNumber(value)) {
      refs.hoverDot.setAttribute('cx', px.toFixed(1));
      refs.hoverDot.setAttribute('cy', y(value).toFixed(1));
      refs.hoverDot.setAttribute('visibility', 'visible');
    } else {
      refs.hoverDot.setAttribute('visibility', 'hidden');
    }
    const info = cfg.describe(clamped);
    tooltip.replaceChildren(
      h('div', { class: 'chart__tooltip-title', text: info.title }),
      h(
        'div',
        { class: 'chart__tooltip-rows' },
        info.rows.map(([label, text, strong]) => [
          h('span', { class: ['chart__tooltip-value', strong ? 'is-strong' : ''], text }),
          h('span', { class: 'chart__tooltip-label', text: label }),
        ]),
      ),
      (info.notes || []).map((note) => h('div', { class: 'chart__tooltip-note', text: note })),
    );
    tooltip.hidden = false;
    const box = tooltip.offsetWidth || 150;
    const rightSide = px + 12 + box <= width;
    const leftEdge = rightSide ? px + 12 : Math.max(0, px - 12 - box);
    tooltip.style.left = `${Math.round(Math.min(leftEdge, Math.max(0, left + plotWidth - box)))}px`;
    if (announce) {
      announcer.textContent = [info.title, ...info.rows.map(([label, text]) => `${label} ${text}`), ...(info.notes || [])].join('. ');
    }
  }

  function onPointerMove(event) {
    if (!geometry) return;
    const bounds = plot.getBoundingClientRect();
    const px = event.clientX - bounds.left;
    if (px < geometry.left - 6 || px > geometry.left + geometry.plotWidth + 6) {
      hideHover();
      return;
    }
    showHover(geometry.x.invert(px));
  }

  function onKeyDown(event) {
    if (!geometry || event.altKey || event.ctrlKey || event.metaKey) return;
    const day = 24;
    const current = hoverIndex === null ? Math.min(cfg.to, Math.max(cfg.from, cfg.cursor ?? cfg.to)) : hoverIndex;
    let next = null;
    if (event.key === 'ArrowLeft') next = hoverIndex === null ? current : current - (event.shiftKey ? day : 1);
    else if (event.key === 'ArrowRight') next = hoverIndex === null ? current : current + (event.shiftKey ? day : 1);
    else if (event.key === 'Home') next = cfg.from;
    else if (event.key === 'End') next = cfg.to;
    else if (event.key === 'Escape' && hoverIndex !== null) {
      hideHover();
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    showHover(next, { announce: true });
  }

  plot.addEventListener('pointermove', onPointerMove);
  plot.addEventListener('pointerleave', hideHover);
  plot.addEventListener('keydown', onKeyDown);
  plot.addEventListener('blur', hideHover);

  // ---- table twin ---------------------------------------------------------------------------------------------
  function buildTable() {
    const { columns, rowAt, caption } = cfg.table;
    const body = document.createElement('tbody');
    for (let index = cfg.from; index <= cfg.to; index += 1) {
      const cells = rowAt(index);
      const row = document.createElement('tr');
      if (index === cfg.cursor) row.className = 'is-current';
      cells.forEach((text, column) => {
        const cell = document.createElement(column === 0 ? 'th' : 'td');
        if (column === 0) cell.scope = 'row';
        if (columns[column] && columns[column].num) cell.className = 'num';
        cell.textContent = text;
        row.append(cell);
      });
      body.append(row);
    }
    tableHost.replaceChildren(
      h(
        'table',
        { class: 'data-table' },
        h('caption', { class: 'visually-hidden', text: caption || cfg.title || '' }),
        h('thead', {}, h('tr', {}, columns.map((column) => h('th', { scope: 'col', class: column.num ? 'num' : null, text: column.label })))),
        body,
      ),
    );
    tableBuiltFor = `${cfg.from}:${cfg.to}`;
  }

  function showTable(open) {
    tableOpen = Boolean(open) && Boolean(cfg.table);
    tableButton.setAttribute('aria-expanded', tableOpen ? 'true' : 'false');
    tableButton.textContent = tableOpen ? 'View as chart' : 'View as table';
    if (tableOpen && tableBuiltFor !== `${cfg.from}:${cfg.to}`) buildTable();
    tableHost.hidden = !tableOpen;
    plot.hidden = tableOpen;
    legendHost.hidden = tableOpen || !(cfg.legend && cfg.legend.length);
    if (!tableOpen) render();
  }
  tableButton.addEventListener('click', () => showTable(!tableOpen));

  // ---- responsive width ---------------------------------------------------------------------------------------
  let lastWidth = 0;
  let frame = null;
  const onResize = () => {
    const width = Math.floor(plot.clientWidth);
    if (width === lastWidth || width < 60) return;
    lastWidth = width;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = null;
      render();
    });
  };
  let observer = null;
  if (typeof ResizeObserver === 'function') {
    observer = new ResizeObserver(onResize);
    observer.observe(plot);
  } else {
    window.addEventListener('resize', onResize);
  }

  renderFrame();
  lastWidth = Math.floor(plot.clientWidth);
  render();

  return {
    element,
    /** Move the clock: two clip rectangles and one line change; nothing is rebuilt. */
    setCursor(index) {
      if (destroyed || cfg.cursor === index) return;
      cfg.cursor = index;
      if (!tableOpen) applyCursor();
    },
    /** Replace parts of the configuration (window, series, title...) and redraw. */
    update(patch) {
      if (destroyed) return;
      cfg = { ...cfg, ...patch };
      tableBuiltFor = null;
      renderFrame();
      if (tableOpen) showTable(true);
      else render();
    },
    destroy() {
      destroyed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      if (observer) observer.disconnect();
      else window.removeEventListener('resize', onResize);
      element.remove();
    },
  };
}
