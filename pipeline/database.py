"""Persist the asset inventory, sensor network, telemetry and incidents.

Default backend is a zero-install SQLite file with GeoJSON geometry + a bounding-box
index table (R*Tree), so spatial window queries work out of the box. The same
schema is available for PostGIS in sql/postgis_schema.sql (see load_postgis.py).
"""
import json
import sqlite3

from . import config

SCHEMA = """
DROP TABLE IF EXISTS assets;      DROP TABLE IF EXISTS asset_rtree;
DROP TABLE IF EXISTS sensors;     DROP TABLE IF EXISTS readings;
DROP TABLE IF EXISTS incidents;   DROP TABLE IF EXISTS injected_faults;
DROP TABLE IF EXISTS hotspots;

CREATE TABLE assets (
  asset_id TEXT PRIMARY KEY, asset_type TEXT NOT NULL, name TEXT,
  geom_type TEXT, geojson TEXT NOT NULL, props TEXT NOT NULL,
  condition REAL, criticality INTEGER, risk REAL, risk_class TEXT, synthetic INTEGER
);
CREATE VIRTUAL TABLE asset_rtree USING rtree(id, min_x, max_x, min_y, max_y);
CREATE TABLE sensors (
  sensor_id TEXT PRIMARY KEY, sensor_type TEXT, unit TEXT, asset_id TEXT REFERENCES assets(asset_id),
  lon REAL, lat REAL, install_date TEXT, battery_pct INTEGER
);
CREATE TABLE readings (
  sensor_id TEXT, ts TEXT, value REAL, expected REAL, robust_z REAL, iforest_score REAL,
  PRIMARY KEY (sensor_id, ts)
) WITHOUT ROWID;
CREATE TABLE incidents (
  incident_id TEXT PRIMARY KEY, sensor_id TEXT, asset_id TEXT, sensor_type TEXT,
  start_ts TEXT, end_ts TEXT, hours INTEGER, diagnosis TEXT, description TEXT, scope TEXT,
  severity REAL, priority INTEGER, peak_value REAL, expected REAL, peak_z REAL,
  impacted_buildings INTEGER, lon REAL, lat REAL
);
CREATE TABLE injected_faults (
  sensor_id TEXT, asset_id TEXT, kind TEXT, start_ts TEXT, end_ts TEXT, severity REAL
);
CREATE TABLE hotspots (i INTEGER, j INTEGER, mean_risk REAL, n_assets INTEGER, gi_z REAL, class TEXT, polygon TEXT);
CREATE INDEX idx_readings_ts ON readings(ts);
CREATE INDEX idx_assets_type ON assets(asset_type);
"""


def _bbox(geom):
    def walk(c):
        if isinstance(c[0], (int, float)):
            yield c
        else:
            for x in c:
                yield from walk(x)
    pts = list(walk(geom["coordinates"]))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def save(assets, sensors, times, readings, expected, z, iso, incidents, events, hot):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.executescript(SCHEMA)
    for rowid, a in enumerate(assets, 1):
        p = a["props"]
        con.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (a["id"], a["type"], a["name"], a["geometry"]["type"], json.dumps(a["geometry"]),
                     json.dumps(p), p.get("condition", p.get("pci")), p.get("criticality"),
                     p.get("risk"), p.get("risk_class"), int(p.get("synthetic", False))))
        con.execute("INSERT INTO asset_rtree VALUES (?,?,?,?,?)", (rowid, *_bbox(a["geometry"])))
    con.executemany("INSERT INTO sensors VALUES (?,?,?,?,?,?,?,?)",
                    [(s["id"], s["type"], s["unit"], s["asset_id"], s["lon"], s["lat"],
                      s["install_date"], s["battery_pct"]) for s in sensors])
    rows = []
    for s in sensors:
        sid = s["id"]
        for i, t in enumerate(times):
            rows.append((sid, t, readings[sid][i], round(expected[sid][i], 3), z[sid][i], iso[sid][i]))
    con.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(i["id"], i["sensor_id"], i["asset_id"], i["sensor_type"], i["start"], i["end"], i["hours"],
                      i["diagnosis"], i["description"], i["scope"], i["severity"], i["priority"], i["peak_value"],
                      i["expected"], i["peak_z"], i["impacted_buildings"], i["lon"], i["lat"]) for i in incidents])
    con.executemany("INSERT INTO injected_faults VALUES (?,?,?,?,?,?)",
                    [(e["sensor_id"], e["asset_id"], e["kind"], e["start"], e["end"], e["severity"]) for e in events])
    con.executemany("INSERT INTO hotspots VALUES (?,?,?,?,?,?,?)",
                    [(h["i"], h["j"], h["mean_risk"], h["n_assets"], h["gi_z"], h["class"], json.dumps(h["polygon"]))
                     for h in hot])
    con.commit()
    con.close()
    print(f"  wrote {config.DB_PATH.relative_to(config.ROOT)} ({len(rows):,} readings)")
