# Real-sensor integration

The simulator is only the current data source. This page describes the two doors through which real readings can
enter the system, what each expects, and what has to happen afterwards. Nothing downstream of
`infra.sensor_readings` (detection, spatial analysis, API, dashboard) knows or cares where a reading came from.

```
             pull (stage 4)                                   push (any time)
SENSOR_SOURCE=simulated | http | <yours>              POST /ingest/readings  (X-API-Key)
        │  SensorSource.read(start, end)                      │
        └──────────────► IngestionService(conn).ingest(readings, source) ◄──┘
                                   │  validate, mark suspect, upsert (COPY)
                                   ▼
                         infra.sensor_readings ──► detect_anomalies ──► analyze_spatial ──► API / dashboard
```

## 1. The `SensorSource` protocol (pull)

`pipeline/sensors/sources.py`:

```python
class SensorSource(Protocol):
    name: str
    def read(self, start: datetime, end: datetime) -> Iterator[Reading]: ...
```

- `read` yields `Reading(sensor_id, ts, value, unit)` (`pipeline/models.py`) with `start <= ts <= end`; both bounds
  and every `ts` are timezone-aware.
- A source only **yields** readings. Validation and storage are done by `IngestionService`; reading the same range
  twice is harmless because ingestion upserts on (`sensor_id`, `ts`).
- `get_sensor_source(settings, sensors)` returns the implementation registered under `SENSOR_SOURCE`;
  `register_sensor_source(key, factory)` adds one (`factory(settings, sensors) -> SensorSource`).
- Stage 4 (`scripts/generate_sensors.py`) places the sensors, asks the selected source for
  `SIM_START .. SIM_START + SIM_DAYS` and ingests the result with `source = <name>`. Ground truth
  (`simulation_events`) is written only for the simulator; for any other source the detection run is not evaluated
  and the simulator's final-hour check is skipped.

Shipped implementations:

| `SENSOR_SOURCE` | Class | Notes |
|---|---|---|
| `simulated` (default) | `SimulatedSensorSource` | The deterministic simulator ("Simulated Sensor Data") |
| `http` | `HttpPollingSource` | Polls `HTTP_SOURCE_URL`; unit-tested with `httpx.MockTransport` (`tests/test_sources_ingestion.py`) |

## 2. HTTP adapter contract (`SENSOR_SOURCE=http`)

```bash
SENSOR_SOURCE=http
HTTP_SOURCE_URL=https://gateway.example.org/readings
```

Request, one per time range (no pagination):

```
GET https://gateway.example.org/readings?start=2026-09-01T05:00:00Z&end=2026-10-01T04:00:00Z
Accept: application/json
```

Response `200`, either an object with a `readings` array or the bare array:

```json
{"readings": [
  {"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": 21.4, "unit": "°C"},
  {"sensor_id": "VIB-003", "ts": "2026-09-01T05:00:00+00:00", "value": 1.02}
]}
```

| Field | Rule |
|---|---|
| `sensor_id` | string, an id in `infra.sensors` |
| `ts` | ISO 8601 string with a UTC offset or `Z` |
| `value` | number, in the sensor's unit |
| `unit` | optional; exactly `°C`, `mm/s`, `%` or `psi`. When omitted, the unit registered for the sensor is assumed |

A response that does not follow the shape (HTTP error, invalid JSON, a record without `sensor_id`/`ts`/`value`, an
unparsable timestamp, a non-numeric value) raises `SensorSourceError` and stage 4 fails. Well-formed records that are
not acceptable (unknown sensor, timestamp without offset, wrong unit) are passed on and rejected, with a reason, by
the ingestion service. Timeout: 30 s. The class accepts extra `headers` (for example an API key of the gateway) when
constructed in code.

## 3. Ingestion endpoint (push)

`POST /ingest/readings` stores readings through the same `IngestionService`. It is **disabled** (404) unless the
server has `INGEST_API_KEY` set, and it requires that key in the `X-API-Key` header.

```bash
curl -s -X POST http://localhost:8000/ingest/readings \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INGEST_API_KEY" \
  -d '{"readings":[{"sensor_id":"VIB-001","ts":"2026-10-01T05:00:00Z","value":1.42,"unit":"mm/s"}]}'
# {"accepted":1,"rejected":0,"reasons":{}}
```

| | |
|---|---|
| Body | `{"readings":[{"sensor_id", "ts", "value", "unit"}, ...]}`; all four fields required; at most 10,000 readings and 4 MB per call |
| Responses | 200 `{"accepted", "rejected", "reasons"}`; 401 wrong or missing key; 404 endpoint disabled; 413 body too large; 422 body not in this shape; 503 database unavailable |
| Rejected (not stored) | `invalid_record`, `unknown_sensor`, `naive_timestamp`, `non_finite_value`, `unit_mismatch`, `duplicate_reading` (same sensor and timestamp twice in one call; the later one is kept) |
| Stored as `status='suspect'` | values outside a wide plausible range: temperature -60..150 °C, vibration 0..500 mm/s, moisture 0..100 %, pressure 0..500 psi |
| Stored | upsert on (`sensor_id`, `ts`) with `source='api'`: re-sending a reading replaces it |

Real responses from the build environment (a server started with a key; every reading rejected, nothing written):

```bash
curl -s -X POST http://localhost:8000/ingest/readings -H "Content-Type: application/json" -H "X-API-Key: wrong" -d '{"readings":[]}'
# 401 {"detail":"missing or invalid API key (header X-API-Key)"}

curl -s -X POST http://localhost:8000/ingest/readings -H "Content-Type: application/json" -H "X-API-Key: $INGEST_API_KEY" -d '{"readings":[
  {"sensor_id":"XYZ-999","ts":"2026-10-01T05:00:00Z","value":1.0,"unit":"mm/s"},
  {"sensor_id":"VIB-001","ts":"2026-10-01T05:00:00Z","value":1.0,"unit":"psi"},
  {"sensor_id":"VIB-001","ts":"2026-10-01T05:00:00","value":1.0,"unit":"mm/s"}]}'
# 200 {"accepted":0,"reasons":{"naive_timestamp":1,"unit_mismatch":1,"unknown_sensor":1},"rejected":3}
```

The successful call above (`accepted: 1`) is the shape asserted by `tests/test_api_ingest.py` against the test
database; it was not sent to the project database.

## 4. MQTT: an example against the protocol

**Example only — not shipped, not tested, and `paho-mqtt` is not a project dependency.** It shows the size of the
change: one class with `name` and `read`, registered under a new `SENSOR_SOURCE` key. It buffers messages from a
broker and hands over those inside the requested range.

```python
# EXAMPLE ONLY. Requires: pip install "paho-mqtt>=2.0". Message payload: {"sensor_id", "ts", "value", "unit"}.
import json
import queue
from collections.abc import Iterator, Sequence
from datetime import datetime

import paho.mqtt.client as mqtt

from pipeline.config import Settings
from pipeline.models import Reading, SensorSpec
from pipeline.sensors.sources import register_sensor_source


class MqttSensorSource:
    name = "mqtt"

    def __init__(self, host: str, topic: str = "sensors/+/readings", port: int = 1883) -> None:
        self._inbox: queue.Queue[Reading] = queue.Queue()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(host, port)
        self._client.subscribe(topic, qos=1)
        self._client.loop_start()  # network loop in a background thread

    def _on_message(self, client, userdata, message) -> None:
        record = json.loads(message.payload)
        ts = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
        self._inbox.put(Reading(record["sensor_id"], ts, float(record["value"]), record["unit"]))

    def read(self, start: datetime, end: datetime) -> Iterator[Reading]:
        while not self._inbox.empty():
            reading = self._inbox.get_nowait()
            if start <= reading.ts <= end:
                yield reading


def _factory(settings: Settings, sensors: Sequence[SensorSpec]) -> MqttSensorSource:
    return MqttSensorSource(host="broker.example.org")


register_sensor_source("mqtt", _factory)  # then SENSOR_SOURCE=mqtt
```

The registration must run before stage 4 asks for the source: add the factory to `SENSOR_SOURCES` in
`pipeline/sensors/sources.py`, or import the module from wherever the pipeline is started. A production version would
also need reconnects, authentication/TLS, back-pressure and a store for messages that arrive between pipeline runs.
For a continuous feed it is often simpler to keep the broker client outside this code base and let a small bridge
post batches to `POST /ingest/readings`.

## 5. What has to change for real sensors, and what does not

Has to change:

- **The sensor register.** `infra.sensors` must describe the real devices: id, host asset, type, placement, unit,
  location and sampling interval. Today stage 4 creates it by rule (and stage 4 **deletes** all sensors, and with
  them all readings, before it places new ones). A real deployment loads its register instead of running placement.
- **Thresholds.** `infra.sensor_thresholds` per (sensor type, placement) must come from the devices' specifications or
  the owner's operating limits, not from the prototype's values.
- **Sensor types.** The schema accepts `temperature`, `vibration`, `moisture`, `pressure`; other types need a CHECK
  change and detector settings (work domain, scale floor) in `pipeline/detection/baseline.py`.
- **Scheduling.** Detection is a retrospective batch over everything stored. Continuous operation needs a schedule
  for stages 5-6 (or an incremental detector with trailing baselines), and probably a rolling window.
- **Evaluation.** Without `simulation_events` there is no recall/precision; validation against real maintenance or
  inspection records would be a separate piece of work.
- **The labels.** "Simulated Sensor Data" is set because the data is simulated; it should change only when it is no
  longer true.

Does not change: the database schema for readings and results, the ingestion service, the detection and
spatial-analysis code, the API and the dashboard.

## 6. Operational notes: after ingesting

Ingesting never runs the detection. To analyse new readings:

```bash
python run_pipeline.py --from detect --skip-export      # stages 5 and 6
# or step by step:
python scripts/detect_anomalies.py                      # replaces detection_runs, reading_scores, anomalies
python scripts/analyze_spatial.py                       # clusters, risk zones, asset health for the new run
python scripts/export_static.py                         # only if the static snapshot should change
```

- Do **not** re-run stages 3 or 4 after ingesting real readings: stage 3 deletes the study area (which cascades to
  everything) and stage 4 deletes the sensors (which cascades to their readings).
- Stage 5 truncates the previous run with `CASCADE`, which also removes the stage 6 results; always run stage 6
  after it. The API serves the new run without a restart (its playback cache follows the run).
- The analysed time axis runs from the first to the last stored reading on a grid of the sensors' most common
  sampling interval (60 min by default). A reading belongs to the first grid point at or after its timestamp; when
  several readings fall into one interval, the latest one is used. Readings after the current end extend the window,
  and the dashboard's timeline follows `GET /meta` -> `time`.
- Readings marked `suspect` are stored and shown; they are not excluded from detection.
- Until stages 5-6 have run, the API shows the previous analysis next to the new raw readings (for example in
  `GET /sensor-readings`, where new readings have no `expected` or `robust_z` yet).
