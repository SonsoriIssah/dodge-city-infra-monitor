"""Per-sensor baselines and robust z-scores (build contract section 8, steps 1-3). Pure numpy, no database.

For the sensors of ONE sensor type, given a matrix of readings (sensor x time step, NaN = no reading):

1. **Work domain.** Vibration is modelled in ln(mm/s) because its scatter grows with its level; the other
   types are modelled in their native unit.
2. **Profile.** ``p_i`` = median of the sensor's values by local hour of day (vibration: additionally split
   into weekdays and weekends). Residual ``r_i = x_i - p_i``.
3. **Peer adjustment** (temperature and moisture - the "spatial anomaly analysis" step). For every
   placement class ``g`` of the type with at least four sensors, ``M_g(t)`` is the median over the class of
   ``r_j(t) / s_j`` with ``s_j = max(1.4826 * MAD(r_j), floor)``. Each sensor's residual is regressed on
   ALL class medians of its type (moisture: also on their exponential moving averages over 24 h and 72 h,
   which lets a sensor dry down slower or faster than its class) by Huber-weighted least squares without
   an intercept. What the sensors of a class do together (weather) is therefore expected; what one sensor
   does alone is not. The profile is then estimated once more on the peer-adjusted series (the value minus
   the fitted weather part) and the regression is repeated: with the weather removed, the hourly median no
   longer depends on which days happened to be warm or wet, which matters on short data windows.
4. **Robust z.** ``z = (x - expected) / max(1.4826 * MAD, floor)`` where ``expected`` = profile + peer
   adjustment + the median of what is left. ``expected`` is returned in native units together with the band
   ``expected +- 3`` robust sigma (vibration: ``expected * exp(-+ 3 * scale)``).
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]
IntArray = NDArray[np.int64]

MAD_TO_SIGMA = 1.4826  # MAD of a normal distribution times this factor is its standard deviation
# Smallest robust sigma per sensor type, in work-domain units (deg C, ln-units, % water content, psi).
SCALE_FLOORS: dict[str, float] = {"temperature": 0.5, "vibration": 0.10, "moisture": 0.5, "pressure": 0.5}
DEFAULT_SCALE_FLOOR = 1e-6  # a sensor type without a configured floor
MIN_CLASS_SENSORS = 4  # a placement class needs this many sensors to provide a peer median
# Temperature classes too small for a peer median are compared less strictly (wider floor).
SMALL_CLASS_FLOORS: dict[str, float] = {"temperature": 1.5}
LOG_DOMAIN_TYPES: frozenset[str] = frozenset({"vibration"})
WEEKPART_PROFILE_TYPES: frozenset[str] = frozenset({"vibration"})
PEER_ADJUSTED_TYPES: frozenset[str] = frozenset({"temperature", "moisture"})
PEER_EMA_HOURS: dict[str, tuple[float, ...]] = {"moisture": (24.0, 72.0)}
HUBER_C = 1.345
HUBER_ITERATIONS = 8
BAND_SIGMAS = 3.0  # expected_low / expected_high = expected -+ this many robust sigma
LOG_VALUE_FLOOR = 1e-3  # mm/s; a non-positive vibration value is modelled as this level
WEEKEND_FIRST_WEEKDAY = 5  # datetime.weekday(): Saturday
HOURS_PER_DAY = 24
# Times the hourly profile is re-estimated on the peer-adjusted series (peer-adjusted types only).
PROFILE_REFINEMENTS = 1


@dataclass(frozen=True, slots=True)
class TypeBaseline:
    """Baseline of every sensor of one sensor type (rows = sensors in input order, columns = time steps)."""

    sensor_type: str
    log_domain: bool
    expected: FloatArray  # native units, defined at every time step
    expected_low: FloatArray
    expected_high: FloatArray
    z: FloatArray  # robust z-score, NaN where the sensor has no reading
    scale: FloatArray  # per sensor: robust sigma in the work domain (after the floor)
    floor: FloatArray  # per sensor: the floor that applied
    center: FloatArray  # per sensor: median of the residual that was folded into ``expected``
    peer_classes: tuple[str, ...]  # placement classes whose medians were used as regressors


def local_time_keys(timestamps: Sequence[datetime], tz: tzinfo) -> tuple[IntArray, BoolArray]:
    """Local hour of day (0-23) and weekend flag (Saturday, Sunday) of every timestamp."""
    local = [ts.astimezone(tz) for ts in timestamps]
    hours = np.array([moment.hour for moment in local], dtype=np.int64)
    weekend = np.array([moment.weekday() >= WEEKEND_FIRST_WEEKDAY for moment in local], dtype=bool)
    return hours, weekend


def to_work_domain(values: FloatArray, log_domain: bool) -> FloatArray:
    """Values in the detector's work domain: natural log for vibration, unchanged otherwise."""
    values = np.asarray(values, dtype=float)
    if not log_domain:
        return values.copy()
    with np.errstate(invalid="ignore"):
        return np.log(np.where(np.isnan(values), np.nan, np.maximum(values, LOG_VALUE_FLOOR)))


def from_work_domain(work: FloatArray, log_domain: bool) -> FloatArray:
    """Back-transform work-domain values to native units."""
    return np.exp(work) if log_domain else np.asarray(work, dtype=float).copy()


def nan_median(values: FloatArray, axis: int | None = None) -> FloatArray:
    """``np.nanmedian`` without the warning for slices that hold no value (those give NaN)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmedian(values, axis=axis)


def mad_sigma(residual: FloatArray) -> tuple[float, float]:
    """Median and robust sigma (1.4826 * MAD) of a series, ignoring NaN; (NaN, NaN) when it is empty."""
    median = float(nan_median(residual))
    sigma = float(MAD_TO_SIGMA * nan_median(np.abs(residual - median)))
    return median, sigma


def hourly_profile(work: FloatArray, local_hour: IntArray, weekend: BoolArray | None = None) -> FloatArray:
    """Median by local hour of day for every sensor row; with ``weekend`` also by weekday / weekend.

    A bucket in which a sensor has no reading falls back to that sensor's overall median.
    """
    work = np.atleast_2d(np.asarray(work, dtype=float))
    profile = np.full(work.shape, np.nan)
    parts = (None,) if weekend is None else (False, True)
    for hour in range(HOURS_PER_DAY):
        for part in parts:
            bucket = local_hour == hour if part is None else (local_hour == hour) & (weekend == part)
            if bucket.any():
                profile[:, bucket] = nan_median(work[:, bucket], axis=1)[:, None]
    overall = nan_median(work, axis=1)[:, None]
    return np.where(np.isnan(profile), overall, profile)


def ema(series: FloatArray, span_hours: float, step_hours: float = 1.0) -> FloatArray:
    """Exponential moving average with time constant ``span_hours``, started at zero; NaN holds the level."""
    alpha = 1.0 - float(np.exp(-step_hours / span_hours))
    out = np.zeros(len(series))
    level = 0.0
    for i, value in enumerate(series):
        if i > 0 and not np.isnan(value):
            level += alpha * (float(value) - level)
        out[i] = level
    return out


def class_median(residual: FloatArray, floor: float) -> FloatArray:
    """``M_g(t)``: median over the sensors of a class of their residual divided by their robust sigma."""
    scales = np.array([max(mad_sigma(row)[1], floor) if np.isfinite(row).any() else np.nan for row in residual])
    return nan_median(residual / scales[:, None], axis=0)


def huber_fit(
    features: FloatArray, target: FloatArray, c: float = HUBER_C, iterations: int = HUBER_ITERATIONS
) -> FloatArray:
    """Huber-weighted least squares without intercept (IRLS); rows where the target is NaN are ignored.

    Returns the coefficient vector; all zeros when there are fewer usable rows than coefficients.
    """
    usable = np.isfinite(target) & np.isfinite(features).all(axis=1)
    x, y = features[usable], target[usable]
    beta = np.zeros(features.shape[1])
    if len(y) <= features.shape[1]:
        return beta
    weights = np.ones(len(y))
    for _ in range(max(iterations, 1)):
        root = np.sqrt(weights)
        beta, *_ = np.linalg.lstsq(x * root[:, None], y * root, rcond=None)
        residual = y - x @ beta
        sigma = max(MAD_TO_SIGMA * float(np.median(np.abs(residual - np.median(residual)))), 1e-9)
        ratio = np.abs(residual) / (c * sigma)
        weights = np.where(ratio <= 1.0, 1.0, 1.0 / np.maximum(ratio, 1.0))
    return beta


def peer_features(
    residual: FloatArray, placements: Sequence[str], sensor_type: str, step_hours: float = 1.0
) -> tuple[FloatArray, tuple[str, ...]]:
    """Regressors of the peer adjustment: one column per class median (plus its moving averages for moisture).

    Returns ``(features, classes)``; ``features`` has no column when the type is not peer-adjusted or no
    placement class has enough sensors.
    """
    steps = residual.shape[1]
    if sensor_type not in PEER_ADJUSTED_TYPES:
        return np.zeros((steps, 0)), ()
    floor = SCALE_FLOORS.get(sensor_type, DEFAULT_SCALE_FLOOR)
    columns: list[FloatArray] = []
    classes: list[str] = []
    for name in sorted(set(placements)):
        rows = [i for i, placement in enumerate(placements) if placement == name]
        if len(rows) < MIN_CLASS_SENSORS:
            continue
        median = class_median(residual[rows], floor)
        columns.append(np.nan_to_num(median, nan=0.0))
        columns.extend(ema(median, span, step_hours) for span in PEER_EMA_HOURS.get(sensor_type, ()))
        classes.append(name)
    if not columns:
        return np.zeros((steps, 0)), ()
    return np.column_stack(columns), tuple(classes)


def peer_adjustments(residual: FloatArray, features: FloatArray) -> FloatArray:
    """Peer adjustment of every sensor: its Huber fit on the class-median regressors (zeros without regressors)."""
    adjustment = np.zeros(residual.shape)
    if features.shape[1] == 0:
        return adjustment
    for i, row in enumerate(residual):
        if np.isfinite(row).any():
            adjustment[i] = features @ huber_fit(features, row)
    return adjustment


def scale_floor(sensor_type: str, class_size: int) -> float:
    """Smallest robust sigma for a sensor of this type in a placement class of this size."""
    if class_size < MIN_CLASS_SENSORS and sensor_type in SMALL_CLASS_FLOORS:
        return SMALL_CLASS_FLOORS[sensor_type]
    return SCALE_FLOORS.get(sensor_type, DEFAULT_SCALE_FLOOR)


def fit_type_baseline(
    values: FloatArray,
    placements: Sequence[str],
    sensor_type: str,
    local_hour: IntArray,
    weekend: BoolArray,
    step_hours: float = 1.0,
) -> TypeBaseline:
    """Fit the baseline of every sensor of one type.

    ``values`` is (sensors x time steps) in native units with NaN where a sensor has no reading;
    ``placements`` gives the placement class of every row; ``local_hour`` / ``weekend`` describe the columns.
    """
    values = np.atleast_2d(np.asarray(values, dtype=float))
    sensors, steps = values.shape
    if len(placements) != sensors or len(local_hour) != steps or len(weekend) != steps:
        raise ValueError("values, placements and the time keys do not have matching shapes")
    log_domain = sensor_type in LOG_DOMAIN_TYPES
    work = to_work_domain(values, log_domain)
    weekpart = weekend if sensor_type in WEEKPART_PROFILE_TYPES else None
    profile = hourly_profile(work, local_hour, weekpart)
    features, classes = peer_features(work - profile, placements, sensor_type, step_hours)
    adjustment = peer_adjustments(work - profile, features)
    for _ in range(PROFILE_REFINEMENTS if features.shape[1] else 0):
        profile = hourly_profile(work - adjustment, local_hour, weekpart)
        features, classes = peer_features(work - profile, placements, sensor_type, step_hours)
        adjustment = peer_adjustments(work - profile, features)
    residual = work - profile
    class_sizes = {name: sum(1 for placement in placements if placement == name) for name in set(placements)}

    expected_work = np.full((sensors, steps), np.nan)
    z = np.full((sensors, steps), np.nan)
    scale = np.full(sensors, np.nan)
    floor = np.array([scale_floor(sensor_type, class_sizes[placement]) for placement in placements], dtype=float)
    center = np.zeros(sensors)
    for i in range(sensors):
        if not np.isfinite(residual[i]).any():
            continue  # a sensor without any reading has no baseline
        remainder = residual[i] - adjustment[i]
        center[i], sigma = mad_sigma(remainder)
        scale[i] = max(sigma, floor[i])
        expected_work[i] = profile[i] + adjustment[i] + center[i]
        z[i] = (remainder - center[i]) / scale[i]

    band = BAND_SIGMAS * scale[:, None]
    return TypeBaseline(
        sensor_type=sensor_type,
        log_domain=log_domain,
        expected=from_work_domain(expected_work, log_domain),
        expected_low=from_work_domain(expected_work - band, log_domain),
        expected_high=from_work_domain(expected_work + band, log_domain),
        z=z,
        scale=scale,
        floor=floor,
        center=center,
        peer_classes=classes,
    )
