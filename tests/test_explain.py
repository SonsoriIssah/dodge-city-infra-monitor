"""Explanations of anomalies (build contract section 8, step 6): numbers match, wording is descriptive only.

No database: the last tests read the explanations of the in-memory detection over the default simulation.
"""

from __future__ import annotations

import re

import pytest

from pipeline.detection import explain
from pipeline.detection.events import Breach, anomaly_label

# Causal or safety claims an explanation must never make.
FORBIDDEN = (
    "fail", "unsafe", "safe", "damage", "danger", "leak", "break", "broke", "collapse", "caused", "because",
    "due to", "result of", "fault", "defect", "deteriorat", "structural", "imminent", "warning sign",
)  # fmt: skip

EXAMPLE = dict(  # noqa: C408 - keyword arguments of explain(), passed on as **EXAMPLE
    label="Sustained high vibration",
    sensor_id="VIB-003",
    placement="bridge_deck",
    unit="mm/s",
    observed=4.9,
    expected=1.6,
    robust_z=7.4,
    log_domain=True,
    peer_adjusted=False,
    duration_hours=19,
    active=False,
    methods=("robust_zscore", "rolling_median", "isolation_forest"),
    severity="high",
    score=0.62,
    components={"magnitude": 0.33, "duration": 0.66, "threshold": 0.0},
)


def test_the_example_of_the_contract_is_reproduced_word_for_word():
    assert explain.explain(**EXAMPLE) == (
        "Sustained high vibration on VIB-003 (bridge deck): 4.9 mm/s at peak versus an expected 1.6 mm/s for that "
        "hour — 7.4 robust standard deviations (log scale) above this sensor's baseline, 3.1× the expected level, "
        "lasting 19 h. Flagged by robust z-score and rolling median; corroborated by Isolation Forest. Severity "
        "high (score 0.62: magnitude 0.33, duration 0.66, threshold 0.0)."
    )


def test_native_units_state_the_difference_instead_of_a_ratio():
    text = explain.explain(
        **{**EXAMPLE, "label": "Pressure drop", "sensor_id": "PRS-007", "placement": "water_main", "unit": "psi",
           "observed": 31.26, "expected": 62.4, "robust_z": -41.52, "log_domain": False, "duration_hours": 5,
           "methods": ("robust_zscore",), "severity": "critical", "score": 0.734,
           "components": {"magnitude": 0.948, "duration": 0.392, "threshold": 0.5}}
    )  # fmt: skip
    assert text == (
        "Pressure drop on PRS-007 (water main): 31.3 psi at peak versus an expected 62.4 psi for that hour — "
        "41.5 robust standard deviations below this sensor's baseline, a difference of 31.1 psi, lasting 5 h. "
        "Flagged by robust z-score. Severity critical (score 0.734: magnitude 0.95, duration 0.39, threshold 0.5)."
    )
    assert "log scale" not in text and "×" not in text


def test_peer_adjusted_types_say_what_the_expectation_is_based_on():
    text = explain.explain(**{**EXAMPLE, "unit": "°C", "log_domain": False, "peer_adjusted": True})
    assert "for that hour given what comparable sensors recorded —" in text


def test_an_event_that_is_still_running_says_so():
    assert "lasting 19 h and still present at the end of the analysed window." in explain.explain(**{**EXAMPLE, "active": True})
    assert "still present" not in explain.explain(**EXAMPLE)


@pytest.mark.parametrize(
    ("breach", "sentence"),
    [
        (Breach("critical", "below", 20.0, 16.48), " The lowest reading of the event, 16.5 psi, is below the critical limit of 20.0 psi."),
        (Breach("warning", "above", 90.0, 104.2), " The highest reading of the event, 104 psi, is above the warning limit of 90.0 psi."),
    ],
)
def test_a_limit_breach_is_stated_with_its_numbers(breach, sentence):
    text = explain.explain(**{**EXAMPLE, "unit": "psi", "log_domain": False, "breach": breach})
    assert sentence in text


@pytest.mark.parametrize(
    ("methods", "phrase"),
    [
        (("threshold",), "Flagged by critical threshold."),
        (("robust_zscore",), "Flagged by robust z-score."),
        (("threshold", "robust_zscore"), "Flagged by critical threshold and robust z-score."),
        (("threshold", "robust_zscore", "rolling_median"), "Flagged by critical threshold, robust z-score and rolling median."),
        (("rolling_median", "isolation_forest"), "Flagged by rolling median; corroborated by Isolation Forest."),
    ],
)
def test_detectors_are_named_and_the_forest_only_corroborates(methods, phrase):
    text = explain.explain(**{**EXAMPLE, "methods": methods})
    assert phrase in text
    assert "Flagged by Isolation Forest" not in text and "by isolation" not in text


@pytest.mark.parametrize(("value", "text"), [(0.123, "0.12"), (4.94, "4.9"), (61.26, "61.3"), (104.4, "104"), (-3.21, "-3.2")])
def test_values_are_written_with_a_precision_that_suits_their_size(value, text):
    assert explain.format_value(value) == text


@pytest.mark.parametrize(("score", "text"), [(0.62, "0.62"), (0.934, "0.934"), (0.7, "0.70"), (0.699, "0.699"), (1.0, "1.00")])
def test_the_score_is_never_rounded_across_a_severity_band(score, text):
    assert explain.format_score(score) == text


def test_assert_descriptive_rejects_causal_and_safety_wording():
    explain.assert_descriptive(explain.explain(**EXAMPLE))
    for claim in ("The bridge is unsafe.", "Caused by a pipe leak.", "Likely failure of the deck.", "This is due to rain."):
        with pytest.raises(ValueError, match="not purely descriptive"):
            explain.assert_descriptive(claim)


# --- explanations of a whole run ----------------------------------------------------------------------------------
def test_no_explanation_of_the_default_run_makes_a_causal_or_safety_claim(default_detection):
    _, result = default_detection
    assert len(result.anomalies) >= 30
    for anomaly in result.anomalies:
        lowered = anomaly.explanation.lower()
        assert not [word for word in FORBIDDEN if word in lowered], anomaly.explanation


def test_the_numbers_in_every_explanation_are_the_numbers_of_the_anomaly(default_detection):
    _, result = default_detection
    for anomaly in result.anomalies:
        text = anomaly.explanation
        assert text.startswith(f"{anomaly_label(anomaly.anomaly_type)} on {anomaly.sensor_id} (")
        z = re.search(r"— ([\d.]+) robust standard deviations(?: \(log scale\))? (above|below) this sensor's baseline", text)
        assert z, text
        assert z.group(1) == f"{abs(anomaly.robust_z):.1f}"
        assert z.group(2) == ("above" if anomaly.robust_z > 0 else "below")
        assert ("(log scale)" in text) == (anomaly.sensor_type == "vibration")
        assert f"lasting {anomaly.duration_hours} h" in text
        peak = re.search(r"\): (-?[\d.]+) (\S+) at peak versus an expected (-?[\d.]+) (\S+) for that hour", text)
        assert peak and peak.group(2) == peak.group(4) == anomaly.unit
        assert float(peak.group(1)) == pytest.approx(anomaly.observed_value, abs=0.51 if abs(anomaly.observed_value) >= 100 else 0.051)
        assert float(peak.group(3)) == pytest.approx(anomaly.expected_value, abs=0.51 if abs(anomaly.expected_value) >= 100 else 0.051)
        if anomaly.sensor_type == "vibration":
            ratio = re.search(r", ([\d.]+)× the expected level", text)
            assert float(ratio.group(1)) == pytest.approx(anomaly.observed_value / anomaly.expected_value, abs=0.051)
        severity = re.search(r"Severity (\w+) \(score ([\d.]+): magnitude ([\d.]+), duration ([\d.]+), threshold ([\d.]+)\)\.$", text)
        assert severity, text
        assert severity.group(1) == anomaly.severity
        assert float(severity.group(2)) == anomaly.anomaly_score
        for position, name in enumerate(("magnitude", "duration", "threshold"), start=3):
            assert float(severity.group(position)) == pytest.approx(anomaly.score_components[name], abs=0.0051)
        assert ("corroborated by Isolation Forest" in text) == ("isolation_forest" in anomaly.detection_method)
        assert ("still present at the end of the analysed window" in text) == (anomaly.status == "active")
