-- 03  Assets within a selected radius
--
-- Which assets lie within 150 m of a point?
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/03_assets_within_radius.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area: the query uses the centre of
-- the study area as its example point. Distances are metres on the spheroid (geography).

WITH centre AS (
    SELECT ST_Centroid(sa.geom) AS pt
    FROM infra.study_areas sa
    LIMIT 1
)
SELECT a.asset_id,
       a.asset_type,
       a.name,
       a.is_simulated,
       round(ST_Distance(a.geom::geography, c.pt::geography)::numeric, 1) AS distance_m
FROM centre c
JOIN infra.infrastructure_assets a ON ST_DWithin(a.geom::geography, c.pt::geography, 150)
ORDER BY distance_m, a.asset_id;

-- The same question through the SQL function used by the API
-- (GET /spatial/assets-within?lon=...&lat=...&radius_m=150), summarised per asset type:
SELECT f.asset_type,
       count(*)          AS assets,
       min(f.distance_m) AS nearest_m,
       max(f.distance_m) AS farthest_m
FROM (
    SELECT ST_X(ST_Centroid(sa.geom)) AS lon, ST_Y(ST_Centroid(sa.geom)) AS lat
    FROM infra.study_areas sa
    LIMIT 1
) c
CROSS JOIN LATERAL infra.assets_within_radius(c.lon, c.lat, 150) AS f
GROUP BY f.asset_type
ORDER BY assets DESC, f.asset_type;
