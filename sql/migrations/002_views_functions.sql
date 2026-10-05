-- 002_views_functions.sql
-- Convenience views and the spatial query functions of the prototype.
--
-- Distances are metres on the spheroid (geography). Proximity and nearest-neighbour searches use the
-- expression indexes on (geom::geography) created in 001_schema.sql.
-- Functions pin their search_path so they behave the same for every database role.

-- ---------------------------------------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------------------------------------

-- Latest reading of every sensor (NULL reading columns when the sensor has never reported).
CREATE OR REPLACE VIEW infra.v_sensor_latest AS
SELECT s.sensor_id,
       s.asset_id,
       s.sensor_type,
       s.placement,
       s.unit,
       s.is_simulated,
       s.source,
       r.ts,
       r.value,
       r.status AS reading_status,
       sc.expected,
       sc.robust_z,
       sc.flagged
FROM infra.sensors s
LEFT JOIN LATERAL (
    SELECT sr.ts, sr.value, sr.status
    FROM infra.sensor_readings sr
    WHERE sr.sensor_id = s.sensor_id
    ORDER BY sr.ts DESC
    LIMIT 1
) r ON true
LEFT JOIN infra.reading_scores sc ON sc.sensor_id = s.sensor_id AND sc.ts = r.ts;

-- Most recent Derived Asset Health Score of every monitored asset.
CREATE OR REPLACE VIEW infra.v_asset_health_latest AS
SELECT DISTINCT ON (h.asset_id)
       h.asset_id,
       h.as_of,
       h.run_id,
       h.health_score,
       h.status,
       h.frequency_penalty,
       h.severity_penalty,
       h.reading_penalty,
       h.sensor_penalty,
       h.anomalies_in_window,
       h.active_anomalies,
       h.sensors_reporting,
       h.sensors_total
FROM infra.asset_health h
ORDER BY h.asset_id, h.as_of DESC;

-- One row per asset with its monitoring summary. Assets without sensors have no score:
-- status = 'not_monitored' and health_score IS NULL.
CREATE OR REPLACE VIEW infra.v_asset_summary AS
SELECT a.asset_id,
       a.asset_type,
       a.category,
       a.name,
       a.is_simulated,
       a.source_id,
       a.properties,
       a.geom,
       a.centroid,
       COALESCE(s.sensor_count, 0)               AS sensor_count,
       COALESCE(s.sensor_types, ARRAY[]::text[]) AS sensor_types,
       COALESCE(s.sensor_count, 0) > 0           AS monitored,
       COALESCE(n.anomaly_count, 0)              AS anomaly_count,
       CASE WHEN COALESCE(s.sensor_count, 0) > 0 THEN h.health_score END AS health_score,
       CASE WHEN COALESCE(s.sensor_count, 0) > 0 THEN h.status ELSE 'not_monitored' END AS status,
       h.as_of                                   AS health_as_of
FROM infra.infrastructure_assets a
LEFT JOIN (
    SELECT se.asset_id,
           count(*)::integer AS sensor_count,
           array_agg(DISTINCT se.sensor_type ORDER BY se.sensor_type) AS sensor_types
    FROM infra.sensors se
    GROUP BY se.asset_id
) s ON s.asset_id = a.asset_id
LEFT JOIN (
    SELECT an.asset_id, count(*)::integer AS anomaly_count
    FROM infra.anomalies an
    GROUP BY an.asset_id
) n ON n.asset_id = a.asset_id
LEFT JOIN infra.v_asset_health_latest h ON h.asset_id = a.asset_id;

-- Anomalies still active at the end of the analysed window, with the asset they belong to.
CREATE OR REPLACE VIEW infra.v_active_anomalies AS
SELECT an.anomaly_id,
       an.run_id,
       an.sensor_id,
       an.asset_id,
       a.name AS asset_name,
       a.asset_type,
       an.sensor_type,
       an.anomaly_type,
       an.started_at,
       an.ended_at,
       an.peak_at,
       an.duration_hours,
       an.observed_value,
       an.expected_value,
       an.unit,
       an.robust_z,
       an.anomaly_score,
       an.severity,
       an.detection_method,
       an.explanation,
       an.cluster_id,
       an.geom
FROM infra.anomalies an
JOIN infra.infrastructure_assets a ON a.asset_id = an.asset_id
WHERE an.status = 'active';

-- ---------------------------------------------------------------------------------------------------------
-- Functions
-- ---------------------------------------------------------------------------------------------------------

-- Assets within radius_m metres of a point, nearest first.
CREATE OR REPLACE FUNCTION infra.assets_within_radius(
    p_lon double precision,
    p_lat double precision,
    p_radius_m double precision
)
RETURNS TABLE (
    asset_id     text,
    asset_type   text,
    category     text,
    name         text,
    is_simulated boolean,
    distance_m   double precision
)
LANGUAGE sql
STABLE
SET search_path = infra, public
AS $$
    SELECT a.asset_id,
           a.asset_type,
           a.category,
           a.name,
           a.is_simulated,
           round(ST_Distance(a.geom::geography,
                             ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography)::numeric, 1)::double precision
    FROM infra.infrastructure_assets a
    WHERE ST_DWithin(a.geom::geography, ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography, p_radius_m)
    ORDER BY ST_Distance(a.geom::geography, ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography), a.asset_id;
$$;

-- Nearest asset to a point (optionally of one asset type). Geography KNN: ordering by geometry would rank
-- candidates by degrees and pick the wrong asset at this latitude.
CREATE OR REPLACE FUNCTION infra.nearest_asset(
    p_lon double precision,
    p_lat double precision,
    p_asset_type text DEFAULT NULL
)
RETURNS TABLE (
    asset_id     text,
    asset_type   text,
    category     text,
    name         text,
    is_simulated boolean,
    distance_m   double precision
)
LANGUAGE sql
STABLE
SET search_path = infra, public
AS $$
    SELECT a.asset_id,
           a.asset_type,
           a.category,
           a.name,
           a.is_simulated,
           round(ST_Distance(a.geom::geography,
                             ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography)::numeric, 1)::double precision
    FROM infra.infrastructure_assets a
    WHERE p_asset_type IS NULL OR a.asset_type = p_asset_type
    ORDER BY a.geom::geography <-> ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography
    LIMIT 1;
$$;

-- Sensors inside an asset's area: within buffer_m metres of the asset geometry (0 = touching it only).
-- Returns the sensors of neighbouring assets too, which is the point of the query.
CREATE OR REPLACE FUNCTION infra.sensors_in_asset_area(
    p_asset_id text,
    p_buffer_m double precision DEFAULT 25
)
RETURNS TABLE (
    sensor_id   text,
    asset_id    text,
    sensor_type text,
    placement   text,
    unit        text,
    distance_m  double precision
)
LANGUAGE sql
STABLE
SET search_path = infra, public
AS $$
    SELECT s.sensor_id,
           s.asset_id,
           s.sensor_type,
           s.placement,
           s.unit,
           round(ST_Distance(s.geom::geography, a.geom::geography)::numeric, 1)::double precision
    FROM infra.infrastructure_assets a
    JOIN infra.sensors s
      ON ST_DWithin(s.geom::geography, a.geom::geography, GREATEST(p_buffer_m, 0))
    WHERE a.asset_id = p_asset_id
    ORDER BY ST_Distance(s.geom::geography, a.geom::geography), s.sensor_id;
$$;

INSERT INTO infra.schema_migrations (version) VALUES ('002_views_functions') ON CONFLICT (version) DO NOTHING;
