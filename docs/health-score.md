# Derived Asset Health Score

A number from 0 to 100 per monitored asset and per hour, computed from the asset's **simulated** sensors and the
anomalies the Prototype Anomaly Detection found on them. It summarises how unusual those simulated readings have
been recently. It is **not** an assessment of the condition of the structure, it is not calibrated against any
inspection, and recorded attributes of real features (for example National Bridge Inventory condition ratings)
never feed it.

Implementation: `pipeline/analysis/health.py` (`penalties()` is the one implementation of the formula; the hourly
computation for all assets calls it). Stored in `infra.asset_health`; served by `GET /assets`,
`GET /assets/{asset_id}`, `GET /assets/{asset_id}/health` and `GET /playback`; the formula text is in
`GET /meta` -> `health.formula`.

## Formula

For every monitored asset (at least one sensor) and every hour t of the analysed window:

```
window  = anomalies on the asset's sensors with started_at <= t and ended_at >= t - HEALTH_WINDOW_DAYS
w_i     = 1 while active at t, else 0.5 ** ((t - ended_at_i) / HEALTH_HALF_LIFE_HOURS)
s_i     = {low: 1, medium: 2, high: 4, critical: 7}[severity_i]
frequency_penalty = min(20, 6 · Σ w_i)
severity_penalty  = min(45, 6 · Σ w_i · s_i)
reading_penalty   = min(10, 2 · mean over reporting sensors of clip(median(|z|, trailing 6 h) - 3, 0, 5));  0 if none reporting
sensor_penalty    = 20 · (sensors with no reading at t / sensors_total)
health  = clamp(round(100 - frequency - severity - reading - sensor), 0, 100)
status  = normal >= 90 > watch >= 70 > at_risk >= 45 > critical
```

An anomaly is active at t while `started_at <= t <= ended_at`. The score at hour t uses only anomalies that had
started by t and readings up to t, but the anomalies themselves (their severity, start and end) and the baselines
behind the z-scores come from the retrospective detection run over the whole window.

## Constants

| Constant | Value | Setting / code |
|---|---|---|
| Look-back window | 7 days | `HEALTH_WINDOW_DAYS` |
| Half-life of a resolved anomaly's weight | 48 h | `HEALTH_HALF_LIFE_HOURS` |
| Severity weights low / medium / high / critical | 1 / 2 / 4 / 7 | `SEVERITY_WEIGHTS` (shared with the risk zones) |
| Frequency penalty | 6 per weighted anomaly, cap 20 | `FREQUENCY_FACTOR`, `FREQUENCY_CAP` |
| Severity penalty | 6 per weighted severity unit, cap 45 | `SEVERITY_FACTOR`, `SEVERITY_CAP` |
| Reading penalty | 2 per robust σ of trailing-median \|z\| above 3 (counted up to 5 σ), averaged over reporting sensors, cap 10 | `READING_FACTOR`, `READING_Z_OFFSET`, `READING_Z_RANGE`, `READING_WINDOW_HOURS = 6` |
| Sensor penalty | 20 x share of the asset's sensors without a reading at t | `SENSOR_FACTOR` |
| Bands | normal >= 90, watch >= 70, at_risk >= 45, critical below | `GET /meta` -> `health.bands` |
| Assets at Risk | monitored assets with health < 70 | `AT_RISK_BELOW` in `pipeline/analysis/status.py` |

A sensor "has no reading at t" when it has no reading in the sampling interval ending at t (the same rule as the
sensor status `offline`).

## Worked scenarios

Single-sensor asset unless noted. "Quiet" = trailing-median |z| below 3 (no reading penalty). Every row is asserted
in `tests/test_health.py`.

| Scenario | frequency | severity | reading | sensor | Health | Status |
|---|---|---|---|---|---|---|
| Nothing in the window | 0 | 0 | 0 | 0 | 100 | normal |
| One low anomaly that ended 72 h ago: w = 0.5^1.5 = 0.354 | 2.12 | 2.12 | 0 | 0 | 96 | normal |
| One active critical anomaly, trailing-median \|z\| >= 8 | 6 | 42 | 10 | 0 | 42 | critical |
| Same anomaly on a 3-sensor asset whose other two sensors are quiet: reading = 2 · 5 / 3 | 6 | 42 | 3.33 | 0 | 49 | at_risk |
| Two medium anomalies that ended 24 h and 96 h ago: w = 0.707 and 0.25 | 5.74 | 11.49 | 0 | 0 | 83 | watch |
| The sole sensor is offline, no anomaly | 0 | 0 | 0 | 20 | 80 | watch |
| One active high anomaly, trailing-median \|z\| = 6 | 6 | 24 | 6 | 0 | 64 | at_risk |
| One active high anomaly, trailing-median \|z\| >= 8 | 6 | 24 | 10 | 0 | 60 | at_risk |
| One active high anomaly on a quiet sensor | 6 | 24 | 0 | 0 | 70 | watch |
| One active medium anomaly, quiet / \|z\| >= 8 | 6 | 12 | 0 / 10 | 0 | 82 / 72 | watch |

An active high anomaly therefore gives 60-64 (at_risk) only while its sensor's last six hours stay far from the
baseline; on a sensor that has calmed down the same anomaly leaves the asset at 70 (watch). One active critical
anomaly alone, with its sensor still far off, makes a single-sensor asset critical.

**From the default dataset.** BRG-001 (2nd Avenue bridge over the Arkansas River, 5 sensors) at the last hour,
2026-10-01T04:00:00Z (`GET /assets/BRG-001`):

| Component | Value |
|---|---|
| frequency_penalty | 9.319 |
| severity_penalty | 30.638 |
| reading_penalty | 1.955 |
| sensor_penalty | 0 |
| health | round(100 - 41.912) = 58, **at_risk** |

The active anomaly behind it is ANM-0038 (sustained high vibration on VIB-001, high, started 2026-09-29T17:00:00Z);
earlier anomalies on the bridge's sensors still count with decayed weights. The bridge's recorded NBI condition
("Fair") is shown in a separate block of the asset panel and plays no part in this number.

At the last hour of the default dataset: 102 monitored assets are normal, 4 watch, 5 at_risk, 1 critical
(`GET /health` -> `asset_health`), so **Assets at Risk = 6**; 594 assets have no sensors and no score.

## How to read it

- **Status bands** describe how unusual the simulated readings have been: `watch` = something recent or ongoing on
  one of the asset's sensors, `at_risk` = an active high-severity anomaly or several recent ones, `critical` = an
  active critical anomaly with the sensor still far from its baseline, or a combination of anomalies and offline
  sensors.
- **The four components** (returned with every score) say why: frequency and severity come from anomalies, reading
  from the current level of the z-scores, sensor from missing readings.
- **Recovery is gradual.** After an anomaly ends its weight halves every 48 h and it drops out of the window 7 days
  after it ended.
- **"Needs attention" vs "Assets at Risk".** The dashboard's ranking and status sentence list monitored assets below
  90 (watch, at_risk, critical: 10 at the last hour); the KPI "Assets at Risk" counts those below 70 (6).
- **Unmonitored assets** have `health_score = null` and `status = 'not_monitored'`, never 100.

## What it is not

- Not a structural, safety or condition rating, and not a prediction of failure.
- Not comparable between asset types in any engineering sense: a building, a road and a simulated water main with
  the same score only share the same pattern of simulated anomalies.
- Not calibrated: the penalty factors, caps and bands are design choices made so that the scenarios above read
  sensibly; they have not been fitted to any outcome data.
- Not a streaming score: the anomaly intervals and baselines are known from the whole window (see
  [anomaly-detection.md](anomaly-detection.md)).
