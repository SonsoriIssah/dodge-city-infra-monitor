# Data provenance

What every dataset in the prototype is, where it comes from, under which terms it is used, and how real, simulated
and derived data are kept apart. Summary table: [README section 5](../README.md#5-data-provenance).

## Principles

- **Real** data comes only from public sources: OpenStreetMap, the FHWA National Bridge Inventory, U.S. Census
  TIGERweb and USGS 3DEP lidar. Real features carry only attributes that their source records; nothing is
  invented for them (no condition, age, material or traffic values that the source does not have).
- **Simulated** data (sensors, readings, sensor status, the simulated weather behind them, the simulated water
  network, the simulator's ground truth) is produced by `pipeline/sensors/` and flagged `is_simulated = true`.
- **Derived** data (anomalies, flagged readings, clusters, risk zones, health scores, rule-based building heights)
  is computed by this project from the above and labelled as such.
- The kind is stored in `infra.data_sources.kind` (`real` / `simulated` / `derived`), in `is_simulated` on assets,
  sensors and anomalies, and in `height_source` on buildings. `GET /meta` returns every `data_sources` row;
  `GET /assets/{id}` returns the `provenance` of one asset.
- Recorded NBI condition ratings are shown as recorded and never feed the Derived Asset Health Score, the status
  colours or the risk zones.

## How the raw data is cached

Stage 1 (`python scripts/download_data.py`, or `run_pipeline.py` without `--skip-download`) writes the raw files to
`data/raw/` and describes them in `data/raw/SOURCES.json`. The files are **committed**, so the pipeline, the tests,
CI and the Docker image never need the network. A cached file is reused unless `--refresh` is given. When a
download fails and a cache exists, the cache is used with a warning; without a cache, `osm` is fatal and the other
layers are skipped with a warning (the layer is then absent and the processing report lists it).

`SOURCES.json` has one entry per file:

| Field | Meaning |
|---|---|
| `source_id` | `osm`, `nbi`, `tiger`, `usgs_3dep` |
| `file` | file name in `data/raw/` |
| `url` | request URL (with the bbox) |
| `retrieved_at` | UTC time of the download |
| `sha256` | hash of the cached file (`.gitattributes` stores `data/raw/**` byte for byte) |
| `feature_count` | number of elements / records / features |
| `license`, `attribution_text`, `terms_url` | terms of use and the credit line |
| `vintage` | the source's own date: `osm3s.timestamp_osm_base` for OSM, the service description for NBI and TIGER, the lidar collection for 3DEP |

Stage 3 copies these entries into `infra.data_sources`, adding `basemap`, `imagery` (display-only services),
`simulator` and `derived`.

## Real sources

### OpenStreetMap (`osm`)

| | |
|---|---|
| What | Building footprints (closed ways), highways, railways, `bridge=*` ways, power substations and lines, street lamps, with their names and tags |
| Endpoint | Overpass API `https://overpass-api.de/api/interpreter`; mirrors `https://overpass.private.coffee/api/interpreter` and `https://overpass.kumi.systems/api/interpreter` as fallback. One combined query with `out geom` for the bbox, a descriptive User-Agent, at least 30 s back-off after HTTP 429/504 before one retry, then the next mirror |
| File | `data/raw/osm.json` (896 elements) |
| Vintage | OSM base timestamp 2026-10-04T11:58:25Z; retrieved 2026-10-04T12:00:24Z |
| Licence | Open Database License (ODbL) 1.0 |
| Attribution | Docs and `SOURCES.json`: "Contains OpenStreetMap data © OpenStreetMap contributors, available under the Open Database License (https://opendatacommons.org/licenses/odbl/1-0/)". Map UI: "© OpenStreetMap contributors" linking to https://www.openstreetmap.org/copyright |

Processing (`pipeline/gis/process.py`, no database, report in `data/processed/processing_report.json`):

- Buildings are kept whole when their centroid lies in the study area and their footprint is at least 25 m²
  (485 footprints in the download: 26 smaller, 1 centroid outside, 458 kept). Purely numeric names ("1"..."10") are
  treated as unnamed (10 cleared).
- Line features (roads, rail, power lines, simulated mains) are clipped to the study-area rectangle; when clipping
  leaves several parts the longest is kept; parts shorter than 5 m are dropped; `length_m` is the clipped length.
- Roads of class motorway, trunk, primary, secondary, tertiary, unclassified, residential and their `_link`s become
  assets (137); every highway way stays in the `roads` base-map table (314 rows).
- Contiguous bridge ways of the same class sharing an end node are merged into one bridge asset; a bridge way is
  never also a road asset.
- Kept tags: highway class, surface, lanes, maxspeed, building type, levels, amenity, operator, `addr:*`. Measures
  computed from the geometry (`length_m`, `footprint_m2`) are labelled as computed.

### FHWA National Bridge Inventory (`nbi`)

| | |
|---|---|
| What | Highway bridge and culvert records with their recorded attributes |
| Endpoint | `https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_National_Bridge_Inventory/FeatureServer/0/query` with the bbox envelope, `inSR=4326&outSR=4326&outFields=*&f=json`. The downloader checks that the expected field names exist and otherwise keeps the existing cache |
| File | `data/raw/nbi_bridges.json` (4 records) |
| Vintage | "data as of June 20, 2025" (service description); retrieved 2026-10-04T12:00:28Z |
| Licence | US Government work, unrestricted public use |
| Attribution | "FHWA National Bridge Inventory (data as of June 20, 2025), distributed by USDOT/BTS NTAD" |

Parsing rules (`pipeline/gis/nbi.py`): `STRUCTURE_NUMBER_008` is an opaque string; `DATE_OF_INSPECT_090` is MMYY
with the leading zero dropped ('223' = February 2023); `YEAR_RECONSTRUCTED_106` 0 means none; condition code `N` =
not applicable (culverts carry `CULVERT_COND_062` instead); ADT is always shown with its year; `BRIDGE_CONDITION`
G/F/P is labelled "Good / Fair / Poor (FHWA classification from the lowest component rating)" and is never
presented as a safety verdict; owner codes 01 = State Highway Agency, 04 = City or Municipal Highway Agency.

Coordinate check: the recorded `LAT_016` / `LONG_017` (DDMMSSss / DDDMMSSss) are converted to decimal degrees and
compared with the point geometry; more than 500 m apart means `location_check = 'mismatch'` and **no asset is
created**. Matching: the nearest merged highway bridge within 60 m; unmatched valid records become point assets.

Result for the default area:

| Structure number | Facility | Result |
|---|---|---|
| 406950290827010 | 2nd Avenue over the Arkansas River (built 1935, reconstructed 2001) | matched to BRG-001 (OSM ways 13068265 + 51849921) at 0.8 m |
| 406950290808003 | West Trail St. over Drainage Ditch | point asset BRG-003, culvert |
| 406950290826027 | Wyatt Earp Blvd over Arkansas River Drainage | point asset BRG-004, culvert |
| 999905600290641 | US-56 HWY over the Arkansas River | **rejected**: recorded coordinates (-99.979167, 37.733889) are 3,841 m from the point geometry (-100.019386, 37.747367), which lies next to the 2nd Avenue bridge |

The CVRR rail-spur bridge (BRG-002) comes from OSM only.

### U.S. Census Bureau TIGERweb (`tiger`)

| | |
|---|---|
| What | The Dodge City incorporated-place boundary (an 8-part MultiPolygon, much larger than the study area), drawn as a context outline ("City limits" toggle) |
| Endpoint | `https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4/query?where=GEOID='2018250'&outFields=*&outSR=4326&f=geojson`. `TIGER_PLACE_GEOID` selects another place, or `auto` for the place containing the study-area centre |
| File | `data/raw/city_boundary.geojson` (1 feature) |
| Vintage | "Incorporated Places; January 1, 2026 vintage"; retrieved 2026-10-04T12:00:30Z |
| Licence | US Government work, not subject to copyright |
| Attribution | "U.S. Census Bureau, TIGERweb". A statistical boundary, not a legal land description |

### USGS 3D Elevation Program lidar heights (`usgs_3dep`)

| | |
|---|---|
| What | One measured height per OSM building footprint |
| Source | USGS 3DEP lidar project USGS_LPC_KS_Area1_2014_LAS_2015, collected 2013-12-12 to 2014-01-22 (quality level 3), as 2 m Cloud Optimized GeoTIFFs hosted by the Microsoft Planetary Computer: collections `3dep-lidar-dsm` (surface) and `3dep-lidar-dtm-native` (terrain), read with an anonymous SAS token |
| Files | `data/raw/building_heights_3dep.csv` (`osm_id,height_m,n_pixels,lidar_project,collected`; 424 rows) and `building_heights_3dep.meta.json` (merged into `SOURCES.json` by stage 1) |
| Retrieved | 2026-10-04T11:59:36Z |
| Licence | US public domain; the Planetary Computer's own terms apply to the hosting service |
| Attribution | "U.S. Geological Survey, 3D Elevation Program" |

The CSV is produced offline by `tools/derive_building_heights.py` in a separate environment (rasterio/GDAL are not
part of the application). Stage 2 only joins it by `osm_id`; when it is absent nothing changes. Full tool
documentation: [tools/README.md](../tools/README.md).

**Method.** Height above ground = surface model minus terrain model, pixel by pixel (terrain gaps under large roofs
interpolated from the surrounding ground). For each footprint, the pixels whose centre lies inside the polygon are
selected; at least 4 valid pixels are required; the height is their 90th percentile; only values between 2.5 m and
60 m are kept, rounded to 0.1 m. The ready-made `3dep-lidar-hag` product was tried first and rejected for this area
because it reports tree canopy and masts as roof height and classifies the interior of large flat roofs as ground
(evidence in `tools/README.md`).

**Precedence** of `buildings.height_source`: `osm_height` (OSM `height` tag) > `lidar_3dep` > `osm_levels`
(`building:levels` x 3.6 m) > `estimated` (deterministic rule: house/residential/detached/cabin 5.0 m;
garage/shed/roof/pavilion 3.5 m; industrial/warehouse 9.0 m; storage_tank 10.0 m; otherwise 4.5 m below 400 m²,
6.0 m below 1,500 m², else 8.0 m). Default area: 419 `lidar_3dep`, 39 `estimated`, none from OSM tags.

**Caveats.**

- *Vintage.* The lidar predates later construction. Footprints with no structure in 2014 are normally rejected by the
  2.5 m rule, but where a new building overlaps an older structure a wrong value passes. Known case: **Holiday Inn
  Express & Suites** (way 1003769677): 6.0 m from an older structure under part of the footprint.
- *Parts of a complex.* A small footprint next to a much taller part can take the neighbour's height: **the annex of
  the First National Bank Building** (way 965214125, OSM `building:levels=1`, 17.8 m) and **a two-level part of the
  Ford County Government Center** (way 965217140, 17.8 m).
- *Resolution and registration.* 2 m pixels from a sparse point cloud; roof-surface heights (parapets and ridges can
  be 1-2 m higher); no shift is applied between OSM outlines and the raster (a one-pixel offset changes 3 of 421
  heights by more than 1 m).
- *Shape.* One number per footprint: flat-topped extrusions, not 3D building models. UI wording: "3D building
  extrusions derived from OSM footprints. Heights: measured from USGS 3DEP lidar (2013–14) where available, otherwise
  OSM tags, otherwise estimated. Not detailed 3D building models."

### Display-only services (not stored)

| source_id | Service | Credit |
|---|---|---|
| `basemap` | OpenFreeMap dark style `https://tiles.openfreemap.org/styles/dark` (`BASEMAP_STYLE_URL`) | "OpenFreeMap © OpenMapTiles Data from OpenStreetMap" |
| `imagery` | USGS The National Map `https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` (imagery toggle, hidden if its first tile fails) | "USDA, USGS The National Map: Orthoimagery" |

If the basemap cannot be fetched within 4 s the dashboard draws the project data on a plain background; the project
layers carry their own attribution so the credits survive.

## Simulated data (`simulator`, kind `simulated`)

| Data | Table | Default count |
|---|---|---|
| Simulated water mains: 4.5 m offsets of real street centre lines, only for the segments that host a pressure sensor; no attributes besides host road and length. Label "Simulated water network (not a record of real utilities)" | `infrastructure_assets` (`asset_type='water_main'`, `is_simulated=true`, `source_id='simulator'`) | 28 |
| Sensors | `sensors` (`is_simulated=true`, `source='simulator'`) | 128 |
| Hourly readings | `sensor_readings` (`source='simulator'`) | 92,028 |
| Ground truth: injected abnormal events (`is_anomaly=true`, with sensor and asset) and benign regional events (rain x 3, hot spell; `sensor_id` NULL) | `simulation_events` | 44 |

The simulated weather that drives the readings is never presented as observed weather. The City of Dodge City
publishes utility layers, but without a licence that permits reuse; they are deliberately not used.

## Derived data (`derived`, kind `derived`)

| Data | Table | Default count |
|---|---|---|
| Per-reading detector output (expected value, band, robust z, Isolation Forest score, flag) | `reading_scores` | 92,028 (1,449 flagged) |
| Anomalies (Prototype Anomaly Detection) | `anomalies` | 42 |
| Co-occurrence clusters | `anomaly_clusters` | 3 |
| Risk-zone cells and hourly scores | `risk_zones`, `risk_zone_scores` | 88 cells |
| Derived Asset Health Score per monitored asset and hour | `asset_health` | 112 assets x 720 hours |
| Rule-based building heights | `buildings` (`height_source='estimated'`) | 39 |

Methods: [anomaly-detection.md](anomaly-detection.md), [health-score.md](health-score.md),
[README section 9](../README.md#9-spatial-analysis-and-derived-asset-health-score).
