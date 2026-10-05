/**
 * What js/ui/format.js prints for the first and last hour of the committed snapshot and a few fixed instants.
 * Imported by format.test.js in this process and in child processes started with another TZ: the output must
 * be identical whatever the zone of the machine. Not a test file.
 */

import { readFileSync } from 'node:fs';

import { createFormatter } from '../../js/ui/format.js';

const read = (name) => JSON.parse(readFileSync(new URL(`../../data/snapshot/${name}`, import.meta.url), 'utf8'));

export function renderSamples() {
  const zone = read('meta.json').study_area.timezone;
  const timestamps = read('playback.json').timestamps;
  const format = createFormatter(zone);
  const first = timestamps[0];
  const last = timestamps[timestamps.length - 1];
  const sampled = timestamps.filter((_, index) => index % 97 === 0);
  return {
    zone,
    first: format.dateTime(first),
    firstLong: format.dateTimeLong(first),
    firstShort: format.dateTimeShort(first),
    firstDay: format.dayKey(first),
    firstHour: format.hourOfDay(first),
    last: format.dateTime(last),
    lastDay: format.dayKey(last),
    noon: format.dateTime('2026-09-15T17:00:00Z'),
    winter: format.dateTime('2026-12-01T06:00:00Z'),
    dayKeys: sampled.map((iso) => format.dayKey(iso)),
    hours: sampled.map((iso) => format.hour(iso)),
  };
}
