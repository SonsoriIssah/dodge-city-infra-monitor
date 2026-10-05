/**
 * Honesty labels (build contract 12.1, brief R22).
 *
 * - The three mandatory labels are present in the page and in the scripts.
 * - Every exact string of contract 12.1 that is written in the sources is still there.
 * - No UI string contains the banned words "live" or "real-time" / "realtime".
 *
 * "UI string" = every string and template literal of the dashboard's own scripts (comments are ignored),
 * plus the text and attribute values of index.html and the `content` strings of the style sheets.
 * `aria-live` is an ARIA attribute name, not wording, and is ignored.
 */

import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = new URL('../', import.meta.url);
const read = (relative) => readFileSync(new URL(relative, ROOT), 'utf8');

function filesUnder(relative, extension) {
  const found = [];
  const walk = (folder) => {
    for (const entry of readdirSync(new URL(folder, ROOT), { withFileTypes: true })) {
      if (entry.isDirectory()) walk(`${folder}${entry.name}/`);
      else if (entry.name.endsWith(extension)) found.push(`${folder}${entry.name}`);
    }
  };
  walk(relative);
  return found.sort();
}

const REGEX_MAY_FOLLOW = new Set(['(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}', ';', '+', '-', '*', '%', '<', '>', '~', '^']);
const REGEX_KEYWORDS = /(?:^|[^\w$])(?:return|typeof|case|in|of|delete|void|throw|new)$/;

/**
 * String and template literals of a JavaScript source, without comments and regular expressions.
 * A template literal is returned whole, `${...}` parts included (their own strings are scanned too).
 */
export function stringLiterals(source) {
  const literals = [];
  let i = 0;
  let lastSignificant = '';
  let before = '';
  const n = source.length;

  function readQuoted(quote) {
    let text = '';
    i += 1;
    while (i < n && source[i] !== quote) {
      if (source[i] === '\\') {
        text += source[i + 1] === 'n' ? '\n' : source[i + 1];
        i += 2;
      } else {
        text += source[i];
        i += 1;
      }
    }
    i += 1;
    return text;
  }

  function readTemplate() {
    let text = '';
    i += 1;
    while (i < n && source[i] !== '`') {
      if (source[i] === '\\') {
        text += source[i + 1];
        i += 2;
      } else if (source[i] === '$' && source[i + 1] === '{') {
        // Scan the expression with the same rules; nested literals are collected on their own.
        i += 2;
        let depth = 1;
        const start = i;
        while (i < n && depth > 0) {
          if (source[i] === '{') depth += 1;
          else if (source[i] === '}') depth -= 1;
          else if (source[i] === "'" || source[i] === '"') {
            readQuoted(source[i]);
            continue;
          } else if (source[i] === '`') {
            readTemplate();
            continue;
          }
          i += 1;
        }
        literals.push(...stringLiterals(source.slice(start, i - 1)));
        text += '${}';
      } else {
        text += source[i];
        i += 1;
      }
    }
    i += 1;
    return text;
  }

  while (i < n) {
    const char = source[i];
    const next = source[i + 1];
    if (char === '/' && next === '/') {
      while (i < n && source[i] !== '\n') i += 1;
    } else if (char === '/' && next === '*') {
      i = source.indexOf('*/', i + 2);
      i = i < 0 ? n : i + 2;
    } else if (char === "'" || char === '"') {
      literals.push(readQuoted(char));
      lastSignificant = char;
    } else if (char === '`') {
      literals.push(readTemplate());
      lastSignificant = '`';
    } else if (char === '/' && (lastSignificant === '' || REGEX_MAY_FOLLOW.has(lastSignificant) || REGEX_KEYWORDS.test(before))) {
      // Regular expression literal: skip to the closing slash (character classes may hold a slash).
      i += 1;
      let inClass = false;
      while (i < n && (source[i] !== '/' || inClass) && source[i] !== '\n') {
        if (source[i] === '\\') i += 1;
        else if (source[i] === '[') inClass = true;
        else if (source[i] === ']') inClass = false;
        i += 1;
      }
      i += 1;
      lastSignificant = '/';
    } else {
      if (!/\s/.test(char)) {
        lastSignificant = char;
        before = (before + char).slice(-12);
      } else {
        before = `${before} `.slice(-12);
      }
      i += 1;
    }
  }
  return literals;
}

/** Text nodes and attribute values of an HTML document (scripts and styles removed). */
function htmlStrings(html) {
  const cleaned = html.replace(/<!--[\s\S]*?-->/g, ' ');
  const inlineScripts = [...cleaned.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map((match) => match[1]);
  const body = cleaned.replace(/<script\b[\s\S]*?<\/script>/gi, ' ').replace(/<style\b[\s\S]*?<\/style>/gi, ' ');
  const attributes = [...body.matchAll(/\s([a-zA-Z-:]+)="([^"]*)"/g)].filter((match) => match[1] !== 'd' && match[1] !== 'href').map((match) => match[2]);
  const text = body
    .replace(/<[^>]+>/g, '\n')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  return [...text, ...attributes, ...inlineScripts.flatMap(stringLiterals)];
}

const scriptFiles = ['config.js', ...filesUnder('js/', '.js')];
const scripts = new Map(scriptFiles.map((file) => [file, read(file)]));
const html = read('index.html');
const cssStrings = filesUnder('css/', '.css').flatMap((file) =>
  [...read(file).replace(/\/\*[\s\S]*?\*\//g, ' ').matchAll(/content:\s*(['"])(.*?)\1/g)].map((match) => ({ file, text: match[2] })),
);
const uiStrings = [
  ...htmlStrings(html).map((text) => ({ file: 'index.html', text })),
  ...[...scripts].flatMap(([file, source]) => stringLiterals(source).map((text) => ({ file, text }))),
  ...cssStrings,
];
const allScriptText = [...scripts.values()].join('\n');

test('the extractor reads strings and templates and skips comments and regular expressions', () => {
  const sample = [
    "// a comment with 'live' data",
    '/* block "real-time" */',
    "const a = 'one'; const b = \"two\";",
    'const c = `three ${a ? \'four\' : "five"} six`;',
    "const d = text.replace(/['\"]live/g, 'seven');",
    'const e = value / 2; const f = "eight";',
  ].join('\n');
  assert.deepEqual(stringLiterals(sample), ['one', 'two', 'four', 'five', 'three ${} six', 'seven', 'eight']);
});

test('the sources that carry the wording were found', () => {
  assert.ok(scriptFiles.includes('js/main.js'));
  for (const file of ['js/ui/chart.js', 'js/panels/assets.js', 'js/panels/anomalies.js', 'js/panels/sensors.js', 'js/panels/tabs.js', 'js/panels/kpis.js', 'js/timeline/timeline.js', 'js/map/map.js']) {
    assert.ok(scriptFiles.includes(file), `${file} is part of the dashboard`);
  }
  assert.ok(!scriptFiles.some((file) => file.startsWith('vendor/')));
  assert.ok(uiStrings.length > 500, `only ${uiStrings.length} strings were extracted`);
});

test('R22: the three mandatory labels are in the page header and in the scripts', () => {
  for (const label of ['Simulated Sensor Data', 'Prototype Anomaly Detection', 'Derived Asset Health Score']) {
    assert.ok(html.includes(`>${label}</li>`), `header badge "${label}"`);
    assert.ok(uiStrings.some((entry) => entry.file !== 'index.html' && entry.text.includes(label)), `"${label}" in a script string`);
  }
});

test('contract 12.1: the exact honesty strings are present', () => {
  const inScripts = [
    'Source: PostGIS API',
    'Source: static snapshot',
    'Simulated time',
    'End of simulation',
    'prototype detection',
    'severity = critical',
    'derived health score < ',
    'Derived Asset Health Score (from simulated sensors)',
    'Simulated sensors',
    'Risk zones — derived from simulated anomalies',
    'Recorded attributes — source: ',
    'OpenStreetMap',
    'FHWA National Bridge Inventory',
    "Simulated monitoring — Derived Asset Health Score, simulated sensors; not an assessment of this structure's real condition",
    'Not monitored — no simulated sensors on this asset',
    'All monitored assets are normal at this time',
    'No anomalies match these filters at the selected time',
    'Simulated regional event (benign — not flagged)',
    'Basemap unavailable — showing project data only',
    'View as table',
    'Use static snapshot',
    'Clear filters',
  ];
  for (const text of inScripts) {
    assert.ok(uiStrings.some((entry) => entry.file !== 'index.html' && entry.text.includes(text)), `"${text}" is a string of a script`);
  }
  // The watermark on the map is part of the page itself, so it is there before any script runs.
  assert.ok(html.includes('Simulated sensor data · prototype — not a record of real infrastructure condition'));
  // Every anomaly card carries the "Simulated" tag; the Anomalies tab subtitle and every chart caption are fixed.
  assert.match(scripts.get('js/ui/dom.js'), /tag\('Simulated', 'simulated'\)/);
  assert.match(scripts.get('js/panels/anomalies.js'), /export const SUBTITLE = 'Prototype Anomaly Detection';/);
  assert.match(scripts.get('js/ui/chart.js'), /export const CHART_CAPTION = 'Simulated Sensor Data';/);
  for (const panel of ['js/panels/anomalies.js', 'js/panels/assets.js', 'js/panels/sensors.js']) {
    assert.ok(scripts.get(panel).includes('simulatedTag()'), `${panel} tags its anomaly rows as simulated`);
  }
});

test('contract 12.1: NBI ratings and the health score are worded apart', () => {
  const assets = scripts.get('js/panels/assets.js');
  assert.ok(assets.includes('not used by the Derived Asset Health Score'));
  // Never a safety verdict (contract 2).
  for (const banned of [/structurally deficient/i, /\bunsafe\b/i]) {
    assert.ok(!uiStrings.some((entry) => banned.test(entry.text)), `no UI string matches ${banned}`);
  }
});

test('no UI string contains "live" or "real-time"', () => {
  const banned = [/\blive\b/i, /real[\s-]?time/i];
  const offending = uiStrings.filter((entry) => {
    const text = entry.text.replace(/aria-live/gi, '');
    return banned.some((pattern) => pattern.test(text));
  });
  assert.deepEqual(offending, []);
  // The check itself works.
  assert.ok(banned[0].test('Live data'));
  assert.ok(banned[1].test('realtime monitoring') && banned[1].test('real-time') && banned[1].test('Real time'));
  assert.ok(!banned[0].test('delivered alive'));
});

test('times are never formatted outside js/ui/format.js, and the zone abbreviation is never written by hand', () => {
  for (const [file, source] of scripts) {
    if (file === 'js/ui/format.js') continue;
    assert.ok(!/toLocale(Date|Time)String\s*\(/.test(source), `${file} formats a time itself`);
    assert.ok(!/new Intl\.DateTimeFormat/.test(source), `${file} builds its own DateTimeFormat`);
  }
  const zoneNames = uiStrings.filter((entry) => /\b(CDT|CST)\b/.test(entry.text));
  assert.deepEqual(zoneNames, []);
  assert.ok(!/datetime-local/.test(allScriptText) && !/datetime-local/.test(html));
});

test('the dashboard folder has no top-level entry that shadows an API route (contract 10.5)', () => {
  const reserved = new Set(['assets', 'sensors', 'sensor-readings', 'anomalies', 'statistics', 'health', 'meta', 'spatial', 'layers', 'playback', 'simulation-events', 'ingest', 'docs', 'redoc', 'openapi.json']);
  const entries = readdirSync(fileURLToPath(ROOT));
  assert.deepEqual(entries.filter((name) => reserved.has(name)), []);
  // config.js is the one deliberate exception: the API route /config.js shadows the static file.
  assert.ok(entries.includes('config.js'));
  assert.ok(entries.includes('index.html'));
});
