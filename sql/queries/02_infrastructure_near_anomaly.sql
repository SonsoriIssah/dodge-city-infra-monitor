-- 02  Infrastructure near an anomaly
--
-- Which assets lie within 100 m of an anomalous sensor? (proximity analysis)
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/02_infrastructure_near_anomaly.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area: the query picks its own
-- example target from the data (the anomaly with the highest anomaly score). Distances are metres on the
-- spheroid (geography); the search uses the expression index infrastructure_assets_geog_gix.
-- Returns no rows until detection has run (scripts/detect_anomalies.py).

WITH target AS (
    SELECT an.anomaly_id, an.asset_id, an.sensor_id, an.severity, an.anomaly_score, an.geom
    FROM infra.anomalies an
    ORDER BY an.anomaly_score DESC, an.anomaly_id
    LIMIT 1
)
SELECT t.anomaly_id,
       t.sensor_id,
       t.severity,
       t.anomaly_score,
       a.asset_id,
       a.asset_type,
       a.name                                   AS asset_name,
       (a.asset_id = t.asset_id)                AS is_host_asset,
       round(ST_Distance(a.geom::geography, t.geom::geography)::numeric, 1) AS distance_m
FROM target t
JOIN infra.infrastructure_assets a ON ST_DWithin(a.geom::geography, t.geom::geography, 100)
ORDER BY distance_m, a.asset_id;
