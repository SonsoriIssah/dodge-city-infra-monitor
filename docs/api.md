# API reference

FastAPI service `backend.app.main:create_app` (`python -m uvicorn backend.app.main:create_app --factory`). The
interactive OpenAPI documentation is at `/docs` (Swagger UI) and `/redoc`; the schema at `/openapi.json`. Every
example below is an excerpt of a real response from the default dataset (seed 42), shortened with `...`.

## Conventions

| Topic | Rule |
|---|---|
| Timestamps | Always `YYYY-MM-DDTHH:MM:SSZ` (UTC). Datetime parameters are ISO 8601; a value without an offset is UTC |
| Time axis | Hourly, `2026-09-01T05:00:00Z` .. `2026-10-01T04:00:00Z` (720 steps) in the default dataset; `T_end` = the last hour |
| `as_of` | Floored to the hour, clamped to the window, echoed in the response (`?as_of=2026-09-20T12:34:00Z` answers for `2026-09-20T12:00:00Z`). Without it an endpoint answers for `T_end` |
| Active anomaly | Active at t while `started_at <= t <= ended_at`; resolved when `ended_at < t`; not yet visible when `started_at > t` |
| Sensor status at t | First match wins: `offline` (no reading in the sampling interval ending at t), `anomaly` (an anomaly of the sensor is active), `warning` (reading flagged, or outside the warning limits), `normal` |
| `bbox` | `west,south,east,north` in WGS84 degrees |
| Geometry | GeoJSON, WGS84; coordinates rounded to 6 decimals |
| Rounding | values 3 decimals, robust z 2, scores 3, distances 0.1 m |
| Missing values | `null` (never `""`) |
| Errors | `404 {"detail": "..."}` unknown id; `503 {"detail": "database unavailable"}` when PostGIS cannot be reached; `422` with FastAPI's list of problems for invalid parameters; unknown paths answer `404 {"detail":"Not Found"}` |
| `data_notice` | `/meta`, `/statistics`, `/anomalies`, `/sensor-readings` and `/playback` carry "Simulated sensor data and prototype anomaly detection. Not a record of real infrastructure condition." |
| Transport | gzip for larger responses; CORS from `CORS_ORIGINS` (methods GET, POST, OPTIONS; header `ETag` exposed) |
| Simulation flags | Every sensor, reading and anomaly object has `is_simulated: true`; simulated water mains have `is_simulated: true` |

Examples use `B=http://localhost:8000`.

---

## Service and metadata

### `GET /health`

**Service health, not asset health.** Database connectivity, PostGIS version, the analysed window and the number of
monitored assets per status of the Derived Asset Health Score at `T_end`.

```bash
curl -s $B/health
```
```json
{"asset_health":{"as_of":"2026-10-01T04:00:00Z","at_risk":5,"critical":1,"normal":102,"not_monitored":594,"watch":4},
 "data_window":{"end":"2026-10-01T04:00:00Z","start":"2026-09-01T05:00:00Z"},"database":"ok",
 "note":"service health; asset health scores are at /assets and /assets/{id}/health","postgis":"3.4.3",
 "service":"dodge-city-infra-monitor","status":"ok","version":"1.0.0"}
```

When the database is unreachable or holds no analysed data: HTTP 503
`{"database":"unavailable","service":"dodge-city-infra-monitor","status":"degraded"}`. The dashboard's `auto` mode
and the container healthcheck use this endpoint.

### `GET /meta`

Study area, time axis, mandatory labels, sensor types with thresholds, anomaly type labels, severity levels, the
health and risk formulas with their constants, counts, every data source with licence and retrieval date, and the
latest detection run with its parameters and evaluation metrics. No parameters.

```json
{"service":"dodge-city-infra-monitor","version":"1.0.0","data_notice":"Simulated sensor data and prototype anomaly detection. ...",
 "study_area":{"bbox":[-100.03,37.745,-100.005,37.762],"center":[-100.0175,37.7535],"name":"Downtown Dodge City, Kansas",
               "slug":"dodge-city-downtown","timezone":"America/Chicago","utm_srid":32614},
 "time":{"count":720,"end":"2026-10-01T04:00:00Z","start":"2026-09-01T05:00:00Z","step_minutes":60},
 "labels":{"sensor_data":"Simulated Sensor Data","detection":"Prototype Anomaly Detection","health":"Derived Asset Health Score",
           "buildings":"3D building extrusions derived from OSM footprints. Heights: measured from USGS 3DEP lidar (2013–14) where available, otherwise OSM tags, otherwise estimated. Not detailed 3D building models.",
           "water_network":"Simulated water network (not a record of real utilities)",
           "playback":"Playback replays a retrospective analysis of simulated readings."},
 "sensor_types":{"pressure":{"label":"Pressure","unit":"psi","placements":{"water_main":{"warn_low":40,"warn_high":90,"crit_low":20,"crit_high":110,
                 "description":"Operating pressure, simulated water main"}}}, ...},
 "anomaly_types":{"pressure_drop":"Pressure drop","sustained_high_vibration":"Sustained high vibration", ...},
 "severity_levels":["low","medium","high","critical"],
 "health":{"at_risk_below":70,"bands":{"at_risk":45,"critical":0,"normal":90,"watch":70},"formula":"health = clamp(round(100 - frequency - severity - reading - sensor), 0, 100); ...",
           "half_life_hours":48,"window_days":7},
 "risk":{"bandwidth_m":250,"half_life_hours":72,"hex_edge_m":150,"levels":{"high":50,"low":0,"moderate":25,"very_high":75},"reference":14},
 "spatial":{"cluster_eps_hours":48,"cluster_eps_m":200,"cluster_min_points":3,"cluster_min_sensors":3,"proximity_radius_m":100},
 "counts":{"anomalies":42,"assets":706,"monitored_assets":112,"readings":92028,"real_assets":678,"sensors":128,"simulated_assets":28,
           "assets_by_type":{"bridge":4,"building":458,"power":3,"rail":50,"road":137,"street_light":26,"water_main":28},
           "sensors_by_type":{"moisture":34,"pressure":28,"temperature":30,"vibration":36},
           "building_height_sources":{"estimated":39,"lidar_3dep":419,"osm_height":0,"osm_levels":0}},
 "data_sources":[{"source_id":"nbi","kind":"real","name":"National Bridge Inventory: highway bridge and culvert records",
                  "license":"US Government work, unrestricted public use",
                  "attribution_text":"FHWA National Bridge Inventory (data as of June 20, 2025), distributed by USDOT/BTS NTAD",
                  "vintage":"data as of June 20, 2025","retrieved_at":"2026-10-04T12:00:28Z", ...}, ...],
 "detection_run":{"run_id":1,"finished_at":"...Z","evaluation_note":"Scored against injected simulated events — a self-consistency check, not field validation.",
                  "metrics":{"anomalies":42,"anomaly_precision":0.952,"detected_events":40,"event_recall":1,"false_anomalies":2,
                             "false_anomalies_during_benign_events":0,"injected_events":40,"split_events":0,"true_anomalies":40,
                             "severity_counts":{"critical":3,"high":17,"low":5,"medium":17},
                             "detection_delay_hours":{"pressure_decline":19,"temperature_drift":23.5, ...}},
                  "params":{"z_strong":6,"z_min":3,"rolling_hours":6,"merge_gap_hours":2,"iforest":{"threshold":0.62, ...},"sensor_baselines":{...}, ...}}}
```

`detection_run.finished_at` is the wall-clock time the run finished, so it changes on every pipeline run.

### `GET /statistics`

| Parameter | Type | Default |
|---|---|---|
| `as_of` | datetime | `T_end` |

The dashboard KPIs at one hour. `total_assets`, `real_assets`, `simulated_assets`, `monitored_assets`,
`total_sensors` and `assets_by_type` are static; `active_sensors` = total - offline; `active_anomalies` = anomalies
active at `as_of`; `critical_alerts` = active anomalies of severity critical; `assets_at_risk` = monitored assets with
health < 70; `anomalies_to_date` and the two breakdowns count anomalies that had started by `as_of`.

```bash
curl -s "$B/statistics"
```
```json
{"active_anomalies":6,"active_sensors":126,"anomalies_by_sensor_type":{"moisture":7,"pressure":13,"temperature":9,"vibration":13},
 "anomalies_by_severity":{"critical":3,"high":17,"low":5,"medium":17},"anomalies_to_date":42,"as_of":"2026-10-01T04:00:00Z",
 "assets_at_risk":6,"assets_by_type":{"bridge":4,"building":458,"power":3,"rail":50,"road":137,"street_light":26,"water_main":28},
 "critical_alerts":1,"data_notice":"Simulated sensor data and prototype anomaly detection. ...","monitored_assets":112,
 "offline_sensors":2,"real_assets":678,"simulated_assets":28,"total_assets":706,"total_sensors":128,"warning_sensors":0}
```

`curl -s "$B/statistics?as_of=2026-09-20T12:34:00Z"` answers for `2026-09-20T12:00:00Z` (0 active anomalies,
19 anomalies to date, 1 asset at risk).

---

## Assets

### `GET /assets`

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `asset_type` | `building`, `road`, `bridge`, `rail`, `power`, `street_light`, `water_main` | all | |
| `category` | string | all | `Buildings`, `Transportation`, `Utilities`, `Simulated network` |
| `monitored` | boolean | all | at least one sensor |
| `status` | `normal`, `watch`, `at_risk`, `critical`, `not_monitored` | all | at `T_end` |
| `bbox` | `west,south,east,north` | none | |
| `limit` | 1..20000 | all rows | |
| `offset` | >= 0 | 0 | |

GeoJSON FeatureCollection with `numberMatched` and `numberReturned`. Feature properties: `asset_id`, `asset_type`,
`category`, `name`, `is_simulated`, `source_id`, `monitored`, `sensor_count`, `sensor_types`, `anomaly_count`,
`health_score`, `status`, `centroid` (+ buildings: `height_m`, `height_source`, `building_type`, `levels`,
`footprint_m2`; roads: `highway_class`, `surface`, `lanes`, `length_m`; bridges: `structure_kind`, `length_m`, `nbi`;
water mains: `host_road_id`). `health_score`, `status` and `anomaly_count` are the `T_end` values.

```bash
curl -s "$B/assets?asset_type=bridge"
```
```json
{"type":"FeatureCollection","numberMatched":4,"numberReturned":4,"features":[{"type":"Feature","geometry":{"type":"LineString",...},
 "properties":{"anomaly_count":4,"asset_id":"BRG-001","asset_type":"bridge","category":"Transportation","centroid":[-100.019496,37.747438],
  "health_score":58,"is_simulated":false,"length_m":152.1,"monitored":true,"name":"2nd. Avenue over Arkansas River",
  "nbi":{"adt":14615,"adt_year":2022,"bridge_condition":"F","bridge_condition_label":"Fair (FHWA classification from the lowest component rating)",
         "culvert_condition":"N","deck_condition":"7","inspection_label":"February 2023","owner":"City or Municipal Highway Agency",
         "structure_number":"406950290827010","year_built":1935,"year_reconstructed":2001, ...},
  "sensor_count":5,"sensor_types":["moisture","temperature","vibration"],"source_id":"osm","status":"at_risk","structure_kind":"bridge"}}, ...]}
```

`curl -s "$B/assets?monitored=true&status=at_risk"` returns 5 features (BLD-0033, BRG-001, RD-0079, RD-0119,
WM-027); `status=critical` returns WM-006.

### `GET /assets/{asset_id}`

| Parameter | Type | Default |
|---|---|---|
| `as_of` | datetime | `T_end` |

One Feature with `health` (`score`, `status`, `components` with the four penalties) at `as_of`, `sensors` (sensor
items, see `/sensors`), `recent_anomalies` (anomaly items), and `provenance` (`is_simulated`, `attributes` as
recorded, `sources` = the `data_sources` rows behind the asset). 404 for an unknown id.

```bash
curl -s "$B/assets/BRG-001"
```
```json
{"type":"Feature","as_of":"2026-10-01T04:00:00Z","geometry":{"type":"LineString",...},"properties":{...},
 "health":{"components":{"frequency_penalty":9.319,"reading_penalty":1.955,"sensor_penalty":0,"severity_penalty":30.638},"score":58,"status":"at_risk"},
 "sensors":[{"sensor_id":"MST-031","sensor_type":"moisture","placement":"abutment_backfill","status":"normal",
             "latest":{"expected":21.947,"robust_z":-0.05,"status":"ok","ts":"2026-10-01T04:00:00Z","value":21.921}, ...}, ...],
 "recent_anomalies":[{"anomaly_id":"ANM-0038","anomaly_label":"Sustained high vibration","severity":"high", ...}, ...],
 "provenance":{"is_simulated":false,"attributes":{"highway_class":"primary","length_m":152.1,"osm_way_ids":[13068265,51849921],"nbi":{...},"structure_kind":"bridge"},
               "sources":[{"source_id":"osm","kind":"real",...},{"source_id":"nbi","kind":"real",...}]}}
```

```bash
curl -s "$B/assets/NOPE"
# 404 {"detail":"asset 'NOPE' not found; list the assets with GET /assets"}
```

### `GET /assets/{asset_id}/health`

| Parameter | Type | Default |
|---|---|---|
| `start` | datetime | window start |
| `end` | datetime | `T_end` |

Hourly Derived Asset Health Score of one monitored asset as parallel arrays; `status` has one character per hour
(`n` normal, `w` watch, `r` at_risk, `c` critical). 404 for an asset without sensors.

```bash
curl -s "$B/assets/BRG-001/health?start=2026-09-30T20:00:00Z"
```
```json
{"active_anomalies":[1,1,1,1,1,1,1,1,1],"asset_id":"BRG-001","count":9,
 "frequency_penalty":[9.726,9.672,9.62,9.568,9.516,9.466,9.416,9.367,9.319],"health_score":[57,57,57,57,57,58,58,58,58],
 "reading_penalty":[2,1.891,2,1.89,2,2,2,2,1.955],"sensor_penalty":[0,0,0,0,0,0,0,0,0],"sensors_reporting":[5,5,5,5,5,5,5,5,5],
 "sensors_total":5,"severity_penalty":[31.451,31.344,31.239,31.135,31.033,30.932,30.833,30.735,30.638],
 "start":"2026-09-30T20:00:00Z","status":"rrrrrrrrr","step_minutes":60}
```

---

## Sensors and readings

### `GET /sensors`

| Parameter | Type | Default |
|---|---|---|
| `sensor_type` | `temperature`, `vibration`, `moisture`, `pressure` | all |
| `asset_id` | string | all |
| `status` | `normal`, `warning`, `anomaly`, `offline` (at `as_of`) | all |
| `as_of` | datetime | `T_end` |
| `limit` | 1..5000 | 1000 |
| `offset` | >= 0 | 0 |

`{total, limit, offset, as_of, items}`. **Sensor item:** `sensor_id`, `asset_id`, `asset_name`, `asset_type`,
`sensor_type`, `placement`, `unit`, `description`, `is_simulated`, `source`, `lon`, `lat`, `status` (at `as_of`),
`latest` (the reading current at `as_of`: `ts`, `value`, `status`, `expected`, `robust_z`; `null` when the sensor
has no reading at that hour), `anomaly_count` (anomalies started by `as_of`).

```bash
curl -s "$B/sensors?asset_id=BRG-001"
```
```json
{"as_of":"2026-10-01T04:00:00Z","limit":1000,"offset":0,"total":5,"items":[
 {"anomaly_count":0,"asset_id":"BRG-001","asset_name":"2nd. Avenue over Arkansas River","asset_type":"bridge",
  "description":"Simulated abutment backfill moisture probe (volumetric water content) on 2nd. Avenue over Arkansas River",
  "is_simulated":true,"lat":37.746781,"lon":-100.019471,"placement":"abutment_backfill","sensor_id":"MST-031","sensor_type":"moisture",
  "source":"simulator","status":"normal","unit":"%",
  "latest":{"expected":21.947,"robust_z":-0.05,"status":"ok","ts":"2026-10-01T04:00:00Z","value":21.921}}, ...]}
```

`curl -s "$B/sensors?status=offline"` returns 2 sensors (MST-010, VIB-010) with `latest: null`.

### `GET /sensors/{sensor_id}`

| Parameter | Type | Default |
|---|---|---|
| `as_of` | datetime | `T_end` |

Sensor item + `thresholds` (`warn_low`, `warn_high`, `crit_low`, `crit_high`), `baseline` (`scale`, `floor` of the
robust z-score, in the work domain: ln-units for vibration), `anomaly_count`, `anomalies` (anomaly items).

```bash
curl -s "$B/sensors/VIB-001"
```
```json
{"sensor_id":"VIB-001","asset_id":"BRG-001","sensor_type":"vibration","placement":"bridge_deck","unit":"mm/s","status":"anomaly",
 "description":"Simulated deck vibration sensor (hourly RMS velocity) on 2nd. Avenue over Arkansas River (1 of 2)",
 "latest":{"expected":0.413,"robust_z":6.69,"status":"ok","ts":"2026-10-01T04:00:00Z","value":1.08},
 "thresholds":{"crit_high":10,"crit_low":null,"warn_high":5,"warn_low":null},"baseline":{"floor":0.1,"scale":0.1437},
 "anomaly_count":1,"anomalies":[{"anomaly_id":"ANM-0038", ...}], ...}
```

### `GET /sensor-readings`

| Parameter | Type | Default |
|---|---|---|
| `sensor_id` | string, **required** | |
| `start`, `end` | datetime | the analysed window |
| `shape` | `records`, `columns` | `records` |
| `limit` | 1..5000 | 5000 |

`records`: one object per stored reading with the detector's output (`expected`, `expected_low`, `expected_high`,
`robust_z`, `flagged`). `columns`: parallel arrays aligned to the hourly grid from `start` (`null` where the sensor
did not report), `flagged` = indices of flagged readings, plus `thresholds`. Missing `sensor_id` -> 422; unknown ->
404.

```bash
curl -s "$B/sensor-readings?sensor_id=VIB-001&start=2026-10-01T01:00:00Z"
```
```json
{"count":4,"data_notice":"...","end":"2026-10-01T04:00:00Z","is_simulated":true,"placement":"bridge_deck","sensor_id":"VIB-001",
 "sensor_type":"vibration","source":"simulator","start":"2026-10-01T01:00:00Z","unit":"mm/s",
 "readings":[{"expected":0.783,"expected_high":1.205,"expected_low":0.509,"flagged":true,"robust_z":8.9,"status":"ok",
              "ts":"2026-10-01T01:00:00Z","value":2.816}, ...]}
```

```bash
curl -s "$B/sensor-readings?sensor_id=VIB-001&start=2026-10-01T01:00:00Z&shape=columns"
```
```json
{"count":4,"start":"2026-10-01T01:00:00Z","step_minutes":60,"sensor_id":"VIB-001","unit":"mm/s",
 "thresholds":{"crit_high":10,"crit_low":null,"warn_high":5,"warn_low":null},
 "value":[2.816,1.806,1.522,1.08],"expected":[0.783,0.63,0.491,...],"expected_low":[0.509,0.41,0.319,...],
 "expected_high":[1.205,0.97,0.755,...],"robust_z":[8.9,7.32,7.88,...],"flagged":[0,1,2,...], ...}
```

---

## Anomalies

### `GET /anomalies`

| Parameter | Type | Default |
|---|---|---|
| `severity` | comma list of `low`, `medium`, `high`, `critical` | all |
| `sensor_type` | comma list | all |
| `asset_id`, `sensor_id` | string | all |
| `status` | `active`, `resolved` (evaluated at `as_of`) | all |
| `start`, `end` | datetime: filter on `started_at` (`start <= started_at <= end`) | open |
| `as_of` | datetime | `T_end`; only anomalies that had started by `as_of` are listed |
| `bbox` | `west,south,east,north` | none |
| `include` | `nearby_assets` | none |
| `sort` | `-started_at`, `started_at`, `-anomaly_score`, `severity` | `-started_at` |
| `limit` | 1..1000 | 500 |
| `offset` | >= 0 | 0 |

`{total, limit, offset, as_of, data_notice, items}`. **Anomaly item:** `anomaly_id`, `sensor_id`, `asset_id`,
`asset_name`, `asset_type`, `sensor_type`, `placement`, `anomaly_type`, `anomaly_label`, `started_at`, `ended_at`,
`peak_at`, `duration_hours`, `observed_value`, `expected_value`, `unit`, `robust_z`, `anomaly_score`,
`score_components`, `severity`, `detection_method`, `explanation`, `status` (at `as_of`), `is_simulated`,
`cluster_id`, `lon`, `lat`, `nearby_asset_count` (+ `nearby_assets` with `include=nearby_assets`).

```bash
curl -s "$B/anomalies?status=active&sort=-anomaly_score"
```
```json
{"as_of":"2026-10-01T04:00:00Z","data_notice":"...","limit":500,"offset":0,"total":6,"items":[
 {"anomaly_id":"ANM-0039","anomaly_label":"Pressure drop","anomaly_score":0.934,"anomaly_type":"pressure_drop","asset_id":"WM-006",
  "asset_name":"Simulated water main along 11th Avenue","asset_type":"water_main","cluster_id":null,
  "detection_method":"threshold+robust_zscore+rolling_median+isolation_forest","duration_hours":28,
  "started_at":"2026-09-30T01:00:00Z","peak_at":"2026-09-30T06:00:00Z","ended_at":"2026-10-01T04:00:00Z",
  "observed_value":15.631,"expected_value":55.934,"unit":"psi","robust_z":-64.71,
  "score_components":{"duration":0.736,"magnitude":1,"threshold":1},"severity":"critical","status":"active","is_simulated":true,
  "explanation":"Pressure drop on PRS-006 (water main): 15.6 psi at peak versus an expected 55.9 psi for that hour — ...",
  "lat":37.755106,"lon":-100.029333,"nearby_asset_count":3,"placement":"water_main","sensor_id":"PRS-006","sensor_type":"pressure"}, ...]}
```

`?severity=critical` gives 3 (one active); `?as_of=2026-09-15T00:00:00Z&status=active` gives 2; `?sort=bogus` -> 422.

### `GET /anomalies/{anomaly_id}`

| Parameter | Type | Default |
|---|---|---|
| `radius_m` | > 0, <= 5000 | `PROXIMITY_RADIUS_M` (100) |

Anomaly item + `nearby_assets` (other assets within `radius_m` of the sensor, nearest first: `asset_id`, `name`,
`asset_type`, `distance_m`) + `cluster` (the co-occurrence cluster, or `null`).

```bash
curl -s "$B/anomalies/ANM-0038"
```
```json
{"anomaly_id":"ANM-0038","sensor_id":"VIB-001","asset_id":"BRG-001","severity":"high","anomaly_score":0.546, ...,
 "nearby_assets":[{"asset_id":"RD-0097","asset_type":"road","distance_m":50.6,"name":"South 2nd Avenue"}],
 "cluster":{"cluster_id":1,"anomaly_ids":["ANM-0025","ANM-0036","ANM-0038"],"first_started_at":"2026-09-25T06:00:00Z",
            "last_ended_at":"2026-10-01T04:00:00Z","max_severity":"high","n_anomalies":3,"n_assets":2,"n_sensors":3,
            "sensor_types":["pressure","temperature","vibration"]}}
```

### `GET /simulation-events`

| Parameter | Type | Default |
|---|---|---|
| `is_anomaly` | boolean | all |

The simulator's ground truth: 40 injected abnormal events (`is_anomaly: true`) and 4 benign regional events
(`regional_rain` x 3, `regional_hot_spell`; `sensor_id: null`). `{total, items}`.

```json
{"total":40,"items":[...,{"asset_id":"RD-0037","description":"Injected simulated event: short elevated vibration, 7.7 times the normal level, 1 h",
 "ended_at":"2026-09-30T04:00:00Z","event_id":40,"event_type":"vibration_spike","is_anomaly":true,"magnitude":7.705,
 "sensor_id":"VIB-030","sensor_type":"vibration","started_at":"2026-09-30T04:00:00Z"}]}
```

---

## Spatial queries

All distances are metres on the spheroid (PostGIS geography); searches use the `(geom::geography)` expression
indexes.

### `GET /spatial/assets-within`

| Parameter | Type | Default |
|---|---|---|
| `lon` | -180..180, **required** | |
| `lat` | -90..90, **required** | |
| `radius_m` | > 0, <= 5000 | `PROXIMITY_RADIUS_M` (100) |

Asset features within the radius, nearest first, each with `distance_m`.

```bash
curl -s "$B/spatial/assets-within?lon=-100.0175&lat=37.7535&radius_m=60"
# FeatureCollection: RD-0044 3.2 m, BLD-0128 10.5 m, BLD-0125 10.7 m, BLD-0432 11.5 m, BLD-0126 17.7 m, ...
```

### `GET /spatial/nearest-asset`

| Parameter | Type | Default |
|---|---|---|
| `lon`, `lat` | **required** | |
| `asset_type` | asset type | any |

```bash
curl -s "$B/spatial/nearest-asset?lon=-100.0175&lat=37.7535"
```
```json
{"type":"Feature","geometry":{"type":"LineString",...},"properties":{"anomaly_count":0,"asset_id":"RD-0044","asset_type":"road",
 "category":"Transportation","centroid":[-100.018614,37.753537],"distance_m":3.2,"health_score":null,"highway_class":"residential",
 "is_simulated":false,"length_m":295,"monitored":false,"name":"Gunsmoke Street","sensor_count":0,"source_id":"osm","status":"not_monitored", ...}}
```

### `GET /spatial/sensors-in-asset-area`

| Parameter | Type | Default |
|---|---|---|
| `asset_id` | **required** | |
| `buffer_m` | 0..5000 | 25 |

Sensors within `buffer_m` of the asset geometry (its own and those of neighbouring assets), nearest first:
`{asset_id, buffer_m, items: [sensor item + distance_m]}`.

```bash
curl -s "$B/spatial/sensors-in-asset-area?asset_id=BRG-001"
# {"asset_id":"BRG-001","buffer_m":25,"items":[MST-031 0 m, TMP-001 0 m, VIB-002 0 m, TMP-002 0 m, VIB-001 0 m]}
```

### `GET /spatial/anomaly-density`

| Parameter | Type | Default |
|---|---|---|
| `start`, `end` | datetime | open-ended |

Every hexagonal cell (88) with `cell_id`, `anomaly_count` (anomalies whose sensor lies in the cell and whose interval
overlaps `start`..`end`) and `weighted_severity` (low 1, medium 2, high 4, critical 7).

```json
{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Polygon",...},
 "properties":{"anomaly_count":2,"cell_id":"1819_16085","weighted_severity":9}}, ...]}
```

### `GET /spatial/risk-zones`

| Parameter | Type | Default |
|---|---|---|
| `as_of` | datetime | `T_end` |

Every cell with `risk_score` (0..100), `risk_level` (`low`, `moderate`, `high`, `very_high`) and `anomaly_count`
(anomalies active in the cell at `as_of`); 0 where there is none. Derived from simulated anomalies.

```json
{"type":"FeatureCollection","as_of":"2026-10-01T04:00:00Z","features":[...,{"type":"Feature","geometry":{"type":"Polygon",...},
 "properties":{"anomaly_count":1,"cell_id":"1819_16085","risk_level":"high","risk_score":51.1}}, ...]}
```

### `GET /spatial/clusters`

Co-occurrence cluster hulls (descriptive, not causal): `cluster_id`, `n_anomalies`, `n_sensors`, `n_assets`,
`sensor_types`, `max_severity`, `first_started_at`, `last_ended_at`, `anomaly_ids`. Default dataset: 3 clusters.

```json
{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Polygon",...},
 "properties":{"anomaly_ids":["ANM-0029","ANM-0031","ANM-0035","ANM-0040","ANM-0041"],"cluster_id":2,
  "first_started_at":"2026-09-27T23:00:00Z","last_ended_at":"2026-10-01T04:00:00Z","max_severity":"high",
  "n_anomalies":5,"n_assets":5,"n_sensors":5,"sensor_types":["moisture","pressure","temperature","vibration"]}}, ...]}
```

---

## Base-map layers

| Endpoint | Content |
|---|---|
| `GET /layers/roads` | All 314 OSM highway rows (LineString): `road_id`, `osm_id`, `name`, `highway_class`, `surface`, `lanes`, `maxspeed`, `oneway`, `is_bridge`, `length_m`, `source_id` |
| `GET /layers/study-area` | The study-area rectangle: `slug`, `name`, `description`, `timezone`, `utm_srid` |
| `GET /layers/city-boundary` | TIGER city limits (MultiPolygon): `kind: "city_limits"`, `name: "Dodge City city"`, `source_id: "tiger"` |

---

## Playback

### `GET /playback`

Everything that changes with time, for the dashboard timeline, in one bundle (about 1.28 MB, about 220 kB gzipped):
`timestamps` (720), per sensor `values` (`null` while offline) and `status` (one character per hour: `n` normal,
`w` warning, `a` anomaly, `o` offline), per monitored asset `health` and `status` (`n`, `w`, `r` at_risk,
`c` critical), per cell that is ever above zero the hourly integer `risk`, and `stats` (the `/statistics` key
figures per hour). Element `i` of every series belongs to `timestamps[i]`. Built once per detection run and served
with an `ETag`; send `If-None-Match` to get 304.

```json
{"data_notice":"...","run_id":1,"timestamps":["2026-09-01T05:00:00Z","2026-09-01T06:00:00Z",...,"2026-10-01T04:00:00Z"],
 "sensors":{"VIB-001":{"values":[...,1.806,1.522,1.08],"status":"...aaa"}, ...},
 "assets":{"BRG-001":{"health":[...,58,58,58],"status":"...rrr"}, ...},
 "zones":{"1819_16085":{"risk":[...]}, ...},
 "stats":{"active_anomalies":[...,6,6,6],"active_sensors":[...,126,126,126],"assets_at_risk":[...,6,6,6],
          "critical_alerts":[...,1,1,1],"offline_sensors":[...,2,2,2],"warning_sensors":[...,2,0,0]}}
```

`stats[*][i]` equals `/statistics?as_of=timestamps[i]`, sensor status characters equal `/sensors?as_of=` and asset
health equals `/assets/{id}?as_of=` (parity tests).

---

## Dashboard configuration

### `GET /config.js`

Served only when `SERVE_DASHBOARD=true` (then `/` is the dashboard). It shadows the static `dashboard/config.js` so
that the dashboard served by the API always uses the API:

```js
window.DCIM_CONFIG = {"mode": "api", "apiBaseUrl": "", "basemapStyleUrl": "https://tiles.openfreemap.org/styles/dark"};
```

---

## Ingestion

### `POST /ingest/readings`

Stores readings from an external source through the same `IngestionService` the pipeline uses. Disabled unless
`INGEST_API_KEY` is set on the server. It does **not** run the detection: re-run `scripts/detect_anomalies.py` and
`scripts/analyze_spatial.py` afterwards. Details and a gateway example: [real-sensor-integration.md](real-sensor-integration.md).

| | |
|---|---|
| Header | `X-API-Key: <INGEST_API_KEY>` (compared in constant time) |
| Body | `{"readings":[{"sensor_id": str, "ts": ISO 8601 with offset, "value": number, "unit": str}, ...]}`, at most 10,000 readings and 4 MB |
| 200 | `{"accepted": n, "rejected": m, "reasons": {reason: count}}` |
| Rejection reasons | `invalid_record`, `unknown_sensor`, `naive_timestamp`, `non_finite_value`, `unit_mismatch`, `duplicate_reading` (the later duplicate is kept) |
| Accepted but `status='suspect'` | values outside a wide plausible range: temperature -60..150 °C, vibration 0..500 mm/s, moisture 0..100 %, pressure 0..500 psi |
| Order of checks | endpoint enabled (404) -> key (401) -> body size (413) -> body shape (422) |

Responses recorded against a server started with `INGEST_API_KEY` set (nothing was written: every reading was
rejected):

```bash
curl -s -X POST $B/ingest/readings -H "Content-Type: application/json" -d '{"readings":[]}'
# without INGEST_API_KEY on the server: 404 {"detail":"ingestion endpoint is disabled (INGEST_API_KEY not set)"}

curl -s -X POST $B/ingest/readings -H "Content-Type: application/json" -H "X-API-Key: wrong" -d '{"readings":[]}'
# 401 {"detail":"missing or invalid API key (header X-API-Key)"}

curl -s -X POST $B/ingest/readings -H "Content-Type: application/json" -H "X-API-Key: $INGEST_API_KEY" -d '{"readings":[
  {"sensor_id":"XYZ-999","ts":"2026-10-01T05:00:00Z","value":1.0,"unit":"mm/s"},
  {"sensor_id":"VIB-001","ts":"2026-10-01T05:00:00Z","value":1.0,"unit":"psi"},
  {"sensor_id":"VIB-001","ts":"2026-10-01T05:00:00","value":1.0,"unit":"mm/s"}]}'
# 200 {"accepted":0,"reasons":{"naive_timestamp":1,"unit_mismatch":1,"unknown_sensor":1},"rejected":3}

curl -s -X POST $B/ingest/readings -H "Content-Type: application/json" -H "X-API-Key: $INGEST_API_KEY" \
  -d '{"readings":[{"sensor_id":"VIB-001","value":"x"}]}'
# 422 {"detail":[{"type":"missing","loc":["body","readings",0,"ts"],"msg":"Field required"},
#                {"type":"float_parsing","loc":["body","readings",0,"value"],...},{"type":"missing","loc":["body","readings",0,"unit"],...}]}
```

A successful call answers, for example, `{"accepted":1,"rejected":0,"reasons":{}}`.
