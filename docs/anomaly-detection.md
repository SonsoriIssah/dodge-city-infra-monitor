# Prototype Anomaly Detection

How stage 5 (`python scripts/detect_anomalies.py`, `pipeline/detection/`) turns the stored readings into anomalies,
how each anomaly is scored and explained, and how the method was evaluated. Everything here runs on **simulated
sensor data**; the method is a prototype, not a certified infrastructure safety system.

> Detection is a retrospective batch analysis: baselines and scales are estimated from the whole data window.
> Playback replays those results hour by hour; it does not reproduce what a streaming detector would have known at
> that hour.

| Module | Role |
|---|---|
| `runner.py` | Loads sensors, thresholds and every reading from PostGIS onto an hourly grid, runs the steps below per sensor type, writes `detection_runs`, `reading_scores`, `anomalies` in one transaction (a re-run replaces the previous run) |
| `baseline.py` | Work domain, local-hour profile, peer adjustment, robust z-score, expected band |
| `detectors.py` | The four detectors (array functions) |
| `events.py` | Merging, persistence rule, scoring, severity, anomaly type |
| `explain.py` | Plain-English explanation (descriptive only) |
| `evaluate.py` | Comparison with the simulator's ground truth |

Detection reads only `infra.sensor_readings`, `infra.sensors` and `infra.sensor_thresholds`;
`infra.simulation_events` is read for the evaluation only and never influences what is detected. The time axis is
the hourly grid from the first to the last stored reading; `T_end` (the last hour) is stored as
`detection_runs.window_end`.

## 1. Baseline

**Work domain.** Vibration is modelled in ln(mm/s) because its scatter grows with its level; temperature, moisture
and pressure in their native units.

**Profile.** For each sensor, `p_i` = the median of its values by local hour of day (`TIMEZONE`, America/Chicago);
vibration uses separate weekday and weekend profiles. The residual is `r_i = x_i - p_i`.

**Peer adjustment** (temperature and moisture: the "spatial anomaly analysis" step). The simulated weather moves
every sensor of a placement class together (a rain event wets every subgrade; a hot afternoon heats every deck). For
every placement class g of the sensor's type with at least 4 sensors, the class signal is

```
M_g(t) = median over the class of r_j(t) / s_j,     s_j = max(1.4826 · MAD(r_j), floor)
```

Each sensor's residual is regressed on **all** class signals of its type (moisture also on their exponential moving
averages over 24 h and 72 h, so a sensor may dry down faster or slower than its class) by Huber-weighted least
squares (c = 1.345, 8 IRLS iterations, no intercept). The profile is then estimated once more on the peer-adjusted
series and the regression repeated (`PROFILE_REFINEMENTS = 1`). The expected value is

```
expected_i(t) = p_i(t) + Σ_g β_ig · M_g(t) + median of what is left
```

so what the class does together is expected and what one sensor does alone is not. Pressure and vibration have no
peer classes (their behaviour is not weather-driven in the simulation). Classes in the default dataset:

| Type | Peer classes (>= 4 sensors) | Without peers |
|---|---|---|
| temperature | bridge_deck (4), building_envelope (12), road_surface (12) | equipment (2 sensors): wider scale floor 1.5 °C |
| moisture | abutment_backfill (4), foundation_perimeter (8), road_subgrade (22) | - |

**Robust z.**

```
z = (x - expected) / max(1.4826 · MAD(x - expected), floor)
floors: temperature 0.5 °C (1.5 °C in classes with fewer than 4 sensors), vibration 0.10 ln-units, moisture 0.5 %, pressure 0.5 psi
```

`expected` is stored back-transformed to native units with the band `expected_low/high = expected ± 3` robust σ
(vibration: `expected · exp(∓3 · scale)`). Each sensor's scale and floor are in `detection_runs.params`
(`sensor_baselines`) and in `GET /sensors/{id}` -> `baseline`.

## 2. Detectors

Each produces hourly flags; a missing reading is never flagged.

| Detector | Hour is positive when | Setting |
|---|---|---|
| `threshold` | the value is beyond the critical limit of its (sensor type, placement) | `infra.sensor_thresholds` |
| `robust_zscore` | \|z\| >= 6.0 (strong); hours with \|z\| >= 3.0 are "flagged" and can be merged into an event | `DETECT_Z_STRONG`, `DETECT_Z_MIN` |
| `rolling_median` | \|median of z over the trailing 6 h\| >= 3 with at least 4 readings in the window, either sign (sustained shifts and drift) | `DETECT_ROLLING_HOURS` |
| `isolation_forest` | scikit-learn `IsolationForest(n_estimators=100, max_samples=256, random_state=SIM_SEED)`, one model per sensor type, features [z, Δz, 3 h mean of z, 3 h std of z, 12 h mean of z]; score `-model.score_samples(X)` (the original-paper score in (0, 1]) >= 0.62 **and** \|z\| >= 3 | `DETECT_IFOREST_THRESHOLD` |

Isolation Forest is used as corroborating evidence only. On this simulated dataset it agreed with the
robust-statistics detectors on every anomaly and did not identify events they missed. It is named in
`detection_method` and the explanation, but it never creates or extends an event.

## 3. Events and the persistence rule

1. Flagged hours of one sensor (threshold breach, \|z\| >= 3, or rolling-median positive) are merged when the gap
   between them is at most `DETECT_MERGE_GAP_HOURS` (2 h).
2. The event is trimmed to its first and last hour with \|z\| >= 3 (or a critical breach).
3. **Persistence:** the event is kept only if its peak \|z\| >= 6, or it has at least 3 flagged hours, or a critical
   limit is breached. Otherwise its hours stay `reading_scores.flagged = true` and the sensor shows `warning` at
   those hours: not every unusual reading is an anomaly. Default dataset: 1,449 flagged readings, 42 anomalies.
4. Per event: `started_at` / `ended_at` = first / last flagged reading (never NULL), `duration_hours =
   (ended_at - started_at) / 1 h + 1`, `peak_at` = the reading with the largest \|z\| (always an existing reading,
   enforced by the foreign key `(sensor_id, peak_at) -> sensor_readings`), `observed_value`, `expected_value` and
   `robust_z` at the peak.

## 4. Scoring and severity

```
M = clip(log2(|z_peak| / 3) / 4, 0, 1)            magnitude: 0 at |z| = 3, 1 at |z| = 48
D = clip(ln(1 + duration_hours) / ln(97), 0, 1)    duration: 1 at 96 h
T = 1   if any reading of the event is beyond a critical limit
    0.5 if beyond a warning limit
    0   otherwise
anomaly_score    = round(0.50·M + 0.25·D + 0.25·T, 3)
score_components = {"magnitude": M, "duration": D, "threshold": T}
severity: low < 0.30 <= medium < 0.50 <= high < 0.70 <= critical
```

Worked example, ANM-0039 (PRS-006, simulated water main along 11th Avenue): peak z = -64.71, so
M = clip(log2(21.6) / 4) = 1; 28 h, so D = ln(29) / ln(97) = 0.736; lowest reading 12.1 psi < critical limit 20 psi,
so T = 1; score = 0.5 + 0.184 + 0.25 = 0.934 -> **critical**.

`detection_method` lists the detectors with at least one positive hour inside the event, joined by `+`, in the
order threshold, robust_zscore, rolling_median, isolation_forest (an event made only of moderate \|z\| >= 3 hours is
attributed to robust_zscore). Default dataset: 29 x `robust_zscore+rolling_median+isolation_forest`, 12 x
`robust_zscore+isolation_forest`, 1 x `threshold+robust_zscore+rolling_median+isolation_forest`.

`anomaly_type` is a descriptive signature of the readings, never a cause:

| Sensor type | Rule | Types (labels) |
|---|---|---|
| vibration | z < 0 -> `vibration_drop`; <= 3 h -> `vibration_spike`; else `sustained_high_vibration` | Unusually low vibration / Short elevated vibration / Sustained high vibration |
| moisture | z > 0 -> `moisture_increase`, else `moisture_decrease` | Unusual moisture increase / decrease |
| pressure | z > 0 -> `pressure_spike`; z < 0 and >= 36 h -> `pressure_decline`; else `pressure_drop` | Short pressure excursion / Gradual pressure decline / Pressure drop |
| temperature | >= 24 h -> `temperature_drift`; else z > 0 -> `temperature_spike`, z < 0 -> `temperature_drop` | Temperature drift / Abnormal temperature rise / Abnormal temperature drop |

**Status.** Active at t while `started_at <= t <= ended_at`; the stored `anomalies.status` is that rule at `T_end`
(6 active, 36 resolved in the default dataset). The API evaluates it at the requested `as_of`.

**Explanation.** Plain English with the numbers, descriptive only; the generator refuses words that imply a cause
or a verdict. Example (ANM-0038):

> Sustained high vibration on VIB-001 (bridge deck): 4.9 mm/s at peak versus an expected 1.1 mm/s for that hour —
> 10.4 robust standard deviations (log scale) above this sensor's baseline, 4.4× the expected level, lasting 36 h and
> still present at the end of the analysed window. The highest reading of the event, 5.1 mm/s, is above the warning
> limit of 5.0 mm/s. Flagged by robust z-score and rolling median; corroborated by Isolation Forest. Severity high
> (score 0.546: magnitude 0.45, duration 0.79, threshold 0.5).

## 5. Evaluation

Scored against injected simulated events — a self-consistency check, not field validation.

An injected event counts as detected when an anomaly on the same sensor overlaps
[started_at - 2 h, ended_at + 2 h]; an anomaly is a true positive when it overlaps such a window of an injected
event on its sensor. The metrics are stored in `detection_runs.metrics` and returned by `GET /meta` ->
`detection_run`.

| Metric (default dataset, seed 42) | Value |
|---|---|
| `injected_events` / `detected_events` / `event_recall` | 40 / 40 / 1.0 |
| `anomalies` / `true_anomalies` / `anomaly_precision` | 42 / 40 / 0.952 |
| `false_anomalies` | 2, both low: ANM-0040 (VIB-006, 5 h, z 4.14, score 0.156) and ANM-0042 (PRS-010, 5 h, z -3.69, score 0.135) |
| `false_anomalies_during_benign_events` (3 rain events, 1 hot spell) | 0 |
| `split_events` | 0 |
| `severity_counts` | critical 3, high 17, medium 17, low 5 |
| `detection_delay_hours` (median by type) | 0 for vibration_spike, sustained_high_vibration, moisture_increase, pressure_drop, pressure_spike, temperature_spike; 19 for pressure_decline; 23.5 for temperature_drift |

Targets asserted by the test suite: recall >= 0.9, precision >= 0.85, at most 2 anomalies overlapping benign
regional events, at least one critical and one high anomaly active at `T_end`, every stored status consistent with
the time rule. Seed 42 is checked on every test run (in memory and through the full pipeline on PostGIS); seeds 7
and 123 are checked without a database in `tests/test_detection_targets_seeds.py` (marker `slow`). Reduced windows
of 10-14 days have thin margins (precision 0.87-0.91 during development) and are not used to assert the targets.

## Known weaknesses

- **The evaluation is circular by construction.** The simulator injects abnormal events of at least 8 robust σ
  (short) or 4 σ for 6 h or more (sustained) and keeps benign behaviour within 3.5 σ, measured in the detector's own
  work domain with the detector's own scale floors. Recall 1.0 shows the pipeline is wired correctly, not that it
  would find real faults.
- **Retrospective baselines.** Profiles, MADs and peer coefficients use the whole window, including the anomalous
  hours (robust statistics limit, but do not remove, their influence). A streaming deployment would need trailing
  baselines and would detect later and less cleanly.
- **Gradual changes are detected late.** Ramps are found 19-24 h after onset (median); `started_at` is the
  detection time, typically 12-24 h after the onset of a 2-4 day ramp.
- **Peer adjustment can hide a class-wide problem.** A genuine change on every sensor of a placement class at once is
  indistinguishable from weather and is not flagged. Small classes (fewer than 4 sensors) have no peer signal and a
  wider scale floor, so they are less sensitive.
- **No peer model for pressure and vibration.** A network-wide pressure event would appear as many simultaneous
  single-sensor anomalies.
- **Isolation Forest adds no detections** on this dataset (amendment wording above); it is kept for transparency
  as corroboration, not as a detector.
- **Thresholds and weights are expert choices**: the 3/6 σ levels, the persistence rule and the score weights were
  set on the simulated data, not on real failure records.
- **Missing data is not imputed.** Offline hours are absent rows; an event that spans an outage may be cut in two
  (none was in the default dataset: `split_events` = 0).

## Re-running

```bash
python scripts/detect_anomalies.py      # replaces detection_runs, reading_scores, anomalies (and everything that depends on them)
python scripts/analyze_spatial.py       # recomputes clusters, risk zones and asset health for the new run
```

Stage 5 truncates `detection_runs` with `CASCADE`, so the stage 6 outputs of the previous run are removed with it;
always run stage 6 afterwards. Tunables: `DETECT_Z_STRONG`, `DETECT_Z_MIN`, `DETECT_ROLLING_HOURS`,
`DETECT_IFOREST_THRESHOLD`, `DETECT_MERGE_GAP_HOURS` (see the README's
[environment variables](../README.md#12-environment-variables)).
