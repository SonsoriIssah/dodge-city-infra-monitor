"""Spatial SQL (build contract sections 6 and 9): the three ``infra`` functions, geography nearest-neighbour,
and the documented example queries in ``sql/queries``. Integration tests on ``infra_test``.

Each function is compared with a brute-force query written for the test (plain ``ST_Distance`` on geography
over every row, no index, no function).
"""

from __future__ import annotations

import pytest

from pipeline.analysis import spatial
from pipeline.config import QUERIES_DIR

pytestmark = pytest.mark.db

CENTRE = (-100.0175, 37.7535)
POINTS = [CENTRE, (-100.0195, 37.7474), (-100.0290, 37.7610), (-100.0060, 37.7460), (-100.0120, 37.7570)]
DISTANCE_SQL = "ST_Distance(a.geom::geography, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography)"


def brute_force(conn, lon: float, lat: float, asset_type: str | None = None) -> list[tuple[str, float]]:
    """Every asset with its geodesic distance from the point, nearest first."""
    rows = conn.execute(
        f"SELECT a.asset_id, {DISTANCE_SQL} FROM infra.infrastructure_assets a "
        "WHERE %(asset_type)s::text IS NULL OR a.asset_type = %(asset_type)s::text",
        {"lon": lon, "lat": lat, "asset_type": asset_type},
    ).fetchall()
    return sorted(rows, key=lambda row: (row[1], row[0]))


# --- assets within a radius ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize("radius_m", [25.0, 150.0, 600.0])
def test_assets_within_radius_equals_a_brute_force_search(db_conn, point, radius_m):
    rows = db_conn.execute(
        "SELECT asset_id, asset_type, category, name, is_simulated, distance_m FROM infra.assets_within_radius(%s, %s, %s)",
        (*point, radius_m),
    ).fetchall()
    expected = [(asset_id, distance) for asset_id, distance in brute_force(db_conn, *point) if distance <= radius_m]
    assert {row[0] for row in rows} == {asset_id for asset_id, _ in expected}
    exact = dict(expected)
    for asset_id, *_rest, distance in rows:
        assert distance == pytest.approx(exact[asset_id], abs=0.051)  # metres on the spheroid, rounded to 0.1 m
        assert round(distance, 1) == distance
    assert [row[5] for row in rows] == sorted(row[5] for row in rows)  # nearest first


def test_assets_within_radius_returns_types_and_simulated_flag(db_conn):
    rows = db_conn.execute("SELECT asset_id, asset_type, is_simulated FROM infra.assets_within_radius(%s, %s, 800)", CENTRE).fetchall()
    stored = {row[0]: (row[1], row[2]) for row in db_conn.execute("SELECT asset_id, asset_type, is_simulated FROM infra.infrastructure_assets")}
    assert len(rows) > 50
    assert all(stored[asset_id] == (asset_type, simulated) for asset_id, asset_type, simulated in rows)
    assert {asset_type for _, asset_type, _ in rows} >= {"building", "road", "water_main"}
    assert db_conn.execute("SELECT count(*) FROM infra.assets_within_radius(0, 0, 500)").fetchone()[0] == 0


def test_python_wrappers_return_the_function_rows(db_conn):
    rows = spatial.assets_within_radius(db_conn, *CENTRE, 120.0)
    assert [row["asset_id"] for row in rows] == [r[0] for r in db_conn.execute("SELECT asset_id FROM infra.assets_within_radius(%s, %s, 120)", CENTRE)]
    assert set(rows[0]) == {"asset_id", "asset_type", "category", "name", "is_simulated", "distance_m"}
    nearest = spatial.nearest_asset(db_conn, *CENTRE)
    assert nearest["asset_id"] == brute_force(db_conn, *CENTRE)[0][0]
    assert spatial.nearest_asset(db_conn, *CENTRE, "no_such_type") is None


# --- nearest asset ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize("asset_type", [None, "building", "road", "bridge", "water_main", "street_light"])
def test_nearest_asset_is_the_minimum_geodesic_distance(db_conn, point, asset_type):
    row = db_conn.execute("SELECT asset_id, asset_type, distance_m FROM infra.nearest_asset(%s, %s, %s)", (*point, asset_type)).fetchone()
    ranked = brute_force(db_conn, *point, asset_type)
    nearest_distance = ranked[0][1]
    assert row[2] == pytest.approx(nearest_distance, abs=0.051)
    ties = {asset_id for asset_id, distance in ranked if distance - nearest_distance < 1e-6}
    assert row[0] in ties
    assert asset_type is None or row[1] == asset_type


def test_nearest_asset_uses_geography_not_degrees(db_conn):
    """A/B case: at this latitude a degree of longitude is about 88 km and a degree of latitude about 111 km.

    From P, asset A lies 0.00100 degrees north (111 m) and asset B 0.00115 degrees east (101 m). Ranked by
    degrees A looks nearer; in metres B is nearer. ``infra.nearest_asset`` must answer B.
    """
    lon, lat = -100.5, 37.5  # far from every real asset of the study area
    db_conn.execute(
        """
        INSERT INTO infra.infrastructure_assets (asset_id, study_area_id, asset_type, category, source_id, geom, centroid)
        SELECT v.asset_id, sa.study_area_id, 'street_light', 'Utilities', 'osm', p.geom, p.geom
        FROM (VALUES ('SL-A', %(lon)s, %(lat)s + 0.00100), ('SL-B', %(lon)s + 0.00115, %(lat)s)) AS v(asset_id, x, y)
        CROSS JOIN LATERAL (SELECT ST_SetSRID(ST_MakePoint(v.x, v.y), 4326) AS geom) p
        CROSS JOIN infra.study_areas sa
        """,
        {"lon": lon, "lat": lat},
    )
    metres = dict(
        db_conn.execute(
            f"SELECT a.asset_id, {DISTANCE_SQL} FROM infra.infrastructure_assets a WHERE a.asset_id IN ('SL-A', 'SL-B')",
            {"lon": lon, "lat": lat},
        ).fetchall()
    )
    assert metres["SL-A"] == pytest.approx(111.0, abs=0.5) and metres["SL-B"] == pytest.approx(101.6, abs=0.5)
    by_degrees = db_conn.execute(
        "SELECT asset_id FROM infra.infrastructure_assets ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) LIMIT 1", (lon, lat)
    ).fetchone()[0]
    assert by_degrees == "SL-A"  # what a geometry KNN would answer: wrong
    for asset_type in (None, "street_light"):
        row = db_conn.execute("SELECT asset_id, distance_m FROM infra.nearest_asset(%s, %s, %s)", (lon, lat, asset_type)).fetchone()
        assert row == ("SL-B", pytest.approx(101.6, abs=0.5))
    within = db_conn.execute("SELECT asset_id FROM infra.assets_within_radius(%s, %s, 105)", (lon, lat)).fetchall()
    assert within == [("SL-B",)]  # 105 m reaches B (101.6 m) but not A (111 m)


def test_nearest_asset_of_an_unknown_type_returns_no_row(db_conn):
    assert db_conn.execute("SELECT count(*) FROM infra.nearest_asset(%s, %s, 'tunnel')", CENTRE).fetchone()[0] == 0


# --- sensors in an asset area -------------------------------------------------------------------------------------
def most_monitored_assets(conn, limit: int = 4) -> list[str]:
    return [row[0] for row in conn.execute("SELECT asset_id FROM infra.sensors GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT %s", (limit,))]


@pytest.mark.parametrize("buffer_m", [0.5, 25.0, 100.0])
def test_sensors_in_asset_area_equals_a_brute_force_search(db_conn, buffer_m):
    for asset_id in most_monitored_assets(db_conn):
        rows = db_conn.execute(
            "SELECT sensor_id, asset_id, sensor_type, placement, unit, distance_m FROM infra.sensors_in_asset_area(%s, %s)", (asset_id, buffer_m)
        ).fetchall()
        expected = dict(
            db_conn.execute(
                """
                SELECT s.sensor_id, ST_Distance(s.geom::geography, a.geom::geography)
                FROM infra.sensors s CROSS JOIN infra.infrastructure_assets a
                WHERE a.asset_id = %s AND ST_Distance(s.geom::geography, a.geom::geography) <= %s
                """,
                (asset_id, buffer_m),
            ).fetchall()
        )
        assert {row[0] for row in rows} == set(expected), asset_id
        own = {row[0] for row in db_conn.execute("SELECT sensor_id FROM infra.sensors WHERE asset_id = %s", (asset_id,))}
        assert own <= {row[0] for row in rows}  # its own sensors sit on the asset
        assert [row[5] for row in rows] == sorted(row[5] for row in rows)
        for sensor_id, *_rest, distance in rows:
            assert distance == pytest.approx(expected[sensor_id], abs=0.051)


def test_sensors_in_asset_area_includes_the_sensors_of_neighbouring_assets(db_conn):
    asset_id = most_monitored_assets(db_conn, 1)[0]
    rows = db_conn.execute("SELECT sensor_id, asset_id FROM infra.sensors_in_asset_area(%s, 150)", (asset_id,)).fetchall()
    assert {owner for _, owner in rows} - {asset_id}  # that is the point of the query
    assert db_conn.execute("SELECT count(*) FROM infra.sensors_in_asset_area(%s)", (asset_id,)).fetchone()[0] >= 3  # default buffer 25 m


def test_sensors_in_asset_area_of_an_unknown_asset_or_negative_buffer(db_conn):
    assert db_conn.execute("SELECT count(*) FROM infra.sensors_in_asset_area('NOPE-1', 50)").fetchone()[0] == 0
    asset_id = most_monitored_assets(db_conn, 1)[0]
    negative = db_conn.execute("SELECT count(*) FROM infra.sensors_in_asset_area(%s, -10)", (asset_id,)).fetchone()[0]
    zero = db_conn.execute("SELECT count(*) FROM infra.sensors_in_asset_area(%s, 0)", (asset_id,)).fetchone()[0]
    assert negative == zero  # a negative buffer is treated as zero


def test_functions_do_not_depend_on_the_callers_search_path(db_conn):
    db_conn.execute("SET LOCAL search_path = pg_catalog")
    assert db_conn.execute("SELECT count(*) FROM infra.assets_within_radius(%s, %s, 100)", CENTRE).fetchone()[0] > 0
    assert db_conn.execute("SELECT count(*) FROM infra.nearest_asset(%s, %s)", CENTRE).fetchone()[0] == 1
    assert db_conn.execute("SELECT count(*) FROM infra.sensors_in_asset_area('BRG-001', 25)").fetchone()[0] > 0


# --- proximity and density helpers --------------------------------------------------------------------------------
def test_assets_near_an_anomaly_excludes_its_own_asset_unless_asked(db_conn):
    anomaly_id, own_asset = db_conn.execute("SELECT anomaly_id, asset_id FROM infra.anomalies ORDER BY anomaly_score DESC, anomaly_id LIMIT 1").fetchone()
    near = spatial.assets_near_anomaly(db_conn, anomaly_id, 100.0)
    with_own = spatial.assets_near_anomaly(db_conn, anomaly_id, 100.0, include_own_asset=True)
    assert own_asset not in {row["asset_id"] for row in near}
    assert {row["asset_id"] for row in with_own} == {row["asset_id"] for row in near} | {own_asset}
    expected = db_conn.execute(
        """
        SELECT a.asset_id FROM infra.anomalies an CROSS JOIN infra.infrastructure_assets a
        WHERE an.anomaly_id = %s AND a.asset_id <> an.asset_id AND ST_Distance(a.geom::geography, an.geom::geography) <= 100
        """,
        (anomaly_id,),
    ).fetchall()
    assert {row["asset_id"] for row in near} == {row[0] for row in expected}
    assert all(row["distance_m"] <= 100.0 for row in near)
    assert [row["distance_m"] for row in near] == sorted(row["distance_m"] for row in near)
    counts = spatial.nearby_asset_counts(db_conn, 100.0)
    assert counts[anomaly_id] == len(near)
    assert set(counts) == {row[0] for row in db_conn.execute("SELECT anomaly_id FROM infra.anomalies")}


def test_proximity_analysis_lists_assets_near_active_anomalies(db_conn):
    t_end = db_conn.execute("SELECT window_end FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    rows = spatial.assets_near_active_anomalies(db_conn, t_end, 100.0)
    expected = db_conn.execute(
        """
        SELECT a.asset_id, count(*), min(ST_Distance(a.geom::geography, an.geom::geography))
        FROM infra.anomalies an CROSS JOIN infra.infrastructure_assets a
        WHERE an.started_at <= %(t)s AND an.ended_at >= %(t)s AND ST_Distance(a.geom::geography, an.geom::geography) <= 100
        GROUP BY a.asset_id
        """,
        {"t": t_end},
    ).fetchall()
    assert {row["asset_id"]: row["active_anomalies"] for row in rows} == {row[0]: row[1] for row in expected}
    nearest = {row[0]: row[2] for row in expected}
    assert all(row["distance_m"] == pytest.approx(nearest[row["asset_id"]], abs=0.051) for row in rows)
    assert all(row["max_severity"] in ("low", "medium", "high", "critical") for row in rows)
    hosts = {row[0] for row in db_conn.execute("SELECT asset_id FROM infra.anomalies WHERE started_at <= %(t)s AND ended_at >= %(t)s", {"t": t_end})}
    assert hosts <= {row["asset_id"] for row in rows}  # the anomalous assets themselves are part of the answer
    assert spatial.assets_near_active_anomalies(db_conn, t_end - (t_end - db_conn.execute("SELECT window_start FROM infra.detection_runs").fetchone()[0]), 100.0) == []


def test_anomaly_density_counts_each_anomaly_once_with_severity_weights(db_conn):
    cells = spatial.anomaly_density(db_conn)
    total = db_conn.execute("SELECT count(*) FROM infra.anomalies").fetchone()[0]
    weighted = db_conn.execute(
        "SELECT sum(CASE severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 4 ELSE 7 END) FROM infra.anomalies"
    ).fetchone()[0]
    assert len(cells) == db_conn.execute("SELECT count(*) FROM infra.risk_zones").fetchone()[0]  # every cell, zeros included
    assert sum(cell["anomaly_count"] for cell in cells) == total
    assert sum(cell["weighted_severity"] for cell in cells) == weighted
    assert any(cell["anomaly_count"] == 0 for cell in cells)
    start, end = db_conn.execute("SELECT window_start, window_start + interval '100 hours' FROM infra.detection_runs").fetchone()
    early = spatial.anomaly_density(db_conn, start, end)
    overlapping = db_conn.execute("SELECT count(*) FROM infra.anomalies WHERE started_at <= %s AND ended_at >= %s", (end, start)).fetchone()[0]
    assert sum(cell["anomaly_count"] for cell in early) == overlapping < total
    with_geometry = spatial.anomaly_density(db_conn, with_geometry=True)
    assert with_geometry[0]["geometry"]["type"] == "Polygon"
    per_type = spatial.anomalies_per_asset_type(db_conn)
    assert sum(row["anomaly_count"] for row in per_type) == total and sum(row["weighted_severity"] for row in per_type) == weighted


# --- documented example queries -----------------------------------------------------------------------------------
QUERY_FILES = sorted(path.name for path in QUERIES_DIR.glob("*.sql"))
EXPECTED_COLUMNS = {
    "01_sensors_within_asset_area.sql": [{"asset_id", "sensor_id", "sensor_type", "distance_m"}, {"sensor_id", "distance_m"}],
    "02_infrastructure_near_anomaly.sql": [{"anomaly_id", "asset_id", "asset_type", "distance_m", "is_host_asset"}],
    "03_assets_within_radius.sql": [{"asset_id", "asset_type", "is_simulated", "distance_m"}, {"asset_type", "assets", "nearest_m"}],
    "04_anomaly_density_hex.sql": [{"cell_id", "anomaly_count", "weighted_severity", "anomalies_per_hectare"}],
    "05_nearest_asset.sql": [{"searched_type", "asset_id", "distance_m"}, {"asset_id", "asset_type", "distance_m"}],
    "06_spatial_aggregation.sql": [{"asset_type", "assets", "monitored_assets", "sensors", "anomalies"}, {"asset_id", "active_anomalies", "weighted_severity"}],
    "07_dbscan_clusters.sql": [{"cluster_no", "n_anomalies", "n_sensors", "n_assets"}],
}


def run_file(conn, name: str) -> list[tuple[list[str], list[tuple]]]:
    """Execute every statement of a query file; (column names, rows) per result set."""
    results = []
    cursor = conn.execute((QUERIES_DIR / name).read_text(encoding="utf-8"))
    while True:
        if cursor.description is not None:
            results.append(([column.name for column in cursor.description], cursor.fetchall()))
        if not cursor.nextset():
            return results


def test_the_seven_documented_queries_exist():
    assert sorted(EXPECTED_COLUMNS) == QUERY_FILES
    for name in QUERY_FILES:
        text = (QUERIES_DIR / name).read_text(encoding="utf-8")
        assert text.lstrip().startswith("--"), name  # commented
        assert "psql" in text and "ON_ERROR_STOP" in text, name  # says how to run it


@pytest.mark.parametrize("name", sorted(EXPECTED_COLUMNS))
def test_documented_query_runs_and_returns_rows(db_conn, name):
    results = run_file(db_conn, name)
    assert len(results) == len(EXPECTED_COLUMNS[name])
    for (columns, rows), expected in zip(results, EXPECTED_COLUMNS[name]):
        assert expected <= set(columns), (name, columns)
        assert rows, f"{name} returned no rows on a populated database"


def test_nothing_in_the_example_queries_is_hard_coded_to_the_default_study_area():
    for name in QUERY_FILES:
        sql = "\n".join(line for line in (QUERIES_DIR / name).read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--"))
        assert "-100." not in sql and "37.7" not in sql, name
        assert "Dodge" not in sql and "BRG-0" not in sql and "ANM-0" not in sql, name
        assert "infra." in sql  # schema-qualified


def test_query_04_density_matches_the_stored_grid(db_conn):
    (columns, rows), = run_file(db_conn, "04_anomaly_density_hex.sql")
    cell, count, weight = columns.index("cell_id"), columns.index("anomaly_count"), columns.index("weighted_severity")
    stored = {
        row[0]: (row[1], row[2])
        for row in db_conn.execute(
            """
            SELECT z.cell_id, count(an.anomaly_id),
                   COALESCE(sum(CASE an.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 4 WHEN 'critical' THEN 7 END), 0)
            FROM infra.risk_zones z LEFT JOIN infra.anomalies an ON ST_Covers(z.geom, an.geom) GROUP BY z.cell_id
            """
        )
    }
    from_query = {row[cell]: (row[count], row[weight]) for row in rows}
    assert set(stored) <= set(from_query)  # the same 'i_j' cell ids
    assert {cell_id: from_query[cell_id] for cell_id in stored} == stored
    assert sum(value[0] for value in from_query.values()) == db_conn.execute("SELECT count(*) FROM infra.anomalies").fetchone()[0]


def test_query_07_is_space_only_dbscan(db_conn):
    (columns, rows), = run_file(db_conn, "07_dbscan_clusters.sql")
    n_anomalies = columns.index("n_anomalies")
    assert all(row[n_anomalies] >= 3 for row in rows)  # minpoints := 3
    stored = db_conn.execute("SELECT count(*) FROM infra.anomalies WHERE cluster_id IS NOT NULL").fetchone()[0]
    assert sum(row[n_anomalies] for row in rows) >= stored  # space-only clusters are a superset of the stored ones
