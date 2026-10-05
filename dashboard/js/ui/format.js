/**
 * Formatting of times and numbers (build contract 12.7).
 *
 * Every time on screen goes through `createFormatter(meta.study_area.timezone)`: the zone comes from the
 * data, never from the browser, and the abbreviation ("CDT") is produced by Intl, never written by hand.
 * Times are compared as epoch milliseconds; the date and hour pickers are built by formatting the playback
 * timestamps with this module and mapping back by index.
 *
 * Pure module: no DOM access, importable from Node tests.
 */

const LOCALE = 'en-US';
const MISSING = '—';
/** Intl puts narrow or non-breaking spaces before "AM"/"PM" on recent ICU versions; the UI uses plain spaces. */
const ODD_SPACES = /[   ]/g;

/** Epoch milliseconds of an ISO string, a Date or a number; NaN when it cannot be read. */
export function toEpochMs(value) {
  if (typeof value === 'number') return value;
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'string' && value !== '') return Date.parse(value);
  return Number.NaN;
}

function partsOf(formatter, ms) {
  const parts = {};
  for (const part of formatter.formatToParts(new Date(ms))) {
    if (part.type !== 'literal') parts[part.type] = part.value.replace(ODD_SPACES, ' ');
  }
  return parts;
}

/**
 * Formatter bound to one IANA time zone.
 *
 * @param {string} timeZone e.g. "America/Chicago" (from `meta.study_area.timezone`)
 * @returns {object} functions that accept an ISO string, a Date or epoch milliseconds
 */
export function createFormatter(timeZone, locale = LOCALE) {
  if (!timeZone) throw new Error('createFormatter needs the time zone of the study area');
  const make = (options) => new Intl.DateTimeFormat(locale, { timeZone, ...options });
  const clock = make({
    weekday: 'short',
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
    timeZoneName: 'short',
  });
  const numeric = make({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    hourCycle: 'h23',
  });

  const read = (value) => {
    const ms = toEpochMs(value);
    return Number.isFinite(ms) ? partsOf(clock, ms) : null;
  };
  const hourText = (p) => `${p.hour}:${p.minute} ${p.dayPeriod}`;

  return {
    timeZone,

    /** "Sep 1, 12:00 AM CDT" */
    dateTime(value) {
      const p = read(value);
      return p ? `${p.month} ${p.day}, ${hourText(p)} ${p.timeZoneName}` : MISSING;
    },

    /** "Tue, Sep 1, 2026, 12:00 AM CDT" */
    dateTimeLong(value) {
      const p = read(value);
      return p ? `${p.weekday}, ${p.month} ${p.day}, ${p.year}, ${hourText(p)} ${p.timeZoneName}` : MISSING;
    },

    /** "Sep 1, 12:00 AM" (no zone; for dense lists whose header names the zone) */
    dateTimeShort(value) {
      const p = read(value);
      return p ? `${p.month} ${p.day}, ${hourText(p)}` : MISSING;
    },

    /** "Sep 1" */
    date(value) {
      const p = read(value);
      return p ? `${p.month} ${p.day}` : MISSING;
    },

    /** "Tue, Sep 1" */
    dateWithWeekday(value) {
      const p = read(value);
      return p ? `${p.weekday}, ${p.month} ${p.day}` : MISSING;
    },

    /** "Sep 1, 2026" */
    dateLong(value) {
      const p = read(value);
      return p ? `${p.month} ${p.day}, ${p.year}` : MISSING;
    },

    /** "12:00 AM" */
    hour(value) {
      const p = read(value);
      return p ? hourText(p) : MISSING;
    },

    /** "CDT" - the zone abbreviation in force at that instant. */
    zoneName(value) {
      const p = read(value);
      return p ? p.timeZoneName : '';
    },

    /** "2026-09-01": the calendar day in the study-area zone; sorts and compares as text. */
    dayKey(value) {
      const ms = toEpochMs(value);
      if (!Number.isFinite(ms)) return '';
      const p = partsOf(numeric, ms);
      return `${p.year}-${p.month}-${p.day}`;
    },

    /** 0-23: the hour of the day in the study-area zone. */
    hourOfDay(value) {
      const ms = toEpochMs(value);
      if (!Number.isFinite(ms)) return Number.NaN;
      return Number(partsOf(numeric, ms).hour) % 24;
    },
  };
}

/** Number with at most `digits` decimals and thousands separators; "—" for null/NaN. */
export function formatNumber(value, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return MISSING;
  return Number(value).toLocaleString(LOCALE, { maximumFractionDigits: digits });
}

/** Whole number with thousands separators. */
export function formatInteger(value) {
  return formatNumber(value, 0);
}

/** Decimals that keep about three significant digits for a sensor value (0.113 mm/s, 21.4 %, 68.1 psi). */
export function valueDigits(value) {
  const magnitude = Math.abs(Number(value));
  if (!Number.isFinite(magnitude)) return 1;
  if (magnitude < 1) return 3;
  if (magnitude < 10) return 2;
  return 1;
}

/** Sensor value with its unit: "21.4 %", "0.113 mm/s", "18.2 °C"; "No reading" when the value is missing. */
export function formatValue(value, unit, { missing = 'No reading' } = {}) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return missing;
  const text = Number(value).toLocaleString(LOCALE, {
    minimumFractionDigits: 0,
    maximumFractionDigits: valueDigits(value),
  });
  return unit ? `${text} ${unit}` : text;
}

/** Signed difference with unit: "+2.4 psi", "−0.31 mm/s". */
export function formatDelta(value, unit) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return MISSING;
  const number = Number(value);
  const text = Math.abs(number).toLocaleString(LOCALE, { maximumFractionDigits: valueDigits(number) });
  const sign = number > 0 ? '+' : number < 0 ? '−' : '±';
  return `${sign}${text}${unit ? ` ${unit}` : ''}`;
}

/** Anomaly score, risk weight: three decimals. */
export function formatScore(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return MISSING;
  return Number(value).toFixed(3);
}

/** Robust z-score: signed, two decimals. */
export function formatZ(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return MISSING;
  const number = Number(value);
  return `${number > 0 ? '+' : number < 0 ? '−' : ''}${Math.abs(number).toFixed(2)}`;
}

/** Share between 0 and 1 as a percentage: 0.952 -> "95.2%". */
export function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return MISSING;
  return `${(Number(value) * 100).toLocaleString(LOCALE, { maximumFractionDigits: digits })}%`;
}

/** Latitude/longitude pair with five decimals: "37.75347° N, 100.01750° W". */
export function formatLatLon(lat, lon, digits = 5) {
  // Number(null) and Number('') are 0: a missing coordinate must not be printed as 0°.
  const missing = (value) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value));
  if (missing(lat) || missing(lon)) return MISSING;
  const ns = Number(lat) >= 0 ? 'N' : 'S';
  const ew = Number(lon) >= 0 ? 'E' : 'W';
  return `${Math.abs(Number(lat)).toFixed(digits)}° ${ns}, ${Math.abs(Number(lon)).toFixed(digits)}° ${ew}`;
}

/** Duration in hours: "1 h", "19 h", "3 d 4 h". */
export function formatDuration(hours) {
  if (hours === null || hours === undefined || !Number.isFinite(Number(hours))) return MISSING;
  const whole = Math.round(Number(hours));
  if (whole < 48) return `${whole} h`;
  const days = Math.floor(whole / 24);
  const rest = whole % 24;
  return rest ? `${days} d ${rest} h` : `${days} d`;
}

/** Distance in metres: "4.5 m", "152 m", "1.2 km". */
export function formatDistance(metres) {
  if (metres === null || metres === undefined || !Number.isFinite(Number(metres))) return MISSING;
  const value = Number(metres);
  if (value >= 1000) return `${(value / 1000).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} km`;
  return `${value.toLocaleString(LOCALE, { maximumFractionDigits: value < 100 ? 1 : 0 })} m`;
}

/** "1 anomaly" / "3 anomalies": count with the right noun form. */
export function plural(count, singular, pluralForm = `${singular}s`) {
  const number = Number(count);
  return `${formatInteger(number)} ${number === 1 ? singular : pluralForm}`;
}

/** "road_subgrade" -> "Road subgrade". */
export function humanize(text) {
  if (text === null || text === undefined || text === '') return MISSING;
  const words = String(text).replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}
