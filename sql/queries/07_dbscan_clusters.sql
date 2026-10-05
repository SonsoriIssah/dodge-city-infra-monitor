-- 07  Spatial clustering of anomalies with ST_ClusterDBSCAN (illustrative)
--
-- Density clusters of anomaly locations: eps = 200 m, at least 3 anomalies, in the study area's UTM zone.
-- This example is SPACE-ONLY: it also groups anomalies that happened weeks apart. The clusters the pipeline
-- stores (infra.anomaly_clusters, method 'st_dbscan') additionally require closeness in time and at least
-- three distinct sensors; they are descriptive co-occurrence clusters, not causal findings.
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/07_dbscan_clusters.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area.
-- Returns no rows until detection has run (scripts/detect_anomalies.py).

WITH labelled AS (
    SELECT an.anomaly_id,
           an.sensor_id,
           an.asset_id,
           an.started_at,
           an.ended_at,
           an.geom,
           ST_ClusterDBSCAN(ST_Transform(an.geom, sa.utm_srid), eps := 200, minpoints := 3) OVER () AS cluster_no
    FROM infra.anomalies an
    CROSS JOIN (SELECT s.utm_srid FROM infra.study_areas s LIMIT 1) sa
)
SELECT l.cluster_no,
       count(*)                    AS n_anomalies,
       count(DISTINCT l.sensor_id) AS n_sensors,
       count(DISTINCT l.asset_id)  AS n_assets,
       min(l.started_at)           AS first_started_at,
       max(l.ended_at)             AS last_ended_at,
       ST_AsText(ST_Centroid(ST_Collect(l.geom)), 6) AS centre
FROM labelled l
WHERE l.cluster_no IS NOT NULL
GROUP BY l.cluster_no
ORDER BY n_anomalies DESC, l.cluster_no;
