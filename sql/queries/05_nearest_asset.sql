-- 05  Nearest infrastructure asset
--
-- Which asset is nearest to a point? Uses geography KNN (<->) on the expression index
-- infrastructure_assets_geog_gix. Ordering by geometry <-> would rank candidates by degrees, which picks
-- the wrong neighbour at this latitude (a degree of longitude is about 88 km here, a degree of latitude
-- about 111 km).
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/05_nearest_asset.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area: the query uses the centre of
-- the study area as its example point. Distances are metres on the spheroid (geography).

-- Nearest asset of any type, and the nearest of each asset type, through the SQL function used by the API
-- (GET /spatial/nearest-asset?lon=...&lat=...&asset_type=...).
WITH centre AS (
    SELECT ST_Centroid(sa.geom) AS pt
    FROM infra.study_areas sa
    LIMIT 1
),
types AS (
    SELECT NULL::text AS asset_type
    UNION ALL
    SELECT DISTINCT a.asset_type FROM infra.infrastructure_assets a
)
SELECT COALESCE(t.asset_type, 'any type') AS searched_type,
       n.asset_id,
       n.asset_type,
       n.name,
       n.distance_m
FROM centre c
CROSS JOIN types t
CROSS JOIN LATERAL infra.nearest_asset(ST_X(c.pt), ST_Y(c.pt), t.asset_type) AS n
ORDER BY n.distance_m, n.asset_id;

-- The plain SQL form of the same search (five nearest):
SELECT a.asset_id,
       a.asset_type,
       a.name,
       round(ST_Distance(a.geom::geography, c.pt::geography)::numeric, 1) AS distance_m
FROM infra.infrastructure_assets a
CROSS JOIN (SELECT ST_Centroid(sa.geom) AS pt FROM infra.study_areas sa LIMIT 1) c
ORDER BY a.geom::geography <-> c.pt::geography
LIMIT 5;
