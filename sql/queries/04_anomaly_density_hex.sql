-- 04  Anomaly density per hexagonal cell
--
-- How many anomalies fall in each cell of a hexagonal grid over the study area, and how severe are they?
-- Grid: ST_HexagonGrid with a 150 m edge (cells about 300 m across, 5.8 ha) in the study area's UTM zone;
-- cell id = 'i_j' as in infra.risk_zones. Severity weights: low 1, medium 2, high 4, critical 7.
--
-- Run:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/queries/04_anomaly_density_hex.sql
-- Everything is schema-qualified and nothing is hard-coded to one study area.
-- Every cell is returned; all counts are 0 until detection has run (scripts/detect_anomalies.py).

WITH area AS (
    SELECT sa.utm_srid, ST_Transform(sa.geom, sa.utm_srid) AS geom_utm
    FROM infra.study_areas sa
    LIMIT 1
),
grid AS (
    SELECT h.i, h.j, h.geom
    FROM area
    CROSS JOIN LATERAL ST_HexagonGrid(150, area.geom_utm) AS h
    WHERE ST_Intersects(h.geom, area.geom_utm)
)
SELECT g.i || '_' || g.j                                      AS cell_id,
       count(an.anomaly_id)                                   AS anomaly_count,
       COALESCE(sum(CASE an.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2
                                     WHEN 'high' THEN 4 WHEN 'critical' THEN 7 END), 0) AS weighted_severity,
       round((count(an.anomaly_id) / (ST_Area(g.geom) / 10000.0))::numeric, 3) AS anomalies_per_hectare,
       ST_AsText(ST_Transform(ST_Centroid(g.geom), 4326), 6)  AS cell_centre
FROM grid g
CROSS JOIN area
LEFT JOIN infra.anomalies an ON ST_Intersects(g.geom, ST_Transform(an.geom, area.utm_srid))
GROUP BY g.i, g.j, g.geom
ORDER BY anomaly_count DESC, weighted_severity DESC, g.i, g.j;
