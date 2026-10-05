"""The four detectors of the prototype (build contract section 8, step 4): small functions over arrays.

Every function works on the last axis (time), so it accepts one series or a (sensors x time steps) matrix.
A missing reading is NaN and is never flagged.

``threshold``         value beyond the critical limits of its (sensor type, placement) class
``robust_zscore``     |z| at or above the strong level
``rolling_median``    |median of z over the trailing window| at or above a level, with enough readings in
                      the window (either sign: sustained shifts and drift)
``isolation_forest``  scikit-learn Isolation Forest on short-term features of z, one model per sensor type;
                      the original-paper score in (0, 1]. Corroborating evidence only: it is reported with an
                      event but never creates or extends one.

scikit-learn is imported inside ``isolation_forest_scores`` only: the health score and the API import this
module for its small array helpers and must not pay for (or depend on) loading scikit-learn.
"""

from __future__ import annotations

import warnings

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from pipeline.detection.baseline import BoolArray, FloatArray

ROLLING_MEDIAN_LEVEL = 3.0  # |trailing median of z| at or above this is a sustained shift
ROLLING_MIN_READINGS = 4  # readings needed in the trailing window
IFOREST_ESTIMATORS = 100
IFOREST_MAX_SAMPLES = 256
IFOREST_SHORT_HOURS = 3.0  # window of the short mean / standard deviation features
IFOREST_LONG_HOURS = 12.0  # window of the long mean feature
IFOREST_FEATURES = ("z", "delta_z", "mean_3h", "std_3h", "mean_12h")


def beyond_limits(values: FloatArray, low: float | None, high: float | None) -> BoolArray:
    """True where a value lies below ``low`` or above ``high`` (None = no limit on that side)."""
    values = np.asarray(values, dtype=float)
    out = np.zeros(values.shape, dtype=bool)
    with np.errstate(invalid="ignore"):
        if low is not None:
            out |= values < low
        if high is not None:
            out |= values > high
    return out


def at_or_above(z: FloatArray, level: float) -> BoolArray:
    """True where |z| is at or above ``level`` (the robust z-score detector for the strong level)."""
    with np.errstate(invalid="ignore"):
        return np.abs(np.asarray(z, dtype=float)) >= level


def trailing_windows(series: FloatArray, window: int) -> FloatArray:
    """View of the trailing ``window`` values at every time step (NaN before the start); last axis = window."""
    series = np.asarray(series, dtype=float)
    window = max(int(window), 1)
    pad = [(0, 0)] * (series.ndim - 1) + [(window - 1, 0)]
    return sliding_window_view(np.pad(series, pad, constant_values=np.nan), window, axis=-1)


def trailing_median(series: FloatArray, window: int, min_readings: int = 1) -> FloatArray:
    """Median of the readings in the trailing window; NaN where it holds fewer than ``min_readings``."""
    windows = trailing_windows(series, window)
    count = np.isfinite(windows).sum(axis=-1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        median = np.nanmedian(windows, axis=-1)
    return np.where(count >= max(min_readings, 1), median, np.nan)


def rolling_median_flags(
    z: FloatArray, window: int, level: float = ROLLING_MEDIAN_LEVEL, min_readings: int = ROLLING_MIN_READINGS
) -> BoolArray:
    """True where the |trailing median of z| is at or above ``level`` and the step itself has a reading."""
    z = np.asarray(z, dtype=float)
    with np.errstate(invalid="ignore"):
        return (np.abs(trailing_median(z, window, min_readings)) >= level) & np.isfinite(z)


def iforest_features(z: FloatArray, step_hours: float = 1.0) -> FloatArray:
    """Feature rows [z, change since the previous step, 3 h mean, 3 h std, 12 h mean] of one z series.

    Rows of time steps without a reading are NaN. A missing previous reading gives a change of zero.
    """
    z = np.asarray(z, dtype=float)
    short = max(int(round(IFOREST_SHORT_HOURS / step_hours)), 1)
    long = max(int(round(IFOREST_LONG_HOURS / step_hours)), 1)
    previous = np.concatenate(([np.nan], z[:-1]))
    delta = np.where(np.isfinite(previous), z - previous, 0.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mean_short = np.nanmean(trailing_windows(z, short), axis=-1)
        std_short = np.nanstd(trailing_windows(z, short), axis=-1)
        mean_long = np.nanmean(trailing_windows(z, long), axis=-1)
    features = np.column_stack([z, delta, mean_short, std_short, mean_long])
    features[~np.isfinite(z)] = np.nan
    return features


def isolation_forest_scores(z: FloatArray, seed: int, step_hours: float = 1.0) -> FloatArray:
    """Isolation Forest score of every reading of the sensors of ONE sensor type.

    ``z`` is (sensors x time steps). One model is fitted on the feature rows of all those sensors; the
    result has the shape of ``z`` with NaN where there is no reading. The score is
    ``-model.score_samples(X)``: the anomaly score of the original paper in (0, 1], about 0.5 for ordinary
    readings, not rescaled.
    """
    from sklearn.ensemble import IsolationForest  # deferred: see the module docstring

    z = np.atleast_2d(np.asarray(z, dtype=float))
    scores = np.full(z.shape, np.nan)
    blocks = [iforest_features(row, step_hours) for row in z]
    valid = [np.isfinite(block).all(axis=1) for block in blocks]
    rows = np.vstack([block[mask] for block, mask in zip(blocks, valid)])
    if len(rows) < 2:
        return scores
    model = IsolationForest(
        n_estimators=IFOREST_ESTIMATORS, max_samples=min(IFOREST_MAX_SAMPLES, len(rows)), random_state=seed
    ).fit(rows)
    paper_scores = -model.score_samples(rows)
    offset = 0
    for i, mask in enumerate(valid):
        count = int(mask.sum())
        scores[i, mask] = paper_scores[offset : offset + count]
        offset += count
    return scores


def corroborating(scores: FloatArray, z: FloatArray, threshold: float, z_min: float) -> BoolArray:
    """Isolation-Forest-positive hours: score at or above ``threshold`` AND |z| at or above ``z_min``."""
    with np.errstate(invalid="ignore"):
        return (np.asarray(scores, dtype=float) >= threshold) & (np.abs(np.asarray(z, dtype=float)) >= z_min)
