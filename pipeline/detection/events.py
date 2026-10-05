"""From flagged hours to anomaly events (build contract section 8, steps 5 and 6). Pure numpy, no database.

* Flagged hours of one sensor are merged when the gap between them is at most ``DETECT_MERGE_GAP_HOURS``.
* An event is trimmed to its first and last hour with |z| >= ``DETECT_Z_MIN`` (or a critical-limit breach).
* **Persistence rule** - an event is kept only if its peak |z| reaches ``DETECT_Z_STRONG``, or it has at
  least three flagged hours, or a critical limit is breached. Everything else stays a flagged reading:
  not every unusual reading is an anomaly.
* Score ``= 0.50 * M + 0.25 * D + 0.25 * T`` with M = clip(log2(|z_peak| / 3) / 4, 0, 1),
  D = clip(ln(1 + duration_hours) / ln(97), 0, 1), T = 1 beyond a critical limit, 0.5 beyond a warning
  limit, else 0. Severity: low < 0.30 <= medium < 0.50 <= high < 0.70 <= critical.
* ``anomaly_type`` is a descriptive signature of the readings (sensor type, sign, duration) - never a cause.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from pipeline.config import Settings
from pipeline.detection import detectors
from pipeline.detection.baseline import BoolArray, FloatArray
from pipeline.sensors.thresholds import Threshold

SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
SEVERITY_MIN_SCORE: tuple[tuple[str, float], ...] = (("critical", 0.70), ("high", 0.50), ("medium", 0.30))
SCORE_WEIGHTS: dict[str, float] = {"magnitude": 0.50, "duration": 0.25, "threshold": 0.25}
SCORE_DECIMALS = 3
MAGNITUDE_BASE_Z = 3.0  # |z| at which the magnitude component starts ...
MAGNITUDE_DOUBLINGS = 4.0  # ... and the number of doublings of |z| after which it is 1 (|z| = 48)
DURATION_FULL_HOURS = 96.0  # duration at which the duration component is 1
THRESHOLD_CRITICAL = 1.0
THRESHOLD_WARNING = 0.5
MIN_FLAGGED_HOURS = 3  # an event with fewer flagged hours needs a strong peak or a critical breach

DETECTOR_THRESHOLD = "threshold"
DETECTOR_ZSCORE = "robust_zscore"
DETECTOR_ROLLING = "rolling_median"
DETECTOR_FOREST = "isolation_forest"
DETECTOR_ORDER: tuple[str, ...] = (DETECTOR_THRESHOLD, DETECTOR_ZSCORE, DETECTOR_ROLLING, DETECTOR_FOREST)

STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"

VIBRATION_SPIKE_MAX_HOURS = 3
PRESSURE_DECLINE_MIN_HOURS = 36
TEMPERATURE_DRIFT_MIN_HOURS = 24

# Descriptive signature -> label shown to people. No label names a cause.
ANOMALY_LABELS: dict[str, str] = {
    "vibration_spike": "Short elevated vibration",
    "sustained_high_vibration": "Sustained high vibration",
    "vibration_drop": "Unusually low vibration",
    "moisture_increase": "Unusual moisture increase",
    "moisture_decrease": "Unusual moisture decrease",
    "pressure_drop": "Pressure drop",
    "pressure_spike": "Short pressure excursion",
    "pressure_decline": "Gradual pressure decline",
    "temperature_spike": "Abnormal temperature rise",
    "temperature_drift": "Temperature drift",
    "temperature_drop": "Abnormal temperature drop",
}
GENERIC_LABEL = "Unusual readings"


@dataclass(frozen=True, slots=True)
class DetectionParams:
    """Detector settings expressed in time steps of the analysed grid."""

    z_strong: float
    z_min: float
    rolling_steps: int
    iforest_threshold: float
    merge_gap_steps: int
    step_hours: float = 1.0
    rolling_level: float = detectors.ROLLING_MEDIAN_LEVEL
    rolling_min_readings: int = detectors.ROLLING_MIN_READINGS
    min_flagged_hours: int = MIN_FLAGGED_HOURS

    @classmethod
    def from_settings(cls, settings: Settings, step_hours: float = 1.0) -> DetectionParams:
        """Translate the DETECT_* settings (hours) into steps of ``step_hours``."""
        return cls(
            z_strong=settings.DETECT_Z_STRONG,
            z_min=settings.DETECT_Z_MIN,
            rolling_steps=max(int(round(settings.DETECT_ROLLING_HOURS / step_hours)), 1),
            iforest_threshold=settings.DETECT_IFOREST_THRESHOLD,
            merge_gap_steps=max(int(math.floor(settings.DETECT_MERGE_GAP_HOURS / step_hours + 1e-9)), 0),
            step_hours=step_hours,
        )


@dataclass(frozen=True, slots=True)
class HourFlags:
    """Detector output of one sensor over the time axis (False where there is no reading)."""

    critical: BoolArray  # threshold detector: beyond a critical limit
    strong: BoolArray  # robust z-score detector: |z| >= z_strong
    rolling: BoolArray  # rolling-median detector
    moderate: BoolArray  # |z| >= z_min: counts as a flagged hour for merging
    forest: BoolArray  # Isolation-Forest-positive (corroboration only)

    @property
    def flagged(self) -> BoolArray:
        """Hours that take part in merging (stored as ``reading_scores.flagged``)."""
        return self.critical | self.strong | self.rolling | self.moderate

    @property
    def core(self) -> BoolArray:
        """Hours an event may start or end on."""
        return self.moderate | self.critical


@dataclass(frozen=True, slots=True)
class Breach:
    """A limit that readings of an event went beyond."""

    level: str  # 'critical' | 'warning'
    side: str  # 'below' | 'above'
    limit: float
    extreme: float  # the lowest (side 'below') or highest (side 'above') reading of the event


@dataclass(frozen=True, slots=True)
class Event:
    """One anomaly event of one sensor, in indices of the time axis."""

    start: int
    end: int
    peak: int
    flagged_hours: int
    duration_hours: int
    robust_z: float
    components: dict[str, float]
    score: float
    severity: str
    methods: tuple[str, ...]
    anomaly_type: str
    status: str
    breach: Breach | None


def hour_flags(
    values: FloatArray, z: FloatArray, forest_scores: FloatArray, limits: Threshold, params: DetectionParams
) -> HourFlags:
    """Run the detectors on one sensor's series."""
    return HourFlags(
        critical=detectors.beyond_limits(values, limits.crit_low, limits.crit_high),
        strong=detectors.at_or_above(z, params.z_strong),
        rolling=detectors.rolling_median_flags(
            z, params.rolling_steps, params.rolling_level, params.rolling_min_readings
        ),
        moderate=detectors.at_or_above(z, params.z_min),
        forest=detectors.corroborating(forest_scores, z, params.iforest_threshold, params.z_min),
    )


def merge_runs(flagged: BoolArray, max_gap_steps: int) -> list[tuple[int, int]]:
    """(first, last) index of every group of flagged steps separated by at most ``max_gap_steps`` unflagged ones."""
    indices = np.flatnonzero(flagged)
    if indices.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(indices) > max_gap_steps + 1)
    starts = np.concatenate(([indices[0]], indices[breaks + 1]))
    ends = np.concatenate((indices[breaks], [indices[-1]]))
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def score_components(peak_z: float, duration_hours: int, breach_level: str | None) -> dict[str, float]:
    """The three parts of the anomaly score: magnitude M, duration D and threshold T (each 0..1)."""
    ratio = abs(peak_z) / MAGNITUDE_BASE_Z
    magnitude = min(max(math.log2(ratio) / MAGNITUDE_DOUBLINGS, 0.0), 1.0) if ratio > 0 else 0.0
    duration = min(max(math.log(1.0 + duration_hours) / math.log(1.0 + DURATION_FULL_HOURS), 0.0), 1.0)
    threshold = {"critical": THRESHOLD_CRITICAL, "warning": THRESHOLD_WARNING}.get(breach_level or "", 0.0)
    return {"magnitude": magnitude, "duration": duration, "threshold": threshold}


def anomaly_score(components: dict[str, float]) -> float:
    """Weighted sum of the score components, rounded to three decimals (0..1)."""
    return round(sum(SCORE_WEIGHTS[name] * components[name] for name in SCORE_WEIGHTS), SCORE_DECIMALS)


def severity_for(score: float) -> str:
    """Severity band of an anomaly score."""
    for name, minimum in SEVERITY_MIN_SCORE:
        if score >= minimum:
            return name
    return "low"


def classify(sensor_type: str, peak_z: float, duration_hours: int) -> str:
    """Descriptive signature of an event from its sensor type, the sign of its peak and its duration."""
    above = peak_z > 0
    if sensor_type == "vibration":
        if not above:
            return "vibration_drop"
        return "vibration_spike" if duration_hours <= VIBRATION_SPIKE_MAX_HOURS else "sustained_high_vibration"
    if sensor_type == "moisture":
        return "moisture_increase" if above else "moisture_decrease"
    if sensor_type == "pressure":
        if above:
            return "pressure_spike"
        return "pressure_decline" if duration_hours >= PRESSURE_DECLINE_MIN_HOURS else "pressure_drop"
    if sensor_type == "temperature":
        if duration_hours >= TEMPERATURE_DRIFT_MIN_HOURS:
            return "temperature_drift"
        return "temperature_spike" if above else "temperature_drop"
    return f"{sensor_type}_anomaly"


def anomaly_label(anomaly_type: str) -> str:
    """Human label of a signature."""
    return ANOMALY_LABELS.get(anomaly_type, GENERIC_LABEL)


def find_breach(values: FloatArray, limits: Threshold) -> Breach | None:
    """The most serious limit the given readings went beyond (critical before warning), or None."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    lowest, highest = float(finite.min()), float(finite.max())
    for level, low, high in (
        ("critical", limits.crit_low, limits.crit_high),
        ("warning", limits.warn_low, limits.warn_high),
    ):
        if low is not None and lowest < low:
            return Breach(level, "below", float(low), lowest)
        if high is not None and highest > high:
            return Breach(level, "above", float(high), highest)
    return None


def fired_detectors(flags: HourFlags, start: int, end: int) -> tuple[str, ...]:
    """Detectors with at least one positive hour inside the event, in the contract's order.

    An event made only of moderate hours (|z| >= z_min, kept by the persistence rule) is attributed to the
    robust z-score, the detector those flags come from.
    """
    window = slice(start, end + 1)
    fired = {
        DETECTOR_THRESHOLD: bool(flags.critical[window].any()),
        DETECTOR_ZSCORE: bool(flags.strong[window].any()),
        DETECTOR_ROLLING: bool(flags.rolling[window].any()),
        DETECTOR_FOREST: bool(flags.forest[window].any()),
    }
    if not (fired[DETECTOR_THRESHOLD] or fired[DETECTOR_ZSCORE] or fired[DETECTOR_ROLLING]):
        fired[DETECTOR_ZSCORE] = True
    return tuple(name for name in DETECTOR_ORDER if fired[name])


def find_events(
    sensor_type: str,
    values: FloatArray,
    z: FloatArray,
    flags: HourFlags,
    limits: Threshold,
    params: DetectionParams,
) -> list[Event]:
    """Merge, trim and filter the flagged hours of one sensor into anomaly events (in time order)."""
    flagged, core = flags.flagged, flags.core
    last_index = len(z) - 1
    events: list[Event] = []
    for run_start, run_end in merge_runs(flagged, params.merge_gap_steps):
        core_steps = np.flatnonzero(core[run_start : run_end + 1])
        if core_steps.size == 0:
            continue
        start, end = run_start + int(core_steps[0]), run_start + int(core_steps[-1])
        hours = start + np.flatnonzero(flagged[start : end + 1])
        magnitudes = np.abs(z[hours])
        if not np.isfinite(magnitudes).any():
            continue
        peak = int(hours[int(np.nanargmax(magnitudes))])
        peak_z = float(z[peak])
        critical = bool(flags.critical[start : end + 1].any())
        if not (abs(peak_z) >= params.z_strong or hours.size >= params.min_flagged_hours or critical):
            continue  # the hours stay flagged readings only
        duration_hours = int(round((end - start) * params.step_hours)) + 1
        breach = find_breach(values[start : end + 1], limits)
        components = score_components(peak_z, duration_hours, breach.level if breach else None)
        score = anomaly_score(components)
        # One definition of "active" (build contract 10.1 / amendment A1): the event is still running at the last
        # time step. An event that ended earlier - even one step earlier - is resolved.
        active = end >= last_index
        events.append(
            Event(
                start=start,
                end=end,
                peak=peak,
                flagged_hours=int(hours.size),
                duration_hours=duration_hours,
                robust_z=peak_z,
                components={name: round(value, SCORE_DECIMALS) for name, value in components.items()},
                score=score,
                severity=severity_for(score),
                methods=fired_detectors(flags, start, end),
                anomaly_type=classify(sensor_type, peak_z, duration_hours),
                status=STATUS_ACTIVE if active else STATUS_RESOLVED,
                breach=breach,
            )
        )
    return events
