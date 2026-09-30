-- PostGIS schema for the Dodge City Urban Infrastructure Monitoring prototype.
-- Load with:  python -m pipeline.load_postgis "postgresql://user:pass@host/db"
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS infra;

DROP TABLE IF EXISTS infra.readings, infra.incidents, infra.injected_faults,
                     infra.hotspots, infra.sensors, infra.assets CASCADE;

CREATE TABLE infra.assets (
  asset_id     text PRIMARY KEY,
  asset_type   text NOT NULL,          -- building | road | water_main | hydrant | streetlight | rail
  name         text,
  condition    real,                   -- 0-100 (PCI for roads)
  criticality  smallint,               -- 1-5
  risk         real,                   -- 0-100 composite GeoAI risk
  risk_class   text,
  synthetic    boolean DEFAULT false,  -- true = derived/simulated, not authoritative
  props        jsonb,
  geom         geometry(Geometry, 4326) NOT NULL
);
CREATE INDEX assets_geom_gix ON infra.assets USING gist (geom);
CREATE INDEX assets_type_idx ON infra.assets (asset_type);

CREATE TABLE infra.sensors (
  sensor_id    text PRIMARY KEY,
  sensor_type  text NOT NULL,
  unit         text,
  asset_id     text REFERENCES infra.assets(asset_id),
  install_date date,
  battery_pct  smallint,
  geom         geometry(Point, 4326) NOT NULL
);
CREATE INDEX sensors_geom_gix ON infra.sensors USING gist (geom);

CREATE TABLE infra.readings (
  sensor_id     text REFERENCES infra.sensors(sensor_id),
  ts            timestamptz NOT NULL,
  value         real,
  expected      real,
  robust_z      real,
  iforest_score real,
  PRIMARY KEY (sensor_id, ts)
);
CREATE INDEX readings_ts_idx ON infra.readings (ts);

CREATE TABLE infra.incidents (
  incident_id text PRIMARY KEY, sensor_id text, asset_id text, sensor_type text,
  start_ts timestamptz, end_ts timestamptz, hours int, diagnosis text, description text,
  scope text, severity real, priority int, peak_value real, expected real, peak_z real,
  impacted_buildings int, geom geometry(Point, 4326)
);
CREATE INDEX incidents_geom_gix ON infra.incidents USING gist (geom);

CREATE TABLE infra.injected_faults (sensor_id text, asset_id text, kind text,
                                    start_ts timestamptz, end_ts timestamptz, severity real);

CREATE TABLE infra.hotspots (i int, j int, mean_risk real, n_assets int, gi_z real, class text,
                             geom geometry(Polygon, 4326));

-- Handy analytical views --------------------------------------------------------
-- Buildings within 150 m of an open local water incident
CREATE OR REPLACE VIEW infra.v_buildings_at_risk AS
SELECT DISTINCT b.asset_id, b.name, b.props->>'building_type' AS building_type, i.incident_id, i.diagnosis
FROM infra.incidents i
JOIN infra.assets b ON b.asset_type = 'building'
 AND ST_DWithin(b.geom::geography, i.geom::geography, 150)
WHERE i.sensor_type = 'water_pressure' AND i.scope = 'local';

-- Water mains ranked for capital replacement
CREATE OR REPLACE VIEW infra.v_main_replacement_priority AS
SELECT asset_id, name, props->>'material' AS material, (props->>'install_year')::int AS install_year,
       (props->>'break_history')::int AS breaks, condition, risk,
       ST_Length(geom::geography) AS length_m
FROM infra.assets WHERE asset_type = 'water_main'
ORDER BY risk DESC;
