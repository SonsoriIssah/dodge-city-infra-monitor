/**
 * Time playback (build contract 12.7, brief R12).
 *
 * Controls, in order: first hour · back one hour · Play/Pause · forward one hour · "End of simulation" ·
 * speed (6, 12, 24 simulated hours per second) · date and hour pickers · slider over a histogram of the
 * active anomalies per hour (critical ones stacked at the base).
 *
 * - Play at the last hour restarts from the first hour; playback stops at the end (no loop).
 * - The date and hour pickers are built by formatting `playback.timestamps` in the study-area time zone and
 *   map back to an hour by index - never through the browser's own zone.
 * - This module moves the clock only by `store.set({t})`; everything else follows through the store. It is
 *   the first subscriber of a tick (order 10): clock, slider and histogram cursor.
 * - Keyboard: Space play/pause, Left/Right one hour, Shift+Left/Right 24 hours, Home/End first/last hour.
 */

import { h, icon } from '../ui/dom.js';
import { SEVERITY, SURFACE, withAlpha } from '../ui/tokens.js';

export const SPEEDS = Object.freeze([6, 12, 24]);
export const DEFAULT_SPEED = 12;
const TICK_ORDER = 10;
const HISTOGRAM_HEIGHT = 36;
const THUMB_WIDTH = 12;
const DAY_STEPS = 24;

/**
 * @param {HTMLElement} container the <footer class="timeline">
 * @param {{store: object, data: object, actions: object, perf: object}} ctx
 */
export function mountTimeline(container, { store, data, actions, perf }) {
  const { format } = data;
  const last = data.lastIndex;
  const stats = data.playback.stats;
  const stepsPerHour = 60 / data.stepMinutes;

  // ---- DOM ---------------------------------------------------------------------------------------------
  const clockLong = h('span', { class: 'tl-clock__long' });
  const clockShort = h('span', { class: 'tl-clock__short' });
  const clockTime = h('span', { class: 'tl-clock__time', id: 'clock', 'aria-live': 'polite', 'aria-atomic': 'true' }, clockLong, clockShort);
  const clock = h('div', { class: 'tl-clock' }, clockTime, h('span', { class: 'tl-clock__caption', text: 'Simulated time' }));

  const firstButton = h('button', { class: 'btn btn--icon', type: 'button', 'aria-label': 'First hour', title: 'First hour (Home)', on: { click: () => actions.setTime(0) } }, icon('first'));
  const backButton = h('button', { class: 'btn btn--icon', type: 'button', 'aria-label': 'Back one hour', title: 'Back one hour (Left arrow)', on: { click: () => actions.stepTime(-1) } }, icon('back'));
  const playButton = h('button', { class: 'btn btn--icon btn--play', type: 'button', on: { click: () => actions.togglePlay() } });
  const forwardButton = h('button', { class: 'btn btn--icon', type: 'button', 'aria-label': 'Forward one hour', title: 'Forward one hour (Right arrow)', on: { click: () => actions.stepTime(1) } }, icon('forward'));
  const endButton = h(
    'button',
    { class: 'btn tl-end', type: 'button', 'aria-label': 'End of simulation', title: 'End of simulation (End)', on: { click: () => actions.setTime(last) } },
    icon('last'),
    h('span', { class: 'tl-end__text', text: 'End of simulation' }),
  );
  const controls = h('div', { class: 'tl-controls', role: 'group', 'aria-label': 'Playback controls' }, firstButton, backButton, playButton, forwardButton, endButton);

  const speedSelect = h(
    'select',
    { class: 'select', 'aria-label': 'Playback speed, simulated hours per second', title: 'Playback speed', on: { change: (event) => store.set({ speed: Number(event.target.value) }) } },
    SPEEDS.map((speed) => h('option', { value: speed, text: `${speed} h/s` })),
  );
  const dateSelect = h(
    'select',
    { class: 'select', 'aria-label': 'Simulated date', title: 'Simulated date', on: { change: onDateChange } },
    data.days.map((day, index) => h('option', { value: index, text: day.label })),
  );
  const hourSelect = h('select', { class: 'select', 'aria-label': 'Simulated hour', title: 'Simulated hour', on: { change: (event) => actions.setTime(Number(event.target.value)) } });
  const fields = h('div', { class: 'tl-fields' }, speedSelect, dateSelect, hourSelect);

  const canvas = h('canvas', { class: 'tl-histogram', 'aria-hidden': 'true' });
  const slider = h('input', {
    class: 'tl-slider',
    type: 'range',
    min: 0,
    max: last,
    step: 1,
    'aria-label': 'Simulated time',
    on: { input: (event) => actions.setTime(Number(event.target.value)) },
  });
  const legend = h(
    'div',
    { class: 'tl-legend', 'aria-hidden': 'true' },
    h('span', { text: 'Active anomalies per hour' }),
    h('span', {}, h('i', { style: { background: SEVERITY.critical } }), 'critical'),
    h('span', {}, h('i', { style: { background: SURFACE.muted } }), 'other severities'),
  );
  const ticks = h('div', { class: 'tl-ticks', 'aria-hidden': 'true' });
  const track = h('div', { class: 'tl-track' }, canvas, legend, slider, ticks);

  container.replaceChildren(clock, controls, fields, track);
  container.removeAttribute('aria-busy');

  // ---- date / hour pickers (index lookups only) --------------------------------------------------------------
  let shownDay = -1;
  function showHoursOf(dayIndex) {
    if (dayIndex === shownDay) return;
    shownDay = dayIndex;
    const hours = data.days[dayIndex].hours;
    while (hourSelect.options.length > hours.length) hourSelect.remove(hourSelect.options.length - 1);
    while (hourSelect.options.length < hours.length) hourSelect.add(document.createElement('option'));
    for (let i = 0; i < hours.length; i += 1) {
      const option = hourSelect.options[i];
      option.value = String(hours[i].index);
      option.textContent = hours[i].label;
    }
  }

  function onDateChange(event) {
    const day = data.days[Number(event.target.value)];
    const current = store.getState().t;
    // Keep the hour of the day when the chosen date has it; otherwise the nearest hour of that date.
    const offset = current - data.days[data.dayIndexOf(current)].firstIndex;
    actions.setTime(Math.min(day.lastIndex, day.firstIndex + offset));
  }

  // ---- histogram -------------------------------------------------------------------------------------------
  // Same horizontal mapping as the range input's thumb, so the thumb sits exactly on its hour.
  const xOf = (index, width) => THUMB_WIDTH / 2 + (last === 0 ? 0 : (index / last) * (width - THUMB_WIDTH));

  function drawHistogram() {
    const width = track.clientWidth;
    if (width === 0) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(HISTOGRAM_HEIGHT * ratio);
    const g = canvas.getContext('2d');
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, width, HISTOGRAM_HEIGHT);
    let max = 1;
    for (let i = 0; i <= last; i += 1) max = Math.max(max, stats.active_anomalies[i]);
    const usable = HISTOGRAM_HEIGHT - 6;
    const barWidth = Math.max(1, (width - THUMB_WIDTH) / Math.max(1, last));
    for (let i = 0; i <= last; i += 1) {
      const active = stats.active_anomalies[i];
      if (active === 0) continue;
      const critical = Math.min(active, stats.critical_alerts[i]);
      const x = xOf(i, width) - barWidth / 2;
      const total = (active / max) * usable;
      const criticalHeight = (critical / max) * usable;
      g.fillStyle = withAlpha(SURFACE.muted, 0.75);
      g.fillRect(x, HISTOGRAM_HEIGHT - total, barWidth + 0.4, total - criticalHeight);
      if (critical > 0) {
        g.fillStyle = SEVERITY.critical;
        g.fillRect(x, HISTOGRAM_HEIGHT - criticalHeight, barWidth + 0.4, criticalHeight);
      }
    }
    drawTicks(width);
  }

  function drawTicks(width) {
    const minGap = 64;
    const every = Math.max(1, Math.ceil((minGap * data.days.length) / Math.max(1, width - THUMB_WIDTH)));
    const nodes = [];
    for (let d = 0; d < data.days.length; d += every) {
      const day = data.days[d];
      const x = xOf(day.firstIndex, width);
      if (x < 22 || x > width - 22) continue;
      nodes.push(h('span', { style: { left: `${x}px` }, text: format.date(data.times[day.firstIndex]) }));
    }
    ticks.replaceChildren(...nodes);
  }

  let resizeObserver = null;
  if (typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(() => drawHistogram());
    resizeObserver.observe(track);
  } else {
    window.addEventListener('resize', drawHistogram);
  }
  drawHistogram();

  // ---- render ----------------------------------------------------------------------------------------------
  function renderTime(t) {
    const ms = data.times[t];
    clockLong.textContent = format.dateTimeLong(ms);
    clockShort.textContent = format.dateTime(ms);
    if (Number(slider.value) !== t) slider.value = String(t);
    slider.setAttribute('aria-valuetext', `${format.dateTimeLong(ms)} (simulated time)`);
    const dayIndex = data.dayIndexOf(t);
    if (dateSelect.selectedIndex !== dayIndex) dateSelect.selectedIndex = dayIndex;
    showHoursOf(dayIndex);
    const hourIndex = t - data.days[dayIndex].firstIndex;
    if (hourSelect.selectedIndex !== hourIndex) hourSelect.selectedIndex = hourIndex;
    firstButton.disabled = t === 0;
    backButton.disabled = t === 0;
    forwardButton.disabled = t === last;
    endButton.disabled = t === last;
  }

  function renderPlaying(playing) {
    playButton.replaceChildren(icon(playing ? 'pause' : 'play'));
    playButton.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    playButton.title = playing ? 'Pause (Space)' : store.getState().t === last ? 'Play from the first hour (Space)' : 'Play (Space)';
    playButton.setAttribute('aria-pressed', playing ? 'true' : 'false');
    // The clock is not announced hour by hour while it runs.
    clockTime.setAttribute('aria-live', playing ? 'off' : 'polite');
  }

  // ---- playback timer --------------------------------------------------------------------------------------
  let timer = null;
  function stopTimer() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }
  function tick() {
    const state = store.getState();
    if (!state.playing) return;
    if (state.t >= last) {
      store.set({ playing: false });
      return;
    }
    const next = state.t + 1;
    const started = performance.now();
    // Stops at the end: the last hour is shown, then playback ends (no loop).
    store.set(next >= last ? { t: last, playing: false } : { t: next });
    perf.record(performance.now() - started);
  }
  function startTimer() {
    stopTimer();
    const state = store.getState();
    const interval = 1000 / (state.speed * stepsPerHour);
    timer = setInterval(tick, interval);
  }

  // ---- store -----------------------------------------------------------------------------------------------
  const unsubscribe = [
    store.subscribe(['t'], (state) => renderTime(state.t), { order: TICK_ORDER }),
    store.subscribe(['playing'], (state) => {
      renderPlaying(state.playing);
      if (state.playing) startTimer();
      else stopTimer();
    }, { order: TICK_ORDER }),
    store.subscribe(['speed'], (state) => {
      speedSelect.value = String(state.speed);
      if (state.playing) startTimer();
    }, { order: TICK_ORDER }),
  ];

  const initial = store.getState();
  speedSelect.value = String(initial.speed);
  renderTime(initial.t);
  renderPlaying(initial.playing);
  if (initial.playing) startTimer();

  // ---- keyboard shortcuts (build contract 12.8) -------------------------------------------------------------
  function onKeyDown(event) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target;
    const tagName = target && target.tagName ? target.tagName.toLowerCase() : '';
    // Space, arrows, Home and End keep their native meaning inside form controls and on buttons.
    if (['input', 'select', 'textarea', 'button'].includes(tagName) || (target && target.isContentEditable)) return;
    // A widget that reads the arrow keys itself (a focused chart) marks its element with data-own-keys.
    if (target && typeof target.closest === 'function' && target.closest('[data-own-keys]')) return;
    if (document.querySelector('dialog[open]')) return;
    const day = DAY_STEPS * stepsPerHour;
    switch (event.key) {
      case ' ':
      case 'Spacebar':
        actions.togglePlay();
        break;
      case 'ArrowLeft':
        actions.stepTime(event.shiftKey ? -day : -1);
        break;
      case 'ArrowRight':
        actions.stepTime(event.shiftKey ? day : 1);
        break;
      case 'Home':
        actions.setTime(0);
        break;
      case 'End':
        actions.setTime(last);
        break;
      default:
        return;
    }
    // Capture phase: these keys drive the clock even when the map canvas has the focus (the map keeps +/-).
    event.preventDefault();
    event.stopPropagation();
  }
  document.addEventListener('keydown', onKeyDown, true);

  return {
    redraw: drawHistogram,
    destroy() {
      stopTimer();
      unsubscribe.forEach((off) => off());
      document.removeEventListener('keydown', onKeyDown, true);
      if (resizeObserver) resizeObserver.disconnect();
      else window.removeEventListener('resize', drawHistogram);
    },
  };
}
