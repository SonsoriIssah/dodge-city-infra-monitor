"""Project-wide configuration for the Dodge City infrastructure prototype."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "dodge_city_infra.sqlite"
DASHBOARD_DATA = ROOT / "dashboard" / "data" / "data.js"

# Study area: downtown Dodge City, KS (Wyatt Earp Blvd / Central Ave corridor).
# (south, west, north, east) in WGS84 degrees, Overpass order.
BBOX = (37.745, -100.030, 37.762, -100.005)
CENTER = (37.7528, -100.0171)  # lat, lon

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "DodgeCityInfraPrototype/0.1 (GIS research prototype)"

# Simulation window
SIM_START = "2026-09-01T00:00:00"
SIM_HOURS = 24 * 14  # two weeks of hourly telemetry
RANDOM_SEED = 42

# Asset generation parameters
STREETLIGHT_SPACING_M = 70
HYDRANT_SPACING_M = 150
MAX_SENSORS_PER_TYPE = 45

# GeoAI parameters
IFOREST_TREES = 80
IFOREST_SAMPLE = 256
ANOMALY_SCORE_THRESHOLD = 0.62
ROBUST_Z_THRESHOLD = 4.0
SPATIAL_NEIGHBOR_RADIUS_M = 400
