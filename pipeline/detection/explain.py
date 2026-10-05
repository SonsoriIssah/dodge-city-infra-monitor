"""Plain-English explanation of an anomaly event (build contract section 8, step 6).

The text states what was observed, what was expected, how far apart they are, how long it lasted, which
detectors fired and how the score is composed. It is descriptive only: it never names a cause and never
makes a statement about the condition or safety of the asset.

Example::

    Sustained high vibration on VIB-003 (bridge deck): 4.9 mm/s at peak versus an expected 1.6 mm/s for
    that hour - 7.4 robust standard deviations (log scale) above this sensor's baseline, 3.1x the expected
    level, lasting 19 h. Flagged by robust z-score and rolling median; corroborated by Isolation Forest.
    Severity high (score 0.62: magnitude 0.33, duration 0.66, threshold 0.0).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pipeline.detection.events import (
    DETECTOR_FOREST,
    DETECTOR_ROLLING,
    DETECTOR_THRESHOLD,
    DETECTOR_ZSCORE,
    Breach,
)

DASH = "\u2014"  # em dash
TIMES = "\u00d7"  # multiplication sign
DETECTOR_PHRASES: dict[str, str] = {
    DETECTOR_THRESHOLD: "critical threshold",
    DETECTOR_ZSCORE: "robust z-score",
    DETECTOR_ROLLING: "rolling median",
}
COMPONENT_ORDER: tuple[str, ...] = ("magnitude", "duration", "threshold")
# Words an explanation must never contain (causal or safety claims); checked by ``assert_descriptive``.
FORBIDDEN_WORDS: tuple[str, ...] = (
    "fail", "unsafe", "damage", "leak", "break", "collapse", "danger", "caused", "because", "due to",
)  # fmt: skip


def format_value(value: float) -> str:
    """A reading with a precision that suits its size (0.12, 4.9, 61.3, 104)."""
    magnitude = abs(value)
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 1:
        return f"{value:.1f}"
    return f"{value:.2f}"


def format_component(value: float) -> str:
    """A score component with one or two decimals (0.0, 0.5, 0.74, 1.0)."""
    text = f"{value:.2f}"
    return text[:-1] if text.endswith("0") else text


def format_score(score: float) -> str:
    """The anomaly score with two or three decimals (0.62, 0.934), so a band limit is never rounded across."""
    text = f"{score:.3f}"
    return text[:-1] if text.endswith("0") else text


def join_words(parts: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _deviation(observed: float, expected: float, robust_z: float, unit: str, log_domain: bool) -> str:
    direction = "above" if robust_z > 0 else "below"
    scale_note = " (log scale)" if log_domain else ""
    text = f"{abs(robust_z):.1f} robust standard deviations{scale_note} {direction} this sensor's baseline"
    if log_domain and expected > 0:
        return f"{text}, {observed / expected:.1f}{TIMES} the expected level"
    return f"{text}, a difference of {format_value(abs(observed - expected))} {unit}"


def _duration(duration_hours: int, active: bool) -> str:
    text = f"lasting {duration_hours} h"
    return text + " and still present at the end of the analysed window" if active else text


def _breach(breach: Breach | None, unit: str) -> str:
    if breach is None:
        return ""
    which = "lowest" if breach.side == "below" else "highest"
    return (
        f" The {which} reading of the event, {format_value(breach.extreme)} {unit}, is {breach.side} the "
        f"{breach.level} limit of {format_value(breach.limit)} {unit}."
    )


def _detectors(methods: Sequence[str]) -> str:
    primary = [DETECTOR_PHRASES[name] for name in methods if name in DETECTOR_PHRASES]
    text = f"Flagged by {join_words(primary)}" if primary else "Flagged by the persistence of moderate deviations"
    if DETECTOR_FOREST in methods:
        text += "; corroborated by Isolation Forest"
    return text + "."


def explain(
    *,
    label: str,
    sensor_id: str,
    placement: str,
    unit: str,
    observed: float,
    expected: float,
    robust_z: float,
    log_domain: bool,
    peer_adjusted: bool,
    duration_hours: int,
    active: bool,
    methods: Sequence[str],
    severity: str,
    score: float,
    components: Mapping[str, float],
    breach: Breach | None = None,
) -> str:
    """Compose the explanation of one anomaly event."""
    where = placement.replace("_", " ")
    basis = " given what comparable sensors recorded" if peer_adjusted else ""
    parts = ", ".join(f"{name} {format_component(components[name])}" for name in COMPONENT_ORDER)
    return (
        f"{label} on {sensor_id} ({where}): {format_value(observed)} {unit} at peak versus an expected "
        f"{format_value(expected)} {unit} for that hour{basis} {DASH} "
        f"{_deviation(observed, expected, robust_z, unit, log_domain)}, {_duration(duration_hours, active)}."
        f"{_breach(breach, unit)} {_detectors(methods)} "
        f"Severity {severity} (score {format_score(score)}: {parts})."
    )


def assert_descriptive(text: str) -> None:
    """Raise ``ValueError`` when an explanation contains a causal or safety claim."""
    lowered = text.lower()
    found = [word for word in FORBIDDEN_WORDS if word in lowered]
    if found:
        raise ValueError(f"explanation is not purely descriptive (contains {', '.join(found)}): {text}")
