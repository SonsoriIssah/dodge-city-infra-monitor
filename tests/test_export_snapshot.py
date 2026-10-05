"""Static snapshot of the API (build contract section 11, amendment A2).

The snapshot is exported from ``infra_test`` into temporary folders - never into ``dashboard/data/snapshot``.
The file list and the request behind every file are typed here from the contract; each file is compared
byte for byte with the answer of the API under test. Two exports of the same data are identical; after a
later run on unchanged inputs (a different wall-clock ``finished_at``) only ``meta.json`` and
``manifest.json`` differ. The committed snapshot is checked against its own manifest without a database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend import export
from pipeline.config import SNAPSHOT_DIR
from pipeline.db.connection import connect
from tests import support
from tests.conftest import assert_test_database, make_settings
from tests.support import iso_z

# Contract section 11: file -> the request it is the answer of.
SNAPSHOT_REQUESTS = {
    "meta.json": "/meta",
    "assets.geojson": "/assets",
    "roads.geojson": "/layers/roads",
    "study-area.geojson": "/layers/study-area",
    "city-boundary.geojson": "/layers/city-boundary",
    "sensors.json": "/sensors",
    "anomalies.json": "/anomalies?include=nearby_assets&limit=1000",
    "clusters.geojson": "/spatial/clusters",
    "risk-zones.geojson": "/spatial/risk-zones",
    "simulation-events.json": "/simulation-events",
    "playback.json": "/playback",
}
MANIFEST = "manifest.json"
SNAPSHOT_BUDGET_BYTES = 8_000_000
PLAYBACK_BUDGET_BYTES = 2_000_000
A2_VOLATILE_FILES = {"meta.json", MANIFEST}  # amendment A2: they carry the wall-clock finishing time of the run


def request_of(path: str) -> str:
    """The API request behind a snapshot file (contract section 11)."""
    if path.startswith("readings/"):
        return f"/sensor-readings?sensor_id={Path(path).stem}&shape=columns"
    if path.startswith("health/"):
        return f"/assets/{Path(path).stem}/health"
    return SNAPSHOT_REQUESTS[path]


def files_of(directory: Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Exported:
    directory: Path
    files: dict[str, bytes]

    def json(self, path: str) -> Any:
        return json.loads(self.files[path])


@pytest.fixture(scope="module")
def first(test_db, tmp_path_factory) -> Exported:
    """Export through the function of stage 7."""
    directory = tmp_path_factory.mktemp("snapshot-first")
    summary = export.export_snapshot(test_db.settings, directory)
    exported = Exported(directory, files_of(directory))
    assert summary["files"] == len(exported.files) and summary["bytes"] == sum(map(len, exported.files.values()))
    return exported


@pytest.fixture(scope="module")
def second(test_db, tmp_path_factory, first) -> Exported:
    """Export again, through the command-line entry point, into another folder."""
    directory = tmp_path_factory.mktemp("snapshot-second")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(export, "get_settings", lambda: test_db.settings)
        assert export.main(["--output", str(directory)]) == 0
    return Exported(directory, files_of(directory))


@pytest.fixture(scope="module")
def later_run(test_db, tmp_path_factory, first, second) -> Iterator[Exported]:
    """Export after "another run on unchanged inputs": only the wall-clock ``finished_at`` of the run differs."""
    directory = tmp_path_factory.mktemp("snapshot-later")
    with connect(test_db.dsn) as conn:
        assert_test_database(conn)
        run_id, finished_at = conn.execute(
            "SELECT run_id, finished_at FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE infra.detection_runs SET finished_at = finished_at + interval '1 hour 23 seconds' WHERE run_id = %s",
            (run_id,),
        )
        conn.commit()
        try:
            export.export_snapshot(test_db.settings, directory)
        finally:
            conn.execute("UPDATE infra.detection_runs SET finished_at = %s WHERE run_id = %s", (finished_at, run_id))
            conn.commit()
    yield Exported(directory, files_of(directory))


# --- the file list ------------------------------------------------------------------------------------------------
def test_the_typed_request_table_is_the_contract_list():
    assert set(SNAPSHOT_REQUESTS) | {MANIFEST} == support.SNAPSHOT_TOP_LEVEL


def test_file_list_is_exactly_the_one_of_the_contract(first, db_conn):
    sensors = [row[0] for row in db_conn.execute("SELECT sensor_id FROM infra.sensors")]
    monitored = [row[0] for row in db_conn.execute("SELECT DISTINCT asset_id FROM infra.sensors")]
    assets = db_conn.execute("SELECT count(*) FROM infra.infrastructure_assets").fetchone()[0]
    expected = (
        support.SNAPSHOT_TOP_LEVEL
        | {f"readings/{sensor_id}.json" for sensor_id in sensors}  # readings for every sensor
        | {f"health/{asset_id}.json" for asset_id in monitored}  # health for monitored assets only
    )
    assert set(first.files) == expected
    assert sensors and monitored and len(monitored) < assets
    assert {path.name for path in first.directory.iterdir()} == support.SNAPSHOT_TOP_LEVEL | {"readings", "health"}
    assert {path.name for path in first.directory.iterdir() if path.is_dir()} == {"readings", "health"}


def test_lists_in_the_snapshot_are_complete(first, db_conn):
    count = lambda table: db_conn.execute(f"SELECT count(*) FROM infra.{table}").fetchone()[0]  # noqa: E731
    assets, sensors, anomalies = first.json("assets.geojson"), first.json("sensors.json"), first.json("anomalies.json")
    assert len(assets["features"]) == assets["numberMatched"] == count("infrastructure_assets")
    assert len(sensors["items"]) == sensors["total"] == count("sensors")
    assert len(anomalies["items"]) == anomalies["total"] == count("anomalies")
    assert all("nearby_assets" in item for item in anomalies["items"])  # include=nearby_assets
    assert len(first.json("simulation-events.json")["items"]) == count("simulation_events")
    assert len(first.json("clusters.geojson")["features"]) == count("anomaly_clusters")
    assert len(first.json("risk-zones.geojson")["features"]) == count("risk_zones")
    steps = first.json("meta.json")["time"]["count"]
    assert len(first.json("playback.json")["timestamps"]) == steps
    for path in first.files:
        if path.startswith(("readings/", "health/")):
            body = first.json(path)
            assert body["count"] == steps and body["start"] == first.json("meta.json")["time"]["start"], path  # the full window
            assert body["sensor_id" if path.startswith("readings/") else "asset_id"] == Path(path).stem, path


# --- byte for byte ------------------------------------------------------------------------------------------------
def test_every_file_is_the_api_response_byte_for_byte(first, client):
    compared = 0
    for path, body in first.files.items():
        if path == MANIFEST:
            continue
        response = client.get(request_of(path))
        assert response.status_code == 200, path
        assert response.content == body, f"{path} differs from GET {request_of(path)}"
        compared += 1
    assert compared == len(first.files) - 1 and compared > len(SNAPSHOT_REQUESTS)


def test_risk_zones_file_holds_the_scores_of_the_last_hour(first):
    zones, meta = first.json("risk-zones.geojson"), first.json("meta.json")
    assert zones["as_of"] == meta["time"]["end"]
    playback = first.json("playback.json")
    for feature in zones["features"]:
        series = playback["zones"].get(feature["properties"]["cell_id"])
        assert abs((series["risk"][-1] if series else 0) - feature["properties"]["risk_score"]) <= 0.5 + 5e-4


# --- determinism (amendment A2) -----------------------------------------------------------------------------------
def test_two_exports_of_the_same_data_are_byte_identical(first, second):
    assert set(first.files) == set(second.files)
    assert [path for path in first.files if first.files[path] != second.files[path]] == []


def test_a_later_run_changes_only_meta_and_manifest(first, later_run):
    assert set(later_run.files) == set(first.files)
    changed = {path for path in first.files if first.files[path] != later_run.files[path]}
    assert changed == A2_VOLATILE_FILES

    before, after = first.json("meta.json"), later_run.json("meta.json")
    assert before["detection_run"]["finished_at"] != after["detection_run"]["finished_at"]
    after["detection_run"]["finished_at"] = before["detection_run"]["finished_at"]
    assert after == before  # nothing else in meta.json moved

    old, new = first.json(MANIFEST), later_run.json(MANIFEST)
    assert new["generated_at"] == later_run.json("meta.json")["detection_run"]["finished_at"] != old["generated_at"]
    assert (new["as_of"], new["api_version"]) == (old["as_of"], old["api_version"])
    differing = [a["path"] for a, b in zip(old["files"], new["files"], strict=True) if a != b]
    assert differing == ["meta.json"]


def test_the_database_is_back_to_its_run_after_the_later_export(first, later_run, db_conn):
    finished_at = db_conn.execute("SELECT finished_at FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert iso_z(finished_at) == first.json(MANIFEST)["generated_at"]


# --- manifest -----------------------------------------------------------------------------------------------------
def test_manifest_lists_every_file_with_its_size_and_hash(first, db_conn):
    manifest = first.json(MANIFEST)
    assert set(manifest) == {"generated_at", "as_of", "api_version", "files"}
    finished_at, window_end = db_conn.execute(
        "SELECT finished_at, window_end FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert manifest["generated_at"] == iso_z(finished_at)  # the finishing time of the run, not the wall clock
    assert manifest["as_of"] == iso_z(window_end) == first.json("meta.json")["time"]["end"]
    assert manifest["api_version"] == first.json("meta.json")["version"] == "1.0.0"
    assert all(set(entry) == {"path", "bytes", "sha256"} for entry in manifest["files"])
    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == sorted(paths) and set(paths) == set(first.files) - {MANIFEST} and len(paths) == len(set(paths))
    for entry in manifest["files"]:
        body = first.files[entry["path"]]
        assert entry["bytes"] == len(body), entry["path"]
        assert entry["sha256"] == hashlib.sha256(body).hexdigest(), entry["path"]


# --- budgets, encoding ----------------------------------------------------------------------------------------------
def test_snapshot_and_playback_fit_their_budgets(first):
    total = sum(len(body) for body in first.files.values())
    assert total <= SNAPSHOT_BUDGET_BYTES, f"snapshot is {total} bytes"
    assert len(first.files["playback.json"]) <= PLAYBACK_BUDGET_BYTES
    assert total > 1_000_000  # and it is the full dataset, not a stub


def test_files_are_utf8_without_bom_or_carriage_returns_and_canonical_json(first):
    for path, body in first.files.items():
        text = body.decode("utf-8")  # strict: raises on invalid UTF-8
        assert not body.startswith(b"\xef\xbb\xbf"), path
        assert b"\r" not in body and "\\r" not in text, path
        assert body == body.strip(), path
        assert canonical(json.loads(text)) == body, path  # sorted keys, compact separators, real UTF-8
    assert "°C".encode() in first.files["meta.json"]  # not \u-escaped


def test_snapshot_strings_never_say_live_or_real_time(first):
    for path in ("meta.json", "anomalies.json", "sensors.json", "simulation-events.json"):
        found = [(where, text) for where, text in support.strings(first.json(path)) if support.BANNED_WORDS.search(text)]
        assert found == [], path


# --- writing and clearing the folder (no database) -----------------------------------------------------------------
def test_write_snapshot_replaces_an_older_snapshot_and_writes_bytes_unchanged(tmp_path):
    target = tmp_path / "snapshot"
    export.write_snapshot(target, {MANIFEST: b"{}", "health/OLD-001.json": b"1", "stale.json": b"2"})
    export.write_snapshot(target, {MANIFEST: b'{"files":[]}', "readings/VIB-001.json": b'{"a":"line\\n"}\n'})
    assert files_of(target) == {MANIFEST: b'{"files":[]}', "readings/VIB-001.json": b'{"a":"line\\n"}\n'}
    assert not (target / "health").exists()


def test_a_folder_that_is_not_a_snapshot_is_never_cleared(tmp_path):
    (tmp_path / "thesis.docx").write_bytes(b"precious")
    with pytest.raises(export.ExportError, match="refusing to clear"):
        export.write_snapshot(tmp_path, {MANIFEST: b"{}"})
    assert files_of(tmp_path) == {"thesis.docx": b"precious"}
    with pytest.raises(export.ExportError, match="not a directory"):
        export.clear_directory(tmp_path / "thesis.docx")


def test_manifest_and_budget_helpers():
    files = {"b.json": b"[1,2]", "a/x.json": b"{}", "playback.json": b"0123456789"}
    meta = {"detection_run": {"finished_at": "2026-10-05T10:00:00Z"}, "time": {"end": "2026-10-01T04:00:00Z"}, "version": "1.0.0"}
    manifest = json.loads(export.build_manifest(files, meta))
    assert manifest == {
        "generated_at": "2026-10-05T10:00:00Z",
        "as_of": "2026-10-01T04:00:00Z",
        "api_version": "1.0.0",
        "files": [
            {"path": path, "bytes": len(files[path]), "sha256": hashlib.sha256(files[path]).hexdigest()}
            for path in ("a/x.json", "b.json", "playback.json")
        ],
    }
    assert export.build_manifest(files, meta) == canonical(manifest)
    export.check_budgets(files)
    with pytest.raises(export.ExportError, match="playback.json"):
        export.check_budgets({"playback.json": b"x" * (PLAYBACK_BUDGET_BYTES + 1)})
    with pytest.raises(export.ExportError, match="budget"):
        export.check_budgets({"playback.json": b"x", "big.json": b"x" * SNAPSHOT_BUDGET_BYTES})
    assert (export.TOTAL_BUDGET_BYTES, export.PLAYBACK_BUDGET_BYTES) == (SNAPSHOT_BUDGET_BYTES, PLAYBACK_BUDGET_BYTES)


def test_export_without_a_database_fails_and_leaves_the_folder_alone(tmp_path, monkeypatch):
    previous = {MANIFEST: b'{"files":[]}', "meta.json": b'{"kept":true}'}
    export.write_snapshot(tmp_path, previous)
    monkeypatch.setattr(export, "get_settings", lambda: make_settings(DATABASE_URL=support.unreachable_dsn()))
    assert export.main(["--output", str(tmp_path)]) == 2  # EXIT_DATABASE_UNAVAILABLE
    assert files_of(tmp_path) == previous


# --- the committed snapshot (no database) --------------------------------------------------------------------------
def test_committed_snapshot_is_consistent_with_its_manifest():
    assert SNAPSHOT_DIR.is_dir(), "dashboard/data/snapshot is part of the repository (GitHub Pages serves it)"
    files = files_of(SNAPSHOT_DIR)
    manifest = json.loads(files[MANIFEST])
    assert set(manifest) == {"generated_at", "as_of", "api_version", "files"}
    assert support.ISO_Z.match(manifest["generated_at"]) and support.ISO_Z.match(manifest["as_of"])
    assert {entry["path"] for entry in manifest["files"]} == set(files) - {MANIFEST}
    for entry in manifest["files"]:
        body = files[entry["path"]]
        assert (entry["bytes"], entry["sha256"]) == (len(body), hashlib.sha256(body).hexdigest()), entry["path"]
        assert b"\r" not in body, entry["path"]
    top_level = {path for path in files if "/" not in path}
    assert top_level == support.SNAPSHOT_TOP_LEVEL
    assert {path.split("/")[0] for path in files if "/" in path} == {"readings", "health"}
    assert sum(map(len, files.values())) <= SNAPSHOT_BUDGET_BYTES and len(files["playback.json"]) <= PLAYBACK_BUDGET_BYTES

    meta, sensors, assets = json.loads(files["meta.json"]), json.loads(files["sensors.json"]), json.loads(files["assets.geojson"])
    assert manifest["generated_at"] == meta["detection_run"]["finished_at"] and manifest["as_of"] == meta["time"]["end"]
    assert {f"readings/{item['sensor_id']}.json" for item in sensors["items"]} == {p for p in files if p.startswith("readings/")}
    monitored = {f["properties"]["asset_id"] for f in assets["features"] if f["properties"]["monitored"]}
    assert {f"health/{asset_id}.json" for asset_id in monitored} == {p for p in files if p.startswith("health/")}
    assert meta["labels"] == support.LABELS and meta["data_notice"] == support.DATA_NOTICE
