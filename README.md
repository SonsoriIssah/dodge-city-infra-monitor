# Dodge City 3D Urban Infrastructure Monitoring & GeoAI Dashboard

**Live demo:** https://sonsoriissah.github.io/dodge-city-infra-monitor/

This is a working prototype for monitoring urban infrastructure in downtown **Dodge City, Kansas**. It brings together GIS base data, 3D visualization, a spatial database, Python automation, simulated IoT telemetry and GeoAI anomaly detection in one interactive dashboard.

```
OpenStreetMap ─► asset inventory ─► IoT simulator ─► GeoAI detection ─► SQLite / PostGIS ─► 3D dashboard
 (Overpass API)   (buildings, roads,   (5 sensor types,   (seasonal robust-z,      (R*Tree index,     (MapLibre GL,
                   rail + derived       hourly, 14 days,   Isolation Forest,         views for          extruded buildings,
                   water network)       injected faults)   spatial consensus, Gi*)   capital planning)  time playback)
```

## Quick start

The core pipeline uses **only the Python standard library** (Python 3.9 or later). You don't need to install numpy or pandas.

```bash
python run_pipeline.py            # ~3 s; downloads OSM once, then uses data/raw/osm.json
python run_pipeline.py --refresh  # re-download OpenStreetMap data
python -m http.server 8765 --directory dashboard
```

Then open <http://localhost:8765>.

To load the data into PostGIS (optional):

```bash
pip install "psycopg[binary]"
python -m pipeline.load_postgis postgresql://user:pass@localhost:5432/infra
```

## What's in the box

| Path | Purpose |
|---|---|
| `pipeline/fetch_osm.py` | Overpass API ingest (buildings, streets, rail, hydrants, lamps, utilities), with mirror fallback and a local cache |
| `pipeline/build_assets.py` | Builds the asset inventory. Utility networks that OSM doesn't map (water mains, hydrants, streetlights) are derived from the street network. Places 154 sensors using farthest-point sampling so they cover the whole area |
| `pipeline/simulate_iot.py` | Hourly telemetry with daily and weekly cycles, communication dropouts and **11 fault types** (main break, lamp outage, day-burner, settlement, road closure…). Includes one zone-wide pump trip and one regional dust event |
| `pipeline/geoai.py` | Detection and analytics (see below) |
| `pipeline/database.py` | SQLite spatial store with an R*Tree bbox index |
| `sql/postgis_schema.sql` | Production PostGIS schema with GiST indexes and analysis views (`v_buildings_at_risk`, `v_main_replacement_priority`) |
| `dashboard/` | Static 3D web dashboard (MapLibre GL JS; no build step and no API keys) |

## GeoAI method

1. **Seasonal decomposition.** For each sensor, a robust hour-of-day profile (weekday and weekend separately for traffic) gives the *expected* value.
2. **Robust z-scores.** The residual is divided by 1.4826·MAD. Streetlight power and traffic use relative residuals because their noise grows with the signal level.
3. **Isolation Forest.** Written from scratch, one forest per sensor type, over the features [z, Δz, 3-hour mean, 3-hour std].
4. **Spatial consensus.** If ≥50% of sensors in the same network zone deviate in the same hour, the incident is labelled **area-wide**, for example a pump station trip or a regional dust event. Otherwise it is a **local asset fault**. This keeps one supply problem from raising 20 separate "leak" work orders.
5. **Root-cause diagnosis.** The signature of each incident (sign, duration, day or night, scope) maps to a diagnosis and a recommended action.
6. **Asset risk.** Risk = 45% condition + 25% criticality + 30% anomaly burden.
7. **Hot-spot analysis.** Getis-Ord Gi* runs on a 130 m grid of asset risk. Cells with z ≥ 1.96 are statistically significant clusters.

### Current results (seed 42)

The simulator knows which faults it injected, so detection is scored against that ground truth:

| Metric | Value |
|---|---|
| Fault events detected (recall) | **100%** |
| Incident precision | **99%** |
| Root-cause diagnosis accuracy | **89%** |
| Zone-wide pump trip classified as area-wide | ✓ |

These numbers come from simulated data, so they show that the method works end to end. They don't predict how it will perform on real sensors.

## Dashboard features

- 3D extruded buildings, colored by **risk**, **condition** or **age**. Click any asset to see its attributes.
- Streets colored by pavement condition (PCI), water mains colored by risk class, and the BNSF rail line.
- IoT sensors drawn as 3D columns. Column height is the anomaly z-score, and color shows status (normal, warning, alarm or offline).
- Pulsing markers for active incidents: red for local faults, blue for area-wide events.
- A **time slider with playback** covering 14 days of hourly data. Its histogram shows incidents per hour.
- A priority-ranked incident feed with type filters. Clicking an incident flies the map to it and opens a sensor chart (observed vs expected, with flagged windows shaded) and a recommended action.
- Model performance panel, Gi* hot-spot layer and a satellite basemap toggle.

## Data honesty

Building footprints, streets and rail come from **OpenStreetMap** (© OSM contributors, ODbL). Water mains, hydrants, streetlights, all attribute values (condition, install year, material) and all telemetry are **simulated** and marked `synthetic: true` / `source: derived`. Building heights are estimated when OSM has no height or level tags (`height_estimated: true`).

## Extending to production

- Replace the simulator with live feeds (MQTT or HTTP into a `readings` hypertable, using TimescaleDB on PostGIS).
- Load the City/County GIS layers (utility network, parcels, pavement management) in place of the derived assets.
- Swap MapLibre for CesiumJS or the ArcGIS Maps SDK if you need 3D Tiles or a LiDAR-derived terrain and building mesh.
- Schedule `run_pipeline.py` (cron or Airflow) and serve the data through a small FastAPI layer instead of `data.js`.
