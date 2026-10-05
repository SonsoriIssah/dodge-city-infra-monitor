/**
 * "How to read this dashboard" card (build contract 12.3; brief R23).
 *
 * Non-modal card over the map with the five answers a first-time viewer needs: WHAT, WHERE, WHAT IS
 * MONITORED, WHAT IS HAPPENING, WHAT NEEDS ATTENTION. Shown on the first visit, closed with Esc or its
 * button, re-opened from the "?" button in the header. Each answer is also readable from persistent UI
 * (title, subtitle, KPI captions, status sentence, Assets tab ranking); the card only gathers them.
 * Every number in it comes from /meta.
 */

import { h, icon } from '../ui/dom.js';
import { formatInteger } from '../ui/format.js';

const STORAGE_KEY = 'dcim.intro.seen';
const SOURCE_SHORT_NAME = Object.freeze({
  osm: 'OpenStreetMap',
  nbi: 'National Bridge Inventory',
  usgs_3dep: 'USGS 3DEP lidar',
  tiger: 'U.S. Census Bureau',
});

/** True when the card has been closed before on this browser (storage may be unavailable: then never). */
export function introSeen() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function rememberSeen() {
  try {
    window.localStorage.setItem(STORAGE_KEY, '1');
  } catch {
    // Private mode or blocked storage: the card simply shows again on the next visit.
  }
}

/**
 * @param {HTMLElement} card
 * @param {{store: object, data: object}} ctx
 */
export function mountIntro(card, { store, data }) {
  const { meta } = data;
  const counts = meta.counts;
  const sensorTypes = Object.values(meta.sensor_types).map((type) => type.label.toLowerCase());
  const typeList = sensorTypes.length > 1 ? `${sensorTypes.slice(0, -1).join(', ')} and ${sensorTypes[sensorTypes.length - 1]}` : sensorTypes.join('');
  const days = Math.round((meta.time.count * meta.time.step_minutes) / (60 * 24));
  const atRiskBelow = meta.health.at_risk_below;

  // Real geographic sources actually loaded (display-only services are left out).
  const preferred = Object.keys(SOURCE_SHORT_NAME);
  const rank = (source) => (preferred.includes(source.source_id) ? preferred.indexOf(source.source_id) : preferred.length);
  const sourceNames = meta.data_sources
    .filter((source) => source.kind === 'real' && source.notes !== 'display only')
    .sort((a, b) => rank(a) - rank(b))
    .map((source) => SOURCE_SHORT_NAME[source.source_id] || source.name.split(':')[0]);
  const sourceList = sourceNames.length ? ` (${sourceNames.join(', ')})` : '';

  const answers = [
    ['WHAT', '3D Urban Infrastructure Monitoring — a research prototype that joins GIS data, a 3D city view, a PostGIS database, simulated sensors and anomaly detection.'],
    ['WHERE', `${meta.study_area.name}. Buildings, roads and bridges are real geographic data${sourceList}.`],
    ['WHAT IS MONITORED', `${formatInteger(counts.monitored_assets)} of ${formatInteger(counts.assets)} infrastructure assets carry ${formatInteger(counts.sensors)} simulated sensors (${typeList}). ${meta.labels.sensor_data} — none is a physical device.`],
    ['WHAT IS HAPPENING', `${formatInteger(days)} days of hourly readings are analysed for anomalies (${meta.labels.detection}). Press Play or drag the timeline: the figures and the map follow the simulated clock.`],
    ['WHAT NEEDS ATTENTION', `Orange and red assets, anomaly rings on the map, and the ranking in the Assets tab — lowest ${meta.labels.health} first. "Assets at Risk" counts scores below ${atRiskBelow}.`],
  ];

  const closeButton = h('button', { class: 'btn btn--icon btn--ghost', type: 'button', 'aria-label': 'Close this card', title: 'Close (Esc)', on: { click: close } }, icon('close'));
  card.setAttribute('role', 'region');
  card.setAttribute('aria-labelledby', 'intro-title');
  card.replaceChildren(
    h('div', { class: 'intro-card__head' }, h('h2', { class: 'intro-card__title', id: 'intro-title', text: 'How to read this dashboard' }), closeButton),
    h(
      'dl',
      { class: 'intro-list' },
      answers.map(([question, answer]) => [h('dt', { text: question }), h('dd', { text: answer })]),
    ),
    h(
      'div',
      { class: 'intro-card__foot' },
      h('span', {}, h('kbd', { text: 'Space' }), ' play or pause · ', h('kbd', { text: '←' }), ' ', h('kbd', { text: '→' }), ' one hour · ', h('kbd', { text: 'Esc' }), ' close'),
      h('button', { class: 'btn btn--small btn--primary', type: 'button', on: { click: close }, text: 'Got it' }),
    ),
  );

  function close() {
    store.set({ introOpen: false });
  }

  function render(open) {
    card.hidden = !open;
    const trigger = document.getElementById('btn-intro');
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) rememberSeen();
  }

  const unsubscribe = store.subscribe(['introOpen'], (state) => render(state.introOpen));
  const open = store.getState().introOpen;
  card.hidden = !open;
  const trigger = document.getElementById('btn-intro');
  if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');

  return {
    destroy() {
      unsubscribe();
    },
  };
}
