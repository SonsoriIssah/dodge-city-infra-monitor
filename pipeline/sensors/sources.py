"""Sensor sources: where readings come from (build contract section 7, brief R14).

A sensor source only *yields readings*. Validation and storage belong to
``pipeline.sensors.ingestion.IngestionService``; detection and the API read only the database. Replacing the
simulator with a real feed therefore means writing one more class with this shape and selecting it with
``SENSOR_SOURCE`` - nothing downstream changes.

    class SensorSource(Protocol):
        name: str
        def read(self, start: datetime, end: datetime) -> Iterator[Reading]: ...

``read`` returns the readings with ``start <= ts <= end`` (both ends inclusive, timezone-aware datetimes).
Reading the same range twice is harmless: ingestion upserts on (sensor_id, ts).

Sources shipped here
--------------------
``simulated``  ``SimulatedSensorSource`` - the deterministic simulator (the current data source).
``http``       ``HttpPollingSource`` - polls a JSON REST endpoint (``HTTP_SOURCE_URL``).

JSON contract of the HTTP source
--------------------------------
Request (no pagination; the caller asks for one time range at a time)::

    GET <HTTP_SOURCE_URL>?start=2026-09-01T05:00:00Z&end=2026-10-01T04:00:00Z
    Accept: application/json

Response ``200`` with either an object that has a ``readings`` array or the bare array::

    {"readings": [
        {"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": 21.4, "unit": "°C"},
        {"sensor_id": "VIB-003", "ts": "2026-09-01T05:00:00+00:00", "value": 1.02}
    ]}

* ``sensor_id`` (string) - an id of ``infra.sensors``.
* ``ts`` (string) - ISO 8601 with a UTC offset or ``Z``.
* ``value`` (number) - in the sensor's unit.
* ``unit`` (string, optional) - exact unit string (``°C``, ``mm/s``, ``%``, ``psi``); when omitted the unit
  registered for the sensor is assumed.

A response that is not shaped like this (HTTP error status, invalid JSON, a record without ``sensor_id`` /
``ts`` / ``value``, an unparsable timestamp, a non-numeric value) raises ``SensorSourceError``. Records that
are well-formed but not acceptable (unknown sensor, timestamp without offset, wrong unit) are passed on and
rejected, with a reason, by the ingestion service.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from pipeline.config import Settings
from pipeline.gis import http_client
from pipeline.models import InjectedEvent, Reading, SensorSpec
from pipeline.sensors import simulator

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 30.0


class SensorSourceError(RuntimeError):
    """A sensor source could not deliver readings (request failed or the payload is malformed)."""


@runtime_checkable
class SensorSource(Protocol):
    """Anything that yields readings for a time range."""

    name: str

    def read(self, start: datetime, end: datetime) -> Iterator[Reading]:
        """Readings with ``start <= ts <= end``."""
        ...


def _require_aware(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware datetimes")
    if end < start:
        raise ValueError("end must not be before start")


def iso_z(moment: datetime) -> str:
    """UTC timestamp as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimulatedSensorSource:
    """The simulator as a sensor source. It needs the sensors to simulate; the run is computed once and cached."""

    name = simulator.SOURCE_NAME

    def __init__(self, settings: Settings, sensors: Sequence[SensorSpec]) -> None:
        self.settings = settings
        self.sensors = list(sensors)
        self._result: simulator.SimulationResult | None = None

    @property
    def result(self) -> simulator.SimulationResult:
        """The simulation run (readings, ground truth, noise scales)."""
        if self._result is None:
            self._result = simulator.simulate(self.sensors, self.settings)
        return self._result

    def read(self, start: datetime, end: datetime) -> Iterator[Reading]:
        """Simulated readings with ``start <= ts <= end``."""
        _require_aware(start, end)
        return self.result.readings(start, end)

    def ground_truth(self) -> list[InjectedEvent]:
        """Injected abnormal events and benign regional events (rows for ``infra.simulation_events``)."""
        return self.result.ground_truth()


class HttpPollingSource:
    """Readings from a JSON REST endpoint (see the module docstring for the contract).

    ``sensors`` supplies the unit of records that do not state one. ``transport`` is for tests
    (``httpx.MockTransport``); ``headers`` are added to the request (for example an API key).
    """

    name = "http"

    def __init__(
        self,
        url: str,
        sensors: Sequence[SensorSpec] = (),
        *,
        timeout_s: float = HTTP_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not url or not url.strip():
            raise ValueError("HttpPollingSource needs the URL of the readings endpoint")
        self.url = url.strip()
        self.units = {sensor.sensor_id: sensor.unit for sensor in sensors}
        self.timeout_s = timeout_s
        self.transport = transport
        self.headers = dict(headers or {})

    def read(self, start: datetime, end: datetime) -> Iterator[Reading]:
        """Request the range once and yield its readings."""
        _require_aware(start, end)
        return iter(self._fetch(start, end))

    def _fetch(self, start: datetime, end: datetime) -> list[Reading]:
        params = {"start": iso_z(start), "end": iso_z(end)}
        try:
            with http_client(self.timeout_s, transport=self.transport) as client:
                response = client.get(self.url, params=params, headers={"Accept": "application/json", **self.headers})
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise SensorSourceError(
                f"sensor source {self.url} answered HTTP {status} for {params['start']} .. {params['end']}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SensorSourceError(
                f"sensor source {self.url} could not be reached: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SensorSourceError(f"sensor source {self.url} did not return JSON") from exc
        readings = self.parse(payload)
        logger.info("http source: %d readings for %s .. %s", len(readings), params["start"], params["end"])
        return readings

    def parse(self, payload: Any) -> list[Reading]:
        """Turn a decoded response into readings; ``SensorSourceError`` when it does not follow the contract."""
        records = payload.get("readings") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise SensorSourceError('bad payload: expected {"readings": [...]} or a JSON array of records')
        return [self._record(index, record) for index, record in enumerate(records)]

    def _record(self, index: int, record: Any) -> Reading:
        if not isinstance(record, dict):
            raise SensorSourceError(f"bad payload: record {index} is not an object")
        missing = [key for key in ("sensor_id", "ts", "value") if record.get(key) is None]
        if missing:
            raise SensorSourceError(f"bad payload: record {index} has no {', '.join(missing)}")
        sensor_id, raw_ts, value = record["sensor_id"], record["ts"], record["value"]
        if not isinstance(sensor_id, str) or not sensor_id.strip():
            raise SensorSourceError(f"bad payload: record {index}: sensor_id must be a non-empty string")
        if not isinstance(raw_ts, str):
            raise SensorSourceError(f"bad payload: record {index}: ts must be an ISO 8601 string")
        try:
            ts = datetime.fromisoformat(raw_ts.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError as exc:
            raise SensorSourceError(f"bad payload: record {index}: ts {raw_ts!r} is not an ISO 8601 timestamp") from exc
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SensorSourceError(f"bad payload: record {index}: value must be a number, got {type(value).__name__}")
        unit = record.get("unit")
        if unit is None:
            unit = self.units.get(sensor_id.strip(), "")
        elif not isinstance(unit, str):
            raise SensorSourceError(f"bad payload: record {index}: unit must be a string")
        return Reading(sensor_id.strip(), ts, float(value), unit)


SourceFactory = Callable[[Settings, Sequence[SensorSpec]], SensorSource]


def _http_factory(settings: Settings, sensors: Sequence[SensorSpec]) -> SensorSource:
    if not settings.HTTP_SOURCE_URL.strip():
        raise ValueError("SENSOR_SOURCE=http needs HTTP_SOURCE_URL (the URL of the readings endpoint)")
    return HttpPollingSource(settings.HTTP_SOURCE_URL, sensors)


# Registry keyed by SENSOR_SOURCE. A new kind of source (an MQTT bridge, an IoT platform client, ...) is one
# class with ``name`` and ``read`` plus one entry here.
SENSOR_SOURCES: dict[str, SourceFactory] = {
    "simulated": SimulatedSensorSource,
    "http": _http_factory,
}


def register_sensor_source(key: str, factory: SourceFactory) -> None:
    """Make another source selectable with ``SENSOR_SOURCE=<key>``."""
    SENSOR_SOURCES[key.strip().lower()] = factory


def get_sensor_source(settings: Settings, sensors: Sequence[SensorSpec]) -> SensorSource:
    """The sensor source selected by ``SENSOR_SOURCE`` (``simulated`` or ``http``)."""
    key = settings.SENSOR_SOURCE.strip().lower()
    factory = SENSOR_SOURCES.get(key)
    if factory is None:
        raise ValueError(
            f"unknown SENSOR_SOURCE {settings.SENSOR_SOURCE!r}; expected one of: {', '.join(sorted(SENSOR_SOURCES))}"
        )
    return factory(settings, sensors)
