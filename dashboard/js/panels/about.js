/**
 * "About the data" dialog (build contract 12.5, amendment A5; brief R1, R22).
 *
 * Built entirely from `/meta`: provenance table with REAL / SIMULATED / DERIVED tags, the detection method
 * and the latest run's figures with their evaluation note, the health-score and risk formulas, the
 * retrospective-analysis statement and the limitations. Modal <dialog>: Esc and the backdrop close it.
 */

import { h, icon, sourceKindTag } from '../ui/dom.js';
import { formatInteger, formatNumber, formatPercent, humanize } from '../ui/format.js';
import { buildingHeightSentence } from './layers.js';

/** Statement of build contract section 8 (shown verbatim). */
export const RETROSPECTIVE_STATEMENT =
  'Detection is a retrospective batch analysis: baselines and scales are estimated from the whole data window. ' +
  'Playback replays those results hour by hour; it does not reproduce what a streaming detector would have ' +
  'known at that hour.';

/** Wording of amendment A5 (shown verbatim). */
export const ISOLATION_FOREST_STATEMENT =
  'Isolation Forest is used as corroborating evidence only. On this simulated dataset it agreed with the ' +
  'robust-statistics detectors on every anomaly and did not identify events they missed.';

const KIND_ORDER = { real: 0, simulated: 1, derived: 2 };
const ISO_TIMESTAMP = /^d{4}-d{2}-d{2}Td{2}:d{2}:d{2}Z$/;

/** A formula given as one sentence with ";" between its terms, one term per line. */
function formulaBlock(text) {
  const lines = String(text)
    .split(/;s+|.s+(?=[A-Z])/)
    .map((line) => line.trim())
    .filter(Boolean);
  return h('div', { class: 'formula' }, lines.map((line) => h('span', { text: line })));
}

function section(title, ...children) {
  return h('section', { class: 'dialog__section' }, h('h3', { text: title }), children);
}

function metric(value, label) {
  return h('div', { class: 'metric' }, h('div', { class: 'metric__value', text: value }), h('div', { class: 'metric__label', text: label }));
}

function provenanceTable(meta, format) {
  const sources = meta.data_sources
    .slice()
    .sort((a, b) => (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) || a.name.localeCompare(b.name));
  const rows = sources.map((source) => {
    const details = [source.provider, source.notes].filter(Boolean).join(' — ');
    // A vintage that is a timestamp (OpenStreetMap data time) is shown as a date in the study-area zone.
    const vintageText = source.vintage && ISO_TIMESTAMP.test(source.vintage) ? format.dateTime(source.vintage) : source.vintage;
    const vintage = [
      vintageText ? `Vintage: ${vintageText}` : null,
      source.retrieved_at ? `Retrieved ${format.dateLong(source.retrieved_at)}` : null,
    ]
      .filter(Boolean)
      .join(' · ');
    return h(
      'tr',
      {},
      h('td', {}, sourceKindTag(source.kind)),
      h('td', {}, h('strong', { text: source.name }), details ? h('div', { class: 'muted', text: details }) : null),
      h('td', {}, source.attribution_text || '—', source.license ? h('div', { class: 'muted', text: source.license }) : null),
      h('td', { text: vintage || '—' }),
    );
  });
  return h(
    'div',
    { class: 'table-scroll' },
    h(
      'table',
      { class: 'data-table' },
      h('caption', { class: 'visually-hidden', text: 'Data sources and what kind of data each one provides' }),
      h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: 'Kind' }), h('th', { scope: 'col', text: 'Source' }), h('th', { scope: 'col', text: 'Credit and licence' }), h('th', { scope: 'col', text: 'Vintage' }))),
      h('tbody', {}, rows),
    ),
  );
}

function detectionSection(meta) {
  const run = meta.detection_run || {};
  const params = run.params || {};
  const metrics = run.metrics || {};
  const forest = params.iforest || {};
  const severity = metrics.severity_counts || {};
  const severityText = meta.severity_levels
    .slice()
    .reverse()
    .map((level) => `${formatInteger(severity[level] || 0)} ${level}`)
    .join(' · ');
  const settings = [
    params.z_strong !== undefined ? `robust z-score at or beyond ${formatNumber(params.z_strong)}` : null,
    params.rolling_hours !== undefined ? `median of z over the trailing ${formatNumber(params.rolling_hours)} h at or beyond ${formatNumber(params.rolling_level)}` : null,
    'value beyond the critical limit of its sensor type and placement',
  ].filter(Boolean);
  return section(
    `${meta.labels.detection} — method and evaluation`,
    params.method ? h('p', { text: params.method }) : null,
    h(
      'ul',
      {},
      h('li', { text: `An hour is flagged by: ${settings.join('; ')}.` }),
      params.merge_gap_hours !== undefined
        ? h('li', { text: `Flagged hours no more than ${formatNumber(params.merge_gap_hours)} h apart form one event. An event is kept as an anomaly only when it is strong, lasts at least ${formatNumber(params.min_flagged_hours)} flagged hours, or breaches a critical limit — not every unusual reading is an anomaly.` })
        : null,
      h('li', { text: `Severity comes from an anomaly score that combines magnitude, duration and threshold breach${params.score && params.score.weights ? ` (weights ${formatNumber(params.score.weights.magnitude, 2)}, ${formatNumber(params.score.weights.duration, 2)}, ${formatNumber(params.score.weights.threshold, 2)})` : ''}.` }),
      h('li', { text: `${ISOLATION_FOREST_STATEMENT}${forest.threshold !== undefined ? ` (scikit-learn, ${formatInteger(forest.n_estimators)} trees, score threshold ${formatNumber(forest.threshold, 2)}.)` : ''}` }),
    ),
    h(
      'div',
      { class: 'metric-grid' },
      metric(formatPercent(metrics.event_recall), `Event recall — ${formatInteger(metrics.detected_events)} of ${formatInteger(metrics.injected_events)} injected events detected`),
      metric(formatPercent(metrics.anomaly_precision), `Anomaly precision — ${formatInteger(metrics.true_anomalies)} of ${formatInteger(metrics.anomalies)} anomalies match an injected event`),
      metric(formatInteger(metrics.false_anomalies), `Anomalies with no injected event (${formatInteger(metrics.false_anomalies_during_benign_events)} during benign regional events)`),
      metric(formatInteger(metrics.anomalies), `Anomalies by severity: ${severityText}`),
    ),
    run.evaluation_note ? h('p', { class: 'callout', text: run.evaluation_note }) : null,
    h('p', { text: RETROSPECTIVE_STATEMENT }),
  );
}

function healthSection(meta) {
  const health = meta.health;
  const bands = health.bands;
  return section(
    `${meta.labels.health} — how it is calculated`,
    h('p', { text: 'A score from 0 to 100 for every monitored asset and every hour, computed only from the simulated sensors on that asset and the anomalies detected on them. It is not an assessment of the real condition of any structure.' }),
    formulaBlock(health.formula),
    h(
      'ul',
      {},
      h('li', { text: `Look-back window ${formatNumber(health.window_days)} days; the weight of an ended anomaly halves every ${formatNumber(health.half_life_hours)} h.` }),
      h('li', { text: `Status bands: normal from ${bands.normal}, watch from ${bands.watch}, at risk from ${bands.at_risk}, critical below ${bands.at_risk}.` }),
      h('li', { text: `"Assets at Risk" counts monitored assets with a score below ${health.at_risk_below}.` }),
      h('li', { text: 'Assets without sensors have no score and are shown as "Not monitored". Recorded attributes of real features, such as National Bridge Inventory ratings, never feed the score.' }),
    ),
  );
}

function riskSection(meta) {
  const risk = meta.risk;
  const levels = risk.levels;
  return section(
    'Risk zones — how they are calculated',
    h('p', { text: `The study area is covered with hexagonal cells (${formatNumber(risk.hex_edge_m)} m edge). For every cell and hour:` }),
    formulaBlock(`risk = min(100, 100 × Σ severity weight × exp(−distance² / (2 × ${formatNumber(risk.bandwidth_m)}² m²)) × time weight / ${formatNumber(risk.reference)})`),
    h(
      'ul',
      {},
      h('li', { text: 'The sum runs over the anomalies that had started by that hour; distance is measured from the cell centre to the sensor; severity weights are the ones of the health score.' }),
      h('li', { text: `Time weight is 1 while the anomaly is active and halves every ${formatNumber(risk.half_life_hours)} h after it ended.` }),
      h('li', { text: `Levels: low from ${levels.low}, moderate from ${levels.moderate}, high from ${levels.high}, very high from ${levels.very_high}. The "Anomaly count" view shows the anomalies active in each cell instead.` }),
      meta.spatial
        ? h('li', { text: `Clusters group anomalies within ${formatNumber(meta.spatial.cluster_eps_m)} m and ${formatNumber(meta.spatial.cluster_eps_hours)} h of each other on at least ${formatNumber(meta.spatial.cluster_min_sensors)} sensors. They describe co-occurrence, not a cause. "Nearby assets" of an anomaly lie within ${formatNumber(meta.spatial.proximity_radius_m)} m.` })
        : null,
    ),
  );
}

function limitationsSection(meta) {
  const counts = meta.counts;
  return section(
    'Limitations',
    h(
      'ul',
      {},
      h('li', { text: `${meta.data_notice}` }),
      h('li', { text: 'This is a research prototype. It is not a certified infrastructure safety system and it does not predict structural failure.' }),
      h('li', { text: `All ${formatInteger(counts.sensors)} sensors and their ${formatInteger(counts.readings)} readings are produced by a deterministic simulator; the weather that drives them is simulated too.` }),
      h('li', { text: `${meta.labels.buildings} ${buildingHeightSentence(meta)}.` }),
      h('li', { text: `${meta.labels.water_network}: ${formatInteger(counts.simulated_assets)} simulated assets drawn along real streets to host the pressure sensors.` }),
      h('li', { text: 'Recorded attributes (OpenStreetMap tags, National Bridge Inventory records) are shown as published by their source and are not verified here.' }),
      h('li', { text: 'The city limits are a statistical boundary from the U.S. Census Bureau, not a legal land description.' }),
      h('li', { text: `${meta.labels.playback} Gradual changes are detected some hours after their onset.` }),
      h('li', { text: 'Detection quality is measured only against the events the simulator injected; nothing here has been validated in the field.' }),
    ),
  );
}

/**
 * @param {HTMLDialogElement} dialog
 * @param {{data: object}} ctx
 * @returns {{open: function, close: function}}
 */
export function mountAbout(dialog, { data }) {
  const { meta, format } = data;
  const closeButton = h('button', { class: 'btn btn--icon', type: 'button', 'aria-label': 'Close', on: { click: () => dialog.close() } }, icon('close'));
  const frame = h(
    'div',
    { class: 'dialog__frame' },
    h('div', { class: 'dialog__head' }, h('h2', { class: 'dialog__title', id: 'about-title', text: 'About the data' }), closeButton),
    h(
      'div',
      { class: 'dialog__body', tabindex: '0' },
      h('p', { class: 'callout', text: meta.data_notice }),
      section(
        'Data provenance',
        h('p', { text: `Real geographic data and simulated or derived monitoring data are kept apart everywhere in this dashboard. ${formatInteger(meta.counts.real_assets)} of the ${formatInteger(meta.counts.assets)} assets are real features; ${formatInteger(meta.counts.simulated_assets)} are simulated. ${formatInteger(meta.counts.monitored_assets)} assets carry simulated sensors.` }),
        provenanceTable(meta, format),
        h('p', { class: 'muted', text: `REAL = published geographic data or a display service. SIMULATED = produced by this project's simulator. DERIVED = computed by this project from the simulated data. Analysis run finished ${format.dateTimeLong(meta.detection_run.finished_at)}; data window ${format.dateTime(meta.time.start)} to ${format.dateTime(meta.time.end)}, one reading per ${humanize(String(meta.time.step_minutes))} minutes.` }),
      ),
      detectionSection(meta),
      healthSection(meta),
      riskSection(meta),
      limitationsSection(meta),
    ),
  );
  dialog.replaceChildren(frame);

  // A click on the backdrop (the dialog element itself, outside the frame) closes the dialog.
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });

  return {
    open() {
      if (dialog.open) return;
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
      closeButton.focus();
    },
    close() {
      if (dialog.open) dialog.close();
    },
  };
}
