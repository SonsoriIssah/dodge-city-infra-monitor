"""Evaluation of a detection run against the simulator's ground truth (build contract section 8, step 7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pipeline.detection.evaluate import evaluate, matches, overlaps
from pipeline.models import AnomalyRecord, InjectedEvent
from tests.support import METRIC_KEYS

T0 = datetime(2026, 9, 1, 5, tzinfo=UTC)


def at(hour: float) -> datetime:
    return T0 + timedelta(hours=hour)


def anomaly(sensor_id: str, start: float, end: float, severity: str = "low", sensor_type: str = "pressure") -> AnomalyRecord:
    return AnomalyRecord(
        anomaly_id=f"ANM-{sensor_id}-{start}", sensor_id=sensor_id, asset_id="A", sensor_type=sensor_type,
        anomaly_type="x", started_at=at(start), ended_at=at(end), peak_at=at(start), duration_hours=int(end - start) + 1,
        observed_value=1.0, expected_value=0.0, unit="psi", robust_z=7.0, anomaly_score=0.3, severity=severity,
        detection_method="robust_zscore", explanation="", status="resolved", lon=0.0, lat=0.0,
    )  # fmt: skip


def injected(sensor_id: str, start: float, end: float, event_type: str = "pressure_drop") -> InjectedEvent:
    return InjectedEvent(event_type, True, at(start), at(end), sensor_id=sensor_id, asset_id="A", sensor_type="pressure")


def benign(start: float, end: float, event_type: str, sensor_type: str) -> InjectedEvent:
    return InjectedEvent(event_type, False, at(start), at(end), sensor_type=sensor_type)


# --- matching rule ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("start", "end", "detected"),
    [
        (12, 18, True),  # inside the event
        (0, 8, True),  # ends exactly two hours before the event starts
        (0, 7.99, False),
        (0, 7, False),
        (22, 30, True),  # starts exactly two hours after the event ends
        (22.01, 30, False),
        (23, 30, False),
        (5, 40, True),  # covers the event
    ],
)
def test_an_anomaly_matches_an_event_on_its_sensor_within_two_hours(start, end, detected):
    event = injected("S1", 10, 20)
    assert matches(anomaly("S1", start, end), event) is detected
    assert matches(anomaly("S2", start, end), event) is False  # never across sensors


def test_closed_intervals_overlap_when_they_share_a_moment():
    assert overlaps(at(0), at(5), at(5), at(9))
    assert not overlaps(at(0), at(5), at(5.01), at(9))


# --- metrics ------------------------------------------------------------------------------------------------------
@pytest.fixture
def scenario():
    truth = [
        injected("S1", 10, 20, "pressure_drop"),
        injected("S2", 30, 31, "pressure_spike"),
        injected("S3", 50, 60, "pressure_drop"),
        injected("S1", 100, 110, "pressure_decline"),  # never detected
        benign(200, 272, "regional_rain", "moisture"),
        benign(300, 335, "regional_hot_spell", "temperature"),
    ]
    anomalies = [
        anomaly("S1", 12, 18, "high"),  # detects the first event two hours late
        anomaly("S2", 33, 34, "medium"),  # two hours after the event ended: still its detection (3 h after onset)
        anomaly("S3", 50, 52, "critical"),  # the third event is found twice ...
        anomaly("S3", 57, 60, "low"),  # ... = one split event
        anomaly("S4", 210, 215, "low", "moisture"),  # false, during the benign rain
        anomaly("S5", 310, 312, "low", "pressure"),  # false; the hot spell acts on temperature sensors only
        anomaly("S9", 400, 401, "medium"),  # false, nothing going on
    ]
    return anomalies, truth


def test_metrics_have_exactly_the_keys_of_the_contract(scenario):
    assert set(evaluate(*scenario).metrics) == METRIC_KEYS


def test_recall_precision_and_counts(scenario):
    result = evaluate(*scenario)
    metrics = result.metrics
    assert metrics["injected_events"] == 4 and metrics["detected_events"] == 3
    assert metrics["event_recall"] == 0.75
    assert metrics["anomalies"] == 7 and metrics["true_anomalies"] == 4 and metrics["false_anomalies"] == 3
    assert metrics["anomaly_precision"] == round(4 / 7, 3) == 0.571
    assert metrics["false_anomalies_during_benign_events"] == 1
    assert metrics["split_events"] == 1
    assert metrics["severity_counts"] == {"low": 3, "medium": 2, "high": 1, "critical": 1}
    assert [e.started_at for e in result.missed_events] == [at(100)]
    assert sorted(a.sensor_id for a in result.false_anomalies) == ["S4", "S5", "S9"]
    assert [(a.sensor_id, e.event_type) for a, e in result.false_during_benign] == [("S4", "regional_rain")]


def test_detection_delay_is_the_median_per_event_type_from_onset_to_the_first_anomaly(scenario):
    delays = evaluate(*scenario).metrics["detection_delay_hours"]
    assert delays == {"pressure_drop": 1.0, "pressure_spike": 3.0}  # drops: 2 h and 0 h; the decline was missed


def test_a_perfect_run():
    truth = [injected("S1", 10, 20), injected("S2", 30, 40)]
    metrics = evaluate([anomaly("S1", 10, 20), anomaly("S2", 31, 40)], truth).metrics
    assert (metrics["event_recall"], metrics["anomaly_precision"]) == (1.0, 1.0)
    assert metrics["false_anomalies"] == 0 and metrics["split_events"] == 0


def test_no_anomalies_gives_zero_recall_and_no_precision():
    metrics = evaluate([], [injected("S1", 10, 20)]).metrics
    assert metrics["event_recall"] == 0.0 and metrics["anomaly_precision"] is None
    assert metrics["anomalies"] == 0 and metrics["severity_counts"] == {"low": 0, "medium": 0, "high": 0, "critical": 0}


def test_without_ground_truth_nothing_is_called_true_or_false():
    """A real sensor source has no injected events: the run cannot be scored."""
    metrics = evaluate([anomaly("S1", 10, 20, "high")], []).metrics
    assert set(metrics) == METRIC_KEYS
    assert metrics["injected_events"] == 0 and metrics["event_recall"] is None
    assert metrics["anomaly_precision"] is None and metrics["true_anomalies"] is None
    assert metrics["false_anomalies"] is None and metrics["false_anomalies_during_benign_events"] is None
    assert metrics["anomalies"] == 1 and metrics["severity_counts"]["high"] == 1


def test_only_benign_events_as_ground_truth_make_every_anomaly_false():
    metrics = evaluate([anomaly("S1", 10, 20, sensor_type="moisture")], [benign(0, 50, "regional_rain", "moisture")]).metrics
    assert metrics["false_anomalies"] == 1 and metrics["false_anomalies_during_benign_events"] == 1
    assert metrics["anomaly_precision"] == 0.0 and metrics["event_recall"] is None


# --- the default run ----------------------------------------------------------------------------------------------
def test_metrics_of_the_default_run_equal_an_independent_count(default_detection, default_simulation):
    _, result = default_detection
    truth = default_simulation.ground_truth()
    metrics = evaluate(result.anomalies, truth).metrics
    tolerance = timedelta(hours=2)
    events = [e for e in truth if e.is_anomaly]

    def hit(a, e) -> bool:
        return a.sensor_id == e.sensor_id and a.started_at <= e.ended_at + tolerance and a.ended_at >= e.started_at - tolerance

    detected = sum(any(hit(a, e) for a in result.anomalies) for e in events)
    true = sum(any(hit(a, e) for e in events) for a in result.anomalies)
    assert metrics["injected_events"] == len(events) == 40
    assert metrics["detected_events"] == detected and metrics["event_recall"] == round(detected / 40, 3)
    assert metrics["anomalies"] == len(result.anomalies)
    assert metrics["true_anomalies"] == true and metrics["false_anomalies"] == len(result.anomalies) - true
    assert metrics["anomaly_precision"] == round(true / len(result.anomalies), 3)
    assert sum(metrics["severity_counts"].values()) == len(result.anomalies)
