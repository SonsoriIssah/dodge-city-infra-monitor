/**
 * Assets tab (build contract 12.1, 12.5).
 *
 * Nothing selected: the "Needs attention" ranking - monitored assets whose Derived Asset Health Score is
 * below the normal band at the selected hour, lowest first.
 *
 * Asset selected: the detail view with two separately titled blocks that never share a row or a colour scale:
 *   - "Recorded attributes"   real data from OpenStreetMap and the FHWA National Bridge Inventory, with the
 *                             retrieval date of each source (`meta.data_sources`);
 *   - "Simulated monitoring"  the Derived Asset Health Score with its four penalty components, the simulated
 *                             sensors with their latest readings, the anomalies, one small chart per sensor
 *                             (small multiples) and the health history.
 * An unmonitored asset shows its recorded attributes and an empty state instead of the second block's content.
 *
 * Panel module interface: see js/panels/tabs.js.
 */

import { BENIGN_EVENT_LABEL, createChart, healthChartConfig, legendRow } from '../ui/chart.js';
import {
  clockChanged,
  createListDetailPanel,
  lazyRegion,
  localHoursOf,
  mountSensorChart,
  sensorTypeLabel,
  stackedRow,
} from './common.js';

export const EMPTY_RANKING = 'All monitored assets are normal at this time';
export const NOT_MONITORED = 'Not monitored — no simulated sensors on this asset';
export const RECORDED_TITLE = 'Recorded attributes — source: ';
export const SIMULATED_TITLE =
  "Simulated monitoring — Derived Asset Health Score, simulated sensors; not an assessment of this structure's real condition";
/** Names used in the title of the recorded block, by `source_id`. */
export const RECORDED_SOURCE_NAME = Object.freeze({ osm: 'OpenStreetMap', nbi: 'FHWA National Bridge Inventory' });
/** One small chart per sensor, at most this many (build contract 12.5). */
export const MAX_SENSOR_CHARTS = 4;
const RECENT_ANOMALIES = 5;

/** Wording of `height_source` (build contract 2). */
const HEIGHT_SOURCE_TEXT = Object.freeze({
  lidar_3dep: 'measured from USGS 3DEP lidar',
  osm_height: 'OSM height tag',
  osm_levels: 'OSM levels',
  estimated: 'estimated',
});

/** Meaning of the NBI condition codes (FHWA Recording and Coding Guide, items 58-62), as text. */
const NBI_CONDITION_TEXT = Object.freeze({
  9: 'Excellent condition',
  8: 'Very good condition',
  7: 'Good condition',
  6: 'Satisfactory condition',
  5: 'Fair condition',
  4: 'Poor condition',
  3: 'Serious condition',
  2: 'Critical condition',
  1: 'Imminent-failure condition',
  0: 'Failed condition',
  N: 'Not applicable',
});

/** Properties that are identity, derived or simulated values - never listed as recorded attributes. */
const NOT_RECORDED = new Set([
  'asset_id',
  'asset_type',
  'category',
  'name',
  'is_simulated',
  'source_id',
  'monitored',
  'sensor_count',
  'sensor_types',
  'anomaly_count',
  'health_score',
  'status',
  'centroid',
  'height_m',
  'height_source',
  'building_type',
  'levels',
  'footprint_m2',
  'highway_class',
  'surface',
  'lanes',
  'length_m',
  'structure_kind',
  'nbi',
  'host_road_id',
]);

const PENALTIES = Object.freeze([
  ['frequency_penalty', 'Anomaly frequency'],
  ['severity_penalty', 'Anomaly severity'],
  ['reading_penalty', 'Unusual readings'],
  ['sensor_penalty', 'Sensors not reporting'],
]);

/** "7 — Good condition" for an NBI condition code; null when the code is missing. */
export function nbiRatingText(code) {
  if (code === null || code === undefined || code === '') return null;
  const key = String(code).trim().toUpperCase();
  const meaning = NBI_CONDITION_TEXT[key];
  if (key === 'N') return 'N — not applicable to this structure';
  return meaning ? `${key} — ${meaning}` : key;
}

/** "8.2 m (measured from USGS 3DEP lidar)" */
export function heightText(properties, formatNumber) {
  if (properties.height_m === null || properties.height_m === undefined) return null;
  const source = HEIGHT_SOURCE_TEXT[properties.height_source] || String(properties.height_source || 'source not recorded');
  return `${formatNumber(properties.height_m, 1)} m (${source})`;
}

/** OSM `addr:*` tags of an asset as [tag, value] pairs, in a stable order. Never composed into an address. */
export function addressTags(properties) {
  return Object.keys(properties)
    .filter((key) => key.startsWith('addr:') && properties[key] !== null && properties[key] !== '')
    .sort()
    .map((key) => [key, String(properties[key])]);
}

/** Sensors charted in the asset detail: those with the most anomalies first, at most `limit`. */
export function sensorsForCharts(sensors, anomalyCountOf, limit = MAX_SENSOR_CHARTS) {
  return sensors
    .slice()
    .sort(
      (a, b) =>
        anomalyCountOf(b.sensor_id) - anomalyCountOf(a.sensor_id) ||
        (a.sensor_id < b.sensor_id ? -1 : a.sensor_id > b.sensor_id ? 1 : 0),
    )
    .slice(0, limit);
}

export function mount(container, ctx) {
  const { data, dom, format, tokens, helpers, actions } = ctx;
  const { h } = dom;
  const { meta } = data;
  const sources = new Map((meta.data_sources || []).map((source) => [source.source_id, source]));
  const typeLabelOf = (properties) => helpers.assetTypeLabel(properties, tokens.ASSET_TYPE_LABEL);
  const asOfText = (t) => `${format.dateTime(data.times[t])} (simulated time)`;

  // ---- "Needs attention" ranking -----------------------------------------------------------------------------
  function renderList(target, initialState) {
    const subtitle = h('p', { class: 'panel-head__sub' });
    const list = h('div', { class: 'list', role: 'group', 'aria-label': 'Assets that need attention, lowest health score first' });
    const footer = h('p', { class: 'figure-caption' });
    target.append(
      h('div', { class: 'panel-head' }, h('div', {}, h('h2', { class: 'panel-head__title', text: 'Needs attention' }), subtitle)),
      list,
      footer,
    );
    let signature = null;

    function draw(t) {
      subtitle.textContent = `Monitored assets with a Derived Asset Health Score below ${data.normalBand}, lowest first · ${asOfText(t)}`;
      const rows = data.needsAttention(t);
      const total = data.attentionCountAt(t);
      const next = rows.map((row) => `${row.assetId}:${row.health}:${row.status}:${row.activeAnomalies}`).join('|');
      if (next === signature) return;
      signature = next;
      const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.id : null;
      if (rows.length === 0) {
        list.replaceChildren(
          dom.emptyState(EMPTY_RANKING, {
            hint: `Every monitored asset has a Derived Asset Health Score of ${data.normalBand} or more at the selected hour.`,
          }),
        );
        footer.textContent = '';
        return;
      }
      list.replaceChildren(
        ...rows.map((row) => {
          const properties = row.asset.properties;
          const name = helpers.assetDisplayName(properties);
          const type = typeLabelOf(properties);
          const anomalies = format.plural(row.activeAnomalies, 'active anomaly', 'active anomalies');
          return stackedRow(dom, {
            id: row.assetId,
            lead: dom.pill('asset', row.status),
            title: name,
            trail: [h('span', { class: 'row__value', text: String(row.health) }), h('span', { class: 'row__unit', text: 'health' })],
            lines: [[properties.name ? `${type} · ${row.assetId}` : type, anomalies]],
            ariaLabel: `${name}, ${type}, ${tokens.labelOf(tokens.ASSET_STATUS_LABEL, row.status)}, health score ${row.health} of 100, ${anomalies}`,
            onClick: () => actions.select('asset', row.assetId),
          });
        }),
      );
      footer.textContent =
        total > rows.length
          ? `Showing the ${rows.length} lowest of ${format.formatInteger(total)} assets below ${data.normalBand}. Health scores are derived from simulated sensors.`
          : `${format.plural(total, 'asset')} below ${data.normalBand} of ${format.formatInteger(meta.counts.monitored_assets)} monitored. Health scores are derived from simulated sensors.`;
      if (focusedId) {
        const again = list.querySelector(`[data-id="${CSS.escape(focusedId)}"]`);
        if (again) again.focus({ preventScroll: true });
      }
    }

    draw(initialState.t);
    return {
      refresh(state, changed) {
        if (clockChanged(changed)) draw(state.t);
      },
      rowFor: (id) => list.querySelector(`[data-id="${CSS.escape(id)}"]`),
    };
  }

  // ---- recorded attributes -----------------------------------------------------------------------------------
  function sourceLine(source) {
    const parts = [];
    if (source.retrieved_at) parts.push(`retrieved ${format.dateLong(source.retrieved_at)}`);
    if (source.vintage) {
      parts.push(/^\d{4}-\d{2}-\d{2}T/.test(source.vintage) ? `data as of ${format.dateLong(source.vintage)}` : source.vintage);
    }
    const name = RECORDED_SOURCE_NAME[source.source_id] || source.name;
    return h('li', {}, dom.sourceKindTag(source.kind), h('span', { text: ` ${name}${parts.length ? ` — ${parts.join(' · ')}` : ''}` }));
  }

  function nbiRows(nbi) {
    const year = (value) => (value ? String(value) : null);
    return [
      ['Structure number', nbi.structure_number],
      ['Facility carried', nbi.facility_carried],
      ['Feature intersected', nbi.features_intersected],
      ['Location (as recorded)', nbi.location],
      ['Year built', year(nbi.year_built)],
      ['Year reconstructed', year(nbi.year_reconstructed) || 'None recorded'],
      [
        'Average daily traffic',
        nbi.adt === null || nbi.adt === undefined
          ? null
          : `${format.formatInteger(nbi.adt)} vehicles per day${nbi.adt_year ? ` (${nbi.adt_year})` : ''}`,
      ],
      ['Deck rating', nbiRatingText(nbi.deck_condition)],
      ['Superstructure rating', nbiRatingText(nbi.superstructure_condition)],
      ['Substructure rating', nbiRatingText(nbi.substructure_condition)],
      ['Culvert rating', nbiRatingText(nbi.culvert_condition)],
      ['FHWA classification', nbi.bridge_condition_label],
      ['Last inspection', nbi.inspection_label],
      ['Owner', nbi.owner || (nbi.owner_code ? `Owner code ${nbi.owner_code}` : null)],
      [
        'Structure length',
        nbi.structure_length_m === null || nbi.structure_length_m === undefined ? null : `${format.formatNumber(nbi.structure_length_m, 1)} m`,
      ],
    ];
  }

  function recordedBlock(properties) {
    if (properties.is_simulated) {
      const host = properties.host_road_id ? data.assetById(properties.host_road_id) : null;
      return dom.block(
        `Simulated asset — ${meta.labels.water_network}`,
        { variant: 'simulated', trailing: dom.simulatedTag() },
        h('p', {
          class: 'block__note',
          text: 'This main does not come from a utility record: it was generated along the centre line of a real street so that simulated pressure sensors have an asset to sit on. It has no recorded attributes.',
        }),
        dom.keyValue([
          [
            'Follows',
            host
              ? dom.linkButton(`${helpers.assetDisplayName(host.properties)} (${properties.host_road_id})`, () => actions.select('asset', properties.host_road_id))
              : properties.host_road_id,
          ],
        ]),
      );
    }

    const sourceIds = [properties.source_id];
    if (properties.nbi && !sourceIds.includes('nbi')) sourceIds.push('nbi');
    const titleNames = sourceIds.map((id) => RECORDED_SOURCE_NAME[id] || (sources.get(id) ? sources.get(id).name : id));
    const noteIds = sourceIds.slice();
    if (properties.height_source === 'lidar_3dep') noteIds.push('usgs_3dep');

    const rows = [['Name', properties.name]];
    if (properties.asset_type === 'building') {
      rows.push(
        ['Building type (OSM tag)', properties.building_type === 'yes' ? 'yes (type not specified)' : properties.building_type],
        ['Levels (OSM tag)', properties.levels],
        [
          'Footprint',
          properties.footprint_m2 === null || properties.footprint_m2 === undefined
            ? null
            : `${format.formatNumber(properties.footprint_m2, 1)} m² (computed from the OSM footprint)`,
        ],
        ['Height', heightText(properties, format.formatNumber)],
      );
    } else if (properties.asset_type === 'road') {
      rows.push(
        ['Highway class', properties.highway_class ? format.humanize(properties.highway_class) : null],
        ['Surface', properties.surface],
        ['Lanes', properties.lanes],
      );
    } else if (properties.asset_type === 'bridge') {
      rows.push(['Structure', properties.structure_kind ? format.humanize(properties.structure_kind) : null]);
    }
    if (properties.length_m !== null && properties.length_m !== undefined) {
      rows.push(['Length', `${format.formatNumber(properties.length_m, 1)} m (computed from the OSM geometry inside the study area)`]);
    }
    for (const [key, value] of Object.entries(properties)) {
      if (NOT_RECORDED.has(key) || key.startsWith('addr:')) continue;
      if (value === null || value === '' || typeof value === 'object') continue;
      rows.push([format.humanize(key), String(value)]);
    }
    const shown = rows.filter(([, value]) => value !== null && value !== undefined && value !== '');

    const children = [
      h('ul', { class: 'source-list' }, noteIds.map((id) => sources.get(id)).filter(Boolean).map(sourceLine)),
      shown.length
        ? dom.keyValue(shown)
        : h('p', { class: 'block__note', text: 'The source records no name or further attributes for this asset.' }),
    ];
    if (properties.nbi) {
      children.push(
        h('h4', { class: 'block__subtitle', text: 'FHWA National Bridge Inventory record' }),
        dom.keyValue(nbiRows(properties.nbi)),
        h('p', {
          class: 'block__note',
          text: 'Component ratings are recorded inspection results on the FHWA scale 0–9 (9 = excellent, N = not applicable). They are shown as recorded and are not used by the Derived Asset Health Score, the status colours or the risk zones.',
        }),
      );
    }
    return dom.block(`${RECORDED_TITLE}${titleNames.join(' / ')}`, { variant: 'recorded' }, children);
  }

  // ---- simulated monitoring ----------------------------------------------------------------------------------
  function simulatedBlock(feature, state, view) {
    const properties = feature.properties;
    const assetId = properties.asset_id;
    const sensors = data.sensorsByAsset(assetId);
    if (!properties.monitored || sensors.length === 0) {
      return dom.block(
        SIMULATED_TITLE,
        { variant: 'simulated' },
        dom.emptyState(NOT_MONITORED, {
          hint: 'This asset is part of the recorded data only. Without simulated sensors it has no readings, no anomalies and no Derived Asset Health Score.',
        }),
      );
    }

    const bands = meta.health.bands;
    const playbackEntry = data.playback.assets[assetId];
    const health = playbackEntry ? playbackEntry.health : [];
    let history = null;

    // Health score at t.
    const scoreValue = h('span', { class: 'health__value' });
    const statusPill = dom.pill('asset', data.assetStatusAt(assetId, state.t));
    const scoreAsOf = h('span', { class: 'health__asof' });
    const equation = h('p', { class: 'health__equation' });
    const penaltyValues = PENALTIES.map(() => h('dd', { class: 'num' }));
    const penaltyList = h(
      'dl',
      { class: 'kv kv--penalties' },
      PENALTIES.map(([, label], index) => [h('dt', { text: label }), penaltyValues[index]]),
    );
    const penaltyHost = h('div', { class: 'health__penalties' });

    function drawPenalties(t) {
      if (!history) return;
      const values = PENALTIES.map(([key]) => history[key][t]);
      values.forEach((value, index) => {
        penaltyValues[index].textContent = value === null || value === undefined ? '—' : `− ${Number(value).toFixed(1)}`;
      });
      const score = history.health_score[t];
      equation.textContent =
        score === null || score === undefined
          ? ''
          : `100 − ${values.map((value) => Number(value).toFixed(1)).join(' − ')} = ${format.formatInteger(score)} (rounded, kept between 0 and 100)`;
    }

    view.lazies.push(
      lazyRegion(penaltyHost, dom, {
        label: 'Loading the health score components',
        errorTitle: 'The health score components could not be loaded.',
        skeleton: { rows: 2 },
        load: () => ctx.provider.getAssetHealth(assetId),
        render(value, target) {
          history = value;
          target.append(penaltyList, equation);
          drawPenalties(ctx.store.getState().t);
          if (view.spark) {
            view.spark.update(
              healthChartConfig({
                health,
                history,
                data,
                localHours: localHoursOf(data),
                cursor: ctx.store.getState().t,
                atRiskBelow: meta.health.at_risk_below,
                assetName: helpers.assetDisplayName(properties),
              }),
            );
          }
        },
      }),
    );

    const sparkHost = h('div', {});
    view.spark = createChart(
      sparkHost,
      healthChartConfig({
        health,
        data,
        localHours: localHoursOf(data),
        cursor: state.t,
        atRiskBelow: meta.health.at_risk_below,
        assetName: helpers.assetDisplayName(properties),
      }),
    );

    // Sensors and their latest readings at t.
    const sensorSummary = h('p', { class: 'block__note' });
    const readingRows = sensors.map((sensor) => {
      const value = h('span', { class: 'row__value' });
      const pill = dom.pill('sensor', data.sensorStatusAt(sensor.sensor_id, state.t));
      const typeLabel = sensorTypeLabel(meta, sensor.sensor_type);
      const row = dom.rowButton({
        leading: dom.sensorGlyph(sensor.sensor_type, typeLabel),
        title: sensor.sensor_id,
        subtitle: `${typeLabel} · ${format.humanize(sensor.placement)}`,
        trailing: [value, pill],
        dataset: { id: sensor.sensor_id },
        onClick: () => actions.select('sensor', sensor.sensor_id),
      });
      row.title = `Open sensor ${sensor.sensor_id}`;
      return { sensor, value, pill, row };
    });

    // Anomalies of this asset up to t.
    const anomalies = data.anomaliesByAsset(assetId);
    const anomalySummary = h('p', { class: 'block__note' });
    const anomalyList = h('div', { class: 'list' });
    let anomalySignature = null;

    function drawAnomalies(t) {
      const started = anomalies.filter((anomaly) => anomaly.start_idx <= t);
      const active = started.filter((anomaly) => helpers.isActiveAt(anomaly, t));
      anomalySummary.textContent =
        `${format.plural(started.length, 'anomaly', 'anomalies')} detected up to this time` +
        ` · ${active.length} active` +
        (anomalies.length > started.length ? ` · ${anomalies.length} in the whole simulation` : '');
      const recent = started
        .slice()
        .sort((a, b) => b.start_idx - a.start_idx || (a.anomaly_id < b.anomaly_id ? 1 : -1))
        .slice(0, RECENT_ANOMALIES);
      const next = recent.map((anomaly) => `${anomaly.anomaly_id}:${data.anomalyStatusAt(anomaly, t)}`).join('|');
      if (next === anomalySignature) return;
      anomalySignature = next;
      if (recent.length === 0) {
        anomalyList.replaceChildren(dom.emptyState('No anomalies on this asset up to the selected time'));
        return;
      }
      anomalyList.replaceChildren(
        ...recent.map((anomaly) =>
          stackedRow(dom, {
            id: anomaly.anomaly_id,
            lead: dom.pill('severity', anomaly.severity),
            title: anomaly.anomaly_label,
            trail: dom.pill('anomaly-status', data.anomalyStatusAt(anomaly, t)),
            lines: [[`${anomaly.sensor_id} · started ${format.dateTimeShort(anomaly.started_at)}`, dom.simulatedTag()]],
            onClick: () => actions.select('anomaly', anomaly.anomaly_id),
          }),
        ),
      );
    }

    // One small chart per sensor (small multiples; each has its own value axis, never a second axis).
    const charted = sensorsForCharts(sensors, (sensorId) => data.anomaliesBySensor(sensorId).length);
    const chartedTypes = new Set(charted.map((sensor) => sensor.sensor_type));
    const legend = [
      { swatch: 'observed', label: 'Observed' },
      { swatch: 'expected', label: 'Expected' },
      { swatch: 'band', label: 'Expected range' },
    ];
    if (charted.some((sensor) => data.anomaliesBySensor(sensor.sensor_id).length > 0)) legend.push({ swatch: 'anomaly', label: 'Anomaly window' });
    if (data.simulationEvents.some((event) => !event.is_anomaly && chartedTypes.has(event.sensor_type))) {
      legend.push({ swatch: 'event', label: BENIGN_EVENT_LABEL });
    }
    const chartHosts = charted.map((sensor) => {
      const host = h('div', { class: 'small-multiple' });
      view.charts.push(mountSensorChart(host, ctx, sensor, { variant: 'compact', legend: false }));
      return host;
    });

    function tick(t) {
      const score = data.assetHealthAt(assetId, t);
      scoreValue.textContent = score === null ? '—' : String(score);
      dom.updatePill(statusPill, data.assetStatusAt(assetId, t));
      scoreAsOf.textContent = `at ${asOfText(t)}`;
      drawPenalties(t);
      let reporting = 0;
      for (const entry of readingRows) {
        const status = data.sensorStatusAt(entry.sensor.sensor_id, t);
        if (status !== 'offline') reporting += 1;
        entry.value.textContent = format.formatValue(data.sensorValueAt(entry.sensor.sensor_id, t), entry.sensor.unit);
        dom.updatePill(entry.pill, status);
      }
      sensorSummary.textContent = `${format.plural(sensors.length, 'simulated sensor')} on this asset · ${reporting} reporting at this time`;
      view.spark.setCursor(t);
      for (const chart of view.charts) chart.setCursor(t);
    }
    view.tick = tick;
    view.slow = drawAnomalies;
    tick(state.t);
    drawAnomalies(state.t);

    return dom.block(
      SIMULATED_TITLE,
      { variant: 'simulated' },
      h(
        'div',
        { class: 'health' },
        h('div', { class: 'health__score' }, scoreValue, h('span', { class: 'health__of', text: '/ 100' })),
        h('div', { class: 'health__meta' }, statusPill, scoreAsOf),
      ),
      h('p', {
        class: 'block__note',
        text: `Derived Asset Health Score. Bands: Normal ≥ ${bands.normal} · Watch ≥ ${bands.watch} · At risk ≥ ${bands.at_risk} · Critical below ${bands.at_risk}.`,
      }),
      h('h4', { class: 'block__subtitle', text: 'Penalty components at this time' }),
      penaltyHost,
      sparkHost,
      h('h4', { class: 'block__subtitle', text: 'Sensors and latest readings' }),
      sensorSummary,
      h('div', { class: 'list' }, readingRows.map((entry) => entry.row)),
      h('h4', { class: 'block__subtitle', text: 'Anomalies' }),
      anomalySummary,
      anomalyList,
      h('h4', { class: 'block__subtitle', text: 'Sensor history' }),
      sensors.length > charted.length
        ? h('p', {
            class: 'block__note',
            text: `Showing ${charted.length} of ${sensors.length} sensors, those with the most anomalies first. Open a sensor above for its full chart.`,
          })
        : null,
      legendRow(legend),
      chartHosts,
    );
  }

  // ---- detail ----------------------------------------------------------------------------------------------
  function renderDetail(target, assetId, state) {
    const feature = data.assetById(assetId);
    const view = { lazies: [], charts: [], spark: null, tick: null, slow: null };
    if (!feature) {
      target.append(
        dom.panelHead({ title: assetId, onBack: () => actions.clearSelection(), backLabel: 'Back to the ranking' }),
        dom.emptyState('This asset is not part of the loaded data'),
      );
      return { refresh() {}, focusTarget: target.querySelector('button') };
    }
    const properties = feature.properties;
    const name = helpers.assetDisplayName(properties);
    const [lon, lat] = properties.centroid || [];
    const tags = addressTags(properties);

    const head = dom.panelHead({
      title: name,
      subtitle: `${typeLabelOf(properties)} · ${properties.category}`,
      trailing: properties.is_simulated ? dom.simulatedTag() : null,
      onBack: () => actions.clearSelection(),
      backLabel: 'Back to the ranking',
    });
    const identity = dom.keyValue([
      ['Asset ID', properties.asset_id],
      ['Asset type', typeLabelOf(properties)],
      ['Location', `${format.formatLatLon(lat, lon)} (centroid)`],
      [
        'OSM address tags',
        tags.length ? h('ul', { class: 'tag-list' }, tags.map(([key, value]) => h('li', {}, h('code', { text: key }), ` = ${value}`))) : null,
      ],
    ]);
    target.append(head, identity, recordedBlock(properties), simulatedBlock(feature, state, view));

    return {
      focusTarget: head.querySelector('button'),
      tick(t) {
        if (view.tick) view.tick(t);
      },
      refresh(current, changed) {
        if (!clockChanged(changed)) return;
        if (changed.has('tab') && view.tick) view.tick(current.t);
        if (view.slow) view.slow(current.t);
      },
      destroy() {
        view.lazies.forEach((region) => region.cancel());
        view.charts.forEach((chart) => chart.destroy());
        if (view.spark) view.spark.destroy();
      },
    };
  }

  return createListDetailPanel(container, ctx, { kind: 'asset', renderList, renderDetail });
}
