# BUILD CONTRACT v2 — Dodge City 3D Urban Infrastructure Monitoring & GeoAI Dashboard

Single source of truth for every implementer. Requirements: `BRIEF.md` (R1–R23) in this folder. This version
incorporates an independent design review (`review/*.md` — read the file for your area if you want the evidence).
Where this contract is silent, choose the simplest technically sound option consistent with it and say what you chose
in your report. Do not silently deviate from a field name, path, formula or constant given here: other people are
building against the same text. If something here is provably wrong, fix the code the right way AND report the
deviation explicitly.

Repo root (`ROOT`): `<repo root>` — git branch
`feature/full-stack-prototype`. **Never commit, push, or create branches.** Never print or commit the DB password.

---------------------------------------------------------------------------------------------------------------------
## 0. Environment (verified)
- Windows 11; Git Bash and PowerShell. `python`/`py` on PATH are broken — always use `.venv/Scripts/python.exe`
  (Python 3.12). Installed: fastapi 0.142, starlette 1.7, uvicorn, psycopg 3.3 (binary + pool), pydantic 2.13,
  pydantic-settings, numpy 2.5, scikit-learn 1.9, httpx, pytest 9, pytest-cov, ruff. `uv` is available
  (`uv pip install --python .venv/Scripts/python.exe …`). Do NOT add pandas, shapely, GDAL, geopandas to the app env.
- PostGIS 3.4.3 / PostgreSQL 16.4 in Docker: `docker compose up -d db` (already running). Connection values are in
  `ROOT/.env` (git-ignored): host `localhost`, port **5433** (5432 belongs to an unrelated local Postgres). The DB role
  is a superuser (can CREATE DATABASE). `ST_HexagonGrid`, `ST_ClusterDBSCAN` available.
- Node 24 for frontend unit tests (`node --test`), no npm dependencies, no bundler.
- Network works (Overpass, ArcGIS, TIGERweb, unpkg answered). Docker daemon start is slow; `docker info` may block —
  wrap docker CLI calls in `timeout`.
- All Python file writes: `encoding="utf-8", newline="\n"`. Log messages ASCII-only (cp1252 consoles).
- **All database access is synchronous psycopg 3** (async psycopg fails on the Windows Proactor loop). No async DB code.

---------------------------------------------------------------------------------------------------------------------
## 1. Existing repo → what happens to it
| Existing | Decision |
|---|---|
| `pipeline/fetch_osm.py` (Overpass, mirrors, cache) | **Reuse** → `pipeline/gis/osm.py`; query extended; polite retry (≥30 s on 429/504) |
| `pipeline/geo.py` | **Reuse as is** + `clip_line_to_bbox`, `point_in_polygon` helpers |
| `pipeline/build_assets.py` | **Refactor** → `pipeline/gis/process.py` + `pipeline/sensors/placement.py`. Keep `_spread_sample` (farthest-point sampling) and water-main offsetting. **Remove every fabricated attribute on real features** (random year_built, condition, PCI, AADT, resurfacing year, criticality, materials, break history, flow). Drop the 801 derived streetlights, 509 derived hydrants and the synthetic street-grid fallback |
| `pipeline/simulate_iot.py` | **Rework** → `pipeline/sensors/simulator.py` (same structure: baseline + injected events + ground truth + dropouts; the four required sensor types) |
| `pipeline/geoai.py` | **Reuse the method** (seasonal robust z, Isolation Forest corroboration, flagged-hour merging, evaluation against ground truth) → `pipeline/detection/*`. Isolation Forest → scikit-learn. Root-cause "diagnosis" and "recommended actions" removed (causal claims). Getis-Ord Gi* **retired**: on ~88 mostly-empty cells it flags 5–19 cells on spatially random anomalies (review evidence) — not a defensible significance test |
| `pipeline/database.py` (SQLite R*Tree), `pipeline/load_postgis.py`, `sql/postgis_schema.sql`, `pipeline/export_dashboard.py`, `dashboard/data/data.js`, `data/metrics.json` | **Retire/delete** — PostGIS is the system of record; schema name `infra` kept; export replaced by a snapshot of real API responses |
| `dashboard/` (MapLibre GL JS, vanilla JS, no build step, dark theme) | **Keep stack + visual language**; restructure into ES modules. **Kept features:** 3D building extrusions, 2D/3D toggle, Home/reset view, imagery toggle, keyboard shortcuts (Space, ←/→), timeline slider + histogram, observed-vs-expected chart with shaded anomaly windows, fly-to on list selection, layer toggles, legend. **Dropped with reason:** building colour by condition/age and road PCI colouring (fabricated attributes), recommended actions and root-cause labels (causal claims), pulsing markers (decorative animation), 3D sensor columns (≈4 px wide at city zoom; replaced by status-shaped circles), Gi* layer (above), CARTO basemap and Esri imagery (licence — §12.6), Google Fonts + unpkg CDN (offline demo reliability) |
| `.github/workflows/pages.yml` | **Keep** (GitHub Pages serves `dashboard/` with the committed static snapshot) |
| `run_pipeline.py` | **Keep** as the one-command orchestrator calling the stage mains |

---------------------------------------------------------------------------------------------------------------------
## 2. Data sources and honesty rules
REAL data — downloaded once, cached in `data/raw/`, **committed** (pipeline reproducible offline; tests/CI/Docker
never hit the network):
| source_id | What | Endpoint | Licence / attribution |
|---|---|---|---|
| `osm` | building footprints (closed ways), highways, rail, `bridge=yes` ways, power, street lamps, names/amenity tags | Overpass API (`out geom`), descriptive User-Agent, one combined query, mirrors as fallback | ODbL 1.0. UI: "© OpenStreetMap contributors" linking to https://www.openstreetmap.org/copyright. Docs/SOURCES: "Contains OpenStreetMap data © OpenStreetMap contributors, available under the Open Database License (https://opendatacommons.org/licenses/odbl/1-0/)" |
| `nbi` | highway bridge/culvert records with real attributes | `https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_National_Bridge_Inventory/FeatureServer/0/query` (envelope = bbox, `inSR=4326&outSR=4326&outFields=*&f=json`) — 4 records in the default bbox | US Government work, unrestricted public use. Credit: "FHWA National Bridge Inventory (data as of <service description date>), distributed by USDOT/BTS NTAD" |
| `tiger` | Dodge City incorporated-place boundary (context outline, 8-part MultiPolygon, much larger than the study area) | `https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4/query?where=GEOID%3D%272018250%27&outFields=*&outSR=4326&f=geojson` (assert layer name = "Incorporated Places"; record its description as vintage) | US Government work. Credit: "U.S. Census Bureau, TIGERweb". Note: statistical boundary, not a legal land description |
| `usgs_3dep` | measured building heights (optional file `data/raw/building_heights_3dep.csv`, produced offline by `tools/derive_building_heights.py`, §4.3) | USGS 3DEP lidar height-above-ground 2 m raster (KS_Area1_2014, collected Dec 2013 – Jan 2014) via Microsoft Planetary Computer | US public domain. Credit: "U.S. Geological Survey, 3D Elevation Program" |
Display-only third-party services (registered in `data_sources` with kind `real`, `notes='display only'`):
`basemap` (OpenFreeMap dark style, "OpenFreeMap © OpenMapTiles Data from OpenStreetMap") and `imagery`
(USGS The National Map orthoimagery, "USDA, USGS The National Map: Orthoimagery").

`data/raw/SOURCES.json`: per file `{source_id, file, url, retrieved_at (UTC), sha256, feature_count, license,
attribution_text, vintage, terms_url}` (vintage = `osm3s.timestamp_osm_base` for OSM; service description string for
NBI/TIGER). Downloaders: use cache unless `--refresh`; network failure with cache → warn + use cache; without cache →
`osm` fatal, others skipped with a warning (layer absent, documented). NBI downloader checks the expected field names
exist; if not, warn and keep the existing cache.

NBI parsing rules: `STRUCTURE_NUMBER_008` opaque string; `DATE_OF_INSPECT_090` is MMYY with the leading zero dropped
('223' → Feb 2023) — left-pad to 4, show month + year; `YEAR_RECONSTRUCTED_106` 0/null → none; condition code 'N' =
not applicable (culverts have deck/super/sub = N and a `CULVERT_COND_062`); always show `ADT_029` with `YEAR_ADT_030`;
`BRIDGE_CONDITION` G/F/P label = "Good / Fair / Poor (FHWA classification from the lowest component rating)" — never
"structurally deficient", never a safety verdict; `OWNER_022` 01 = State Highway Agency, 04 = City or Municipal
Highway Agency (others: show the code). Coordinate check: `LAT_016` = DDMMSSss north, `LONG_017` = DDDMMSSss west →
decimal; if > 500 m from the point geometry set `location_check='mismatch'`, do **not** create an asset from that
record (record `999905600290641` "US-56 HWY" is like this: its point sits 15 m from the 2nd Avenue bridge) and list
it in the processing log + docs. Do not display the undocumented `STATUS`/`DATE` fields.

SIMULATED / DERIVED — always labelled as such in DB (`is_simulated`, `data_sources.kind`), API and UI:
- all sensors, readings, sensor status, anomalies, clusters, risk zones, asset health scores;
- simulated weather driving the simulator (never presented as observed weather);
- **simulated water mains**: `asset_type='water_main'`, `is_simulated=true`; centre-line offsets (4.5 m) of real
  streets; created **only for the segments that host a pressure sensor** (`SIM_WATER_MAINS=28`); no invented attributes.
  Label: "Simulated water network (not a record of real utilities)". (The City publishes real utility layers without a
  licence; they are deliberately not used — docs note.)
- **building heights** `height_source ∈ {osm_height, lidar_3dep, osm_levels, estimated}` in that precedence:
  `osm_height` = OSM `height` tag; `lidar_3dep` = measured from the 3DEP raster (when the CSV exists and the value is
  usable); `osm_levels` = `building:levels` × 3.6 m; `estimated` = deterministic rule, no RNG (house/residential/
  detached/cabin 5.0 m; garage/shed/roof/pavilion 3.5 m; industrial/warehouse 9.0 m; storage_tank 10.0 m;
  everything else: 4.5 m if footprint < 400 m², 6.0 m if < 1500 m², else 8.0 m).
  UI/doc wording: "3D building extrusions derived from OSM footprints. Heights: measured from USGS 3DEP lidar
  (2013–14) where available, otherwise OSM tags, otherwise estimated. Not detailed 3D building models."
Never invent attributes for real features. NBI ratings never feed the health score, status colours or risk zones.
Banned words in UI strings and API descriptions: "live", "real-time"/"realtime". Mandatory literal labels (R22):
**"Simulated Sensor Data"**, **"Prototype Anomaly Detection"**, **"Derived Asset Health Score"**.

---------------------------------------------------------------------------------------------------------------------
## 3. Study area (configurable)
`STUDY_AREA_SLUG=dodge-city-downtown`, `STUDY_AREA_NAME="Downtown Dodge City, Kansas"`,
`STUDY_AREA_BBOX=37.745,-100.030,37.762,-100.005` (**env var only** is south,west,north,east = Overpass order;
≈ 2.2 km × 1.9 km). Everywhere else (API `bbox=`, `/meta`) bbox order is **west,south,east,north**.
Storage SRID 4326; metric work in UTM 14N `STUDY_AREA_UTM_SRID=32614`; distances via `::geography`.
The database holds **exactly one study area at a time**; changing the bbox + `run_pipeline.py --refresh` rebuilds all.
Clipping (in `process.py`, pure Python): every line feature (roads, rail, power lines, simulated mains) is clipped to
the bbox rectangle (`geo.clip_line_to_bbox`); several parts → keep the longest (log it); parts < 5 m dropped;
`length_m` = clipped length. Buildings kept whole when their centroid is inside the bbox, else dropped; footprint
< 25 m² dropped. Every sensor point must satisfy `ST_Covers(study_area.geom, sensor.geom)` (DB test).
Nothing in code may depend on default-area names; all per-type counts are caps `min(target, pool)`.

---------------------------------------------------------------------------------------------------------------------
## 4. Repository layout, packaging, stages
```
pyproject.toml            packages `pipeline`, `backend`; [tool.pytest.ini_options] pythonpath=["."], markers; ruff config
requirements.txt          runtime (fastapi, uvicorn[standard], psycopg[binary,pool], pydantic, pydantic-settings, numpy,
                          scikit-learn, httpx, tzdata)        requirements-dev.txt  (-r requirements.txt, pytest, pytest-cov, ruff)
pipeline/
  config.py               Settings (pydantic-settings) + get_settings()
  geo.py                  reused helpers
  models.py               dataclasses: SensorSpec, Reading, InjectedEvent, AnomalyRecord
  logging_utils.py        setup_logging()
  gis/osm.py  gis/nbi.py  gis/tiger.py  gis/process.py
  db/connection.py        dsn(settings), connect(settings|dsn), connection_pool(settings)
  db/migrate.py           apply_migrations(conn): sql/migrations/*.sql in order, tracked in infra.schema_migrations
  db/loaders.py           load_gis(conn, processed_dir, settings), copy helpers
  sensors/thresholds.py  placement.py  simulator.py  sources.py  ingestion.py
  detection/baseline.py  detectors.py  events.py  explain.py  evaluate.py  runner.py
  analysis/status.py      THE single implementation of sensor status at time t (§10.1)
  analysis/health.py  clustering.py  risk_zones.py  spatial.py
  stages/download.py process.py seed.py generate.py detect.py analyze.py    each: main(argv=None) -> int
backend/__init__.py
backend/app/__init__.py  main.py (create_app(settings=None))  deps.py  schemas.py  timeutil.py (iso_z)
            queries/*.py (SQL lives here)   routers/*.py
backend/export.py         main(argv=None): static snapshot via in-process TestClient
backend/entrypoint.py     container entrypoint (wait for db → migrate → AUTO_SEED → uvicorn)
backend/Dockerfile
scripts/download_data.py process_data.py seed_database.py generate_sensors.py detect_anomalies.py
        analyze_spatial.py export_static.py        thin wrappers: add repo root to sys.path, call the stage main
tools/derive_building_heights.py + tools/requirements-heights.txt     optional, offline, separate env
run_pipeline.py           runs the stage mains in order in-process; flags --refresh --skip-download --skip-export
sql/migrations/001_schema.sql  002_views_functions.sql      sql/queries/01..07_*.sql (documented, runnable)
dashboard/                index.html, config.js, css/, js/ (ES modules), vendor/maplibre-gl/, data/snapshot/ (committed), tests/
data/raw/ (committed)     data/processed/ (generated, git-ignored)
tests/ (pytest)           docs/
docker-compose.yml  .dockerignore  .gitattributes  .env.example  README.md
```
Setup everywhere (README, Dockerfile, CI): `pip install -r requirements.txt && pip install -e . --no-deps`.
Server: `uvicorn backend.app.main:create_app --factory --host $API_HOST --port $API_PORT`.
`.gitignore`: remove `data/raw/`; add `data/processed/`, `.venv-tools/`, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info/`.
`.gitattributes`: `* text=auto eol=lf`, `data/raw/** -text`, `dashboard/vendor/** -text`.

**Every stage/loader/query function takes an explicit `conn` (and `settings` where needed); none opens a connection
from global state** (tests point them at a separate database). Each stage runs in ONE transaction and is idempotent.

### 4.1 Stage table (inputs → outputs → what it clears)
| # | Script / stage main | Reads | Writes | Clears first |
|---|---|---|---|---|
| 1 | `download_data` | network / cache | `data/raw/{osm.json, nbi_bridges.json, city_boundary.geojson, SOURCES.json}` | — |
| 2 | `process_data` | `data/raw/*` (+ optional `building_heights_3dep.csv`) | `data/processed/{study_area, buildings, roads, assets, city_boundary}.geojson` + `processing_report.json` (no DB) | — |
| 3 | `seed_database` | `data/processed/*`, `SOURCES.json` | runs migrations; `data_sources`, `study_areas`, `reference_boundaries`, `buildings`, `roads`, `infrastructure_assets` (real assets only), `sensor_thresholds` | `DELETE FROM infra.study_areas` (cascades to everything), then upserts |
| 4 | `generate_sensors` | assets from DB | placement → simulated `water_main` assets + `sensors`; `get_sensor_source(settings).read(start,end)` → `IngestionService` → `sensor_readings`; ground truth → `simulation_events` | `TRUNCATE infra.detection_runs RESTART IDENTITY CASCADE; DELETE FROM infra.sensors; DELETE FROM infra.simulation_events; DELETE FROM infra.infrastructure_assets WHERE asset_type='water_main'` |
| 5 | `detect_anomalies` | `sensor_readings`, `sensors`, `sensor_thresholds`, `simulation_events` (evaluation only) | `detection_runs`, `reading_scores`, `anomalies` | `TRUNCATE infra.detection_runs RESTART IDENTITY CASCADE` |
| 6 | `analyze_spatial` | `anomalies`, `reading_scores`, `sensor_readings`, assets | `anomaly_clusters` (+ `anomalies.cluster_id`), `risk_zones`, `risk_zone_scores`, `asset_health` | `DELETE` those four tables (never `TRUNCATE … CASCADE` on anomaly_clusters) |
| 7 | `export_static` | the API in-process | `dashboard/data/snapshot/*` | the snapshot folder |
`run_pipeline.py` = 1→7. `--skip-download` uses the committed cache. Stage 5 fails loudly if stage 4 has not run, etc.

### 4.2 Settings (`pipeline/config.py`, pydantic-settings; all documented in `.env.example`)
Reads `ROOT/.env` by absolute path; real environment variables override it. **Precedence: non-empty `DATABASE_URL`
wins; otherwise the DSN is built with `psycopg.conninfo.make_conninfo(host=POSTGRES_HOST, port=POSTGRES_PORT,
dbname=POSTGRES_DB, user=POSTGRES_USER, password=POSTGRES_PASSWORD)`.** `POSTGRES_PORT` = port the app connects to
(5433 on this host); `POSTGRES_HOST_PORT` is used only by docker-compose port publishing. Connections set
`options='-c timezone=UTC -c search_path=infra,public'`. `TEST_DATABASE_URL` (default: same DSN with database
`infra_test`).
Variables: `DATABASE_URL`, `POSTGRES_HOST=localhost`, `POSTGRES_PORT=5433`, `POSTGRES_HOST_PORT=5433`, `POSTGRES_DB=infra`,
`POSTGRES_USER=infra`, `POSTGRES_PASSWORD`, `TEST_DATABASE_URL`, `STUDY_AREA_SLUG`, `STUDY_AREA_NAME`, `STUDY_AREA_BBOX`,
`STUDY_AREA_UTM_SRID=32614`, `TIMEZONE=America/Chicago`, `SENSOR_SOURCE=simulated`, `SIM_SEED=42`,
`SIM_START=2026-09-01T00:00:00-05:00`, `SIM_DAYS=30`, `SIM_STEP_MINUTES=60`, `SIM_ANOMALY_EVENTS=40`,
`SIM_DROPOUT_RATE=0.12`, `SIM_WATER_MAINS=28`, `DETECT_Z_STRONG=6.0`, `DETECT_Z_MIN=3.0`,
`DETECT_ROLLING_HOURS=6`, `DETECT_IFOREST_THRESHOLD=0.62`, `DETECT_MERGE_GAP_HOURS=2`, `PROXIMITY_RADIUS_M=100`,
`CLUSTER_EPS_M=200`, `CLUSTER_EPS_HOURS=48`, `CLUSTER_MIN_POINTS=3`, `CLUSTER_MIN_SENSORS=3`, `RISK_HEX_EDGE_M=150`,
`RISK_BANDWIDTH_M=250`, `RISK_HALF_LIFE_HOURS=72`, `RISK_REFERENCE=14.0`, `HEALTH_WINDOW_DAYS=7`,
`HEALTH_HALF_LIFE_HOURS=48`, `API_HOST=0.0.0.0`, `API_PORT=8000`, `CORS_ORIGINS=*`, `INGEST_API_KEY=` (empty = ingest
disabled), `SERVE_DASHBOARD=true`, `AUTO_SEED=true`, `BASEMAP_STYLE_URL=https://tiles.openfreemap.org/styles/dark`,
`HTTP_SOURCE_URL=` (for `SENSOR_SOURCE=http`).

### 4.3 Optional measured heights (`tools/derive_building_heights.py`)
Offline tool with its own env (`tools/requirements-heights.txt`: rasterio, numpy; create `.venv-tools` with uv). Reads
`data/raw/osm.json` building ways + the Planetary Computer `3dep-lidar-hag` 2 m COG(s) covering the bbox (anonymous
SAS token), computes per-footprint p90 height of pixels whose centre lies inside the footprint (min 4 pixels), keeps
values in [2.5, 60] m, writes `data/raw/building_heights_3dep.csv` with header
`osm_id,height_m,n_pixels,lidar_project,collected` and `data/raw/building_heights_3dep.meta.json`
(`{source_id:"usgs_3dep", url, retrieved_at, license, attribution_text, vintage, n_buildings}`), which `download_data`
merges into SOURCES.json when present. `process.py` joins by `osm_id` if the CSV exists; absent file → nothing changes.

---------------------------------------------------------------------------------------------------------------------
## 5. Asset registry rules
Each OSM way maps to **exactly one** asset. Deterministic ids (sort by source id), zero-padded.
- `building` `BLD-0001…` — every kept building; `category='Buildings'`. Purely numeric OSM names ("1"…"10") are treated
  as unnamed (name NULL). UI falls back to the asset id for unnamed assets.
- `bridge` `BRG-001…` — `category='Transportation'`. OSM ways with `bridge=yes` and not `area=yes`; contiguous bridge
  ways of the same highway/railway class sharing an end node are **merged into one asset** (geometry = merged
  LineString, `properties.osm_way_ids=[…]`). A bridge way is a bridge asset only — never also a `road`/`rail` asset
  (its `roads` row keeps `is_bridge=true` for the base map). NBI match: nearest merged highway bridge within 60 m of a
  record that passes the coordinate check; each record attaches to at most one asset (`properties.nbi={…parsed fields…}`,
  `properties.structure_kind='bridge'`). Unmatched valid NBI records become Point assets
  (`structure_kind='culvert'` when `CULVERT_COND_062` ≠ 'N', else 'bridge'; `source_id='nbi'`).
  Default area ⇒ 2nd Avenue bridge over the Arkansas River (one structure, NBI 1935/2001), the CVRR rail-spur bridge,
  and two NBI culverts (West Trail St, Wyatt Earp Blvd).
- `road` `RD-0001…` — highway ways of class motorway, trunk, primary, secondary, tertiary, unclassified, residential
  and their `_link`s (not service/footway/pedestrian/track — those stay in `roads` as base map only).
- `rail` `RAIL-001…` (`railway=rail`), `power` `PWR-001…` (substations as Point/Polygon centroid Point, lines as
  LineString), `street_light` `SL-001…` (OSM `highway=street_lamp` nodes); `category`: Transportation / Utilities / Utilities.
- `water_main` `WM-001…` — simulated (created in stage 4), `category='Simulated network'`, `properties.host_road_id`.
`properties` jsonb carries only source-backed attributes: OSM tags of interest (highway_class, surface, lanes,
maxspeed, building_type, levels, amenity, operator, addr:*), `length_m`/`footprint_m2`/`height_m`/`height_source`
(computed/derived and labelled), `nbi{…}` for bridges.

---------------------------------------------------------------------------------------------------------------------
## 6. Database (schema `infra`; SRID 4326; `timestamptz`, UTC). All SQL schema-qualified; functions `SET search_path = infra, public`.
```
schema_migrations(version text PK, applied_at timestamptz)
data_sources(source_id text PK, name, kind CHECK in ('real','simulated','derived'), provider, url, license,
             attribution_text, vintage, retrieved_at timestamptz, notes)
study_areas(study_area_id serial PK, slug UNIQUE NOT NULL, name NOT NULL, description, utm_srid int NOT NULL,
            timezone text NOT NULL, geom geometry(Polygon,4326) NOT NULL, created_at)
reference_boundaries(boundary_id serial PK, study_area_id FK CASCADE, kind text, name, source_id FK,
            geom geometry(MultiPolygon,4326) NOT NULL)
buildings(building_id serial PK, study_area_id FK CASCADE, osm_id bigint UNIQUE NOT NULL, name, building_type, levels real,
          height_m real NOT NULL CHECK (height_m > 0), height_source text NOT NULL CHECK in
          ('osm_height','lidar_3dep','osm_levels','estimated'), footprint_m2 real, tags jsonb, source_id FK,
          geom geometry(Polygon,4326) NOT NULL CHECK (ST_IsValid(geom)))
roads(road_id serial PK, study_area_id FK CASCADE, osm_id bigint UNIQUE NOT NULL, name, highway_class text NOT NULL, surface,
      lanes smallint, maxspeed text, oneway boolean, is_bridge boolean NOT NULL DEFAULT false, length_m real, tags jsonb,
      source_id FK, geom geometry(LineString,4326) NOT NULL)
infrastructure_assets(asset_id text PK, study_area_id FK CASCADE NOT NULL, asset_type text NOT NULL CHECK in
      ('building','road','bridge','rail','power','street_light','water_main'), category text NOT NULL, name text,
      building_id int UNIQUE NULL FK CASCADE, road_id int NULL FK CASCADE, is_simulated boolean NOT NULL DEFAULT false,
      source_id text NOT NULL FK, properties jsonb NOT NULL DEFAULT '{}', geom geometry(Geometry,4326) NOT NULL,
      centroid geometry(Point,4326) NOT NULL, created_at)
sensor_thresholds(sensor_type text, placement text, unit text NOT NULL, warn_low, warn_high, crit_low, crit_high
      double precision NULL, description text, PRIMARY KEY (sensor_type, placement),
      CHECK (sensor_type in ('temperature','vibration','moisture','pressure')))
sensors(sensor_id text PK, asset_id text NOT NULL FK → infrastructure_assets CASCADE, sensor_type text NOT NULL,
      placement text NOT NULL, unit text NOT NULL, description, is_simulated boolean NOT NULL DEFAULT true,
      source text NOT NULL DEFAULT 'simulator', installed_at date, sampling_interval_s int NOT NULL DEFAULT 3600,
      geom geometry(Point,4326) NOT NULL, FOREIGN KEY (sensor_type, placement) REFERENCES sensor_thresholds)
sensor_readings(sensor_id text FK → sensors CASCADE, ts timestamptz, value double precision NOT NULL, unit text NOT NULL,
      status text NOT NULL DEFAULT 'ok' CHECK in ('ok','suspect'), source text NOT NULL, ingested_at timestamptz DEFAULT now(),
      PRIMARY KEY (sensor_id, ts))        -- a missing hour is an absent row
detection_runs(run_id serial PK, started_at, finished_at, window_start, window_end timestamptz, params jsonb, metrics jsonb,
      n_readings int, n_anomalies int)
reading_scores(sensor_id text, ts timestamptz, run_id int NOT NULL FK → detection_runs CASCADE, expected double precision,
      expected_low double precision, expected_high double precision, robust_z real, iforest_score real,
      flagged boolean NOT NULL, PRIMARY KEY (sensor_id, ts),
      FOREIGN KEY (sensor_id, ts) REFERENCES sensor_readings CASCADE)
anomaly_clusters(cluster_id int PK, run_id FK CASCADE, method text, params jsonb, n_anomalies int, n_sensors int, n_assets int,
      sensor_types text[], max_severity text, first_started_at, last_ended_at timestamptz,
      centroid geometry(Point,4326), geom geometry(Polygon,4326))
anomalies(anomaly_id text PK ('ANM-0001'… numbered by (started_at, sensor_id)), run_id int NOT NULL FK CASCADE,
      sensor_id text NOT NULL FK CASCADE, asset_id text NOT NULL FK CASCADE, sensor_type text NOT NULL, anomaly_type text NOT NULL,
      started_at, ended_at, peak_at timestamptz NOT NULL, duration_hours int NOT NULL, observed_value, expected_value
      double precision NOT NULL, unit text NOT NULL, robust_z real NOT NULL, anomaly_score real NOT NULL CHECK (0..1),
      score_components jsonb NOT NULL, severity text NOT NULL CHECK in ('low','medium','high','critical'),
      detection_method text NOT NULL, explanation text NOT NULL, status text NOT NULL CHECK in ('active','resolved'),
      cluster_id int NULL FK → anomaly_clusters ON DELETE SET NULL, geom geometry(Point,4326) NOT NULL,
      FOREIGN KEY (sensor_id, peak_at) REFERENCES sensor_readings (sensor_id, ts) ON DELETE CASCADE,   -- reading → anomaly (R3)
      CHECK (started_at <= peak_at AND peak_at <= ended_at))
asset_health(asset_id text FK CASCADE, as_of timestamptz, run_id int NOT NULL FK CASCADE, health_score smallint NOT NULL
      CHECK (0..100), status text NOT NULL CHECK in ('normal','watch','at_risk','critical'), frequency_penalty,
      severity_penalty, reading_penalty, sensor_penalty real NOT NULL, anomalies_in_window int, active_anomalies int,
      sensors_reporting int, sensors_total int, PRIMARY KEY (asset_id, as_of))
risk_zones(cell_id text PK ('i_j'), study_area_id FK CASCADE, geom geometry(Polygon,4326) NOT NULL, centroid geometry(Point,4326))
risk_zone_scores(cell_id text FK → risk_zones CASCADE, as_of timestamptz, run_id int NOT NULL FK CASCADE,
      risk_score real NOT NULL CHECK (0..100), risk_level text NOT NULL CHECK in ('low','moderate','high','very_high'),
      anomaly_count int NOT NULL, PRIMARY KEY (cell_id, as_of))        -- sparse: only rows with risk_score >= 0.5
simulation_events(event_id serial PK, sensor_id text NULL FK CASCADE, asset_id text NULL FK CASCADE, sensor_type text,
      event_type text NOT NULL, is_anomaly boolean NOT NULL, started_at, ended_at timestamptz NOT NULL, magnitude real,
      description text)                    -- simulator ground truth; regional benign events have sensor_id NULL
```
Indexes: GiST on every `geom` (and `centroid`); **expression GiST `((geom::geography))`** on infrastructure_assets,
sensors, anomalies (geometry-only indexes are not used by geography casts; geometry KNN returns the wrong nearest at
this latitude); btree on `infrastructure_assets(asset_type)`, `sensors(asset_id)`, `sensors(sensor_type)`,
`sensor_readings(ts)`, `anomalies(started_at, ended_at)`, `anomalies(asset_id)`, `anomalies(sensor_id)`,
`anomalies(severity)`, `asset_health(as_of)`, `risk_zone_scores(as_of)`.
Views: `v_sensor_latest`, `v_asset_health_latest`, `v_asset_summary` (asset + sensor_count + sensor_types[] +
anomaly_count + latest health/status; unmonitored → `status='not_monitored'`, `health_score NULL`), `v_active_anomalies`.
Functions: `infra.assets_within_radius(lon, lat, radius_m)`, `infra.nearest_asset(lon, lat, asset_type DEFAULT NULL)`
(`ORDER BY geom::geography <-> pt::geography`), `infra.sensors_in_asset_area(asset_id, buffer_m)`.
`sql/queries/`: 01 sensors within an infrastructure area, 02 infrastructure near an anomaly, 03 assets within a radius,
04 anomaly density per hex cell, 05 nearest infrastructure asset, 06 spatial aggregation (anomalies per road/zone),
07 `ST_ClusterDBSCAN` example (space-only, illustrative). Commented, runnable with psql.

---------------------------------------------------------------------------------------------------------------------
## 7. Sensors and simulation ("Simulated Sensor Data")
Exact unit strings: `°C`, `mm/s`, `%`, `psi`. Sensor id prefixes `TMP-`, `VIB-`, `MST-`, `PRS-` (3 digits).
| sensor_type | placement | hosts (cap) | thresholds warn_low / warn_high / crit_low / crit_high |
|---|---|---|---|
| vibration (hourly RMS velocity, mm/s) | `bridge_deck` | bridges: 2 on structures ≥ 100 m else 1; culverts 1 | – / 5.0 / – / 10.0 |
| | `building_structure` | 22 buildings | – / 1.0 / – / 3.0 |
| | `road_pavement` | 8 roads (highest class first) | – / 2.5 / – / 5.0 |
| moisture (% volumetric water content) | `road_subgrade` | 22 roads | – / 35 / – / 42 |
| | `foundation_perimeter` | 8 buildings | – / 35 / – / 42 |
| | `abutment_backfill` | bridges + culverts (1 each) | – / 35 / – / 42 |
| temperature (°C) | `bridge_deck` | highway/rail bridges (1 each; 2 on ≥ 100 m) | – / 50 / – / 58 |
| | `road_surface` | 12 roads | – / 58 / – / 65 |
| | `building_envelope` | 12 buildings | – / 40 / – / 45 |
| | `equipment` | power substations | – / 65 / – / 75 |
| pressure (psi) | `water_main` | `SIM_WATER_MAINS` simulated mains (1 each) | 40 / 90 / 20 / 110 |
Showcase assets (3 sensor types each), chosen **by rule**: every bridge asset with an NBI match, then up to 3 buildings
ranked by: has a non-numeric name AND (amenity/office/building tag in {courthouse, townhall, government, school,
hospital, public, civic, police, fire_station} OR name matches `/court|government|school|city hall/i`), then footprint
descending; duplicates of one name collapse to the largest footprint; fewer than 3 → fill by footprint. Remaining
hosts by seeded farthest-point sampling (`_spread_sample`). Sensor point on the asset (centroid / clipped-line
midpoint / interpolated), a few metres apart when sharing an asset, always inside the study area.

Simulation: deterministic; per-sensor RNG seeded from `zlib.crc32(sensor_id.encode()) ^ SIM_SEED`; hourly, `SIM_DAYS`
days from `SIM_START` (720 steps, tz-aware UTC timestamps). Weather reaches sensors only through **shared drivers**:
air-temperature anomaly A(t), solar/cloud S(t), wetness state W_p(t) per moisture placement. Each sensor has its own
offset, gains and lag. Constants (tested in review prototype `proto_sim.py`):
- **temperature**: simulated air mean falling 22.5 → 16.5 °C across the window; diurnal half-range 7 °C peaking 16:00
  local; fronts AR(1) sd 3.5 °C; one regional hot spell +7 °C for ~36 h (benign); solar gain up to +12 °C (deck),
  +18 °C (road surface), damped/lagged for building envelope, load heat +15…25 °C for equipment; noise σ 0.3–0.6 °C.
  Abnormal: `temperature_spike` +8…+15 °C half-sine over 3–8 h; `temperature_drift` linear to +5…+10 °C over 48–120 h.
- **vibration**: baselines building 0.05–0.30, road 0.3–0.8, bridge deck 0.8–2.0 mm/s; traffic-like diurnal +
  weekday/weekend pattern in **local time**; multiplicative lognormal noise σ_ln 0.15; benign bursts ×1.3–1.5 for 1 h
  (~6 per sensor-month). Abnormal: `vibration_spike` ×4–8 for 1–2 h; `sustained_high_vibration` ×2.2–3.5 for 6–48 h.
- **moisture**: baseline 14–26 %; noise σ 0.15; 3 regional rain events (9–26 mm) giving +1.5…+7 % with exponential
  dry-down τ: subgrade 60 h, abutment 90 h, foundation 120 h (per-sensor ±10 %) — benign. Abnormal:
  `moisture_increase` +6…+14 % rising over 6 h, held 12–72 h, no rain.
- **pressure**: 55–75 psi operating level, diurnal demand dips (morning/evening, local time), noise σ 0.5, benign
  AR(1) drift sd 0.6. Abnormal: `pressure_drop` −12…−40 psi for 4–24 h; `pressure_spike` +15…+30 psi for 1–3 h;
  `pressure_decline` linear to −8…−15 psi over 48–96 h.
- **Rule:** benign unusual events never exceed 3.5 robust σ of the sensor's own noise in the detector's work domain;
  injected abnormal events are ≥ 8 σ (short) or ≥ 4 σ sustained ≥ 6 h. Injected event hours per sensor ≤ 144 h total.
  A unit test asserts both bounds on the generated ground truth.
- Event mix for `SIM_ANOMALY_EVENTS=40` (scaled proportionally otherwise): vibration_spike 6, sustained_high_vibration 6,
  moisture_increase 7, pressure_drop 5, pressure_spike 4, pressure_decline 3, temperature_spike 5, temperature_drift 4.
  Start times stratified (one per equal slice of the window after hour 72, order shuffled); none in the first 72 h
  (quiet lead-in); ≤ 2 per sensor.
- **Co-located group:** 4 of the 40 are on 4 different sensors on 3–4 neighbouring assets within 150 m of each other,
  starting within the same 36 h, overlapping the final 5 days.
- **Final hour:** exactly 6 events are ongoing at the last timestamp: 1 `pressure_drop` sized to fall below crit_low
  (→ critical), 1 `moisture_increase`, 1 `sustained_high_vibration` on the NBI-matched highway bridge, 2 of the
  co-located group, 1 `temperature_drift`; step-type ones start ≥ 24 h and ramps ≥ 60 h before the end so all are
  detected. The pipeline asserts ≥ 1 critical and ≥ 1 high anomaly active at T_end and fails otherwise.
- Dropouts: `SIM_DROPOUT_RATE` of sensors lose 2–10 consecutive hours (absent rows) + 2 sensors with an outage running
  through the final hour (30 h and 6 h) so the offline state is visible in the default view.
- `simulation_events`: every injected abnormal event (`is_anomaly=true`, with sensor/asset) and every benign regional
  event — rain ×3, hot spell — (`is_anomaly=false`, `sensor_id NULL`).

Source abstraction (`pipeline/sensors/sources.py`):
```python
class SensorSource(Protocol):
    name: str
    def read(self, start: datetime, end: datetime) -> Iterator[Reading]: ...
class SimulatedSensorSource      # wraps the simulator; needs the SensorSpec list
class HttpPollingSource          # working adapter over a JSON REST endpoint (httpx); unit-tested with httpx.MockTransport
def get_sensor_source(settings, sensors) -> SensorSource        # registry keyed by SENSOR_SOURCE ('simulated' | 'http')
```
(MQTT is a documented example against this protocol in `docs/real-sensor-integration.md`, not shipped code.)
`IngestionService(conn).ingest(readings, source) -> IngestResult(accepted, rejected, reasons: dict[str,int])`: validates
known sensor, tz-aware timestamp, finite value, unit match; physically implausible values (outside a wide per-type
range) → `status='suspect'`; upsert `ON CONFLICT (sensor_id, ts) DO UPDATE`; bulk via COPY into a temp table.
The simulator only yields `Reading`s; detection reads only `sensor_readings`. `POST /ingest/readings` uses the same
service and does NOT trigger detection (documented: re-run `detect_anomalies.py` + `analyze_spatial.py`).

---------------------------------------------------------------------------------------------------------------------
## 8. Detection ("Prototype Anomaly Detection") — retrospective batch analysis, reads only the DB
Statement used in docs/UI: "Detection is a retrospective batch analysis: baselines and scales are estimated from the
whole data window. Playback replays those results hour by hour; it does not reproduce what a streaming detector would
have known at that hour."
Reference prototype (recall 1.0, precision 0.90–0.93 over 8 seeds): `proto_sim.py`, `proto2.py … proto12.py` in the
scratchpad — read them, do not copy blindly.
1. **Work domain:** vibration in ln(mm/s); others native. Profile `p_i` = median by **local** hour of day
   (`settings.TIMEZONE`; vibration also weekday/weekend); `r_i = x_i − p_i`.
2. **Peer adjustment** (temperature, moisture only — the "spatial anomaly analysis" step): for every placement class g
   of the sensor's type with ≥ 4 sensors, `M_g(t)` = median over the class of `r_j(t)/s_j`,
   `s_j = max(1.4826·MAD(r_j), floor)`. Each sensor's `r_i` is regressed on ALL class medians of its type (moisture:
   also on EMA(M_g, 24 h) and EMA(M_g, 72 h)) by Huber-weighted least squares (c = 1.345, 8 IRLS iterations, no
   intercept). `expected_i(t) = p_i(t) + Σ_g β_ig·M_g(t)`.
3. **Robust z:** `z = (x − expected − median)/max(1.4826·MAD, floor)`; floors 0.5 °C, 0.10 ln-units, 0.5 %, 0.5 psi
   (temperature classes with < 4 sensors: 1.5 °C). `expected` stored back-transformed to native units;
   `expected_low/high` = expected ± 3 robust σ (vibration: `expected·exp(∓3·scale)`).
4. **Detectors** (hour flags): `threshold` — value beyond crit_low/crit_high of its (type, placement);
   `robust_zscore` — |z| ≥ `DETECT_Z_STRONG`; `rolling_median` — |median of z over the trailing `DETECT_ROLLING_HOURS`|
   ≥ 3 with ≥ 4 readings in the window (either sign; sustained shifts and drift); also |z| ≥ `DETECT_Z_MIN` counts as
   a flagged hour for merging. `isolation_forest` — scikit-learn, one model per sensor type, `n_estimators=100,
   max_samples=256, random_state=SIM_SEED`, features [z, Δz, 3 h mean, 3 h std, 12 h mean]; `iforest_score =
   −model.score_samples(X)` (original-paper score in (0,1], no min-max rescaling). **Corroborating evidence only:** an
   hour is IF-positive when score ≥ `DETECT_IFOREST_THRESHOLD` and |z| ≥ `DETECT_Z_MIN`; it is added to
   `detection_method` and the explanation but never creates or extends an event.
5. **Events:** merge flagged hours with gaps ≤ `DETECT_MERGE_GAP_HOURS`; trim started_at/ended_at to the first/last
   hour with |z| ≥ `DETECT_Z_MIN` (or a threshold breach). **Persistence — an event is kept only if** peak |z| ≥
   `DETECT_Z_STRONG`, or it has ≥ 3 flagged hours, or a critical threshold is breached; otherwise its hours remain
   `reading_scores.flagged=true` only (shown as "warning" readings — "not every unusual reading is an anomaly").
6. **Per event:** `started_at` = ts of first flagged reading, `ended_at` = ts of last flagged reading (never NULL),
   `duration_hours = (ended_at − started_at)/1 h + 1`, `peak_at` = ts of max |z| (always an existing reading),
   `observed_value`, `expected_value`, `robust_z` at the peak.
   **Score:** `M = clip(log2(|z_peak|/3)/4, 0, 1)`; `D = clip(ln(1 + duration_hours)/ln(97), 0, 1)`; `T` = 1 if any
   reading in the event is beyond crit limits, 0.5 if beyond warn limits, else 0;
   `anomaly_score = round(0.50·M + 0.25·D + 0.25·T, 3)`; `score_components = {"magnitude": M, "duration": D, "threshold": T}`.
   **Severity:** low < 0.30 ≤ medium < 0.50 ≤ high < 0.70 ≤ critical.
   `detection_method` = fired detectors joined by `+` in the order threshold, robust_zscore, rolling_median, isolation_forest.
   `anomaly_type` (descriptive signature only): vibration: ≤ 3 h → `vibration_spike` else `sustained_high_vibration`;
   moisture: `moisture_increase` (z > 0) / `moisture_decrease`; pressure: z > 0 → `pressure_spike`; z < 0 and
   duration ≥ 36 h → `pressure_decline`, else `pressure_drop`; temperature: ≥ 24 h → `temperature_drift` else
   `temperature_spike` (z < 0 → `temperature_drop`). Human labels: "Short elevated vibration", "Sustained high
   vibration", "Unusual moisture increase", "Pressure drop", "Short pressure excursion", "Gradual pressure decline",
   "Abnormal temperature rise", "Temperature drift".
   `status` = `active` if `ended_at` is within `DETECT_MERGE_GAP_HOURS` of T_end, else `resolved` (value at T_end).
   `explanation` — plain English with the numbers, descriptive only, never a cause, never "failure"/"unsafe", e.g.
   "Sustained high vibration on VIB-003 (bridge deck): 4.9 mm/s at peak versus an expected 1.6 mm/s for that hour —
   7.4 robust standard deviations (log scale) above this sensor's baseline, 3.1× the expected level, lasting 19 h.
   Flagged by robust z-score and rolling median; corroborated by Isolation Forest. Severity high (score 0.62:
   magnitude 0.33, duration 0.66, threshold 0.0)."
   Gradual changes: "started_at is the detection time, typically 12–24 h after onset for a 2–4 day ramp" (documented).
7. **Evaluation vs `simulation_events`** (self-consistency check, not field validation): injected event detected if an
   anomaly on the same sensor overlaps [started_at − 2 h, ended_at + 2 h]; anomaly is a true positive if it overlaps
   such a window of an `is_anomaly=true` event on its sensor. `detection_runs.metrics` = `{injected_events,
   detected_events, event_recall, anomalies, true_anomalies, anomaly_precision, false_anomalies,
   false_anomalies_during_benign_events, split_events, detection_delay_hours{by type}, severity_counts{}}`.
   Targets (asserted in tests on seeds 42, 7, 123 with a reduced simulation where needed): recall ≥ 0.9,
   precision ≥ 0.85, ≤ 2 anomalies overlapping benign regional events; on the default seed 2–8 critical and no severity
   class above 50 % of anomalies. One transaction; re-running replaces the previous run.
   `T_end` := `detection_runs.window_end` of the latest run (= last reading timestamp at detection time).

---------------------------------------------------------------------------------------------------------------------
## 9. Spatial analysis (PostGIS-first) and "Derived Asset Health Score"
- **Proximity:** within radius = `ST_DWithin(a.geom::geography, p::geography, r)`; nearest =
  `ORDER BY a.geom::geography <-> p::geography LIMIT 1`; distance = `ST_Distance` on geography, 0.1 m.
- **Anomaly density:** hex cells `ST_HexagonGrid(RISK_HEX_EDGE_M, …)` in 32614 intersecting the study area
  (150 m edge → cells ≈ 300 m across, 5.8 ha; 88 cells by default). `cell_id = 'i_j'`.
- **Clustering (spatio-temporal, "co-occurrence cluster — descriptive, not a causal finding"):** pairwise distance
  `d_ij = max(ST_Distance in 32614 / CLUSTER_EPS_M, hours between the two [started_at, ended_at] intervals (0 if they
  overlap) / CLUSTER_EPS_HOURS)` from one PostGIS query; labels from scikit-learn
  `DBSCAN(metric='precomputed', eps=1.0, min_samples=CLUSTER_MIN_POINTS)`; clusters with fewer than
  `CLUSTER_MIN_SENSORS` distinct sensors are discarded; hull = `ST_Buffer(ST_ConvexHull(points)::geography, 40)`;
  `method='st_dbscan'`. The planted co-located group must be recovered (pipeline logs a warning if not).
- **Risk zones** (hourly, for every cell and every hour t on the time axis):
  `risk_raw(cell,t) = Σ_i s_i · exp(−d_i²/(2·RISK_BANDWIDTH_M²)) · w_i(t)` over anomalies with `started_at ≤ t`,
  `d_i` from the cell centroid in 32614, `s_i ∈ {low 1, medium 2, high 4, critical 7}`, `w_i = 1` while active at t else
  `0.5 ** ((t − ended_at)/RISK_HALF_LIFE_HOURS)`; anomalies ended > 14 days before t ignored.
  `risk_score = min(100, 100·risk_raw/RISK_REFERENCE)` (reference 14.0 = two active critical anomalies at the cell
  centre; one active critical = 50, one active high = 29, one active medium = 14).
  Levels: low < 25 ≤ moderate < 50 ≤ high < 75 ≤ very_high. `anomaly_count` = anomalies active at t whose point lies
  in the cell. Stored sparse (score ≥ 0.5).
- **Asset health** — for every monitored asset (≥ 1 sensor) and every hour t:
```
window  = anomalies on the asset's sensors with started_at <= t and ended_at >= t − HEALTH_WINDOW_DAYS
w_i     = 1 while active at t, else 0.5 ** ((t − ended_at_i) / HEALTH_HALF_LIFE_HOURS)
s_i     = {low: 1, medium: 2, high: 4, critical: 7}[severity_i]
frequency_penalty = min(20, 6 · Σ w_i)
severity_penalty  = min(45, 6 · Σ w_i · s_i)
reading_penalty   = min(10, 2 · mean over reporting sensors of clip(median(|z|, trailing 6 h) − 3, 0, 5));  0 if none reporting
sensor_penalty    = 20 · (sensors with no reading at t / sensors_total)
health  = clamp(round(100 − frequency − severity − reading − sensor), 0, 100)
status  = normal >= 90 > watch >= 70 > at_risk >= 45 > critical
```
  "Evaluated at every hour t from the anomalies that had started by t and the readings up to t; anomaly severities and
  baselines come from the retrospective run." **Assets at Risk = monitored assets with health < 70 at t.**
  Worked scenarios (must hold; asserted in `tests/test_health.py`; single-sensor asset unless noted): nothing → 100
  normal; one low anomaly ended 72 h ago → 96 normal; one active critical → 42 critical (3-sensor asset: 49 at_risk);
  two medium ended 24 h and 96 h ago → 83 watch; sole sensor offline → 80 watch; active high → 60–64 at_risk;
  active medium → 72–82 watch.
  Unmonitored assets have no score: `status='not_monitored'`, `health_score=null`.

---------------------------------------------------------------------------------------------------------------------
## 10. API (FastAPI; paths at the root exactly as in the brief; plain `def` endpoints; gzip; CORS from env; `/docs`)
App: `create_app(settings=None)`; pool `psycopg_pool.ConnectionPool(min_size=1, max_size=8, timeout=3, open=False)`
opened in lifespan with `wait=False` (the app starts when the DB is down); `psycopg.OperationalError`/`PoolTimeout` →
503 `{"detail":"database unavailable"}`. Errors: `{"detail": <string>}` for 404/503; 422 keeps FastAPI's default.
FastAPI `description` carries the data notice (below).

### 10.1 Time and status semantics (implemented ONCE, in `pipeline/analysis/status.py` + shared SQL; used by health, /sensors, /statistics, /playback)
- Time axis = hourly grid from the first to the last reading timestamp of the latest run window. `T_end` = latest
  `detection_runs.window_end`. `as_of` defaults to `T_end`; given values are floored to the step and clamped to
  [start, T_end]; every response echoes the effective `as_of`. Naive datetimes are UTC.
- **Every timestamp in every response** is `YYYY-MM-DDTHH:MM:SSZ` via one helper `iso_z()` (in SQL:
  `to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')`). An API test asserts this for all endpoints.
- Anomaly: **active at t ⇔ started_at ≤ t ≤ ended_at**; resolved ⇔ ended_at < t; not yet visible ⇔ started_at > t.
  API `status` is evaluated at `as_of` (stored `anomalies.status` = value at T_end).
- Sensor status at t, first match wins: `offline` = no reading with ts in (t − sampling_interval, t]; `anomaly` = has
  an anomaly active at t; `warning` = `reading_scores.flagged` at t or value outside warn_low/warn_high; else `normal`.
- KPIs at t: `total_assets` = all rows of infrastructure_assets (static) with `real_assets`, `simulated_assets`,
  `monitored_assets`; `total_sensors`; `active_sensors` = total − offline; `offline_sensors`; `warning_sensors`;
  `active_anomalies` = anomalies active at t; `critical_alerts` = active anomalies with severity `critical`;
  `assets_at_risk` = monitored assets with health < 70 at t.
- Endpoints without `as_of` are evaluated at T_end.

### 10.2 Endpoints
```
GET /health        service health (NOT asset health): {status:"ok", service:"dodge-city-infra-monitor", version, database:"ok",
                   postgis, data_window:{start,end}, asset_health:{as_of, normal, watch, at_risk, critical, not_monitored},
                   note:"service health; asset health scores are at /assets and /assets/{id}/health"}
                   DB down → 503 {status:"degraded", service, database:"unavailable"}
GET /meta          see §10.3
GET /statistics?as_of=        {as_of, data_notice, total_assets, real_assets, simulated_assets, monitored_assets, total_sensors,
                   active_sensors, offline_sensors, warning_sensors, active_anomalies, critical_alerts, assets_at_risk,
                   anomalies_to_date, anomalies_by_severity{}, anomalies_by_sensor_type{}, assets_by_type{}}
GET /assets?asset_type=&category=&monitored=&status=&bbox=&limit=&offset=     GeoJSON FeatureCollection (+ numberMatched,
                   numberReturned); all rows when limit omitted (max 20000)
GET /assets/{asset_id}?as_of= Feature + health{score,status,components} + sensors[sensor item] + recent_anomalies[] + provenance{}
GET /assets/{asset_id}/health?start=&end=    columnar: {asset_id, start, step_minutes, count, health_score[], status (string, 1 char/hour:
                   n w r c), frequency_penalty[], severity_penalty[], reading_penalty[], sensor_penalty[], active_anomalies[],
                   sensors_reporting[], sensors_total}
GET /sensors?sensor_type=&asset_id=&status=&as_of=&limit=&offset=      {total, limit, offset, as_of, items:[sensor item]}
GET /sensors/{sensor_id}?as_of=              sensor item + thresholds{} + baseline{scale, floor} + anomaly_count + anomalies[]
GET /sensor-readings?sensor_id=(required)&start=&end=&shape=records|columns&limit=
                   records (default): {sensor_id, sensor_type, unit, placement, is_simulated:true, source, data_notice,
                     start, end, count, readings:[{ts, value, status, expected, expected_low, expected_high, robust_z, flagged}]}
                   columns: {sensor_id, sensor_type, unit, placement, is_simulated, source, thresholds{}, start, step_minutes, count,
                     value[], expected[], expected_low[], expected_high[], robust_z[], flagged[idx…]} aligned to the hourly
                     grid, null where no reading. max 5000 points.
GET /anomalies?severity=&sensor_type=&asset_id=&sensor_id=&status=&start=&end=&as_of=&bbox=&include=nearby_assets
               &sort=(-started_at|started_at|-anomaly_score|severity)&limit=(default 500, max 1000)&offset=
                   {total, limit, offset, as_of, data_notice, items:[anomaly item]}; severity/sensor_type accept comma lists
GET /anomalies/{anomaly_id}?radius_m=        anomaly item + nearby_assets[{asset_id,name,asset_type,distance_m}] + cluster{}|null
GET /simulation-events?is_anomaly=           {total, items:[{event_id, sensor_id, asset_id, sensor_type, event_type, is_anomaly,
                   started_at, ended_at, magnitude, description}]}
GET /spatial/assets-within?lon=&lat=&radius_m=      GeoJSON (+ distance_m)
GET /spatial/nearest-asset?lon=&lat=&asset_type=    Feature (+ distance_m)
GET /spatial/sensors-in-asset-area?asset_id=&buffer_m=   {asset_id, buffer_m, items:[sensor item + distance_m]}
GET /spatial/anomaly-density?start=&end=            GeoJSON hex cells {cell_id, anomaly_count, weighted_severity}
GET /spatial/risk-zones?as_of=                      GeoJSON hex cells {cell_id, risk_score, risk_level, anomaly_count} (all cells; 0 where none)
GET /spatial/clusters                               GeoJSON hulls {cluster_id, n_anomalies, n_sensors, n_assets, sensor_types,
                   max_severity, first_started_at, last_ended_at, anomaly_ids[]}
GET /layers/roads      GET /layers/study-area      GET /layers/city-boundary        GeoJSON
GET /playback          §10.4 (cached in-process by run_id; ETag)
GET /config.js         dashboard runtime config (registered before the static mount): `window.DCIM_CONFIG = {mode:'api', apiBaseUrl:'', basemapStyleUrl:'…'}`
POST /ingest/readings  body {readings:[{sensor_id, ts, value, unit}]}, header X-API-Key. INGEST_API_KEY empty → 404
                   {"detail":"ingestion endpoint is disabled (INGEST_API_KEY not set)"}; wrong/missing key → 401; ok → {accepted, rejected, reasons}
```
**Sensor item:** `{sensor_id, asset_id, asset_name, asset_type, sensor_type, placement, unit, description, is_simulated, source,
lon, lat, status, latest:{ts, value, status, expected, robust_z}|null, anomaly_count}`.
**Asset feature properties:** `asset_id, asset_type, category, name, is_simulated, source_id, monitored, sensor_count,
sensor_types[], anomaly_count, health_score, status, centroid:[lon,lat]` + building `height_m, height_source, building_type,
levels, footprint_m2` + road `highway_class, surface, lanes, length_m` + bridge `structure_kind, length_m, nbi{…}` + water_main `host_road_id`.
(`health_score`/`status`/`anomaly_count` on the feature are the T_end values.)
**Anomaly item:** `anomaly_id, sensor_id, asset_id, asset_name, asset_type, sensor_type, placement, anomaly_type, anomaly_label,
started_at, ended_at, peak_at, duration_hours, observed_value, expected_value, unit, robust_z, anomaly_score,
score_components{}, severity, detection_method, explanation, status, is_simulated:true, cluster_id, lon, lat,
nearby_asset_count` (+ `nearby_assets[]` when `include=nearby_assets`).
Rounding: values 3 dp, z 2 dp, scores 3 dp, coordinates 6 dp, distances 0.1 m. Missing text is `null`, never `""`.
`data_notice` (top-level on /meta, /statistics, /anomalies, /sensor-readings, /playback):
"Simulated sensor data and prototype anomaly detection. Not a record of real infrastructure condition."

### 10.3 `/meta` shape
```json
{"service":"dodge-city-infra-monitor","version":"1.0.0","data_notice":"…",
 "study_area":{"slug":"dodge-city-downtown","name":"Downtown Dodge City, Kansas","bbox":[-100.03,37.745,-100.005,37.762],
               "center":[-100.0175,37.7535],"timezone":"America/Chicago","utm_srid":32614},
 "time":{"start":"2026-09-01T05:00:00Z","end":"2026-10-01T04:00:00Z","step_minutes":60,"count":720},
 "labels":{"sensor_data":"Simulated Sensor Data","detection":"Prototype Anomaly Detection","health":"Derived Asset Health Score",
           "buildings":"3D building extrusions derived from OSM footprints. Heights: measured from USGS 3DEP lidar (2013–14) where available, otherwise OSM tags, otherwise estimated. Not detailed 3D building models.",
           "water_network":"Simulated water network (not a record of real utilities)",
           "playback":"Playback replays a retrospective analysis of simulated readings."},
 "sensor_types":{"temperature":{"unit":"°C","label":"Temperature","placements":{"bridge_deck":{"warn_low":null,"warn_high":50,"crit_low":null,"crit_high":58,"description":"…"}}}},
 "anomaly_types":{"vibration_spike":"Short elevated vibration"},
 "severity_levels":["low","medium","high","critical"],
 "health":{"formula":"…text…","window_days":7,"half_life_hours":48,"at_risk_below":70,
           "bands":{"normal":90,"watch":70,"at_risk":45,"critical":0}},
 "risk":{"hex_edge_m":150,"bandwidth_m":250,"half_life_hours":72,"reference":14.0,"levels":{"low":0,"moderate":25,"high":50,"very_high":75}},
 "counts":{"assets":0,"real_assets":0,"simulated_assets":0,"monitored_assets":0,"sensors":0,"readings":0,"anomalies":0,
           "assets_by_type":{},"sensors_by_type":{},"building_height_sources":{"lidar_3dep":0,"osm_levels":0,"estimated":0}},
 "data_sources":[{"source_id":"osm","name":"…","kind":"real","provider":"…","url":"…","license":"…","attribution_text":"…","vintage":"…","retrieved_at":"…Z","notes":null}],
 "detection_run":{"run_id":1,"finished_at":"…Z","params":{},"metrics":{},"evaluation_note":"Scored against injected simulated events — a self-consistency check, not field validation."}}
```

### 10.4 Playback bundle
```json
{"data_notice":"…","run_id":1,"timestamps":["2026-09-01T05:00:00Z","…"],
 "sensors":{"VIB-001":{"values":[1.02,null,"…"],"status":"nnwaao…"}},            // 1 char/hour: n normal, w warning, a anomaly, o offline
 "assets":{"BRG-001":{"health":[100,97,"…"],"status":"nnwrc…"}},                 // n normal, w watch, r at_risk, c critical
 "zones":{"3_7":{"risk":[0,12,"…"]}},                                            // integer 0–100 per hour; only cells that are ever > 0
 "stats":{"active_sensors":[],"offline_sensors":[],"warning_sensors":[],"active_anomalies":[],"critical_alerts":[],"assets_at_risk":[]}}
```
Values 3 dp. `stats[*][i]` MUST equal `/statistics?as_of=timestamps[i]` (same query function; parity test on 5 sampled
indices; likewise `/sensors?as_of=` statuses vs status chars and `/assets/{id}?as_of=` health vs `assets[id].health[i]`).
Budget (tested): `playback.json` ≤ 2 MB.

### 10.5 Static mount
When `SERVE_DASHBOARD=true`, `dashboard/` is mounted at `/` AFTER all API routes (`StaticFiles(html=True)`): `/` is the
dashboard, `/docs` the API docs. `main.py` registers mimetypes `.js → text/javascript`, `.geojson → application/geo+json`.
`dashboard/` must never contain a top-level entry named: assets, sensors, sensor-readings, anomalies, statistics,
health, meta, spatial, layers, playback, simulation-events, ingest, docs, redoc, openapi.json, config.js is the ONE
exception (the API route `/config.js` deliberately shadows the static `dashboard/config.js`).
Unknown GET paths → 404 JSON. The provider never requests trailing-slash URLs.

---------------------------------------------------------------------------------------------------------------------
## 11. Static snapshot (keeps GitHub Pages working; committed)
`backend/export.py` drives the real app through `TestClient` and writes, under `dashboard/data/snapshot/`:
`meta.json` (/meta), `assets.geojson` (/assets), `roads.geojson`, `study-area.geojson`, `city-boundary.geojson`,
`sensors.json` (/sensors, all), `anomalies.json` (/anomalies?include=nearby_assets&limit=1000), `clusters.geojson`,
`risk-zones.geojson` (/spatial/risk-zones at T_end — geometry + T_end scores; hourly scores come from playback.zones),
`simulation-events.json`, `playback.json`, `readings/<sensor_id>.json` (/sensor-readings?shape=columns, full window),
`health/<asset_id>.json` (/assets/{id}/health, monitored assets only), `manifest.json`
(`{generated_at (= run finished_at, not wall clock), as_of, api_version, files:[{path, bytes, sha256}]}`).
Byte-for-byte equal to the API responses; deterministic (sorted keys, `separators=(',',':')`, LF) so unchanged data
yields no git diff. Budget (tested): whole snapshot ≤ 8 MB. Old `dashboard/data/data.js` is deleted.

---------------------------------------------------------------------------------------------------------------------
## 12. Frontend (vanilla ES modules; no build step; dark theme; MapLibre GL JS **5.24.0 vendored**)
### 12.1 Honesty labels (exact strings, all mandatory)
- Header badges: "Simulated Sensor Data" · "Prototype Anomaly Detection" · "Derived Asset Health Score".
- Source badge: "Source: PostGIS API" / "Source: static snapshot (exported <manifest date>)".
- Clock caption "Simulated time"; jump-to-end button label "End of simulation".
- Persistent, non-dismissible map watermark (bottom centre): "Simulated sensor data · prototype — not a record of real infrastructure condition".
- KPI sub-captions: Total Assets "N monitored · M simulated"; Active Sensors "simulated · K offline"; Active Anomalies
  "prototype detection"; Critical Alerts "severity = critical"; Assets at Risk "derived health score < 70".
- Legend/layer titles: "Derived Asset Health Score (from simulated sensors)", "Simulated sensors", "Risk zones — derived
  from simulated anomalies", "Simulated water network (not a record of real utilities)", and `meta.labels.buildings`
  with "X of Y heights measured/tagged, Z estimated" computed from `meta.counts.building_height_sources`.
- Asset detail = two titled blocks: "Recorded attributes — source: OpenStreetMap / FHWA National Bridge Inventory"
  (real data, with retrieval date) and "Simulated monitoring — Derived Asset Health Score, simulated sensors; not an
  assessment of this structure's real condition". NBI ratings never share a row or colour scale with the health score.
- Every anomaly card/detail carries a "Simulated" tag. Anomalies tab subtitle "Prototype Anomaly Detection". Every
  sensor chart caption "Simulated Sensor Data".
- A node test asserts the three R22 strings are present and that no UI string contains "live" or "real-time".

### 12.2 Layout (acceptance: at 1366×768 the map occupies ≥ 50 % of the viewport and nothing on the first screen scrolls)
Rows: header 44 px | KPI strip 52 px (five equal cards) + one-line status sentence | body 1fr | timeline 64 px.
Body columns: map 1fr | right panel 340 px (380 px at ≥ 1600 px). No left column: "Layers & legend" is a collapsible
card over the map (top-left; closed by default < 1600 px), compact legend bottom-left, view buttons (3D/2D · Home ·
Imagery · City limits) top-right, "About the data" dialog and "?" (how to read) button in the header. Min font 12 px.
Status sentence (recomputed from data): "As of <date, hour, tz> (simulated time): N active anomalies on M assets ·
K assets need attention".
Right-panel tabs: **Assets** (default) · **Anomalies** · **Sensors**.
Responsive: ≥ 1280 px grid above; 900–1279 px right panel 300 px; < 900 px single scrolling column (header, KPI strip
with horizontal scroll, map 56 vh, timeline, tabbed panel) — no drawers. Checked at 1366×768, 1920×1080, 768×1024, 390×844.

### 12.3 Initial state
Time = T_end. Camera = `fitBounds(meta.study_area.bbox, {padding: 40, pitch: 50, bearing: -20})` (never hard-coded
coordinates); Home returns to it. Layers ON: study-area outline, roads (base), road assets, bridges, buildings,
sensors, active anomaly markers, risk zones (35 % opacity). OFF: city boundary, rail, power, street lights, simulated
water network, anomaly clusters, imagery. "How to read this dashboard" card: non-modal, over the map, Esc closes,
re-openable from "?", shown on first visit (localStorage in try/catch); answers WHAT / WHERE / WHAT IS MONITORED /
WHAT IS HAPPENING / WHAT NEEDS ATTENTION — and each answer is also readable from persistent UI (title, subtitle, KPI
captions, status sentence, Assets tab ranking).

### 12.4 Modules, store, selection
`js/main.js` (boot), `js/state/store.js`, `js/data/provider.js`, `js/data/index.js` (lookups + pure helpers:
`statusAt`, `activeAnomaliesAt`, filters — node-tested), `js/map/map.js`, `js/map/layers.js`,
`js/panels/{kpis,assets,anomalies,sensors,layers,about,intro}.js`, `js/timeline/timeline.js`,
`js/ui/{chart,format,tokens,dom}.js`. Store: `getState()`, `set(patch)`, `subscribe(keys, fn)`; state
`{t (index), playing, speed, selection:{kind:'asset'|'sensor'|'anomaly'|null, id}, tab, filters:{severity:Set,
sensorType:Set, status, assetId, dateFrom, dateTo}, layers:{id:bool}, pitched, hexMetric:'risk'|'count'}`. Modules talk
only through the store.
Provider (`createProvider(config)` → `ApiProvider` | `StaticProvider`, identical methods): `getMeta, getAssets,
getRoads, getStudyArea, getCityBoundary, getSensors, getAnomalies, getClusters, getRiskZones, getSimulationEvents,
getPlayback, getSensorReadings(sensorId), getAssetHealth(assetId), getAnomalyDetail(id), getManifest()`.
Mode (`dashboard/config.js`: `window.DCIM_CONFIG = {mode:'auto', apiBaseUrl:'', basemapStyleUrl:'https://tiles.openfreemap.org/styles/dark'}`;
URL override `?mode=static|api`): `auto` probes `${apiBaseUrl}/health` (3 s timeout) and accepts only HTTP 200 JSON with
`service === 'dodge-city-infra-monitor' && status === 'ok'`; otherwise static. In `api` mode a failed /health shows the
error state with Retry and "Use static snapshot" — never a silent switch. All URLs relative (`./…`) so the Pages
sub-path works. Lists are loaded once and filtered in memory; time-varying values come from the playback bundle.
Selection: asset click → Assets tab detail (unmonitored: recorded attributes + empty state "Not monitored — no
simulated sensors on this asset"); sensor click → Sensors tab; anomaly marker/list click → Anomalies detail, and if t
is outside [started_at, ended_at] the clock moves to peak_at; list-initiated selections fly to the feature
(zoom 17, `essential:false`); map-initiated ones do not move the camera; hover tooltip (name · type · status); Esc
clears selection. Inline classic script: on `file:` protocol show "Open this dashboard over http — run
`python -m http.server -d dashboard 8080` or start the API".

### 12.5 Panels
**Assets tab** — nothing selected: "Needs attention" ranking of monitored assets with health < 90 at t (lowest first,
max 12; row = status pill with text, name, type, health, active anomaly count) + empty state "All monitored assets are
normal at this time". Detail: asset ID, type, location (centroid lat/lon 5 dp + OSM `addr:*` tags when present — never a
composed address), recorded attributes block, health score + four penalty components at t + status band, sensor count,
latest readings at t (per sensor: value, unit, status), anomaly count, recent anomalies (links), one small history chart
per sensor (max 4; small multiples — never dual axes), health history sparkline.
**Anomalies tab** — filters: severity (multi, all on), sensor type (multi, all on), status (All | Active | Resolved,
evaluated at t), asset (select of assets that have anomalies), date from/to (default full window, applied to
started_at), "Clear filters"; list shows anomalies with started_at ≤ t (active first, then severity, then newest);
row = severity pill · anomaly label · asset name · sensor id · started_at · status at t. Detail: anomaly ID, asset
(name + ID, link), sensor (ID + type, link), anomaly type, severity, started/peak/ended, observed value + unit, expected
value, anomaly score (+ components), detection method, explanation, nearby assets (within `PROXIMITY_RADIUS_M`,
highlighted on the map with a radius ring), cluster membership, chart ±48 h around the event. Empty state "No anomalies
match these filters at the selected time".
**Sensors tab** — sensor type select (All + 4), asset select (monitored assets, "name (ID)"), list rows value + unit +
status at t; detail: latest reading at t, trend (value at t minus value at t−24 h: arrow + "rising/falling/steady"),
history chart: observed line, expected line + expected band (expected_low/high), warn/crit threshold lines when in
range, shaded anomaly windows, grey shading for benign simulated regional events labelled "Simulated regional event
(benign — not flagged)", cursor at t (portion after t at 35 % opacity), hover crosshair + tooltip, "View as table".
**About the data** dialog: provenance table from `meta.data_sources` (REAL / SIMULATED / DERIVED tags), "Prototype
Anomaly Detection" method summary + latest run's recall/precision with `evaluation_note`, health-score formula, risk
formula, retrospective-analysis statement, limitations.

### 12.6 Map
Vendored `dashboard/vendor/maplibre-gl/{maplibre-gl.js, maplibre-gl.css, LICENSE.txt}` (v5.24.0, verify sha384
`5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp` for the js). No CDN for code; no Google Fonts
(`font-family: Inter, "Segoe UI", system-ui, sans-serif`; mono `ui-monospace, "Cascadia Mono", Consolas, monospace`).
Basemap: fetch `basemapStyleUrl` with a 4 s timeout before creating the map; on failure use an inline style
(`background #0b1016`, no glyphs/sprite) + notice "Basemap unavailable — showing project data only"; also handle basemap
`error` events after load. Hide/dim the basemap's own `building` layer. Overlay layers never depend on basemap layer
ids, sprite or glyphs (no symbol layers in fallback mode). Imagery toggle: USGS The National Map
`https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` (maxzoom 16,
attribution "USDA, USGS The National Map: Orthoimagery"); hidden if its first tile fails. Project sources set their own
`attribution` ("© OpenStreetMap contributors", FHWA NBI, U.S. Census Bureau) so credit survives fallback mode.
Sources/layers: one GeoJSON source `assets` (`promoteId:'asset_id'`) feeding all asset layers via static `asset_type`
filters; shared colour expression on feature-state `status`; lines get a 2 px dark casing, at_risk/critical lines
+2 px width; NBI point assets are circles; two building layers split by static `monitored`: `buildings-context`
(neutral #2f3b4a, opacity 0.6) and `buildings-monitored` (status colour, opacity 0.95); feature-state keys `status`,
`selected`, `hover`. Sensors: circle layer on source `sensors` (`promoteId:'sensor_id'`), status by feature-state +
form: normal filled r5, warning filled r7, anomaly filled r9 with 2 px white stroke, offline hollow grey ring.
Active anomaly markers: static ring (transparent fill, 2.5 px stroke in severity colour, radius 10/13/16/20 px by
severity), no pulse, no rAF loop; visibility by `setFilter` on integer `start_idx`/`end_idx` properties added
client-side. Risk zones: hex fill by feature-state `risk` (from `playback.zones`), switch "Risk score | Anomaly count".
Cluster hulls: dashed outline, shown only while `first_started_at ≤ t ≤ last_ended_at + 48 h`. Simulated water mains:
dashed line. No per-tick `setData`; feature-state updates only for entities whose status char changed.
Tokens (`css/tokens.css` mirrored in `js/ui/tokens.js`): surfaces `--bg #0b1016, --panel #111821, --card #151e29,
--line #233142, --text #e6edf5, --muted #8a9bb0, --accent #38bdf8`; asset status normal `#2dd4bf`, watch `#f5b942`,
at_risk `#fb8c3c`, critical `#ef5a45`, not_monitored `#2f3b4a`; sensor status normal `#2dd4bf`, warning `#f5b942`,
anomaly `#ef5a45`, offline `#64748b`; severity low `#8fb8de`, medium `#f5b942`, high `#fb8c3c`, critical `#ef5a45`;
risk levels use the severity ramp. Status/severity are NEVER colour-only: text pills, icons/form on the map, labels in
legends. Sensor type is encoded by label/letter glyph (T V M P), never by hue. Charts: single series colour
`--accent`, expected in `--muted`, one y-axis, recessive solid hairline grid (follow the `dataviz` skill).

### 12.7 Playback (R12)
Controls in order: ⏮ first hour · ◀ −1 h · ▶/❚❚ Play/Pause · ▶ +1 h · ⏭ "End of simulation" · speed select
(6 h/s, 12 h/s default, 24 h/s) · date select + hour select · slider · histogram (active anomalies per hour, critical
stacked). Play at the last hour restarts from index 0; stops at the end (no loop). Per tick, in order: (1) clock +
slider + histogram cursor; (2) KPI cards + status sentence from `playback.stats[t]`; (3) asset status feature-state
(changed only); (4) sensor status feature-state; (5) anomaly markers via setFilter; (6) risk-zone feature-state;
(7) open detail panel: health, latest readings, chart cursor. While playing, list panels re-render at most every
500 ms; immediately on pause. Time formatting only through `js/ui/format.js` with
`Intl.DateTimeFormat('en-US', {timeZone: meta.study_area.timezone, …, timeZoneName:'short'})` — never hard-code "CDT",
never `datetime-local`, compare times as epoch ms; the date/hour selects are built by formatting `playback.timestamps`
and mapping back by index. Node test runs under `TZ=Africa/Accra` and `TZ=Asia/Tokyo` asserting index 0 renders
"Sep 1, 12:00 AM CDT" equivalently.

### 12.8 Accessibility / states
Every selectable entity reachable from a list of `<button>` rows; tabs `role=tablist` with arrow keys; visible
`:focus-visible`; shortcuts Space play/pause, ←/→ ±1 h, Shift+←/→ ±24 h, Home/End first/last hour, Esc clear selection
(ignored when focus is in input/select/textarea/button); clock `aria-live="off"` while playing;
`prefers-reduced-motion` respected. Loading skeletons on first load; refetch holds the previous render at reduced
opacity; error state with Retry; empty state for every list and chart. No hardcoded numbers anywhere.

---------------------------------------------------------------------------------------------------------------------
## 13. Tests, Docker, CI, docs
- **pytest.** Unit (no DB): settings precedence, geo helpers (incl. clipping), OSM/NBI processing on fixtures (bridge
  merge, NBI mismatch, numeric names, height precedence), placement rules, simulator determinism + magnitude bounds +
  final-hour schedule, each detector, event merge/persistence/scoring/severity, explanations, evaluation, health
  scenarios (§9), risk formula, sources + ingestion validation (MockTransport). Integration (marker `db`; skipped when
  PostGIS is unreachable unless `REQUIRE_DB=1`, which makes it a failure): session fixture connects to the `postgres`
  maintenance DB with autocommit, `DROP DATABASE IF EXISTS` / `CREATE DATABASE infra_test`, applies migrations and
  asserts `current_database()` ends with `_test` before any write; runs the full pipeline on a reduced simulation
  (`SIM_DAYS=10`) from the committed `data/raw`; tests: migrations idempotent, loaders, FK cascade re-run (stage 3–6
  twice), spatial SQL functions (incl. geography KNN A/B case), sensors inside study area, every API endpoint
  (status, shape, 404, 422, 503 with a dead DSN), all timestamps end with `Z`, playback/statistics parity, snapshot
  equals API byte for byte, size budgets, ingest auth. `node --test dashboard/tests` for pure frontend modules.
  pytest config must not turn warnings into errors (StarletteDeprecationWarning from TestClient).
- **Docker.** `docker-compose.yml`: `db` (postgis/postgis:16-3.4, healthcheck, named volume, `${POSTGRES_HOST_PORT:-5433}:5432`)
  and `backend` (`build: {context: ., dockerfile: backend/Dockerfile}`; python:3.12-slim; non-root; COPY-only image, no
  bind mounts; `environment: DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}`
  explicitly; `depends_on: db: condition: service_healthy`; `ENTRYPOINT ["python","-m","backend.entrypoint"]`;
  AUTO_SEED runs `run_pipeline --skip-download --skip-export` when `infra.detection_runs` has no finished row;
  healthcheck `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"`,
  interval 10 s, start_period 240 s). `.dockerignore` excludes `.env`, `.venv*`, `.git`, `data/processed`,
  `__pycache__`, `tests`, caches. Two-step start: `cp .env.example .env` (set a password) → `docker compose up --build`
  → dashboard at http://localhost:8000, API docs at /docs.
- **CI** `.github/workflows/ci.yml` (ruff + pytest with a postgis service container + node tests, `REQUIRE_DB=1`).
  `pages.yml` kept.
- **Docs.** `README.md`: headings exactly the 16 R18 titles numbered 1–16 in order, then 17 Deployment (database
  initialisation, frontend on GitHub Pages incl. `config.js`, backend on any Docker host; HTTPS + `CORS_ORIGINS`),
  18 What was reused (old path → new path table), 19 What is new, 20 Remaining limitations. Mermaid architecture
  diagram. `docs/`: `data-provenance.md`, `health-score.md` (formula + worked scenarios), `anomaly-detection.md`,
  `api.md` (curl examples), `deployment.md`, `real-sensor-integration.md` (HTTP adapter, ingest endpoint, MQTT example).

---------------------------------------------------------------------------------------------------------------------
## 14. Amendments (decided after stages 1–6 were built and verified — these override the text above)
- **A1 — one definition of "active".** Stored `anomalies.status` follows the §10.1 time rule evaluated at T_end:
  `active` ⇔ `ended_at >= T_end`, else `resolved`. This supersedes the "within DETECT_MERGE_GAP_HOURS of T_end"
  sentence in §8.6. `infra.v_active_anomalies` and `sql/queries` must agree with the API.
- **A2 — snapshot determinism.** `detection_runs.finished_at` is wall-clock, so `meta.json` (`detection_run.finished_at`)
  and `manifest.json` (`generated_at`, their own hashes) legitimately differ between pipeline runs. Every other
  snapshot file must be byte-stable for unchanged inputs.
- **A3 — integration tests use the full default simulation** (30 days, 40 events, seed 42) on the `infra_test`
  database (the whole pipeline takes < 1 minute). Reduced windows (10–14 days) have thin margins (precision 0.87–0.91)
  and must not be used to assert the detection targets. Seeds 7 and 123 are checked DB-free through
  `pipeline.detection.runner` in a test marked `slow`.
- **A4 — health scenario "active high → 60–64 at_risk"** assumes a trailing median |z| ≥ 6 on the sensor (reading
  penalty 10); with a quiet sensor the formula gives up to 70 (watch). Tests construct it with |z| ≥ 6.
- **A5 — Isolation Forest wording** (UI "About the data", README, docs): "Isolation Forest is used as corroborating
  evidence only. On this simulated dataset it agreed with the robust-statistics detectors on every anomaly and did not
  identify events they missed."
- **A6 — as built and verified:** `moisture_increase` lasts 12–72 h in total including the 6 h rise; the designated
  critical `pressure_drop` lasts 25–29 h; one profile-refinement pass is applied on the peer-adjusted series
  (`baseline.PROFILE_REFINEMENTS = 1`); building vibration readings are rounded to 4 decimals at source.
- **A7 — verified facts about the default dataset** (seed 42) that UI copy and tests may rely on only via the API,
  never hard-coded: 706 assets (678 real + 28 simulated mains), 112 monitored, 128 sensors, 92,028 readings,
  42 anomalies (3 critical / 17 high / 17 medium / 5 low), 3 clusters, 88 risk cells; at T_end: 126 sensors reporting,
  2 offline, 6 active anomalies, 1 critical alert, 6 assets at risk; building heights: 419 lidar_3dep, 39 estimated.
