"""Evaluation of a detection run against the simulator's injected events (build contract section 8, step 7).

This is a self-consistency check, not field validation: the "truth" is what the simulator injected.

* An injected event is **detected** when an anomaly on the same sensor overlaps the event's interval widened
  by two hours on each side.
* An anomaly is a **true anomaly** when it overlaps such a widened interval of an injected abnormal event on
  its own sensor; every other anomaly is a false anomaly.
* A false anomaly counts as **during a benign event** when it overlaps a benign regional event (rain, hot
  spell) that acts on its sensor type.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from pipeline.detection.events import SEVERITIES
from pipeline.models import AnomalyRecord, InjectedEvent

MATCH_TOLERANCE = timedelta(hours=2)
RATIO_DECIMALS = 3
DELAY_DECIMALS = 1


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The metrics plus the detail the reports need (which events were missed, which anomalies are false)."""

    metrics: dict[str, Any]
    missed_events: list[InjectedEvent]
    false_anomalies: list[AnomalyRecord]
    false_during_benign: list[tuple[AnomalyRecord, InjectedEvent]]


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """True when the closed intervals [start_a, end_a] and [start_b, end_b] share a moment."""
    return start_a <= end_b and end_a >= start_b


def matches(anomaly: AnomalyRecord, event: InjectedEvent, tolerance: timedelta = MATCH_TOLERANCE) -> bool:
    """True when the anomaly is on the event's sensor and overlaps its interval widened by ``tolerance``."""
    return anomaly.sensor_id == event.sensor_id and overlaps(
        anomaly.started_at, anomaly.ended_at, event.started_at - tolerance, event.ended_at + tolerance
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, RATIO_DECIMALS) if denominator else None


def evaluate(anomalies: Sequence[AnomalyRecord], events: Sequence[InjectedEvent]) -> Evaluation:
    """Score the anomalies against the ground truth; ratios are None when their denominator is zero."""
    injected = [event for event in events if event.is_anomaly]
    benign = [event for event in events if not event.is_anomaly]

    detected = 0
    split = 0
    missed: list[InjectedEvent] = []
    delays: dict[str, list[float]] = {}
    for event in injected:
        hits = [anomaly for anomaly in anomalies if matches(anomaly, event)]
        if not hits:
            missed.append(event)
            continue
        detected += 1
        split += len(hits) > 1
        first = min(hit.started_at for hit in hits)
        delays.setdefault(event.event_type, []).append((first - event.started_at) / timedelta(hours=1))

    false = [anomaly for anomaly in anomalies if not any(matches(anomaly, event) for event in injected)]
    during_benign: list[tuple[AnomalyRecord, InjectedEvent]] = []
    for anomaly in false:
        for event in benign:
            same_type = event.sensor_type is None or event.sensor_type == anomaly.sensor_type
            if same_type and overlaps(anomaly.started_at, anomaly.ended_at, event.started_at, event.ended_at):
                during_benign.append((anomaly, event))
                break

    severity_counts = dict.fromkeys(SEVERITIES, 0)
    for anomaly in anomalies:
        severity_counts[anomaly.severity] = severity_counts.get(anomaly.severity, 0) + 1
    known = bool(events)  # without ground truth (a real sensor source) nothing can be called true or false
    if not known:
        false, during_benign = [], []
    metrics: dict[str, Any] = {
        "injected_events": len(injected),
        "detected_events": detected,
        "event_recall": _ratio(detected, len(injected)),
        "anomalies": len(anomalies),
        "true_anomalies": len(anomalies) - len(false) if known else None,
        "anomaly_precision": _ratio(len(anomalies) - len(false), len(anomalies)) if known else None,
        "false_anomalies": len(false) if known else None,
        "false_anomalies_during_benign_events": len(during_benign) if known else None,
        "split_events": split,
        "detection_delay_hours": {
            name: round(float(median(values)), DELAY_DECIMALS) for name, values in sorted(delays.items())
        },
        "severity_counts": severity_counts,
    }
    return Evaluation(metrics=metrics, missed_events=missed, false_anomalies=false, false_during_benign=during_benign)
