/**
 * js/ui/format.js: every time is rendered in the study-area zone, whatever the zone of the machine.
 *
 * The same assertions run in this process and in child processes started with TZ=Africa/Accra and
 * TZ=Asia/Tokyo (the child also proves that its local zone really is the requested one), so the result does
 * not depend on how the test runner was started.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  createFormatter,
  formatDelta,
  formatDistance,
  formatDuration,
  formatInteger,
  formatLatLon,
  formatNumber,
  formatPercent,
  formatScore,
  formatValue,
  formatZ,
  humanize,
  plural,
  toEpochMs,
  valueDigits,
} from '../js/ui/format.js';
import { renderSamples } from './helpers/format-samples.js';

const meta = JSON.parse(readFileSync(new URL('../data/snapshot/meta.json', import.meta.url), 'utf8'));
const playback = JSON.parse(readFileSync(new URL('../data/snapshot/playback.json', import.meta.url), 'utf8'));
const ZONE = meta.study_area.timezone;
const FIRST = playback.timestamps[0];

test('index 0 of the playback renders as "Sep 1, 12:00 AM CDT" in the study-area zone', () => {
  const format = createFormatter(ZONE);
  assert.equal(ZONE, 'America/Chicago');
  assert.equal(format.dateTime(FIRST), 'Sep 1, 12:00 AM CDT');
  assert.equal(format.dateTimeLong(FIRST), 'Tue, Sep 1, 2026, 12:00 AM CDT');
  assert.equal(format.dateTimeShort(FIRST), 'Sep 1, 12:00 AM');
  assert.equal(format.date(FIRST), 'Sep 1');
  assert.equal(format.dateWithWeekday(FIRST), 'Tue, Sep 1');
  assert.equal(format.dateLong(FIRST), 'Sep 1, 2026');
  assert.equal(format.hour(FIRST), '12:00 AM');
  assert.equal(format.zoneName(FIRST), 'CDT');
  assert.equal(format.dayKey(FIRST), '2026-09-01');
  assert.equal(format.hourOfDay(FIRST), 0);
});

for (const zone of ['Africa/Accra', 'Asia/Tokyo']) {
  test(`the same text is produced on a machine set to TZ=${zone}`, () => {
    const script = `
      import { renderSamples } from ${JSON.stringify(new URL('./helpers/format-samples.js', import.meta.url).href)};
      const probe = new Date(2026, 0, 15, 12, 0, 0);
      console.log(JSON.stringify({
        localZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        offsetMinutes: probe.getTimezoneOffset(),
        samples: renderSamples(),
      }));
    `;
    const child = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
      env: { ...process.env, TZ: zone, NODE_TEST_CONTEXT: '' },
      cwd: fileURLToPath(new URL('.', import.meta.url)),
      encoding: 'utf8',
    });
    assert.equal(child.status, 0, child.stderr);
    const line = child.stdout.trim().split('\n').filter((entry) => entry.startsWith('{')).pop();
    const result = JSON.parse(line);
    // The child really ran in the requested zone.
    assert.equal(result.localZone, zone);
    assert.equal(result.offsetMinutes, zone === 'Asia/Tokyo' ? -540 : 0);
    assert.equal(result.samples.first, 'Sep 1, 12:00 AM CDT');
    assert.equal(result.samples.firstDay, '2026-09-01');
    assert.equal(result.samples.firstHour, 0);
    assert.deepEqual(result.samples, renderSamples());
  });
}

test('times are read as instants: ISO text, Date and epoch milliseconds give the same result', () => {
  const format = createFormatter(ZONE);
  const ms = Date.parse(FIRST);
  assert.equal(toEpochMs(FIRST), ms);
  assert.equal(toEpochMs(new Date(ms)), ms);
  assert.equal(toEpochMs(ms), ms);
  assert.ok(Number.isNaN(toEpochMs('')));
  assert.ok(Number.isNaN(toEpochMs(null)));
  assert.equal(format.dateTime(ms), format.dateTime(FIRST));
  assert.equal(format.dateTime(new Date(ms)), format.dateTime(FIRST));
  assert.equal(format.dateTime(null), '—');
  assert.equal(format.dayKey('nonsense'), '');
  assert.ok(Number.isNaN(format.hourOfDay(undefined)));
});

test('the zone abbreviation comes from Intl: daylight time in September, standard time in December', () => {
  const format = createFormatter(ZONE);
  assert.equal(format.dateTime('2026-09-15T17:00:00Z'), 'Sep 15, 12:00 PM CDT');
  assert.equal(format.dateTime('2026-12-01T06:00:00Z'), 'Dec 1, 12:00 AM CST');
  // Another study area needs no code change.
  assert.equal(createFormatter('Europe/London').dateTime('2026-09-01T05:00:00Z'), 'Sep 1, 6:00 AM GMT+1');
  assert.throws(() => createFormatter(''), /time zone/);
});

test('date and hour pickers: formatting the timestamps and mapping back by index is lossless', () => {
  const format = createFormatter(ZONE);
  const days = new Map();
  playback.timestamps.forEach((iso, index) => {
    const key = format.dayKey(iso);
    if (!days.has(key)) days.set(key, []);
    days.get(key).push({ index, hour: format.hourOfDay(iso), label: format.hour(iso) });
  });
  assert.equal(days.size, 30);
  assert.deepEqual([...days.keys()].slice(0, 2), ['2026-09-01', '2026-09-02']);
  assert.equal([...days.keys()].pop(), '2026-09-30');
  let expectedIndex = 0;
  for (const hours of days.values()) {
    assert.equal(hours.length, 24);
    hours.forEach((entry, hourOfDay) => {
      // Within a day the hours run 0..23 and the indexes are consecutive: (day, hour) identifies one index.
      assert.equal(entry.hour, hourOfDay);
      assert.equal(entry.index, expectedIndex);
      expectedIndex += 1;
    });
    assert.equal(hours[0].label, '12:00 AM');
    assert.equal(hours[12].label, '12:00 PM');
    assert.equal(hours[23].label, '11:00 PM');
  }
  assert.equal(expectedIndex, playback.timestamps.length);
  // The last hour of the simulation is 11 PM local time on September 30, not October 1 (its UTC date).
  const last = playback.timestamps[playback.timestamps.length - 1];
  assert.equal(last.slice(0, 10), '2026-10-01');
  assert.equal(format.dateTime(last), 'Sep 30, 11:00 PM CDT');
});

test('sensor values keep about three significant digits; vibration needs two or three decimals', () => {
  assert.equal(valueDigits(0.113), 3);
  assert.equal(valueDigits(1.08), 2);
  assert.equal(valueDigits(21.44), 1);
  assert.equal(formatValue(0.1134, 'mm/s'), '0.113 mm/s');
  assert.equal(formatValue(1.084, 'mm/s'), '1.08 mm/s');
  assert.equal(formatValue(21.44, '%'), '21.4 %');
  assert.equal(formatValue(68.07, 'psi'), '68.1 psi');
  assert.equal(formatValue(18.2, '°C'), '18.2 °C');
  assert.equal(formatValue(5, 'mm/s'), '5 mm/s');
  assert.equal(formatValue(null, 'psi'), 'No reading');
  assert.equal(formatValue(undefined, 'psi', { missing: '—' }), '—');
  assert.equal(formatValue(12.345, null), '12.3');
});

test('number helpers', () => {
  assert.equal(formatNumber(1234.567, 1), '1,234.6');
  assert.equal(formatNumber(null), '—');
  assert.equal(formatInteger(92028), '92,028');
  assert.equal(formatDelta(2.44, 'psi'), '+2.44 psi');
  assert.equal(formatDelta(-0.312, 'mm/s'), '−0.312 mm/s');
  assert.equal(formatDelta(0, '%'), '±0 %');
  assert.equal(formatScore(0.9341), '0.934');
  assert.equal(formatZ(-64.714), '−64.71');
  assert.equal(formatZ(7.4), '+7.40');
  assert.equal(formatPercent(0.952), '95.2%');
  assert.equal(formatLatLon(37.747438, -100.019496), '37.74744° N, 100.01950° W');
  assert.equal(formatLatLon(null, 3), '—');
  assert.equal(formatDuration(1), '1 h');
  assert.equal(formatDuration(36), '36 h');
  assert.equal(formatDuration(76), '3 d 4 h');
  assert.equal(formatDuration(72), '3 d');
  assert.equal(formatDistance(4.5), '4.5 m');
  assert.equal(formatDistance(152.1), '152 m');
  assert.equal(formatDistance(1234), '1.2 km');
  assert.equal(plural(1, 'anomaly', 'anomalies'), '1 anomaly');
  assert.equal(plural(3, 'anomaly', 'anomalies'), '3 anomalies');
  assert.equal(plural(0, 'sensor'), '0 sensors');
  assert.equal(humanize('road_subgrade'), 'Road subgrade');
  assert.equal(humanize(null), '—');
});
