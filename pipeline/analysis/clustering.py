"""Spatio-temporal co-occurrence clusters of anomalies (build contract section 9).

A cluster is a group of anomalies that are close in space AND time. It is descriptive, not a causal finding.

* Distance between two anomalies: ``max(metres apart / CLUSTER_EPS_M, hours between their
  [started_at, ended_at] intervals (0 when they overlap) / CLUSTER_EPS_HOURS)``; the whole matrix comes from
  ONE PostGIS query (distances in the study area's UTM zone).
* Labels: scikit-learn ``DBSCAN(metric='precomputed', eps=1.0, min_samples=CLUSTER_MIN_POINTS)``.
* Clusters with fewer than ``CLUSTER_MIN_SENSORS`` distinct sensors are discarded.
* Hull: ``ST_Buffer(ST_ConvexHull(points)::geography, 40 m)``, computed in PostGIS.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import psycopg
from sklearn.cluster import DBSCAN

from pipeline.config import Settings
from pipeline.db.loaders import jsonb
from pipeline.detection.baseline import FloatArray

logger = logging.getLogger(__name__)

METHOD = "st_dbscan"
DBSCAN_EPS = 1.0  # the pairwise distance is already scaled by the two CLUSTER_EPS_* settings
HULL_BUFFER_M = 40.0
MATCH_TOLERANCE_HOURS = 2  # as in the evaluation: an anomaly belongs to an injected event within this margin
# Text the simulator puts in the description of the events of its deliberately co-located group.
COLOCATED_MARKER = "co-located group"

PAIRWISE_SQL = """
WITH area AS (
    SELECT utm_srid FROM infra.study_areas LIMIT 1
),
points AS (
    SELECT an.anomaly_id, an.started_at, an.ended_at, ST_Transform(an.geom, area.utm_srid) AS geom_utm
    FROM infra.anomalies an
    CROSS JOIN area
)
SELECT a.anomaly_id,
       b.anomaly_id,
       GREATEST(
           ST_Distance(a.geom_utm, b.geom_utm) / %(eps_m)s,
           GREATEST(
               EXTRACT(epoch FROM GREATEST(a.started_at, b.started_at) - LEAST(a.ended_at, b.ended_at)), 0
           ) / 3600.0 / %(eps_hours)s
       )::float8 AS distance
FROM points a
JOIN points b ON a.anomaly_id < b.anomaly_id
"""

INSERT_CLUSTER_SQL = """
INSERT INTO infra.anomaly_clusters
    (cluster_id, run_id, method, params, n_anomalies, n_sensors, n_assets, sensor_types, max_severity,
     first_started_at, last_ended_at, centroid, geom)
SELECT %(cluster_id)s,
       %(run_id)s,
       %(method)s,
       %(params)s,
       count(*),
       count(DISTINCT an.sensor_id),
       count(DISTINCT an.asset_id),
       array_agg(DISTINCT an.sensor_type ORDER BY an.sensor_type),
       (ARRAY['low', 'medium', 'high', 'critical'])[
           max(array_position(ARRAY['low', 'medium', 'high', 'critical'], an.severity))
       ],
       min(an.started_at),
       max(an.ended_at),
       ST_Centroid(ST_Collect(an.geom)),
       ST_Buffer(ST_ConvexHull(ST_Collect(an.geom))::geography, %(buffer_m)s)::geometry
FROM infra.anomalies an
WHERE an.anomaly_id = ANY(%(anomaly_ids)s)
"""

COLOCATED_SQL = """
SELECT e.event_id, e.sensor_id, min(an.anomaly_id), min(an.cluster_id)
FROM infra.simulation_events e
LEFT JOIN infra.anomalies an
       ON an.sensor_id = e.sensor_id
      AND an.started_at <= e.ended_at + make_interval(hours => %(tolerance)s)
      AND an.ended_at >= e.started_at - make_interval(hours => %(tolerance)s)
WHERE e.is_anomaly AND e.description LIKE %(marker)s
GROUP BY e.event_id, e.sensor_id
ORDER BY e.event_id
"""


def pairwise_distances(conn: psycopg.Connection, settings: Settings) -> tuple[list[str], list[str], FloatArray]:
    """Anomaly ids (ordered), their sensor ids, and the symmetric space-time distance matrix."""
    rows = conn.execute("SELECT anomaly_id, sensor_id FROM infra.anomalies ORDER BY anomaly_id").fetchall()
    anomaly_ids = [row[0] for row in rows]
    position = {anomaly_id: i for i, anomaly_id in enumerate(anomaly_ids)}
    matrix = np.zeros((len(anomaly_ids), len(anomaly_ids)))
    pairs = conn.execute(
        PAIRWISE_SQL, {"eps_m": settings.CLUSTER_EPS_M, "eps_hours": settings.CLUSTER_EPS_HOURS}
    ).fetchall()
    for first, second, distance in pairs:
        i, j = position[first], position[second]
        matrix[i, j] = matrix[j, i] = distance
    return anomaly_ids, [row[1] for row in rows], matrix


def cluster_members(distances: FloatArray, sensor_ids: list[str], min_points: int, min_sensors: int) -> list[list[int]]:
    """Row indices of every kept cluster: DBSCAN on the precomputed distances, then the distinct-sensor rule.

    Clusters are ordered by their smallest row index, so numbering follows the order of the input.
    """
    if len(sensor_ids) < max(min_points, 1):
        return []
    labels = DBSCAN(eps=DBSCAN_EPS, min_samples=max(min_points, 1), metric="precomputed").fit(distances).labels_
    members: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        if label >= 0:
            members.setdefault(int(label), []).append(index)
    kept = [rows for rows in members.values() if len({sensor_ids[i] for i in rows}) >= min_sensors]
    return sorted(kept, key=lambda rows: rows[0])


def write_clusters(
    conn: psycopg.Connection, clusters: list[list[str]], run_id: int, settings: Settings
) -> list[dict[str, Any]]:
    """Insert the clusters (statistics and hull computed in PostGIS) and tag their anomalies."""
    params = {
        "eps_m": settings.CLUSTER_EPS_M,
        "eps_hours": settings.CLUSTER_EPS_HOURS,
        "min_points": settings.CLUSTER_MIN_POINTS,
        "min_sensors": settings.CLUSTER_MIN_SENSORS,
        "hull_buffer_m": HULL_BUFFER_M,
        "note": "co-occurrence cluster (anomalies close in space and time); descriptive, not a causal finding",
    }
    summaries: list[dict[str, Any]] = []
    for cluster_id, anomaly_ids in enumerate(clusters, start=1):
        conn.execute(
            INSERT_CLUSTER_SQL,
            {"cluster_id": cluster_id, "run_id": run_id, "method": METHOD, "params": jsonb(params),
             "buffer_m": HULL_BUFFER_M, "anomaly_ids": anomaly_ids},
        )  # fmt: skip
        conn.execute("UPDATE infra.anomalies SET cluster_id = %s WHERE anomaly_id = ANY(%s)", (cluster_id, anomaly_ids))
        row = conn.execute(
            """
            SELECT n_anomalies, n_sensors, n_assets, sensor_types, max_severity, first_started_at, last_ended_at,
                   round((ST_Area(geom::geography) / 10000.0)::numeric, 2)
            FROM infra.anomaly_clusters
            WHERE cluster_id = %s
            """,
            (cluster_id,),
        ).fetchone()
        summaries.append(
            {"cluster_id": cluster_id, "anomaly_ids": anomaly_ids, "n_anomalies": row[0], "n_sensors": row[1],
             "n_assets": row[2], "sensor_types": row[3], "max_severity": row[4], "first_started_at": row[5],
             "last_ended_at": row[6], "hull_hectares": float(row[7])}
        )  # fmt: skip
    return summaries


def colocated_group_recovery(conn: psycopg.Connection) -> dict[str, Any] | None:
    """How the simulator's deliberately co-located group shows up in the clusters; None when there is no group.

    ``recovered`` is true when every event of the group has an anomaly and those anomalies share one cluster.
    """
    rows = conn.execute(
        COLOCATED_SQL, {"tolerance": MATCH_TOLERANCE_HOURS, "marker": f"%{COLOCATED_MARKER}%"}
    ).fetchall()
    if not rows:
        return None
    cluster_ids = [row[3] for row in rows if row[3] is not None]
    best = max(set(cluster_ids), key=cluster_ids.count) if cluster_ids else None
    in_best = sum(1 for row in rows if best is not None and row[3] == best)
    return {
        "events": len(rows),
        "sensors": [row[1] for row in rows],
        "detected": sum(1 for row in rows if row[2] is not None),
        "cluster_id": best,
        "in_cluster": in_best,
        "recovered": in_best == len(rows),
    }


def run_clustering(conn: psycopg.Connection, settings: Settings, run_id: int) -> dict[str, Any]:
    """Cluster the anomalies of the latest run and store the clusters; returns a summary."""
    anomaly_ids, sensor_ids, distances = pairwise_distances(conn, settings)
    members = cluster_members(distances, sensor_ids, settings.CLUSTER_MIN_POINTS, settings.CLUSTER_MIN_SENSORS)
    clusters = write_clusters(conn, [[anomaly_ids[i] for i in rows] for rows in members], run_id, settings)
    logger.info(
        "clustering: %d anomalies -> %d co-occurrence cluster(s) holding %d anomalies",
        len(anomaly_ids), len(clusters), sum(cluster["n_anomalies"] for cluster in clusters),
    )  # fmt: skip
    for cluster in clusters:
        logger.info(
            "  cluster %d: %d anomalies on %d sensors / %d assets (%s), max severity %s, %s .. %s",
            cluster["cluster_id"], cluster["n_anomalies"], cluster["n_sensors"], cluster["n_assets"],
            ", ".join(cluster["sensor_types"]), cluster["max_severity"], cluster["first_started_at"].isoformat(),
            cluster["last_ended_at"].isoformat(),
        )  # fmt: skip
    group = colocated_group_recovery(conn)
    if group is not None and not group["recovered"]:
        logger.warning(
            "the simulator's co-located group was not recovered as one cluster: %d of its %d events were "
            "detected and %d share a cluster", group["detected"], group["events"], group["in_cluster"],
        )  # fmt: skip
    elif group is not None:
        logger.info(
            "  the simulator's co-located group (%d events) is cluster %d", group["events"], group["cluster_id"]
        )
    return {"clusters": clusters, "colocated_group": group}
