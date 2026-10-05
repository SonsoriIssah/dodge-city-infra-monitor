-- 06  Spatial aggregation
--
-- (a) Inventory per asset type with measures computed from the geometry (length, footprint).
-- (b) Anomalies aggregated per road: sensors and anomalies within 30 m of each road centre line.
--     (Aggregation per zone - hexagonal cells - is query 04.)
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/06_spatial_aggregation.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area. Lengths, areas and distances
-- are computed on the spheroid (geography).
-- (b) returns no rows until detection has run (scripts/detect_anomalies.py).

-- (a) Per asset type: number of assets, monitored assets, sensors, anomalies, total length and footprint.
SELECT v.asset_type,
       count(*)                                AS assets,
       count(*) FILTER (WHERE v.is_simulated)  AS simulated_assets,
       count(*) FILTER (WHERE v.monitored)     AS monitored_assets,
       sum(v.sensor_count)                     AS sensors,
       sum(v.anomaly_count)                    AS anomalies,
       round((sum(ST_Length(v.geom::geography))
                  FILTER (WHERE GeometryType(v.geom) = 'LINESTRING') / 1000.0)::numeric, 2) AS total_length_km,
       round((sum(ST_Area(v.geom::geography))
                  FILTER (WHERE GeometryType(v.geom) = 'POLYGON') / 10000.0)::numeric, 2)   AS total_footprint_ha
FROM infra.v_asset_summary v
GROUP BY v.asset_type
ORDER BY assets DESC, v.asset_type;

-- (b) Per road asset: sensors and anomalies within 30 m, most severe first.
--     active_anomalies uses the stored anomalies.status, which is the API's time rule evaluated at the end
--     of the analysed window: active when ended_at >= detection_runs.window_end, else resolved.
SELECT r.asset_id,
       r.name,
       r.properties ->> 'highway_class'        AS highway_class,
       s.sensors_within_30m,
       an.anomalies_within_30m,
       an.active_anomalies,
       an.weighted_severity
FROM infra.infrastructure_assets r
CROSS JOIN LATERAL (
    SELECT count(*) AS sensors_within_30m
    FROM infra.sensors se
    WHERE ST_DWithin(se.geom::geography, r.geom::geography, 30)
) s
CROSS JOIN LATERAL (
    SELECT count(*)                                    AS anomalies_within_30m,
           count(*) FILTER (WHERE a.status = 'active') AS active_anomalies,
           COALESCE(sum(CASE a.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2
                                        WHEN 'high' THEN 4 WHEN 'critical' THEN 7 END), 0) AS weighted_severity
    FROM infra.anomalies a
    WHERE ST_DWithin(a.geom::geography, r.geom::geography, 30)
) an
WHERE r.asset_type = 'road'
  AND an.anomalies_within_30m > 0
ORDER BY an.weighted_severity DESC, an.anomalies_within_30m DESC, r.asset_id
LIMIT 25;
