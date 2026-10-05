/**
 * Statistics cards and the one-line status sentence (build contract 12.1, 12.2; brief R11).
 *
 * Every figure comes from loaded data: the time-varying ones from `playback.stats[t]`, the totals from
 * `meta.counts`. Second subscriber of a tick (order 20). Clicking Active Anomalies, Critical Alerts or
 * Assets at Risk opens the matching tab with the matching filter, through the store.
 */

import { h } from '../ui/dom.js';
import { formatInteger, plural } from '../ui/format.js';

const TICK_ORDER = 20;

/**
 * @param {{strip: HTMLElement, sentence: HTMLElement}} elements
 * @param {{store: object, data: object, actions: object}} ctx
 */
export function mountKpis(elements, { store, data, actions }) {
  const { meta, format } = data;
  const atRiskBelow = meta.health.at_risk_below;
  const normalBand = data.normalBand;

  const cards = [
    {
      key: 'total_assets',
      label: 'Total Assets',
      sub: (s) => `${formatInteger(s.monitored_assets)} monitored · ${formatInteger(s.simulated_assets)} simulated`,
      title: () => `${formatInteger(meta.counts.real_assets)} real assets from public geographic data and ${formatInteger(meta.counts.simulated_assets)} simulated assets (the simulated water network)`,
      tone: () => null,
      onClick: null,
    },
    {
      key: 'active_sensors',
      label: 'Active Sensors',
      sub: (s) => `simulated · ${formatInteger(s.offline_sensors)} offline`,
      title: (s) => `${formatInteger(s.active_sensors)} of ${formatInteger(s.total_sensors)} simulated sensors reported at this hour. Open the Sensors tab.`,
      tone: () => null,
      onClick: () => actions.showSensors(),
    },
    {
      key: 'active_anomalies',
      label: 'Active Anomalies',
      sub: () => 'prototype detection',
      title: () => 'Anomalies active at this hour (Prototype Anomaly Detection). Open the Anomalies tab, filtered to active.',
      tone: (s) => (s.active_anomalies > 0 ? 'attention' : null),
      onClick: () => actions.showAnomalies({ status: 'active' }),
    },
    {
      key: 'critical_alerts',
      label: 'Critical Alerts',
      sub: () => 'severity = critical',
      title: () => 'Active anomalies with severity critical. Open the Anomalies tab, filtered to active and critical.',
      tone: (s) => (s.critical_alerts > 0 ? 'critical' : null),
      onClick: () => actions.showAnomalies({ status: 'active', severity: ['critical'] }),
    },
    {
      key: 'assets_at_risk',
      label: 'Assets at Risk',
      sub: () => `derived health score < ${atRiskBelow}`,
      title: () => `Monitored assets whose Derived Asset Health Score is below ${atRiskBelow} at this hour. Open the Assets tab ranking.`,
      tone: (s) => (s.assets_at_risk > 0 ? 'warning' : null),
      onClick: () => actions.showAssetsRanking(),
    },
  ];

  const nodes = cards.map((card) => {
    const value = h('span', { class: 'kpi__value' });
    const sub = h('span', { class: 'kpi__sub' });
    const body = [value, h('span', { class: 'kpi__text' }, h('span', { class: 'kpi__label', text: card.label }), sub)];
    const element = card.onClick
      ? h('button', { class: 'kpi', type: 'button', dataset: { kpi: card.key }, on: { click: card.onClick } }, body)
      : h('div', { class: 'kpi', dataset: { kpi: card.key } }, body);
    return { card, element, value, sub };
  });
  elements.strip.replaceChildren(...nodes.map((node) => node.element));
  elements.strip.removeAttribute('aria-busy');

  const sentenceTime = h('strong');
  const sentenceAnomalies = h('strong');
  const sentenceAttention = h('strong');
  const sentenceAssets = document.createTextNode('');
  elements.sentence.replaceChildren(
    'As of ',
    sentenceTime,
    ' (simulated time): ',
    sentenceAnomalies,
    sentenceAssets,
    ' · ',
    sentenceAttention,
    // The threshold is visible, so the count cannot be read against "Assets at Risk" (a lower threshold).
    ` (derived health score < ${normalBand})`,
  );
  elements.sentence.title = `"Need attention" counts monitored assets whose Derived Asset Health Score is below ${normalBand} at this hour.`;

  function render(t) {
    const stats = data.statsAt(t);
    for (const node of nodes) {
      const text = formatInteger(stats[node.card.key]);
      if (node.value.textContent !== text) node.value.textContent = text;
      const sub = node.card.sub(stats);
      if (node.sub.textContent !== sub) node.sub.textContent = sub;
      const tone = node.card.tone(stats);
      if (tone) node.element.dataset.tone = tone;
      else delete node.element.dataset.tone;
      node.element.title = node.card.title(stats);
      if (node.card.onClick) {
        node.element.setAttribute('aria-label', `${node.card.label}: ${text} (${sub}). ${node.card.title(stats)}`);
      }
    }
    const assetsWithAnomalies = data.assetsWithActiveAnomalies(t);
    const attention = data.attentionCountAt(t);
    sentenceTime.textContent = format.dateTime(data.times[stats.index]);
    sentenceAnomalies.textContent = plural(stats.active_anomalies, 'active anomaly', 'active anomalies');
    sentenceAssets.textContent = ` on ${plural(assetsWithAnomalies, 'asset')}`;
    sentenceAttention.textContent = attention === 1 ? '1 asset needs attention' : `${formatInteger(attention)} assets need attention`;
  }

  // While playing, the sentence is not announced every hour.
  function renderAnnouncements(playing) {
    elements.sentence.setAttribute('aria-live', playing ? 'off' : 'polite');
  }

  const unsubscribe = [
    store.subscribe(['t'], (state) => render(state.t), { order: TICK_ORDER }),
    store.subscribe(['playing'], (state) => renderAnnouncements(state.playing), { order: TICK_ORDER }),
  ];
  render(store.getState().t);
  renderAnnouncements(store.getState().playing);

  return {
    destroy() {
      unsubscribe.forEach((off) => off());
    },
  };
}
