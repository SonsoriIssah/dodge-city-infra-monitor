-- 001_schema.sql
-- Dodge City urban infrastructure monitoring prototype: tables, constraints and indexes.
--
-- Conventions
--   * schema `infra`; every statement is schema-qualified (nothing depends on the role name or search_path);
--   * geometries are stored in EPSG:4326; metric work uses the study area's UTM zone (study_areas.utm_srid)
--     or the geography type; timestamps are timestamptz (UTC);
--   * relationship chain:  infrastructure_assets -> sensors -> sensor_readings -> anomalies
--                          infrastructure_assets -> asset_health
--   * the database holds exactly ONE study area; deleting it cascades to everything that belongs to it.
--
-- Applied by pipeline/db/migrate.py (recorded in infra.schema_migrations). The file is also safe to run by
-- hand:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/migrations/001_schema.sql

-- PostGIS is pinned to schema public: connections use search_path = infra, public, and without the explicit
-- schema a fresh database would get the extension (spatial_ref_sys and every ST_ function) inside infra,
-- where DROP SCHEMA infra CASCADE would remove it.
CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;
CREATE SCHEMA IF NOT EXISTS infra;

CREATE TABLE IF NOT EXISTS infra.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------------------
-- Provenance
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.data_sources (
    source_id        text PRIMARY KEY,
    name             text NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('real', 'simulated', 'derived')),
    provider         text,
    url              text,
    license          text,
    attribution_text text,
    vintage          text,
    retrieved_at     timestamptz,
    notes            text
);
COMMENT ON TABLE infra.data_sources IS
    'Where every layer comes from. kind = real (downloaded public data), simulated (sensor network), derived (analysis outputs).';

-- ---------------------------------------------------------------------------------------------------------
-- Study area and reference layers (real data)
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.study_areas (
    study_area_id serial PRIMARY KEY,
    slug          text NOT NULL UNIQUE,
    name          text NOT NULL,
    description   text,
    utm_srid      integer NOT NULL,
    timezone      text NOT NULL,
    geom          geometry(Polygon, 4326) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
-- Exactly one study area at a time (changing the bounding box rebuilds the database).
CREATE UNIQUE INDEX IF NOT EXISTS study_areas_single_row_uidx ON infra.study_areas ((true));
CREATE INDEX IF NOT EXISTS study_areas_geom_gix ON infra.study_areas USING gist (geom);

CREATE TABLE IF NOT EXISTS infra.reference_boundaries (
    boundary_id   serial PRIMARY KEY,
    study_area_id integer NOT NULL REFERENCES infra.study_areas (study_area_id) ON DELETE CASCADE,
    kind          text NOT NULL,
    name          text,
    source_id     text REFERENCES infra.data_sources (source_id),
    geom          geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS reference_boundaries_geom_gix ON infra.reference_boundaries USING gist (geom);
CREATE INDEX IF NOT EXISTS reference_boundaries_study_area_idx ON infra.reference_boundaries (study_area_id);

CREATE TABLE IF NOT EXISTS infra.buildings (
    building_id   serial PRIMARY KEY,
    study_area_id integer NOT NULL REFERENCES infra.study_areas (study_area_id) ON DELETE CASCADE,
    osm_id        bigint NOT NULL UNIQUE,
    name          text,
    building_type text,
    levels        real,
    height_m      real NOT NULL CHECK (height_m > 0),
    height_source text NOT NULL CHECK (height_source IN ('osm_height', 'lidar_3dep', 'osm_levels', 'estimated')),
    footprint_m2  real,
    tags          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_id     text REFERENCES infra.data_sources (source_id),
    geom          geometry(Polygon, 4326) NOT NULL CHECK (ST_IsValid(geom))
);
COMMENT ON COLUMN infra.buildings.height_source IS
    'osm_height = OSM height tag; lidar_3dep = measured from USGS 3DEP lidar; osm_levels = building:levels x 3.6 m; estimated = deterministic rule (not a measurement).';
CREATE INDEX IF NOT EXISTS buildings_geom_gix ON infra.buildings USING gist (geom);
CREATE INDEX IF NOT EXISTS buildings_study_area_idx ON infra.buildings (study_area_id);

CREATE TABLE IF NOT EXISTS infra.roads (
    road_id       serial PRIMARY KEY,
    study_area_id integer NOT NULL REFERENCES infra.study_areas (study_area_id) ON DELETE CASCADE,
    osm_id        bigint NOT NULL UNIQUE,
    name          text,
    highway_class text NOT NULL,
    surface       text,
    lanes         smallint,
    maxspeed      text,
    oneway        boolean,
    is_bridge     boolean NOT NULL DEFAULT false,
    length_m      real,
    tags          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_id     text REFERENCES infra.data_sources (source_id),
    geom          geometry(LineString, 4326) NOT NULL
);
COMMENT ON TABLE infra.roads IS
    'Base-map road layer: every OSM highway way clipped to the study area. Only the main classes are also infrastructure assets.';
CREATE INDEX IF NOT EXISTS roads_geom_gix ON infra.roads USING gist (geom);
CREATE INDEX IF NOT EXISTS roads_study_area_idx ON infra.roads (study_area_id);

-- ---------------------------------------------------------------------------------------------------------
-- Asset registry (real assets from OSM / NBI; simulated water mains are flagged is_simulated)
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.infrastructure_assets (
    asset_id      text PRIMARY KEY,
    study_area_id integer NOT NULL REFERENCES infra.study_areas (study_area_id) ON DELETE CASCADE,
    asset_type    text NOT NULL CHECK (asset_type IN
                      ('building', 'road', 'bridge', 'rail', 'power', 'street_light', 'water_main')),
    category      text NOT NULL,
    name          text,
    building_id   integer UNIQUE REFERENCES infra.buildings (building_id) ON DELETE CASCADE,
    road_id       integer REFERENCES infra.roads (road_id) ON DELETE CASCADE,
    is_simulated  boolean NOT NULL DEFAULT false,
    source_id     text NOT NULL REFERENCES infra.data_sources (source_id),
    properties    jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom          geometry(Geometry, 4326) NOT NULL,
    centroid      geometry(Point, 4326) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN infra.infrastructure_assets.properties IS
    'Source-backed attributes only: OSM tags of interest, measures computed from the geometry, building height + height_source, parsed NBI fields. Never invented values.';
COMMENT ON COLUMN infra.infrastructure_assets.centroid IS
    'Representative point: polygon centroid, the mid-point along a line, or the point itself.';
CREATE INDEX IF NOT EXISTS infrastructure_assets_geom_gix ON infra.infrastructure_assets USING gist (geom);
CREATE INDEX IF NOT EXISTS infrastructure_assets_centroid_gix ON infra.infrastructure_assets USING gist (centroid);
-- Geography expression index: a geometry index is not used by ::geography casts, and geometry KNN (<->)
-- orders by degrees, which returns the wrong nearest neighbour at this latitude.
CREATE INDEX IF NOT EXISTS infrastructure_assets_geog_gix
    ON infra.infrastructure_assets USING gist ((geom::geography));
CREATE INDEX IF NOT EXISTS infrastructure_assets_type_idx ON infra.infrastructure_assets (asset_type);
CREATE INDEX IF NOT EXISTS infrastructure_assets_road_idx ON infra.infrastructure_assets (road_id);
CREATE INDEX IF NOT EXISTS infrastructure_assets_study_area_idx ON infra.infrastructure_assets (study_area_id);

-- ---------------------------------------------------------------------------------------------------------
-- Sensors and readings ("Simulated Sensor Data" unless a real source is plugged in)
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.sensor_thresholds (
    sensor_type text NOT NULL CHECK (sensor_type IN ('temperature', 'vibration', 'moisture', 'pressure')),
    placement   text NOT NULL,
    unit        text NOT NULL,
    warn_low    double precision,
    warn_high   double precision,
    crit_low    double precision,
    crit_high   double precision,
    description text,
    PRIMARY KEY (sensor_type, placement)
);
COMMENT ON TABLE infra.sensor_thresholds IS
    'Configurable warning / critical limits per (sensor_type, placement). NULL = no limit on that side.';

CREATE TABLE IF NOT EXISTS infra.sensors (
    sensor_id           text PRIMARY KEY,
    asset_id            text NOT NULL REFERENCES infra.infrastructure_assets (asset_id) ON DELETE CASCADE,
    sensor_type         text NOT NULL,
    placement           text NOT NULL,
    unit                text NOT NULL,
    description         text,
    is_simulated        boolean NOT NULL DEFAULT true,
    source              text NOT NULL DEFAULT 'simulator',
    installed_at        date,
    sampling_interval_s integer NOT NULL DEFAULT 3600 CHECK (sampling_interval_s > 0),
    geom                geometry(Point, 4326) NOT NULL,
    FOREIGN KEY (sensor_type, placement) REFERENCES infra.sensor_thresholds (sensor_type, placement)
);
CREATE INDEX IF NOT EXISTS sensors_geom_gix ON infra.sensors USING gist (geom);
CREATE INDEX IF NOT EXISTS sensors_geog_gix ON infra.sensors USING gist ((geom::geography));
CREATE INDEX IF NOT EXISTS sensors_asset_idx ON infra.sensors (asset_id);
CREATE INDEX IF NOT EXISTS sensors_type_idx ON infra.sensors (sensor_type);

CREATE TABLE IF NOT EXISTS infra.sensor_readings (
    sensor_id   text NOT NULL REFERENCES infra.sensors (sensor_id) ON DELETE CASCADE,
    ts          timestamptz NOT NULL,
    value       double precision NOT NULL,
    unit        text NOT NULL,
    status      text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'suspect')),
    source      text NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sensor_id, ts)
);
COMMENT ON TABLE infra.sensor_readings IS
    'One row per sensor and timestamp. A missing hour is an absent row (the sensor did not report).';
CREATE INDEX IF NOT EXISTS sensor_readings_ts_idx ON infra.sensor_readings (ts);

-- ---------------------------------------------------------------------------------------------------------
-- Detection ("Prototype Anomaly Detection")
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.detection_runs (
    run_id       serial PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    window_start timestamptz,
    window_end   timestamptz,
    params       jsonb,
    metrics      jsonb,
    n_readings   integer,
    n_anomalies  integer
);

CREATE TABLE IF NOT EXISTS infra.reading_scores (
    sensor_id     text NOT NULL,
    ts            timestamptz NOT NULL,
    run_id        integer NOT NULL REFERENCES infra.detection_runs (run_id) ON DELETE CASCADE,
    expected      double precision,
    expected_low  double precision,
    expected_high double precision,
    robust_z      real,
    iforest_score real,
    flagged       boolean NOT NULL DEFAULT false,
    PRIMARY KEY (sensor_id, ts),
    FOREIGN KEY (sensor_id, ts) REFERENCES infra.sensor_readings (sensor_id, ts) ON DELETE CASCADE
);
COMMENT ON TABLE infra.reading_scores IS
    'Per-reading detector output. flagged = unusual hour; only persistent or strong flags become anomalies.';

CREATE TABLE IF NOT EXISTS infra.anomaly_clusters (
    cluster_id       integer PRIMARY KEY,
    run_id           integer NOT NULL REFERENCES infra.detection_runs (run_id) ON DELETE CASCADE,
    method           text,
    params           jsonb,
    n_anomalies      integer,
    n_sensors        integer,
    n_assets         integer,
    sensor_types     text[],
    max_severity     text CHECK (max_severity IN ('low', 'medium', 'high', 'critical')),
    first_started_at timestamptz,
    last_ended_at    timestamptz,
    centroid         geometry(Point, 4326),
    geom             geometry(Polygon, 4326)
);
CREATE INDEX IF NOT EXISTS anomaly_clusters_geom_gix ON infra.anomaly_clusters USING gist (geom);
CREATE INDEX IF NOT EXISTS anomaly_clusters_centroid_gix ON infra.anomaly_clusters USING gist (centroid);

CREATE TABLE IF NOT EXISTS infra.anomalies (
    anomaly_id       text PRIMARY KEY,
    run_id           integer NOT NULL REFERENCES infra.detection_runs (run_id) ON DELETE CASCADE,
    sensor_id        text NOT NULL REFERENCES infra.sensors (sensor_id) ON DELETE CASCADE,
    asset_id         text NOT NULL REFERENCES infra.infrastructure_assets (asset_id) ON DELETE CASCADE,
    sensor_type      text NOT NULL,
    anomaly_type     text NOT NULL,
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz NOT NULL,
    peak_at          timestamptz NOT NULL,
    duration_hours   integer NOT NULL,
    observed_value   double precision NOT NULL,
    expected_value   double precision NOT NULL,
    unit             text NOT NULL,
    robust_z         real NOT NULL,
    anomaly_score    real NOT NULL CHECK (anomaly_score >= 0 AND anomaly_score <= 1),
    score_components jsonb NOT NULL,
    severity         text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    detection_method text NOT NULL,
    explanation      text NOT NULL,
    status           text NOT NULL CHECK (status IN ('active', 'resolved')),
    cluster_id       integer REFERENCES infra.anomaly_clusters (cluster_id) ON DELETE SET NULL,
    geom             geometry(Point, 4326) NOT NULL,
    -- reading -> anomaly: the peak of every anomaly is an existing reading of its sensor
    FOREIGN KEY (sensor_id, peak_at) REFERENCES infra.sensor_readings (sensor_id, ts) ON DELETE CASCADE,
    CHECK (started_at <= peak_at AND peak_at <= ended_at)
);
COMMENT ON COLUMN infra.anomalies.status IS
    'active / resolved as of the end of the analysed window (detection_runs.window_end).';
CREATE INDEX IF NOT EXISTS anomalies_geom_gix ON infra.anomalies USING gist (geom);
CREATE INDEX IF NOT EXISTS anomalies_geog_gix ON infra.anomalies USING gist ((geom::geography));
CREATE INDEX IF NOT EXISTS anomalies_interval_idx ON infra.anomalies (started_at, ended_at);
CREATE INDEX IF NOT EXISTS anomalies_asset_idx ON infra.anomalies (asset_id);
CREATE INDEX IF NOT EXISTS anomalies_sensor_idx ON infra.anomalies (sensor_id);
CREATE INDEX IF NOT EXISTS anomalies_severity_idx ON infra.anomalies (severity);
CREATE INDEX IF NOT EXISTS anomalies_cluster_idx ON infra.anomalies (cluster_id);

-- ---------------------------------------------------------------------------------------------------------
-- Spatial analysis outputs ("Derived Asset Health Score", risk zones)
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.asset_health (
    asset_id            text NOT NULL REFERENCES infra.infrastructure_assets (asset_id) ON DELETE CASCADE,
    as_of               timestamptz NOT NULL,
    run_id              integer NOT NULL REFERENCES infra.detection_runs (run_id) ON DELETE CASCADE,
    health_score        smallint NOT NULL CHECK (health_score BETWEEN 0 AND 100),
    status              text NOT NULL CHECK (status IN ('normal', 'watch', 'at_risk', 'critical')),
    frequency_penalty   real NOT NULL,
    severity_penalty    real NOT NULL,
    reading_penalty     real NOT NULL,
    sensor_penalty      real NOT NULL,
    anomalies_in_window integer,
    active_anomalies    integer,
    sensors_reporting   integer,
    sensors_total       integer,
    PRIMARY KEY (asset_id, as_of)
);
COMMENT ON TABLE infra.asset_health IS
    'Derived Asset Health Score per monitored asset and hour. Computed from simulated sensors; not an assessment of real condition.';
CREATE INDEX IF NOT EXISTS asset_health_as_of_idx ON infra.asset_health (as_of);

CREATE TABLE IF NOT EXISTS infra.risk_zones (
    cell_id       text PRIMARY KEY,
    study_area_id integer NOT NULL REFERENCES infra.study_areas (study_area_id) ON DELETE CASCADE,
    geom          geometry(Polygon, 4326) NOT NULL,
    centroid      geometry(Point, 4326)
);
CREATE INDEX IF NOT EXISTS risk_zones_geom_gix ON infra.risk_zones USING gist (geom);
CREATE INDEX IF NOT EXISTS risk_zones_centroid_gix ON infra.risk_zones USING gist (centroid);

CREATE TABLE IF NOT EXISTS infra.risk_zone_scores (
    cell_id       text NOT NULL REFERENCES infra.risk_zones (cell_id) ON DELETE CASCADE,
    as_of         timestamptz NOT NULL,
    run_id        integer NOT NULL REFERENCES infra.detection_runs (run_id) ON DELETE CASCADE,
    risk_score    real NOT NULL CHECK (risk_score >= 0 AND risk_score <= 100),
    risk_level    text NOT NULL CHECK (risk_level IN ('low', 'moderate', 'high', 'very_high')),
    anomaly_count integer NOT NULL,
    PRIMARY KEY (cell_id, as_of)
);
COMMENT ON TABLE infra.risk_zone_scores IS
    'Sparse: only (cell, hour) rows with risk_score >= 0.5 are stored.';
CREATE INDEX IF NOT EXISTS risk_zone_scores_as_of_idx ON infra.risk_zone_scores (as_of);

-- ---------------------------------------------------------------------------------------------------------
-- Simulator ground truth (used only to evaluate the detector; never read by detection itself)
-- ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra.simulation_events (
    event_id    serial PRIMARY KEY,
    sensor_id   text REFERENCES infra.sensors (sensor_id) ON DELETE CASCADE,
    asset_id    text REFERENCES infra.infrastructure_assets (asset_id) ON DELETE CASCADE,
    sensor_type text,
    event_type  text NOT NULL,
    is_anomaly  boolean NOT NULL,
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz NOT NULL,
    magnitude   real,
    description text,
    CHECK (started_at <= ended_at)
);
COMMENT ON TABLE infra.simulation_events IS
    'Injected abnormal events (is_anomaly, with sensor and asset) and benign regional events such as rain (sensor_id NULL).';
CREATE INDEX IF NOT EXISTS simulation_events_sensor_idx ON infra.simulation_events (sensor_id);
CREATE INDEX IF NOT EXISTS simulation_events_asset_idx ON infra.simulation_events (asset_id);

INSERT INTO infra.schema_migrations (version) VALUES ('001_schema') ON CONFLICT (version) DO NOTHING;
