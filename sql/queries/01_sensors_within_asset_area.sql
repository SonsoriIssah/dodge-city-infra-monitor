-- 01  Sensors within an infrastructure area
--
-- Which sensors lie inside, or within 25 m of, the footprint / line of one asset?
-- (Includes sensors mounted on neighbouring assets - that is the point of the question.)
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/01_sensors_within_asset_area.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area: the query picks its own
-- example target from the data (the asset that carries the most sensors). Distances are metres on the
-- spheroid (geography).
-- Returns no rows until the sensor stage has run (scripts/generate_sensors.py).

WITH target AS (
    SELECT v.asset_id
    FROM infra.v_asset_summary v
    WHERE v.sensor_count > 0
    ORDER BY v.sensor_count DESC, v.asset_id
    LIMIT 1
)
SELECT a.asset_id,
       a.asset_type,
       a.name                                   AS asset_name,
       s.sensor_id,
       s.sensor_type,
       s.placement,
       (s.asset_id = a.asset_id)                AS mounted_on_this_asset,
       ST_DWithin(s.geom::geography, a.geom::geography, 0.5) AS on_asset_geometry,  -- within 0.5 m of it
       round(ST_Distance(s.geom::geography, a.geom::geography)::numeric, 1) AS distance_m
FROM target t
JOIN infra.infrastructure_assets a ON a.asset_id = t.asset_id
JOIN infra.sensors s ON ST_DWithin(s.geom::geography, a.geom::geography, 25)
ORDER BY distance_m, s.sensor_id;

-- The same question through the SQL function used by the API
-- (GET /spatial/sensors-in-asset-area?asset_id=...&buffer_m=25):
SELECT f.*
FROM (
    SELECT v.asset_id
    FROM infra.v_asset_summary v
    WHERE v.sensor_count > 0
    ORDER BY v.sensor_count DESC, v.asset_id
    LIMIT 1
) t
CROSS JOIN LATERAL infra.sensors_in_asset_area(t.asset_id, 25) AS f;
