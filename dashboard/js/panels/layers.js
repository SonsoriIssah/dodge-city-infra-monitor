/**
 * "Layers & legend" card (collapsible, over the map, top left) and the compact legend (bottom left)
 * - build contract 12.1 (legend and layer titles) and 12.2.
 *
 * The card lists every layer group of js/map/layers.js with a checkbox bound to `state.layers`, the
 * "Risk score | Anomaly count" switch bound to `state.hexMetric`, and the legends. Every colour in a legend
 * has its meaning written next to it; sensor status and anomaly severity are also shown by form.
 */

import { COUNT_CLASSES, LAYER_GROUPS } from '../map/layers.js';
import { h, icon, segmented } from '../ui/dom.js';
import { formatInteger } from '../ui/format.js';
import {
  ASSET_STATUS,
  ASSET_STATUS_LABEL,
  ASSET_STATUS_ORDER,
  CONTEXT_FEATURE,
  RISK_LEVEL,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_ORDER,
  SENSOR_STATUS,
  SENSOR_STATUS_LABEL,
  SENSOR_STATUS_ORDER,
  SEVERITY,
  SEVERITY_LABEL,
  SEVERITY_ORDER,
  SURFACE,
} from '../ui/tokens.js';

const TICK_ORDER = 40;
export const HEALTH_LEGEND_TITLE = 'Derived Asset Health Score (from simulated sensors)';
export const SENSOR_LEGEND_TITLE = 'Simulated sensors';
export const RISK_LEGEND_TITLE = 'Risk zones — derived from simulated anomalies';

const SWATCH = {
  buildings: ['block', ASSET_STATUS.normal],
  roads: ['line', ASSET_STATUS.normal],
  bridges: ['line', ASSET_STATUS.normal],
  rail: ['dashed', CONTEXT_FEATURE],
  power: ['dashed', CONTEXT_FEATURE],
  street_lights: ['dot', CONTEXT_FEATURE],
  water_mains: ['dashed', ASSET_STATUS.normal],
  sensors: ['dot', SENSOR_STATUS.normal],
  anomalies: ['ring', SEVERITY.critical],
  risk_zones: ['hex', RISK_LEVEL.high],
  clusters: ['outline-dashed', SURFACE.text],
  study_area: ['outline-dashed', SURFACE.accent],
  roads_base: ['line', '#4a5b70'],
  city_boundary: ['outline', SURFACE.text],
  imagery: ['block', '#5d6b4f'],
};

function swatch(form, color) {
  return h('span', { class: `swatch swatch--${form}`, style: { '--swatch': color }, 'aria-hidden': 'true' });
}

function legendItem(mark, text) {
  return h('span', { class: 'legend-item' }, mark, text);
}

function healthLegend() {
  return h(
    'div',
    { class: 'legend-list' },
    ASSET_STATUS_ORDER.map((status) => legendItem(swatch('block', ASSET_STATUS[status]), ASSET_STATUS_LABEL[status])),
  );
}

function sensorLegend() {
  return h(
    'div',
    { class: 'legend-list' },
    SENSOR_STATUS_ORDER.map((status) =>
      legendItem(
        h('span', { class: 'sensor-mark', dataset: { status }, style: { '--swatch': SENSOR_STATUS[status] }, 'aria-hidden': 'true' }),
        SENSOR_STATUS_LABEL[status],
      ),
    ),
  );
}

function severityLegend() {
  return h(
    'div',
    { class: 'legend-list' },
    SEVERITY_ORDER.map((severity) =>
      legendItem(
        h('span', { class: 'ring-mark', dataset: { severity }, style: { '--swatch': SEVERITY[severity] }, 'aria-hidden': 'true' }),
        SEVERITY_LABEL[severity],
      ),
    ),
  );
}

/** "Low 0-24 · Moderate 25-49 · High 50-74 · Very high 75-100" from `meta.risk.levels`. */
function riskLegend(levels) {
  const ordered = RISK_LEVEL_ORDER.filter((level) => levels[level] !== undefined);
  return h(
    'div',
    { class: 'legend-list' },
    ordered.map((level, index) => {
      const from = levels[level];
      const to = index + 1 < ordered.length ? levels[ordered[index + 1]] - 1 : 100;
      return legendItem(swatch('hex', RISK_LEVEL[level]), `${RISK_LEVEL_LABEL[level]} ${from}–${to}`);
    }),
  );
}

function countLegend() {
  return h(
    'div',
    { class: 'legend-list' },
    COUNT_CLASSES.map((entry) => legendItem(swatch('hex', entry.color), entry.label)),
  );
}

/** "419 of 458 heights measured/tagged, 39 estimated" from `meta.counts.building_height_sources`. */
export function buildingHeightSentence(meta) {
  const sources = meta.counts.building_height_sources || {};
  const estimated = sources.estimated || 0;
  const total = Object.values(sources).reduce((sum, value) => sum + value, 0);
  return `${formatInteger(total - estimated)} of ${formatInteger(total)} heights measured/tagged, ${formatInteger(estimated)} estimated`;
}

/**
 * @param {{card: HTMLElement, legend: HTMLElement}} elements
 * @param {{store: object, data: object, actions: object}} ctx
 */
export function mountLayers(elements, { store, data, actions }) {
  const { meta } = data;
  const checkboxes = new Map();
  const counters = new Map();

  const countOf = (group) => {
    if (group.assetType) return meta.counts.assets_by_type[group.assetType] || 0;
    if (group.id === 'sensors') return meta.counts.sensors;
    if (group.id === 'risk_zones') return data.riskZones.length;
    if (group.id === 'clusters') return data.clusters.length;
    return null;
  };

  function toggleRow(group) {
    const input = h('input', {
      type: 'checkbox',
      on: { change: (event) => actions.setLayer(group.id, event.target.checked) },
    });
    checkboxes.set(group.id, input);
    const [form, color] = SWATCH[group.id] || ['block', SURFACE.muted];
    const label = group.labelFromMeta && meta.labels[group.labelFromMeta] ? meta.labels[group.labelFromMeta] : group.label;
    const count = countOf(group);
    const counter = h('span', { class: 'layer-toggle__count', text: count === null ? '' : formatInteger(count) });
    counters.set(group.id, counter);
    return h('label', { class: 'layer-toggle', dataset: { layer: group.id } }, input, swatch(form, color), h('span', { text: label }), counter);
  }

  const groupsOf = (section) => LAYER_GROUPS.filter((group) => group.section === section);
  const byId = (id) => LAYER_GROUPS.find((group) => group.id === id);

  let metricControl = h('div');
  let metricLegend = h('div');
  const metricRow = h('div', { class: 'layer-sub' });

  function renderMetric(metric) {
    const control = segmented({
      label: 'Risk hexagons show',
      options: [
        { value: 'risk', label: 'Risk score' },
        { value: 'count', label: 'Anomaly count' },
      ],
      value: metric,
      onChange: (value) => actions.setHexMetric(value),
    });
    const legend = metric === 'count' ? countLegend() : riskLegend(meta.risk.levels);
    metricControl.replaceWith(control);
    metricLegend.replaceWith(legend);
    metricControl = control;
    metricLegend = legend;
  }

  const body = h(
    'div',
    { class: 'layers-card__body', id: 'layers-card-body' },
    h(
      'section',
      { class: 'layer-group' },
      h('h3', { class: 'layer-group__title', text: HEALTH_LEGEND_TITLE }),
      healthLegend(),
      h('p', { class: 'layer-group__note', text: 'Colour of the monitored assets. At-risk and critical lines are also drawn wider.' }),
      groupsOf('assets').map(toggleRow),
      h('p', { class: 'layer-group__note', text: `${meta.labels.buildings} ${buildingHeightSentence(meta)}.` }),
    ),
    h(
      'section',
      { class: 'layer-group' },
      h('h3', { class: 'layer-group__title', text: SENSOR_LEGEND_TITLE }),
      toggleRow(byId('sensors')),
      sensorLegend(),
      h('p', { class: 'layer-group__note', text: 'Status is shown by size and outline as well as colour. Sensor type is a letter: T temperature, V vibration, M moisture, P pressure.' }),
    ),
    h(
      'section',
      { class: 'layer-group' },
      h('h3', { class: 'layer-group__title', text: `${meta.labels.detection} — anomaly markers` }),
      toggleRow(byId('anomalies')),
      severityLegend(),
      h('p', { class: 'layer-group__note', text: 'Ring size and colour give the severity. Only anomalies active at the selected hour are drawn.' }),
    ),
    h(
      'section',
      { class: 'layer-group' },
      h('h3', { class: 'layer-group__title', text: RISK_LEGEND_TITLE }),
      toggleRow(byId('risk_zones')),
      metricRow,
      metricLegend,
      toggleRow(byId('clusters')),
      h('p', { class: 'layer-group__note', text: 'Clusters are dashed hulls around anomalies that are close in space and time — descriptive, not a causal finding.' }),
    ),
    h(
      'section',
      { class: 'layer-group' },
      h('h3', { class: 'layer-group__title', text: 'Context' }),
      groupsOf('context').map(toggleRow),
    ),
  );
  metricRow.append(metricControl);

  const toggle = h(
    'button',
    {
      class: 'layers-card__toggle',
      type: 'button',
      'aria-controls': 'layers-card-body',
      on: { click: () => store.set({ layersOpen: !store.getState().layersOpen }) },
    },
    icon('layers'),
    h('span', { text: 'Layers & legend' }),
    (() => {
      const chevron = icon('chevron', { size: 14 });
      chevron.classList.add('chevron');
      return chevron;
    })(),
  );
  elements.card.classList.add('layers-card');
  elements.card.replaceChildren(toggle, body);

  elements.legend.classList.add('legend');
  elements.legend.replaceChildren(
    h('div', { class: 'legend__title', text: HEALTH_LEGEND_TITLE }),
    healthLegend(),
    h('div', { class: 'legend__title', text: SENSOR_LEGEND_TITLE }),
    sensorLegend(),
  );

  function renderOpen(open) {
    elements.card.dataset.open = open ? 'true' : 'false';
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.hidden = !open;
  }

  function renderLayers(state) {
    for (const [id, input] of checkboxes) {
      input.checked = Boolean(state.layers[id]);
      const unavailable = id === 'imagery' && state.imagery === 'failed';
      input.disabled = unavailable;
      input.closest('.layer-toggle').dataset.disabled = unavailable ? 'true' : 'false';
    }
  }

  function renderCounts(t) {
    const active = data.statsAt(t).active_anomalies;
    const counter = counters.get('anomalies');
    const text = formatInteger(active);
    if (counter.textContent !== text) counter.textContent = text;
  }

  const unsubscribe = [
    store.subscribe(['layersOpen'], (state) => renderOpen(state.layersOpen)),
    store.subscribe(['layers', 'imagery'], (state) => renderLayers(state)),
    store.subscribe(['hexMetric'], (state) => renderMetric(state.hexMetric)),
    store.subscribe(['t'], (state) => renderCounts(state.t), { order: TICK_ORDER }),
  ];
  const initial = store.getState();
  renderOpen(initial.layersOpen);
  renderLayers(initial);
  renderMetric(initial.hexMetric);
  renderCounts(initial.t);

  return {
    destroy() {
      unsubscribe.forEach((off) => off());
    },
  };
}
