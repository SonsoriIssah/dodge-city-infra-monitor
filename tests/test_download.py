"""Stage 1 - the downloaders and their cache rules (build contract section 2). No network, no database.

Every HTTP exchange goes through ``httpx.MockTransport`` and every file is written under ``tmp_path``:

* the cache is used unless ``refresh`` is asked for;
* a failed download keeps an existing cache (with a warning); without a cache OpenStreetMap is fatal and the
  bridge inventory and the city boundary are skipped;
* a busy Overpass endpoint (429 / 504) is retried after at least 30 s, other failures move on to a mirror;
* the bridge-inventory downloader checks the expected field names, the boundary downloader the layer name;
* ``SOURCES.json`` records one entry per file with the documented fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.gis import nbi, osm, tiger
from pipeline.gis.sources import http_client
from pipeline.stages import download as stage

# Contract section 2: the fields of a SOURCES.json entry.
ENTRY_KEYS = {
    "source_id", "file", "url", "retrieved_at", "sha256", "feature_count", "license", "attribution_text", "vintage",
    "terms_url",
}  # fmt: skip
OSM_BASE = "2026-10-03T21:14:02Z"
OSM_BODY = json.dumps({"osm3s": {"timestamp_osm_base": OSM_BASE}, "elements": [{"type": "way", "id": n} for n in (1, 2, 3)]}).encode()
NBI_LAYER = {"name": "NTAD_National_Bridge_Inventory", "description": "The National Bridge Inventory dataset is as of June 20, 2025 from FHWA."}
NBI_BODY = json.dumps(
    {
        "fields": [{"name": name} for name in nbi.REQUIRED_FIELDS],
        "features": [{"attributes": dict.fromkeys(nbi.REQUIRED_FIELDS, "1"), "geometry": {"x": -100.02, "y": 37.75}}] * 4,
    }
).encode()
TIGER_LAYER = {"name": "Incorporated Places", "description": "Census 2020 incorporated places, TIGERweb vintage 2025"}
TIGER_BODY = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"NAME": "Dodge City", "GEOID": "2018250"},
                      "geometry": {"type": "MultiPolygon", "coordinates": [[[[-100.1, 37.7], [-100.0, 37.7], [-100.0, 37.8], [-100.1, 37.7]]]]}}],
    }
).encode()  # fmt: skip


class Network:
    """A scripted network: answers per host, with a log of the requests that were made."""

    def __init__(self, **answers: Any) -> None:
        self.answers = {"osm": OSM_BODY, "nbi": NBI_BODY, "nbi_layer": NBI_LAYER, "tiger": TIGER_BODY, "tiger_layer": TIGER_LAYER, **answers}
        self.requests: list[httpx.Request] = []

    def service(self, request: httpx.Request) -> str:
        host = request.url.host
        if "overpass" in host:
            return "osm"
        name = "nbi" if host == "services.arcgis.com" else "tiger" if host == "tigerweb.geo.census.gov" else ""
        assert name, f"unexpected request to {request.url}"
        return name if request.url.path.endswith("/query") else f"{name}_layer"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[self.service(request)]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, int):
            return httpx.Response(answer, text="service problem")
        return httpx.Response(200, content=answer if isinstance(answer, bytes) else json.dumps(answer).encode())

    def client(self) -> httpx.Client:
        return http_client(transport=httpx.MockTransport(self.handle))

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


def down(request: httpx.Request) -> Exception:
    return httpx.ConnectError("no route to host", request=request)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def bbox(default_settings):
    return default_settings.bbox


# --- OpenStreetMap ------------------------------------------------------------------------------------------------
def test_osm_download_writes_the_response_bytes_and_describes_them(tmp_path, bbox):
    network = Network()
    entry = osm.download(tmp_path, bbox, client=network.client())
    assert (tmp_path / "osm.json").read_bytes() == OSM_BODY
    assert set(entry) == ENTRY_KEYS
    assert (entry["source_id"], entry["file"], entry["feature_count"], entry["vintage"]) == ("osm", "osm.json", 3, OSM_BASE)
    assert entry["sha256"] == hashlib.sha256(OSM_BODY).hexdigest() and entry["url"] == osm.OVERPASS_ENDPOINTS[0]
    assert "ODbL" in entry["license"] and "OpenStreetMap contributors" in entry["attribution_text"]
    assert entry["retrieved_at"].endswith("Z") and entry["terms_url"]

    (request,) = network.requests
    assert request.method == "POST"
    query = httpx.QueryParams(request.content.decode())["data"]
    assert "[bbox:37.745,-100.03,37.762,-100.005]" in query  # south,west,north,east: the Overpass order
    assert "out geom" in query and 'way["building"]' in query and '"highway"="street_lamp"' in query
    agent = request.headers["user-agent"]
    assert "python-httpx" not in agent and len(agent) > 20  # a descriptive User-Agent


def test_osm_cache_is_used_unless_refresh_is_asked(tmp_path, bbox):
    (tmp_path / "osm.json").write_bytes(OSM_BODY)
    network = Network(osm=json.dumps({"elements": [{"type": "way", "id": 9}]}).encode())
    previous = {"url": "https://mirror.example/api", "retrieved_at": "2026-10-04T12:00:24Z"}
    entry = osm.download(tmp_path, bbox, previous=previous, client=network.client())
    assert network.requests == []
    assert (entry["url"], entry["retrieved_at"], entry["feature_count"], entry["vintage"]) == (previous["url"], previous["retrieved_at"], 3, OSM_BASE)
    assert entry["sha256"] == sha256(tmp_path / "osm.json")

    refreshed = osm.download(tmp_path, bbox, refresh=True, previous=entry, client=network.client())
    assert len(network.requests) == 1 and refreshed["feature_count"] == 1 and refreshed["sha256"] != entry["sha256"]


def test_osm_failure_keeps_the_cache_and_is_fatal_without_one(tmp_path, bbox, caplog):
    network = Network(osm=down)
    with pytest.raises(osm.OverpassError):
        osm.download(tmp_path, bbox, client=network.client())
    assert not (tmp_path / "osm.json").exists()
    assert len(network.requests) == len(osm.OVERPASS_ENDPOINTS)  # every mirror was tried once

    (tmp_path / "osm.json").write_bytes(OSM_BODY)
    with caplog.at_level(logging.WARNING):
        entry = osm.download(tmp_path, bbox, refresh=True, client=network.client())
    assert (tmp_path / "osm.json").read_bytes() == OSM_BODY and entry["feature_count"] == 3
    assert any("keeping the cached file" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "bad",
    [500, 403, b"<html>busy</html>", {"elements": []}, {"elements": [{"id": 1}], "remark": "runtime error: Query timed out"}, {"version": 0.6}],
    ids=["http-500", "http-403", "not-json", "no-elements", "runtime-remark", "no-element-list"],
)
def test_osm_moves_on_to_a_mirror_when_an_endpoint_is_unusable(tmp_path, bbox, bad):
    first = osm.OVERPASS_ENDPOINTS[0]
    network = Network(osm=lambda request: bad if str(request.url) == first else OSM_BODY)
    waits: list[float] = []
    entry = osm.download(tmp_path, bbox, client=network.client(), sleep=waits.append)
    assert [str(request.url) for request in network.requests] == list(osm.OVERPASS_ENDPOINTS[:2])
    assert entry["url"] == osm.OVERPASS_ENDPOINTS[1] and waits == []
    assert (tmp_path / "osm.json").read_bytes() == OSM_BODY


@pytest.mark.parametrize(("status", "retry_after", "at_least"), [(429, None, 30), (504, None, 30), (429, "5", 30), (429, "45", 45), (504, "soon", 30)])
def test_osm_waits_at_least_30_seconds_before_retrying_a_busy_endpoint(tmp_path, bbox, status, retry_after, at_least):
    answers = iter([httpx.Response(status, headers={"Retry-After": retry_after} if retry_after else {}), OSM_BODY])
    network = Network(osm=lambda request: next(answers))
    waits: list[float] = []
    entry = osm.download(tmp_path, bbox, client=network.client(), sleep=waits.append)
    assert len(waits) == 1 and waits[0] >= at_least >= 30
    assert [str(request.url) for request in network.requests] == [osm.OVERPASS_ENDPOINTS[0]] * 2  # the same endpoint again
    assert entry["url"] == osm.OVERPASS_ENDPOINTS[0]


def test_osm_gives_up_on_an_endpoint_that_stays_busy(tmp_path, bbox):
    network = Network(osm=429)
    waits: list[float] = []
    with pytest.raises(osm.OverpassError, match="429"):
        osm.download(tmp_path, bbox, client=network.client(), sleep=waits.append)
    assert len(network.requests) == 2 * len(osm.OVERPASS_ENDPOINTS) and len(waits) == len(osm.OVERPASS_ENDPOINTS)
    assert all(wait >= 30 for wait in waits)


# --- National Bridge Inventory ------------------------------------------------------------------------------------
def test_nbi_download_queries_the_bbox_envelope_and_records_the_vintage(tmp_path, bbox):
    network = Network()
    entry = nbi.download(tmp_path, bbox, client=network.client())
    assert (tmp_path / "nbi_bridges.json").read_bytes() == NBI_BODY
    assert set(entry) == ENTRY_KEYS
    assert (entry["source_id"], entry["file"], entry["feature_count"]) == ("nbi", "nbi_bridges.json", 4)
    assert entry["vintage"] == "data as of June 20, 2025"
    assert entry["attribution_text"] == "FHWA National Bridge Inventory (data as of June 20, 2025), distributed by USDOT/BTS NTAD"
    query = next(request for request in network.requests if request.url.path.endswith("/query"))
    assert "NTAD_National_Bridge_Inventory/FeatureServer/0/query" in query.url.path
    params = dict(query.url.params)
    assert params["geometry"] == "-100.03,37.745,-100.005,37.762"  # west,south,east,north
    assert (params["inSR"], params["outSR"], params["outFields"], params["f"]) == ("4326", "4326", "*", "json")
    assert entry["url"] == str(query.url)


NBI_FAILURES = {
    "network down": {"nbi": down},
    "http 500": {"nbi": 500},
    "service error": {"nbi": {"error": {"code": 400, "message": "Invalid query"}}},
    "no feature list": {"nbi": {"fields": [{"name": name} for name in nbi.REQUIRED_FIELDS]}},
    "renamed fields": {"nbi": {"fields": [{"name": "STRUCTURE_NUMBER"}], "features": [{"attributes": {"STRUCTURE_NUMBER": "1"}}]}},
    "not json": {"nbi": b"<html>maintenance</html>"},
    "layer info unavailable": {"nbi_layer": 503},
}


@pytest.mark.parametrize("answers", list(NBI_FAILURES.values()), ids=list(NBI_FAILURES))
def test_nbi_failure_keeps_the_cache_or_skips_the_layer(tmp_path, bbox, answers, caplog):
    network = Network(**answers)
    with caplog.at_level(logging.WARNING):
        assert nbi.download(tmp_path, bbox, client=network.client()) is None  # no cache: the layer is skipped
    assert not (tmp_path / "nbi_bridges.json").exists()
    assert any("skipped" in record.getMessage() for record in caplog.records)

    cached = b'{"fields": [], "features": [{"attributes": {"STRUCTURE_NUMBER_008": "000000000000001"}}]}'
    (tmp_path / "nbi_bridges.json").write_bytes(cached)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        entry = nbi.download(tmp_path, bbox, refresh=True, previous={"vintage": "data as of May 1, 2024"}, client=network.client())
    assert (tmp_path / "nbi_bridges.json").read_bytes() == cached  # the existing cache is kept
    assert entry["feature_count"] == 1 and entry["sha256"] == hashlib.sha256(cached).hexdigest()
    assert any("keeping the cached file" in record.getMessage() for record in caplog.records)


def test_nbi_names_the_missing_fields(tmp_path, bbox, caplog):
    renamed = [name for name in nbi.REQUIRED_FIELDS if name != "STRUCTURE_NUMBER_008"]
    network = Network(nbi={"fields": [{"name": name} for name in renamed], "features": []})
    with caplog.at_level(logging.WARNING):
        assert nbi.download(tmp_path, bbox, client=network.client()) is None
    assert any("STRUCTURE_NUMBER_008" in record.getMessage() for record in caplog.records)


def test_nbi_cache_is_used_unless_refresh_is_asked(tmp_path, bbox):
    (tmp_path / "nbi_bridges.json").write_bytes(NBI_BODY)
    network = Network()
    entry = nbi.download(tmp_path, bbox, client=network.client())
    assert network.requests == [] and entry["feature_count"] == 4
    nbi.download(tmp_path, bbox, refresh=True, client=network.client())
    assert len(network.requests) == 2  # layer description and query


# --- TIGERweb -----------------------------------------------------------------------------------------------------
def test_tiger_download_asks_for_the_place_and_records_the_layer_description(tmp_path, bbox):
    network = Network()
    entry = tiger.download(tmp_path, bbox, "2018250", client=network.client())
    assert (tmp_path / "city_boundary.geojson").read_bytes() == TIGER_BODY
    assert set(entry) == ENTRY_KEYS
    assert (entry["source_id"], entry["file"], entry["feature_count"]) == ("tiger", "city_boundary.geojson", 1)
    assert entry["vintage"] == TIGER_LAYER["description"]
    query = next(request for request in network.requests if request.url.path.endswith("/query"))
    assert query.url.path.endswith("/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4/query")
    params = dict(query.url.params)
    assert (params["where"], params["outFields"], params["outSR"], params["f"]) == ("GEOID='2018250'", "*", "4326", "geojson")


TIGER_FAILURES = {
    "another layer name": {"tiger_layer": {"name": "Census Designated Places"}},
    "network down": {"tiger": down},
    "http 404": {"tiger": 404},
    "no polygon": {"tiger": {"type": "FeatureCollection", "features": []}},
    "not a feature collection": {"tiger": {"error": {"code": 400}}},
}


@pytest.mark.parametrize("answers", list(TIGER_FAILURES.values()), ids=list(TIGER_FAILURES))
def test_tiger_failure_keeps_the_cache_or_skips_the_layer(tmp_path, bbox, answers):
    network = Network(**answers)
    assert tiger.download(tmp_path, bbox, "2018250", client=network.client()) is None
    assert not (tmp_path / "city_boundary.geojson").exists()

    (tmp_path / "city_boundary.geojson").write_bytes(TIGER_BODY)
    entry = tiger.download(tmp_path, bbox, "2018250", refresh=True, previous={"vintage": "older"}, client=network.client())
    assert (tmp_path / "city_boundary.geojson").read_bytes() == TIGER_BODY and entry["feature_count"] == 1


def test_tiger_place_id_is_digits_only_or_auto(bbox):
    with pytest.raises(ValueError, match="digits"):
        tiger.query_params("2018250' OR '1'='1", bbox)
    automatic = tiger.query_params("auto", bbox)
    assert automatic["geometryType"] == "esriGeometryPoint" and "where" in automatic
    assert [float(part) for part in automatic["geometry"].split(",")] == pytest.approx([-100.0175, 37.7535])  # the centre


# --- the stage ----------------------------------------------------------------------------------------------------
@pytest.fixture
def online(monkeypatch) -> Callable[..., Network]:
    """Route the HTTP clients of the three downloaders through a scripted network."""

    def connect(**answers: Any) -> Network:
        network = Network(**answers)
        for module in (osm, nbi, tiger):
            monkeypatch.setattr(module, "http_client", lambda *args, _network=network, **kwargs: _network.client())
        return network

    return connect


def test_stage_downloads_every_source_and_writes_sources_json(tmp_path, default_settings, online):
    network = online()
    entries = stage.run(default_settings, tmp_path)
    assert list(entries) == ["osm", "nbi", "tiger"]
    assert {path.name for path in tmp_path.iterdir()} == {"osm.json", "nbi_bridges.json", "city_boundary.geojson", "SOURCES.json"}
    written = json.loads((tmp_path / "SOURCES.json").read_text(encoding="utf-8"))
    assert [entry["source_id"] for entry in written["sources"]] == ["osm", "nbi", "tiger"]
    for entry in written["sources"]:
        assert set(entry) == ENTRY_KEYS
        assert entry["sha256"] == sha256(tmp_path / entry["file"]) and entry["retrieved_at"].endswith("Z")
        assert entry == entries[entry["source_id"]]
    assert b"\r" not in (tmp_path / "SOURCES.json").read_bytes()
    assert len(network.requests) == 5  # one Overpass query, two requests each for the inventory and the boundary

    again = stage.run(default_settings, tmp_path)  # everything is cached now
    assert len(network.requests) == 5 and again == entries
    assert json.loads((tmp_path / "SOURCES.json").read_text(encoding="utf-8")) == written


def test_stage_refresh_with_the_network_down_keeps_every_cached_file(tmp_path, default_settings, online):
    online()
    entries = stage.run(default_settings, tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    network = online(osm=down, nbi=down, nbi_layer=down, tiger=down, tiger_layer=down)
    refreshed = stage.run(default_settings, tmp_path, refresh=True)
    assert network.requests  # the refresh was attempted
    assert refreshed == entries
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_stage_without_cache_needs_openstreetmap_only(tmp_path, default_settings, online):
    online(nbi=down, nbi_layer=down, tiger=503, tiger_layer=503)
    entries = stage.run(default_settings, tmp_path / "raw")
    assert list(entries) == ["osm"]  # the optional layers are absent, not invented
    assert {path.name for path in (tmp_path / "raw").iterdir()} == {"osm.json", "SOURCES.json"}

    online(osm=down)
    with pytest.raises(osm.OverpassError):
        stage.run(default_settings, tmp_path / "empty")
    assert not (tmp_path / "empty" / "SOURCES.json").exists()


def test_stage_command_reports_failure_with_its_exit_code(tmp_path, default_settings, online, monkeypatch):
    monkeypatch.setattr(stage, "get_settings", lambda: default_settings)
    online(osm=down)
    assert stage.main(["--raw-dir", str(tmp_path / "raw")]) == 1
    online()
    assert stage.main(["--raw-dir", str(tmp_path / "raw")]) == 0
    assert stage.main(["--raw-dir", str(tmp_path / "raw"), "--refresh"]) == 0
    assert (tmp_path / "raw" / "osm.json").read_bytes() == OSM_BODY


def test_committed_sources_file_describes_the_committed_raw_files():
    from pipeline.config import RAW_DIR

    written = json.loads((RAW_DIR / "SOURCES.json").read_text(encoding="utf-8"))
    by_id = {entry["source_id"]: entry for entry in written["sources"]}
    assert {"osm", "nbi", "tiger"} <= set(by_id)
    for entry in written["sources"]:
        assert set(entry) == ENTRY_KEYS, entry["source_id"]
        assert entry["sha256"] == sha256(RAW_DIR / entry["file"]), entry["file"]
        assert entry["license"] and entry["attribution_text"] and entry["retrieved_at"].endswith("Z"), entry["source_id"]
    assert by_id["nbi"]["feature_count"] == 4  # contract section 2: four records in the default bounding box
