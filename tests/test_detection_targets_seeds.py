"""Detection targets of the contract (section 8, step 7; amendment A3), without a database.

Placement -> simulator -> ``pipeline.detection.runner.detect`` runs in memory on the committed raw data with
the full default simulation (30 days, 40 events). The default seed is checked in every run; the further seeds
7 and 123 are marked ``slow`` (``pytest -m slow``). The scoring below is written independently of
``pipeline.detection.evaluate``.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from pipeline.detection.evaluate import evaluate
from pipeline.sensors import placement, simulator
from tests.conftest import detect_in_memory

TOLERANCE = timedelta(hours=2)
MIN_RECALL = 0.9
MIN_PRECISION = 0.85
MAX_DURING_BENIGN = 2


def score(anomalies, truth) -> dict:
    """Recall, precision and the anomalies that overlap a benign regional event of their sensor type."""
    injected = [event for event in truth if event.is_anomaly]
    benign = [event for event in truth if not event.is_anomaly]

    def hit(anomaly, event) -> bool:
        return (
            anomaly.sensor_id == event.sensor_id
            and anomaly.started_at <= event.ended_at + TOLERANCE
            and anomaly.ended_at >= event.started_at - TOLERANCE
        )

    detected = [event for event in injected if any(hit(a, event) for a in anomalies)]
    false = [a for a in anomalies if not any(hit(a, event) for event in injected)]
    during_benign = [
        a for a in false
        if any(b.sensor_type == a.sensor_type and a.started_at <= b.ended_at and a.ended_at >= b.started_at for b in benign)
    ]  # fmt: skip
    return {
        "injected": len(injected),
        "recall": len(detected) / len(injected),
        "precision": (len(anomalies) - len(false)) / len(anomalies),
        "false": len(false),
        "during_benign": len(during_benign),
        "missed": [(e.event_type, e.sensor_id) for e in injected if e not in detected],
    }


def run_seed(default_assets, settings_factory, seed: int):
    settings = settings_factory(SIM_SEED=seed)
    plan = placement.plan_placement(default_assets, settings)
    simulation = simulator.simulate(plan.sensors, settings)
    grid, result = detect_in_memory(simulation, settings)
    return simulation, grid, result


def assert_targets(simulation, grid, result) -> dict:
    truth = simulation.ground_truth()
    outcome = score(result.anomalies, truth)
    assert outcome["injected"] == 40
    assert outcome["recall"] >= MIN_RECALL, outcome
    assert outcome["precision"] >= MIN_PRECISION, outcome
    assert outcome["during_benign"] <= MAX_DURING_BENIGN, outcome
    # the simulated scenario ends with ongoing events: at least one critical and one high anomaly are active
    t_end = grid.axis.end
    active = Counter(a.severity for a in result.anomalies if a.started_at <= t_end <= a.ended_at)
    assert active["critical"] >= 1 and active["high"] >= 1, dict(active)
    # stored status = the time rule at T_end (amendment A1)
    assert all((a.status == "active") == (a.ended_at >= t_end) for a in result.anomalies)
    # the project's own evaluation reports the same figures
    metrics = evaluate(result.anomalies, truth).metrics
    assert metrics["event_recall"] == round(outcome["recall"], 3)
    assert metrics["anomaly_precision"] == round(outcome["precision"], 3)
    assert metrics["false_anomalies_during_benign_events"] == outcome["during_benign"]
    return outcome


def test_default_seed_meets_the_detection_targets(default_simulation, default_detection):
    grid, result = default_detection
    assert_targets(default_simulation, grid, result)
    severity = Counter(a.severity for a in result.anomalies)
    assert 2 <= severity["critical"] <= 8, dict(severity)
    assert max(severity.values()) / len(result.anomalies) <= 0.5, dict(severity)  # no class above 50 %


def test_default_seed_detects_every_event_that_is_ongoing_at_the_end(default_simulation, default_detection):
    grid, result = default_detection
    t_end = grid.axis.end
    for detail in default_simulation.ongoing_at_end():
        hits = [a for a in result.anomalies if a.sensor_id == detail.event.sensor_id and a.ended_at >= t_end]
        assert hits, f"{detail.event.event_type} on {detail.event.sensor_id} is not active at the end"
    drop = next(d for d in default_simulation.ongoing_at_end() if d.event.event_type == "pressure_drop")
    critical = [a for a in result.anomalies if a.sensor_id == drop.event.sensor_id and a.ended_at >= t_end]
    assert critical[0].severity == "critical" and "threshold" in critical[0].detection_method


def test_isolation_forest_only_corroborates_in_the_default_run(default_detection):
    """Amendment A5: every anomaly is carried by a robust-statistics detector; the forest never stands alone."""
    _, result = default_detection
    for anomaly in result.anomalies:
        methods = anomaly.detection_method.split("+")
        assert set(methods) - {"isolation_forest"}, anomaly.anomaly_id


@pytest.mark.slow
@pytest.mark.parametrize("seed", [7, 123])
def test_further_seeds_meet_the_detection_targets(default_assets, settings_factory, seed):
    simulation, grid, result = run_seed(default_assets, settings_factory, seed)
    assert Counter(d.event.event_type for d in simulation.events) == Counter(
        {"vibration_spike": 6, "sustained_high_vibration": 6, "moisture_increase": 7, "pressure_drop": 5,
         "pressure_spike": 4, "pressure_decline": 3, "temperature_spike": 5, "temperature_drift": 4}
    )  # fmt: skip
    assert simulation.sigma_rule_violations() == []
    assert len(simulation.ongoing_at_end()) == 6
    assert_targets(simulation, grid, result)
