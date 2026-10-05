/**
 * DOM kit shared by the shell and the panel tabs, so pills, lists and states look the same everywhere.
 * Styles: css/components.css. Text is always set as text (never as HTML), so data cannot inject markup.
 *
 *   h(tag, props, ...children)           element builder
 *   clear(node) / replace(node, ...kids) empty a node / replace its content
 *   icon(name)                           inline SVG icon
 *   pill(kind, value, options)           status pill with text: kind 'asset' | 'sensor' | 'severity' | 'risk' |
 *                                        'anomaly-status'
 *   updatePill(node, value)              change a pill in place
 *   tag(text, variant) / simulatedTag()  small label ("Simulated", "REAL", ...)
 *   sourceKindTag(kind)                  REAL / SIMULATED / DERIVED provenance tag
 *   sensorGlyph(sensorType)              letter mark T / V / M / P
 *   emptyState(title, {hint, action})    empty list or chart
 *   errorState(title, {detail, onRetry}) failed request, with Retry
 *   skeleton({rows, block})              loading placeholder
 *   setBusy(node, busy)                  hold the previous render at reduced opacity while refetching
 *   keyValue(rows)                       definition list of [label, value] pairs (null values are skipped)
 *   block(title, options, ...children)   titled card; options {note, variant: 'recorded' | 'simulated', trailing}
 *   rowButton(options)                   list row as a <button> (every selectable entity is a button)
 *   linkButton(text, onClick)            inline link-styled button
 *   select(options) / chipGroup(options) / segmented(options)   form controls
 *   panelHead({title, subtitle, trailing, onBack})
 */

import {
  ANOMALY_STATUS_LABEL,
  ASSET_STATUS_LABEL,
  RISK_LEVEL_LABEL,
  SENSOR_STATUS_LABEL,
  SENSOR_TYPE_GLYPH,
  SEVERITY_LABEL,
  SOURCE_KIND_LABEL,
  labelOf,
} from './tokens.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

function append(parent, child) {
  if (child === null || child === undefined || child === false) return;
  if (Array.isArray(child)) {
    child.forEach((entry) => append(parent, entry));
    return;
  }
  parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
}

/**
 * Create an element.
 * props: `class` (string or array), `text`, `dataset` {k: v}, `style` {k: v}, `on` {event: handler},
 * `hidden`, and any other key as an attribute (null/undefined/false are skipped, true gives an empty one).
 */
export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') {
      element.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
    } else if (key === 'text') {
      element.textContent = String(value);
    } else if (key === 'dataset') {
      for (const [name, entry] of Object.entries(value)) {
        if (entry !== null && entry !== undefined) element.dataset[name] = String(entry);
      }
    } else if (key === 'style') {
      for (const [name, entry] of Object.entries(value)) {
        if (name.startsWith('--')) element.style.setProperty(name, entry);
        else element.style[name] = entry;
      }
    } else if (key === 'on') {
      for (const [name, handler] of Object.entries(value)) element.addEventListener(name, handler);
    } else if (key === 'hidden') {
      element.hidden = Boolean(value);
    } else {
      element.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(element, children);
  return element;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function replace(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

const ICON_PATHS = {
  play: ['M7 4.5v15l13-7.5z', 'fill'],
  pause: ['M6 4.5h4.2v15H6zM13.8 4.5H18v15h-4.2z', 'fill'],
  first: ['M6 5v14M19 5.5v13l-10-6.5z', 'both'],
  last: ['M18 5v14M5 5.5v13l10-6.5z', 'both'],
  back: ['M16.5 5.5v13l-10-6.5z', 'fill'],
  forward: ['M7.5 5.5v13l10-6.5z', 'fill'],
  home: ['M3.5 11.5 12 4.5l8.5 7M6 10v9.5h4.2v-5.5h3.6v5.5H18V10', 'stroke'],
  layers: ['M12 4 3.5 8.5 12 13l8.5-4.5zM3.5 12.5 12 17l8.5-4.5M3.5 16.5 12 21l8.5-4.5', 'stroke'],
  chevron: ['M6 9.5l6 6 6-6', 'stroke'],
  close: ['M6 6l12 12M18 6 6 18', 'stroke'],
  info: ['M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM12 11v5.5M12 7.6v.4', 'stroke'],
  help: ['M9.2 9.2a2.9 2.9 0 1 1 4.3 2.5c-.9.5-1.5 1.1-1.5 2.1v.4M12 17.4v.4', 'stroke'],
  alert: ['M12 4 2.8 19.5h18.4zM12 10v4.5M12 16.9v.4', 'stroke'],
  refresh: ['M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.2h-4.2', 'stroke'],
  imagery: ['M4 5.5h16v13H4zM4 15l4.5-4.5 4 4 2.5-2.5 5 5M15.5 9.2h.01', 'stroke'],
  boundary: ['M5 6.5 10 4l5 3 4-2v12.5L14 20l-5-3-4 2z', 'stroke'],
  cube: ['M12 3.5 4 8v8l8 4.5 8-4.5V8zM4 8l8 4.5L20 8M12 12.5v8', 'stroke'],
  flat: ['M4 6.5h16v11H4zM4 12h16M12 6.5v11', 'stroke'],
  arrowLeft: ['M19 12H5M11 6l-6 6 6 6', 'stroke'],
  buildings: ['M3 21V9l5-3v15M8 21V4l7 3v14M15 21v-9l6-2v11M2 21h20', 'stroke'],
};

/** Inline SVG icon (decorative: hidden from assistive technology; give the button an aria-label). */
export function icon(name, { size = 16 } = {}) {
  const [path, mode] = ICON_PATHS[name] || ICON_PATHS.info;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('icon');
  const shape = document.createElementNS(SVG_NS, 'path');
  shape.setAttribute('d', path);
  shape.setAttribute('fill', mode === 'stroke' ? 'none' : 'currentColor');
  if (mode !== 'fill') {
    shape.setAttribute('stroke', 'currentColor');
    shape.setAttribute('stroke-width', '1.8');
    shape.setAttribute('stroke-linecap', 'round');
    shape.setAttribute('stroke-linejoin', 'round');
  }
  svg.append(shape);
  return svg;
}

const PILL_LABELS = {
  asset: ASSET_STATUS_LABEL,
  sensor: SENSOR_STATUS_LABEL,
  severity: SEVERITY_LABEL,
  risk: RISK_LEVEL_LABEL,
  'anomaly-status': ANOMALY_STATUS_LABEL,
};

/**
 * Status pill: a coloured mark plus the status as text (never colour alone).
 * @param {'asset'|'sensor'|'severity'|'risk'|'anomaly-status'} kind
 * @param {string} value e.g. 'at_risk', 'offline', 'critical', 'very_high', 'active'
 * @param {{label?: string, title?: string}} [options] `label` replaces the default wording
 */
export function pill(kind, value, { label, title } = {}) {
  const text = label || labelOf(PILL_LABELS[kind] || {}, value);
  return h(
    'span',
    { class: 'pill', dataset: { kind, value: value || 'unknown' }, title },
    h('span', { class: 'pill__mark', 'aria-hidden': 'true' }),
    h('span', { class: 'pill__text', text }),
  );
}

/** Change the value of an existing pill in place (playback ticks): no node is replaced, nothing flickers. */
export function updatePill(node, value, { label } = {}) {
  const next = value || 'unknown';
  if (node.dataset.value === next && !label) return node;
  node.dataset.value = next;
  const text = node.querySelector('.pill__text');
  if (text) text.textContent = label || labelOf(PILL_LABELS[node.dataset.kind] || {}, value);
  return node;
}

export function tag(text, variant) {
  return h('span', { class: 'tag', dataset: { variant }, text });
}

/** The "Simulated" tag every anomaly card, sensor reading and simulated asset carries. */
export function simulatedTag() {
  return tag('Simulated', 'simulated');
}

/** REAL / SIMULATED / DERIVED tag for `meta.data_sources[].kind`. */
export function sourceKindTag(kind) {
  return tag(labelOf(SOURCE_KIND_LABEL, kind), kind);
}

/** Letter mark of a sensor type (T, V, M, P) with the full name as tooltip and accessible name. */
export function sensorGlyph(sensorType, label) {
  const letter = SENSOR_TYPE_GLYPH[sensorType] || '?';
  const name = label || labelOf({}, sensorType);
  return h('span', { class: 'glyph', title: `${name} sensor`, role: 'img', 'aria-label': `${name} sensor`, text: letter });
}

/**
 * Empty state for a list or a chart.
 * @param {string} title e.g. "All monitored assets are normal at this time"
 * @param {{hint?: string, action?: {label: string, onClick: function}}} [options]
 */
export function emptyState(title, { hint, action } = {}) {
  return h(
    'div',
    { class: 'empty', role: 'status' },
    h('span', { class: 'empty__title', text: title }),
    hint ? h('span', { class: 'empty__hint', text: hint }) : null,
    action ? h('button', { class: 'btn btn--small', type: 'button', on: { click: action.onClick }, text: action.label }) : null,
  );
}

/**
 * Error state with Retry.
 * @param {string} title what could not be shown
 * @param {{detail?: string, onRetry?: function, retryLabel?: string, actions?: Array<{label, onClick}>}} [options]
 */
export function errorState(title, { detail, onRetry, retryLabel = 'Retry', actions = [] } = {}) {
  const buttons = [];
  if (onRetry) {
    buttons.push(h('button', { class: 'btn btn--small', type: 'button', on: { click: onRetry } }, icon('refresh', { size: 14 }), retryLabel));
  }
  for (const action of actions) {
    buttons.push(h('button', { class: 'btn btn--small', type: 'button', on: { click: action.onClick }, text: action.label }));
  }
  return h(
    'div',
    { class: 'error-state', role: 'alert' },
    h('span', { class: 'error-state__title', text: title }),
    detail ? h('span', { class: 'error-state__detail', text: detail }) : null,
    buttons.length ? h('div', { class: 'error-state__actions' }, buttons) : null,
  );
}

/** Readable one-line reason of an Error / ProviderError for `errorState({detail})`. */
export function describeError(error) {
  if (!error) return '';
  const parts = [error.message || String(error)];
  if (error.status && !String(parts[0]).includes(String(error.status))) parts.push(`HTTP ${error.status}`);
  return parts.join(' — ');
}

/**
 * Loading placeholder.
 * @param {{rows?: number, block?: boolean, label?: string}} [options] `rows` list rows, `block` adds a chart-sized block
 */
export function skeleton({ rows = 3, block = false, label = 'Loading' } = {}) {
  const kids = [];
  if (block) kids.push(h('span', { class: 'skeleton skeleton--block' }));
  for (let i = 0; i < rows; i += 1) kids.push(h('span', { class: 'skeleton skeleton--row' }));
  return h('div', { class: 'skeleton-stack', role: 'status', 'aria-label': label, 'aria-busy': 'true' }, kids);
}

/** Dim a region while its data is refetched; the previous render stays in place. */
export function setBusy(node, busy) {
  node.classList.toggle('is-busy', Boolean(busy));
  if (busy) node.setAttribute('aria-busy', 'true');
  else node.removeAttribute('aria-busy');
}

/**
 * Definition list. `rows`: [[label, value], ...]; value may be text or a Node; rows whose value is
 * null/undefined/'' are left out (missing recorded attributes are not shown as blanks).
 */
export function keyValue(rows) {
  const list = h('dl', { class: 'kv' });
  for (const [label, value] of rows) {
    if (value === null || value === undefined || value === '') continue;
    list.append(h('dt', { text: label }), h('dd', {}, value));
  }
  return list;
}

/**
 * Titled card.
 * @param {string} title
 * @param {{note?: string, variant?: 'recorded'|'simulated', trailing?: Node}} [options]
 */
export function block(title, { note, variant, trailing } = {}, ...children) {
  return h(
    'section',
    { class: 'block', dataset: { variant } },
    h('div', { class: 'block__head' }, h('h3', { class: 'block__title', text: title }), trailing || null),
    note ? h('p', { class: 'block__note', text: note }) : null,
    children,
  );
}

/**
 * List row as a button.
 * @param {object} options
 * @param {Node|string} [options.leading]   e.g. a pill or a sensor glyph
 * @param {string} options.title
 * @param {string} [options.subtitle]
 * @param {Node|string|Array} [options.trailing] right-aligned value(s)
 * @param {boolean} [options.selected]
 * @param {function} options.onClick
 * @param {string} [options.ariaLabel]
 * @param {object} [options.dataset]
 */
export function rowButton({ leading, title, subtitle, trailing, selected = false, onClick, ariaLabel, dataset } = {}) {
  return h(
    'button',
    {
      class: ['row', selected ? 'is-selected' : ''],
      type: 'button',
      'aria-current': selected ? 'true' : null,
      'aria-label': ariaLabel,
      dataset,
      on: { click: onClick },
    },
    h('span', { class: 'row__lead' }, leading || null),
    h(
      'span',
      { class: 'row__main' },
      h('span', { class: 'row__title', text: title }),
      subtitle ? h('span', { class: 'row__sub', text: subtitle }) : null,
    ),
    h('span', { class: 'row__trail' }, trailing || null),
  );
}

export function linkButton(text, onClick, { title } = {}) {
  return h('button', { class: 'link-btn', type: 'button', title, on: { click: onClick }, text });
}

/**
 * Labelled <select>.
 * @param {{label: string, options: Array<{value: string, label: string}>, value: string, onChange: function,
 *          hideLabel?: boolean, id?: string}} options
 * @returns {HTMLLabelElement} the wrapping <label>; its native `.control` property is the <select> (a label's
 *          labelled control is its first form-control descendant - the property is read-only, never assigned)
 */
export function select({ label, options, value, onChange, hideLabel = false, id }) {
  const control = h(
    'select',
    { class: 'select', id, on: { change: (event) => onChange(event.target.value) } },
    options.map((option) => h('option', { value: option.value, text: option.label })),
  );
  control.value = value === null || value === undefined ? '' : String(value);
  return h(
    'label',
    { class: 'field' },
    h('span', { class: hideLabel ? 'visually-hidden' : 'field__label', text: label }),
    control,
  );
}

/**
 * Multi-select chips (toggle buttons with aria-pressed).
 * @param {{label: string, options: Array<{value: string, label: string}>, selected: Set<string>,
 *          onToggle: function(string, boolean)}} options
 */
export function chipGroup({ label, options, selected, onToggle }) {
  return h(
    'div',
    { class: 'field', role: 'group', 'aria-label': label },
    h('span', { class: 'field__label', text: label }),
    h(
      'div',
      { class: 'chips' },
      options.map((option) => {
        const on = selected.has(option.value);
        return h('button', {
          class: 'chip',
          type: 'button',
          'aria-pressed': on ? 'true' : 'false',
          on: { click: () => onToggle(option.value, !on) },
          text: option.label,
        });
      }),
    ),
  );
}

/**
 * Segmented control: one of a few options.
 * @param {{label: string, options: Array<{value: string, label: string}>, value: string, onChange: function}} options
 */
export function segmented({ label, options, value, onChange }) {
  return h(
    'div',
    { class: 'seg', role: 'group', 'aria-label': label },
    options.map((option) =>
      h('button', {
        class: 'seg__option',
        type: 'button',
        'aria-pressed': option.value === value ? 'true' : 'false',
        on: { click: () => onChange(option.value) },
        text: option.label,
      }),
    ),
  );
}

/**
 * Head of a panel or of a detail view.
 * @param {{title: string, subtitle?: string, trailing?: Node, onBack?: function, backLabel?: string}} options
 */
export function panelHead({ title, subtitle, trailing, onBack, backLabel = 'Back to the list' }) {
  return h(
    'div',
    { class: 'panel-head' },
    h(
      'div',
      { style: { minWidth: '0' } },
      onBack
        ? h('button', { class: 'btn btn--ghost btn--small', type: 'button', on: { click: onBack }, style: { marginLeft: '-8px' } }, icon('arrowLeft', { size: 14 }), backLabel)
        : null,
      h('h2', { class: 'panel-head__title', text: title }),
      subtitle ? h('p', { class: 'panel-head__sub', text: subtitle }) : null,
    ),
    trailing || null,
  );
}
