/**
 * Anomalies tab (build contract 12.1, 12.5): "Prototype Anomaly Detection".
 *
 * List: the anomalies that have started by the selected hour, narrowed by five filters kept in the store
 * (severity, sensor type, status at t, asset, start date from/to) so the KPI cards can set them; active first,
 * then by severity, then the newest.
 *
 * Detail: every field of the anomaly, links to its asset and sensor, the assets within the proximity radius
 * (the map highlights them and draws the radius ring), its co-occurrence cluster, and the sensor chart from
 * 48 h before the event to 48 h after it. Every card and the detail carry the "Simulated" tag.
 *
 * Panel module interface: see js/panels/tabs.js.
 */

import { filtersAreDefault } from '../state/store.js';
import {
  assetNameWithId,
  assetOptions,
  clockChanged,
  createListDetailPanel,
  detectionMethodWords,
  lazyRegion,
  mountSensorChart,
  sensorTypeLabel,
  stackedRow,
} from './common.js';

export const SUBTITLE = 'Prototype Anomaly Detection';
export const EMPTY_LIST = 'No anomalies match these filters at the selected time';
const STATUS_OPTIONS = Object.freeze([
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'resolved', label: 'Resolved' },
]);
const SCORE_COMPONENTS = Object.freeze([
  ['magnitude', 'Magnitude'],
  ['duration', 'Duration'],
  ['threshold', 'Threshold'],
]);

export function mount(container, ctx) {
  const { store, data, dom, format, tokens, helpers, actions, provider } = ctx;
  const { h } = dom;
  const { meta } = data;
  const zone = format.zoneName(data.times[data.lastIndex]);
  const radius = meta.spatial ? meta.spatial.proximity_radius_m : null;

  // ---- list ------------------------------------------------------------------------------------------------
  function renderList(target, initialState) {
    const severityOptions = meta.severity_levels.map((level) => ({ value: level, label: tokens.labelOf(tokens.SEVERITY_LABEL, level) }));
    const typeOptions = Object.keys(meta.sensor_types).map((type) => ({ value: type, label: sensorTypeLabel(meta, type) }));
    const assetsWithAnomalies = [...new Set(data.anomalies.map((anomaly) => anomaly.asset_id))]
      .map((assetId) => data.assetById(assetId))
      .filter(Boolean);
    const dayOptions = (emptyLabel) => [{ value: '', label: emptyLabel }, ...data.days.map((day) => ({ value: day.key, label: day.label }))];

    // The chip handlers read the current state, so the controls are built once and only synchronised later.
    const toggle = (key) => (value) => actions.toggleFilterValue(key, value, !store.getState().filters[key].has(value));
    const severityChips = dom.chipGroup({ label: 'Severity', options: severityOptions, selected: initialState.filters.severity, onToggle: toggle('severity') });
    const typeChips = dom.chipGroup({ label: 'Sensor type', options: typeOptions, selected: initialState.filters.sensorType, onToggle: toggle('sensorType') });
    const statusSelect = dom.select({
      label: 'Status at the selected time',
      options: STATUS_OPTIONS,
      value: initialState.filters.status,
      onChange: (value) => actions.setFilter({ status: value }),
    });
    const assetSelect = dom.select({
      label: 'Asset',
      options: assetOptions(assetsWithAnomalies, 'All assets with anomalies'),
      value: initialState.filters.assetId || '',
      onChange: (value) => actions.setFilter({ assetId: value || null }),
    });
    const fromSelect = dom.select({
      label: `Started from (${zone})`,
      options: dayOptions('Start of the simulation'),
      value: initialState.filters.dateFrom || '',
      onChange: (value) => {
        const dateTo = store.getState().filters.dateTo;
        actions.setFilter({ dateFrom: value || null, dateTo: value && dateTo && dateTo < value ? value : dateTo });
      },
    });
    const toSelect = dom.select({
      label: `Started up to (${zone})`,
      options: dayOptions('End of the simulation'),
      value: initialState.filters.dateTo || '',
      onChange: (value) => {
        const dateFrom = store.getState().filters.dateFrom;
        actions.setFilter({ dateTo: value || null, dateFrom: value && dateFrom && dateFrom > value ? value : dateFrom });
      },
    });
    const clearButton = h('button', { class: 'btn btn--small', type: 'button', text: 'Clear filters', on: { click: () => actions.clearFilters() } });
    const count = h('span', { class: 'filters__count', role: 'status' });
    const list = h('div', { class: 'list', role: 'group', 'aria-label': 'Anomalies, active first' });

    target.append(
      dom.panelHead({ title: 'Anomalies', subtitle: SUBTITLE, trailing: dom.simulatedTag() }),
      h(
        'form',
        { class: 'filters', 'aria-label': 'Anomaly filters', on: { submit: (event) => event.preventDefault() } },
        severityChips,
        typeChips,
        h('div', { class: 'filters__row' }, statusSelect, assetSelect),
        h('div', { class: 'filters__row' }, fromSelect, toSelect),
        h('div', { class: 'filters__foot' }, count, clearButton),
      ),
      list,
    );

    const syncChips = (group, selected, options) => {
      group.querySelectorAll('.chip').forEach((chip, index) => {
        chip.setAttribute('aria-pressed', selected.has(options[index].value) ? 'true' : 'false');
      });
    };

    function syncControls(filters) {
      syncChips(severityChips, filters.severity, severityOptions);
      syncChips(typeChips, filters.sensorType, typeOptions);
      statusSelect.control.value = filters.status;
      assetSelect.control.value = filters.assetId || '';
      fromSelect.control.value = filters.dateFrom || '';
      toSelect.control.value = filters.dateTo || '';
      clearButton.disabled = filtersAreDefault(filters, meta);
    }

    let signature = null;
    function draw(state) {
      const { t, filters } = state;
      const started = data.startedAnomaliesAt(t).length;
      const rows = helpers.sortAnomaliesForList(helpers.filterAnomalies(data.anomalies, filters, t), t);
      count.textContent = `${rows.length} of ${format.plural(started, 'anomaly', 'anomalies')} detected up to ${format.dateTime(data.times[t])}`;
      const next = rows.map((anomaly) => `${anomaly.anomaly_id}:${data.anomalyStatusAt(anomaly, t)}`).join('|') || 'empty';
      if (next === signature) return;
      signature = next;
      const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.id : null;
      if (rows.length === 0) {
        list.replaceChildren(
          dom.emptyState(EMPTY_LIST, {
            hint: started === 0 ? 'No anomaly has started yet at this point of the simulation.' : 'Widen the filters or move the clock.',
            action: filtersAreDefault(filters, meta) ? null : { label: 'Clear filters', onClick: () => actions.clearFilters() },
          }),
        );
        return;
      }
      list.replaceChildren(
        ...rows.map((anomaly) => {
          const status = data.anomalyStatusAt(anomaly, t);
          const assetName = anomaly.asset_name || anomaly.asset_id;
          const started_at = format.dateTimeShort(anomaly.started_at);
          return stackedRow(dom, {
            id: anomaly.anomaly_id,
            lead: dom.pill('severity', anomaly.severity),
            title: anomaly.anomaly_label,
            trail: dom.pill('anomaly-status', status),
            lines: [[`${assetName} · ${anomaly.sensor_id}`], [`Started ${started_at}`, dom.simulatedTag()]],
            ariaLabel:
              `${anomaly.anomaly_label}, severity ${tokens.labelOf(tokens.SEVERITY_LABEL, anomaly.severity)}, ${assetName}, sensor ${anomaly.sensor_id}, ` +
              `started ${format.dateTime(anomaly.started_at)}, ${tokens.labelOf(tokens.ANOMALY_STATUS_LABEL, status)}, simulated`,
            onClick: () => actions.select('anomaly', anomaly.anomaly_id),
          });
        }),
      );
      if (focusedId) {
        const again = list.querySelector(`[data-id="${CSS.escape(focusedId)}"]`);
        if (again) again.focus({ preventScroll: true });
      }
    }

    syncControls(initialState.filters);
    draw(initialState);
    return {
      refresh(state, changed) {
        if (changed.has('filters') || changed.has('tab')) syncControls(state.filters);
        if (clockChanged(changed) || changed.has('filters')) draw(state);
      },
      rowFor: (id) => list.querySelector(`[data-id="${CSS.escape(id)}"]`),
    };
  }

  // ---- detail ----------------------------------------------------------------------------------------------
  function scoreBlock(anomaly) {
    const weights = meta.detection_run && meta.detection_run.params && meta.detection_run.params.score ? meta.detection_run.params.score.weights : null;
    const components = anomaly.score_components || {};
    return h(
      'table',
      { class: 'data-table score-table' },
      h('caption', { class: 'visually-hidden', text: 'Anomaly score and its components' }),
      h(
        'thead',
        {},
        h('tr', {}, h('th', { scope: 'col', text: 'Component' }), h('th', { scope: 'col', class: 'num', text: 'Value' }), h('th', { scope: 'col', class: 'num', text: 'Weight' })),
      ),
      h(
        'tbody',
        {},
        SCORE_COMPONENTS.map(([key, label]) =>
          h(
            'tr',
            {},
            h('th', { scope: 'row', text: label }),
            h('td', { class: 'num', text: format.formatScore(components[key]) }),
            h('td', { class: 'num', text: weights && weights[key] !== undefined ? format.formatNumber(weights[key], 2) : '—' }),
          ),
        ),
        h('tr', { class: 'is-total' }, h('th', { scope: 'row', text: 'Anomaly score' }), h('td', { class: 'num', text: format.formatScore(anomaly.anomaly_score) }), h('td', { class: 'num', text: '0 – 1' })),
      ),
    );
  }

  function clusterContent(cluster, anomaly) {
    if (!cluster) {
      const minimum = meta.spatial && meta.spatial.cluster_min_sensors ? `at least ${meta.spatial.cluster_min_sensors} sensors` : 'several sensors';
      return h('p', { class: 'block__note', text: `Not part of a co-occurrence cluster: no group of ${minimum} with anomalies close in space and time includes this one.` });
    }
    const others = (cluster.anomaly_ids || []).filter((id) => id !== anomaly.anomaly_id);
    return [
      dom.keyValue([
        ['Cluster', `Co-occurrence cluster ${cluster.cluster_id}`],
        ['Members', `${format.plural(cluster.n_anomalies, 'anomaly', 'anomalies')} on ${format.plural(cluster.n_sensors, 'sensor')} across ${format.plural(cluster.n_assets, 'asset')}`],
        ['Sensor types', (cluster.sensor_types || []).map((type) => sensorTypeLabel(meta, type)).join(', ')],
        ['Highest severity', cluster.max_severity ? dom.pill('severity', cluster.max_severity) : null],
        ['Period', `${format.dateTime(cluster.first_started_at)} – ${format.dateTime(cluster.last_ended_at)}`],
        [
          'Other members',
          others.length
            ? h('ul', { class: 'link-list' }, others.map((id) => h('li', {}, dom.linkButton(id, () => actions.select('anomaly', id)))))
            : null,
        ],
      ]),
      h('p', { class: 'block__note', text: 'A co-occurrence cluster is descriptive: these anomalies are close in space and time. It is not a causal finding.' }),
      h('button', { class: 'btn btn--small', type: 'button', text: 'Show cluster outlines on the map', on: { click: () => actions.setLayer('clusters', true) } }),
    ];
  }

  function nearbyContent(detail) {
    const nearby = detail.nearby_assets || [];
    if (nearby.length === 0) {
      return dom.emptyState(radius ? `No other asset within ${format.formatDistance(radius)}` : 'No nearby assets recorded');
    }
    return h(
      'div',
      { class: 'list' },
      nearby.map((near) => {
        const feature = data.assetById(near.asset_id);
        const properties = feature ? feature.properties : { asset_id: near.asset_id, asset_type: near.asset_type, name: near.name };
        return dom.rowButton({
          title: helpers.assetDisplayName(properties),
          subtitle: `${helpers.assetTypeLabel(properties, tokens.ASSET_TYPE_LABEL)} · ${near.asset_id}`,
          trailing: h('span', { text: format.formatDistance(near.distance_m) }),
          dataset: { id: near.asset_id },
          onClick: () => actions.select('asset', near.asset_id),
        });
      }),
    );
  }

  function renderDetail(target, anomalyId, state) {
    const anomaly = data.anomalyById(anomalyId);
    const back = () => actions.clearSelection();
    if (!anomaly) {
      target.append(dom.panelHead({ title: anomalyId, subtitle: SUBTITLE, onBack: back }), dom.emptyState('This anomaly is not part of the loaded data'));
      return { refresh() {}, focusTarget: target.querySelector('button') };
    }
    const sensor = data.sensorById(anomaly.sensor_id);
    const asset = data.assetById(anomaly.asset_id);
    const unit = anomaly.unit;
    const statusPill = dom.pill('anomaly-status', data.anomalyStatusAt(anomaly, state.t));
    const statusNote = h('span', { class: 'kv__note' });
    const activeAtEnd = anomaly.end_idx >= data.lastIndex;
    const ratio =
      anomaly.expected_value && Number.isFinite(anomaly.observed_value / anomaly.expected_value) && anomaly.expected_value > 0 && anomaly.observed_value > 0
        ? anomaly.observed_value / anomaly.expected_value
        : null;

    const head = dom.panelHead({
      title: anomaly.anomaly_label,
      subtitle: `${SUBTITLE} · ${anomaly.anomaly_id}`,
      trailing: dom.simulatedTag(),
      onBack: back,
    });

    const summary = dom.block(
      'Anomaly',
      {},
      dom.keyValue([
        ['Anomaly ID', anomaly.anomaly_id],
        [
          'Asset',
          dom.linkButton(asset ? assetNameWithId(asset.properties) : `${anomaly.asset_name || ''} (${anomaly.asset_id})`, () => actions.select('asset', anomaly.asset_id), {
            title: 'Open this asset',
          }),
        ],
        [
          'Sensor',
          dom.linkButton(`${anomaly.sensor_id} · ${sensorTypeLabel(meta, anomaly.sensor_type)} (${format.humanize(anomaly.placement).toLowerCase()})`, () => actions.select('sensor', anomaly.sensor_id), {
            title: 'Open this sensor',
          }),
        ],
        ['Anomaly type', `${anomaly.anomaly_label} (${anomaly.anomaly_type})`],
        ['Severity', dom.pill('severity', anomaly.severity)],
        ['Status', h('span', { class: 'kv__stack' }, statusPill, statusNote)],
        ['Started', format.dateTime(anomaly.started_at)],
        ['Peak', format.dateTime(anomaly.peak_at)],
        [
          'Ended',
          activeAtEnd
            ? `${format.dateTime(anomaly.ended_at)} — last flagged reading; still active when the simulation ends`
            : format.dateTime(anomaly.ended_at),
        ],
        ['Duration', format.formatDuration(anomaly.duration_hours)],
      ]),
    );

    const evidence = dom.block(
      'Reading at the peak and score',
      {},
      dom.keyValue([
        ['Observed value', format.formatValue(anomaly.observed_value, unit)],
        ['Expected value', `${format.formatValue(anomaly.expected_value, unit)}${ratio ? ` (observed is ${format.formatNumber(ratio, 2)}× the expected value)` : ''}`],
        ['Robust z-score', format.formatZ(anomaly.robust_z)],
      ]),
      scoreBlock(anomaly),
      h('p', {
        class: 'block__note',
        text: 'Anomaly score = weighted sum of the three components. Severity follows the score; it describes the reading, not the condition of the asset.',
      }),
    );

    const method = dom.block(
      'Detection method and explanation',
      {},
      dom.keyValue([
        ['Detection method', h('span', { class: 'kv__stack' }, detectionMethodWords(anomaly.detection_method), h('code', { class: 'kv__note', text: anomaly.detection_method }))],
      ]),
      h('p', { class: 'explanation', text: anomaly.explanation }),
      h('p', { class: 'block__note', text: 'Retrospective batch analysis of simulated readings. The explanation describes the numbers; it does not state a cause.' }),
    );

    const lazies = [];
    const nearbyHost = h('div', {});
    const clusterHost = h('div', { class: 'cluster' });
    lazies.push(
      lazyRegion(nearbyHost, dom, {
        label: 'Loading the nearby assets',
        errorTitle: 'The nearby assets and the cluster of this anomaly could not be loaded.',
        skeleton: { rows: 3 },
        load: () => provider.getAnomalyDetail(anomaly.anomaly_id),
        render(detail, host) {
          host.append(nearbyContent(detail));
          dom.replace(clusterHost, clusterContent(detail.cluster, anomaly));
        },
      }),
    );
    dom.replace(clusterHost, dom.skeleton({ rows: 1, label: 'Loading the cluster membership' }));
    const nearbyBlock = dom.block(
      radius ? `Nearby assets — within ${format.formatDistance(radius)}` : 'Nearby assets',
      { note: `Highlighted on the map, with a ring of ${radius ? format.formatDistance(radius) : 'the proximity radius'} around the sensor.` },
      nearbyHost,
    );
    const clusterBlock = dom.block('Cluster membership', {}, clusterHost);

    const chartHost = h('div', {});
    const chart = sensor ? mountSensorChart(chartHost, ctx, sensor, { variant: 'full', around: anomaly }) : null;
    if (!sensor) chartHost.append(dom.emptyState('The sensor of this anomaly is not part of the loaded data'));
    const chartBlock = dom.block('Sensor readings around the event', {}, chartHost);

    target.append(head, summary, evidence, method, chartBlock, nearbyBlock, clusterBlock);

    function tick(t) {
      const status = data.anomalyStatusAt(anomaly, t);
      dom.updatePill(statusPill, status);
      statusNote.textContent = `at ${format.dateTime(data.times[t])} (simulated time)`;
      if (chart) chart.setCursor(t);
    }
    tick(state.t);

    return {
      focusTarget: head.querySelector('button'),
      tick,
      refresh(current, changed) {
        if (changed.has('tab')) tick(current.t);
      },
      destroy() {
        lazies.forEach((region) => region.cancel());
        if (chart) chart.destroy();
      },
    };
  }

  return createListDetailPanel(container, ctx, { kind: 'anomaly', renderList, renderDetail });
}
