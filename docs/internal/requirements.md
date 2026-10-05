# Requirements checklist (R1–R23) — the acceptance standard for this prototype

Project: **3D Urban Infrastructure Monitoring & GeoAI Dashboard**, for a selected area of **Dodge City, Kansas, USA**.
Combines: GIS data, 3D urban visualization, PostgreSQL/PostGIS, Python data processing, simulated IoT/sensor data,
GeoAI anomaly detection, interactive infrastructure monitoring, spatial analysis.
Objective: a working prototype demonstrating how urban infrastructure assets can be monitored spatially and analyzed
using GIS, 3D visualization, automated data processing and anomaly detection. It should look like a serious
research/consulting prototype that could be demonstrated to a client.

Existing repo (starting point, do NOT unnecessarily rewrite working components, build on it wherever practical):
https://github.com/SonsoriIssah/dodge-city-infra-monitor — live at https://sonsoriissah.github.io/dodge-city-infra-monitor/

PRIMARY GOAL: GIS data → PostGIS → Python processing → simulated sensors → anomaly detection → spatial analysis →
API/data layer → interactive 3D dashboard. One coherent application, not disconnected demos.

## R1. GIS DATA
Use real publicly available geographic data for Dodge City where practical; prioritize authoritative/reputable open datasets.
At minimum: building footprints, roads/streets, relevant infrastructure/urban assets, sensor locations, study-area boundary.
If external datasets are unavailable, document the limitation clearly.
Do NOT fabricate real-world infrastructure attributes and present them as factual.
Clearly distinguish REAL DATA (geographic coordinates, building footprints, roads, publicly available geographic features)
from SIMULATED/DERIVED DATA (sensor readings, sensor status, infrastructure health scores, anomaly events, readings not
available from authoritative sources, estimated building heights if not sourced).
Create a clear data provenance section in the documentation.

## R2. STUDY AREA
A manageable study area within Dodge City with enough buildings, roads, infrastructure assets, sensors to be visually
meaningful. Make the study area configurable.

## R3. POSTGRESQL + POSTGIS
Proper spatial database. Tables such as: buildings, infrastructure_assets, roads, sensors, sensor_readings, anomalies,
asset_health, study_areas. Appropriate geometry/geography types, SRIDs, spatial indexes, normal indexes, foreign keys,
constraints. Relationships: infrastructure_asset → sensor → sensor_reading → anomaly; infrastructure_asset → asset_health.
Useful spatial queries: sensors within an infrastructure area; infrastructure near an anomaly; assets within a selected
radius; anomaly density; nearest infrastructure asset; spatial aggregation. Provide SQL initialization scripts/migrations.

## R4. 3D CITY VISUALIZATION
Polished interactive 3D map (MapLibre GL JS / Mapbox GL JS / CesiumJS; prefer what the repo already uses).
Display: buildings, roads, infrastructure assets, sensors, anomaly locations, risk areas. Buildings as 3D extrusions at minimum.
Do not claim extruded OSM footprints are detailed 3D building models. If real 3D/LiDAR/3D Tiles data is available and
legally usable, structure the system so it can be incorporated; otherwise clearly label the visualization as 3D building
extrusions derived from building footprints.

## R5. INFRASTRUCTURE ASSETS
Meaningful categories (buildings, roads, bridges where available, utility-related assets where reliable data exists,
public infrastructure). Do not invent real infrastructure locations. Simulated assets clearly labelled as simulated.
Each asset: unique ID, asset type, location, status, health score, sensor count, latest readings, anomaly count.

## R6. SENSOR SIMULATION
Sensor types: Temperature, Vibration, Moisture, Pressure. Historical readings over time. Each sensor: sensor ID, asset ID,
sensor type, latitude/longitude, timestamp, value, unit, status. 14–30 days of hourly readings. Deterministic with a
configurable random seed.

## R7. REALISTIC SENSOR BEHAVIOR
Not random noise. Temperature: daily variation, gradual weather-like changes, occasional spikes. Vibration: baseline,
occasional elevated activity, sudden spikes. Moisture: gradual changes, occasional increases, localized abnormal events.
Pressure: normal operating range, gradual changes, occasional abnormal spikes/drops. Configurable thresholds.

## R8. ANOMALY SIMULATION
Inject a controlled number of abnormal events (sustained high vibration, sudden vibration spike, unusual moisture increase,
pressure anomaly, abnormal temperature pattern). Store these events in the database. Distinguish normal readings from
potential anomalies. Do NOT make every unusual reading an anomaly.

## R9. GEOAI / ANOMALY DETECTION
An actual anomaly-detection pipeline; combination of approaches (rolling statistics, z-score/robust z-score, IQR,
Isolation Forest, threshold-based, spatial clustering, spatial anomaly analysis). Explainable. For each anomaly:
anomaly ID, sensor ID, asset ID, timestamp, sensor type, observed value, expected/baseline value, anomaly score,
severity, detection method, explanation (e.g. "High vibration detected 3.2 standard deviations above the sensor baseline.").
Not a certified infrastructure safety system — a prototype.

## R10. SPATIAL ANALYSIS
Proximity analysis (assets close to anomalous sensors); anomaly density; spatial clustering of abnormal events;
risk zones (spatial risk layer from anomaly concentration/severity); asset health (simple score from anomaly frequency,
anomaly severity, recent readings, sensor status). Document exactly how the score is calculated.

## R11. DASHBOARD
Main 3D map (large, central): 3D buildings, roads, assets, sensors, anomalies, risk zones; clear visual distinction
between normal and anomalous assets.
Statistics cards: Total Assets, Active Sensors, Active Anomalies, Critical Alerts, Assets at Risk — from application
data, never hardcoded.
Sensor monitoring: select sensor type, select asset, inspect historical readings, view latest reading, view trend.
Asset details (on selecting an asset): asset ID, asset type, location, current health score, sensor count, latest
readings, anomaly count, recent anomalies, historical sensor chart.
Anomaly panel: anomaly ID, asset, sensor, type, severity, timestamp, value, anomaly score, explanation. Filter by:
severity, sensor type, date, asset, anomaly status.

## R12. TIME PLAYBACK
[◀] [▶] [Play] with a timeline/date selector. As time changes: sensor values, anomalies, asset status and map
indicators update.

## R13. API / DATA LAYER
Python, FastAPI, PostgreSQL, PostGIS. Endpoints: GET /assets, GET /assets/{id}, GET /sensors, GET /sensors/{id},
GET /sensor-readings, GET /anomalies, GET /anomalies/{id}, GET /health, GET /statistics. Proper validation and error
handling. Do not expose DB credentials; use environment variables.

## R14. FUTURE REAL SENSOR INTEGRATION
Architect the sensor pipeline so the SIMULATED SENSOR SOURCE can later be replaced with API / MQTT / IoT platform /
real sensor gateway without rebuilding the dashboard. Separate: data generation, data ingestion, data processing,
database storage, API, frontend visualization. Simulated data is simply the current data source.

## R15. DOCKER
docker-compose.yml with PostgreSQL/PostGIS and backend. Frontend may remain separately deployable. `.env.example`.
Never commit credentials.

## R16. TESTING
pytest. At minimum: sensor generation, anomaly detection, health-score calculation, important API endpoints, database
operations where practical. Test instructions in README.

## R17. DATA PIPELINE
Reproducible scripts such as scripts/download_data.py, process_data.py, generate_sensors.py, detect_anomalies.py,
seed_database.py (structure may differ if the repo has a better architecture). Another developer must be able to
reproduce the dataset and processing pipeline.

## R18. DOCUMENTATION
README: 1 overview, 2 architecture, 3 technology stack, 4 data sources, 5 data provenance, 6 database schema,
7 sensor simulation, 8 anomaly detection, 9 spatial analysis, 10 running locally, 11 Docker setup,
12 environment variables, 13 API endpoints, 14 testing, 15 limitations, 16 future real-sensor integration.
Architecture diagram if practical.

## R19. PROFESSIONAL QUALITY
Not a tutorial project. Clean architecture, readable code, reusable functions, proper error handling, meaningful
naming, responsive UI, useful loading states, empty states, error states, sensible visual hierarchy, no unnecessary
animations, no fake statistics, no hardcoded dashboard numbers.

## R20. SECURITY
Never hardcode DB passwords, API keys, tokens, private credentials. Environment variables. `.env.example` with placeholders.

## R21. DEPLOYMENT
Clear instructions for local development, Docker, database initialization, frontend deployment, backend deployment.
Preserve the existing deployment platform (GitHub Pages for the dashboard) where practical.

## R22. DATA HONESTY
Never falsely claim real-time infrastructure monitoring, real sensor data, certified infrastructure safety, actual
structural failure prediction, or authoritative infrastructure condition when simulated. Label simulated components in
the UI and docs: "Simulated Sensor Data", "Prototype Anomaly Detection", "Derived Asset Health Score".

## R23. FINAL CLIENT DEMONSTRATION EXPERIENCE
On start the user immediately understands WHAT (3D Urban Infrastructure Monitoring), WHERE (Dodge City, Kansas),
WHAT IS BEING MONITORED (urban infrastructure assets and simulated sensors), WHAT IS HAPPENING (sensor readings are
analyzed for anomalies), WHAT NEEDS ATTENTION (assets with elevated anomaly/risk scores). Understandable by someone
unfamiliar with the code within 1–2 minutes.

## FINAL OUTPUT
1 complete working source; 2 DB schema/migrations; 3 data-generation scripts; 4 GIS processing scripts; 5 anomaly
detection; 6 backend/API; 7 frontend/dashboard; 8 tests; 9 Docker config; 10 `.env.example`; 11 complete README;
12 clear local run instructions; 13 summary of what was reused; 14 summary of what was newly implemented; 15 remaining
limitations. Real implementation, not pseudocode. If ambiguous: the simplest technically sound implementation that
produces a polished, demonstrable prototype while preserving the existing architecture.
