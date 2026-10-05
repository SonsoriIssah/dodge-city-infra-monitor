/**
 * Sensors tab (build contract 12.5): "Simulated Sensor Data".
 *
 * List: every simulated sensor with its value, unit and status at the selected hour, narrowed by sensor type
 * and by asset. The order is fixed (by sensor id), so rows never jump while the clock runs: only the value
 * and the status pill of a row change.
 *
 * Detail: latest reading at t, trend against the reading 24 h earlier (with a dead-band so that noise is not
 * called a trend), the thresholds of the sensor's placement, its anomalies, and the full history chart.
 *
 * Panel module interface: see js/panels/tabs.js.
 */

import { TREND_LOOKBACK_HOURS, trendDeadband, trendOf } from '../data/index.js';
import { CHART_CAPTION } from '../ui/chart.js';
import { assetNameWithId, assetOptions, clockChanged, createListDetailPanel, mountSensorChart, sensorTypeLabel, stackedRow } from './common.js';

export const SUBTITLE = CHART_CAPTION;
export const EMPTY_LIST = 'No sensors match this sensor type and asset';
const TREND_ARROW = Object.freeze({ rising: '▲', falling: '▼', steady: '▬' });
const THRESHOLD_ROWS = Object.freeze([
  ['crit_high', 'Critical above'],
  ['warn_high', 'Warning above'],
  ['warn_low', 'Warning below'],
  ['crit_low', 'Critical below'],
]);

/**
 * Trend sentence parts for a sensor at hour t.
 * @returns {{arrow: string, word: string, detail: string}} word: 'rising' | 'falling' | 'steady' | 'no trend'
 */
export function describeTrend({ current, previous, unit, deadband, formatDelta, formatValue }) {
  const trend = trendOf(current, previous, deadband);
  if (!trend) {
    const missing = current === null || current === undefined ? 'no reading at this time' : `no reading ${TREND_LOOKBACK_HOURS} h earlier`;
    return { arrow: '–', word: 'no trend', detail: missing };
  }
  return {
    arrow: TREND_ARROW[trend.direction],
    word: trend.direction,
    detail: `${formatDelta(trend.delta, unit)} against ${TREND_LOOKBACK_HOURS} h earlier (${formatValue(previous, unit)})`,
  };
}

export function mount(container, ctx) {
  const { store, data, dom, format, tokens, helpers, actions } = ctx;
  const { h } = dom;
  const { meta } = data;
  const lookback = Math.round((TREND_LOOKBACK_HOURS * 60) / data.stepMinutes);
  const runParams = meta.detection_run ? meta.detection_run.params : null;
  // The two filters of this tab live here: they survive a visit to a sensor's detail and a tab change.
  const filters = { sensorType: 'all', assetId: '' };
  const typeNames = Object.keys(meta.sensor_types);

  // ---- list ------------------------------------------------------------------------------------------------
  function renderList(target, initialState) {
    const typeSelect = dom.select({
      label: 'Sensor type',
      options: [{ value: 'all', label: `All types (${typeNames.length})` }, ...typeNames.map((type) => ({ value: type, label: sensorTypeLabel(meta, type) }))],
      value: filters.sensorType,
      onChange: (value) => {
        filters.sensorType = value;
        rebuild(store.getState().t);
      },
    });
    const assetSelect = dom.select({
      label: 'Asset',
      options: assetOptions(data.monitoredAssets, 'All monitored assets'),
      value: filters.assetId,
      onChange: (value) => {
        filters.assetId = value;
        rebuild(store.getState().t);
      },
    });
    const summary = h('p', { class: 'figure-caption', role: 'status' });
    const list = h('div', { class: 'list', role: 'group', 'aria-label': 'Simulated sensors' });
    target.append(
      dom.panelHead({ title: 'Sensors', subtitle: SUBTITLE, trailing: dom.simulatedTag() }),
      h('form', { class: 'filters', 'aria-label': 'Sensor filters', on: { submit: (event) => event.preventDefault() } }, h('div', { class: 'filters__row' }, typeSelect, assetSelect)),
      summary,
      list,
    );

    let rows = [];

    function drawValues(t) {
      const counts = { normal: 0, warning: 0, anomaly: 0, offline: 0 };
      for (const row of rows) {
        const status = data.sensorStatusAt(row.sensor.sensor_id, t);
        counts[status] = (counts[status] || 0) + 1;
        const text = format.formatValue(data.sensorValueAt(row.sensor.sensor_id, t), row.sensor.unit);
        if (row.value.textContent !== text) row.value.textContent = text;
        dom.updatePill(row.pill, status);
      }
      summary.textContent =
        rows.length === 0
          ? ''
          : `${format.plural(rows.length, 'sensor')} · ${tokens.SENSOR_STATUS_ORDER.filter((status) => counts[status] > 0)
              .map((status) => `${counts[status]} ${tokens.labelOf(tokens.SENSOR_STATUS_LABEL, status).toLowerCase()}`)
              .join(' · ')} · ${format.dateTime(data.times[t])} (simulated time)`;
    }

    function rebuild(t) {
      const sensors = helpers.filterSensors(data.sensors, filters);
      if (sensors.length === 0) {
        rows = [];
        list.replaceChildren(
          dom.emptyState(EMPTY_LIST, {
            action: {
              label: 'Show all sensors',
              onClick: () => {
                filters.sensorType = 'all';
                filters.assetId = '';
                typeSelect.control.value = 'all';
                assetSelect.control.value = '';
                rebuild(store.getState().t);
              },
            },
          }),
        );
        summary.textContent = '';
        return;
      }
      rows = sensors.map((sensor) => {
        const value = h('span', { class: 'row__value' });
        const pill = dom.pill('sensor', data.sensorStatusAt(sensor.sensor_id, t));
        const typeLabel = sensorTypeLabel(meta, sensor.sensor_type);
        const button = dom.rowButton({
          leading: dom.sensorGlyph(sensor.sensor_type, typeLabel),
          title: sensor.sensor_id,
          subtitle: `${sensor.asset_name || sensor.asset_id} · ${format.humanize(sensor.placement).toLowerCase()}`,
          trailing: [value, pill],
          dataset: { id: sensor.sensor_id },
          onClick: () => actions.select('sensor', sensor.sensor_id),
        });
        button.title = `${sensor.sensor_id} — ${typeLabel} sensor on ${sensor.asset_name || sensor.asset_id}`;
        return { sensor, value, pill, button };
      });
      list.replaceChildren(...rows.map((row) => row.button));
      drawValues(t);
    }

    rebuild(initialState.t);
    return {
      refresh(state, changed) {
        if (clockChanged(changed)) drawValues(state.t);
      },
      rowFor: (id) => list.querySelector(`[data-id="${CSS.escape(id)}"]`),
    };
  }

  // ---- detail ----------------------------------------------------------------------------------------------
  function renderDetail(target, sensorId, state) {
    const sensor = data.sensorById(sensorId);
    const back = () => actions.clearSelection();
    if (!sensor) {
      target.append(dom.panelHead({ title: sensorId, subtitle: SUBTITLE, onBack: back }), dom.emptyState('This sensor is not part of the loaded data'));
      return { refresh() {}, focusTarget: target.querySelector('button') };
    }
    const typeLabel = sensorTypeLabel(meta, sensor.sensor_type);
    const placement = meta.sensor_types[sensor.sensor_type] ? meta.sensor_types[sensor.sensor_type].placements[sensor.placement] : null;
    const asset = data.assetById(sensor.asset_id);
    const deadband = trendDeadband(sensor.sensor_type, runParams);
    const values = data.playback.sensors[sensor.sensor_id] ? data.playback.sensors[sensor.sensor_id].values : [];
    const valueAt = (index) => (index >= 0 && index < values.length && values[index] !== undefined ? values[index] : null);

    const head = dom.panelHead({
      title: sensor.sensor_id,
      subtitle: `${typeLabel} sensor · ${format.humanize(sensor.placement).toLowerCase()} · ${SUBTITLE}`,
      trailing: dom.simulatedTag(),
      onBack: back,
    });

    // Latest reading and trend at t.
    const latestValue = h('span', { class: 'reading__value' });
    const latestPill = dom.pill('sensor', data.sensorStatusAt(sensor.sensor_id, state.t));
    const latestAsOf = h('span', { class: 'health__asof' });
    const latestNote = h('p', { class: 'block__note' });
    const trendArrow = h('span', { class: 'trend__arrow', 'aria-hidden': 'true' });
    const trendWord = h('strong', { class: 'trend__word' });
    const trendDetail = h('span', { class: 'trend__detail' });
    const deadbandText = deadband.relative
      ? `Steady means a change of less than about ${format.formatPercent(Math.expm1(deadband.deadband), 0)} — the noise floor the detection run uses for ${typeLabel.toLowerCase()} sensors.`
      : `Steady means a change of no more than ${format.formatValue(deadband.deadband, sensor.unit)} — the noise floor the detection run uses for ${typeLabel.toLowerCase()} sensors.`;

    const latestBlock = dom.block(
      'Latest reading and trend',
      { trailing: dom.simulatedTag() },
      h('div', { class: 'reading' }, latestValue, h('div', { class: 'health__meta' }, latestPill, latestAsOf)),
      latestNote,
      h('p', { class: 'trend', title: deadbandText }, trendArrow, ' ', trendWord, ' ', trendDetail),
      h('p', { class: 'block__note', text: deadbandText }),
    );

    // Thresholds of the placement, and the sensor's identity.
    const thresholdRows = THRESHOLD_ROWS.map(([key, label]) => [
      label,
      placement && placement[key] !== null && placement[key] !== undefined ? format.formatValue(placement[key], sensor.unit) : null,
    ]);
    const hasThresholds = thresholdRows.some(([, value]) => value !== null);
    const thresholdBlock = dom.block(
      `Thresholds — ${format.humanize(sensor.placement).toLowerCase()} placement`,
      { note: placement && placement.description ? placement.description : null },
      hasThresholds ? dom.keyValue(thresholdRows) : dom.emptyState('No thresholds are defined for this placement'),
    );
    const identityBlock = dom.block(
      'Sensor',
      {},
      dom.keyValue([
        ['Sensor ID', sensor.sensor_id],
        ['Sensor type', `${typeLabel} (${sensor.unit})`],
        ['Placement', format.humanize(sensor.placement)],
        [
          'Asset',
          dom.linkButton(asset ? assetNameWithId(asset.properties) : sensor.asset_id, () => actions.select('asset', sensor.asset_id), { title: 'Open this asset' }),
        ],
        ['Description', sensor.description],
        ['Location', format.formatLatLon(sensor.lat, sensor.lon)],
        ['Data source', sensor.is_simulated ? `Simulated (${sensor.source})` : sensor.source],
        ['Sampling', `every ${format.formatDuration(data.stepMinutes / 60)}`],
      ]),
    );

    // Anomalies of this sensor up to t.
    const anomalies = data.anomaliesBySensor(sensor.sensor_id);
    const anomalySummary = h('p', { class: 'block__note' });
    const anomalyList = h('div', { class: 'list' });
    let anomalySignature = null;
    function drawAnomalies(t) {
      const started = anomalies.filter((anomaly) => anomaly.start_idx <= t).sort((a, b) => b.start_idx - a.start_idx);
      const active = started.filter((anomaly) => helpers.isActiveAt(anomaly, t)).length;
      anomalySummary.textContent = `${format.plural(started.length, 'anomaly', 'anomalies')} detected up to this time · ${active} active`;
      const next = started.map((anomaly) => `${anomaly.anomaly_id}:${data.anomalyStatusAt(anomaly, t)}`).join('|') || 'empty';
      if (next === anomalySignature) return;
      anomalySignature = next;
      if (started.length === 0) {
        anomalyList.replaceChildren(dom.emptyState('No anomalies on this sensor up to the selected time'));
        return;
      }
      anomalyList.replaceChildren(
        ...started.map((anomaly) =>
          stackedRow(dom, {
            id: anomaly.anomaly_id,
            lead: dom.pill('severity', anomaly.severity),
            title: anomaly.anomaly_label,
            trail: dom.pill('anomaly-status', data.anomalyStatusAt(anomaly, t)),
            lines: [[`Started ${format.dateTimeShort(anomaly.started_at)} · ${format.formatDuration(anomaly.duration_hours)}`, dom.simulatedTag()]],
            onClick: () => actions.select('anomaly', anomaly.anomaly_id),
          }),
        ),
      );
    }
    const anomalyBlock = dom.block('Anomalies on this sensor', {}, anomalySummary, anomalyList);

    const chartHost = h('div', {});
    const chart = mountSensorChart(chartHost, ctx, sensor, { variant: 'full' });
    const chartBlock = dom.block('History — whole simulation', {}, chartHost);

    target.append(head, latestBlock, chartBlock, thresholdBlock, anomalyBlock, identityBlock);

    function tick(t) {
      const current = valueAt(t);
      latestValue.textContent = format.formatValue(current, sensor.unit);
      dom.updatePill(latestPill, data.sensorStatusAt(sensor.sensor_id, t));
      latestAsOf.textContent = `at ${format.dateTime(data.times[t])} (simulated time)`;
      if (current === null) {
        let last = t - 1;
        while (last >= 0 && valueAt(last) === null) last -= 1;
        latestNote.textContent =
          last >= 0
            ? `No reading at this time. Last reading: ${format.formatValue(valueAt(last), sensor.unit)} at ${format.dateTime(data.times[last])}.`
            : 'No reading at this time, and none earlier in the simulation.';
      } else {
        latestNote.textContent = '';
      }
      latestNote.hidden = current !== null;
      const trend = describeTrend({
        current,
        previous: valueAt(t - lookback),
        unit: sensor.unit,
        deadband,
        formatDelta: format.formatDelta,
        formatValue: format.formatValue,
      });
      trendArrow.textContent = trend.arrow;
      trendWord.textContent = trend.word.charAt(0).toUpperCase() + trend.word.slice(1);
      trendDetail.textContent = `— ${trend.detail}`;
      chart.setCursor(t);
    }
    tick(state.t);
    drawAnomalies(state.t);

    return {
      focusTarget: head.querySelector('button'),
      tick,
      refresh(current, changed) {
        if (!clockChanged(changed)) return;
        if (changed.has('tab')) tick(current.t);
        drawAnomalies(current.t);
      },
      destroy() {
        chart.destroy();
      },
    };
  }

  return createListDetailPanel(container, ctx, { kind: 'sensor', renderList, renderDetail });
}
