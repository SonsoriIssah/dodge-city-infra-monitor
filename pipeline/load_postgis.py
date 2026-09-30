"""Optional: copy the SQLite store into PostGIS.

    pip install "psycopg[binary]"
    python -m pipeline.load_postgis postgresql://user:pass@host:5432/dbname
"""
import json
import sqlite3
import sys

from . import config


def main(dsn):
    import psycopg  # imported lazily so the core pipeline stays dependency-free

    src = sqlite3.connect(config.DB_PATH)
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute((config.ROOT / "sql" / "postgis_schema.sql").read_text(encoding="utf-8"))
        for r in src.execute("SELECT asset_id, asset_type, name, condition, criticality, risk, risk_class,"
                             " synthetic, props, geojson FROM assets"):
            cur.execute("INSERT INTO infra.assets VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                        " ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))", (*r[:7], bool(r[7]), r[8], r[9]))
        for r in src.execute("SELECT sensor_id, sensor_type, unit, asset_id, install_date, battery_pct, lon, lat FROM sensors"):
            cur.execute("INSERT INTO infra.sensors VALUES (%s,%s,%s,%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326))", r)
        with cur.copy("COPY infra.readings FROM STDIN") as cp:
            for r in src.execute("SELECT * FROM readings"):
                cp.write_row(r)
        for r in src.execute("SELECT * FROM incidents"):
            cur.execute("INSERT INTO infra.incidents VALUES (" + ",".join(["%s"] * 16) +
                        ", ST_SetSRID(ST_MakePoint(%s,%s),4326))", r)
        for r in src.execute("SELECT * FROM injected_faults"):
            cur.execute("INSERT INTO infra.injected_faults VALUES (%s,%s,%s,%s,%s,%s)", r)
        for r in src.execute("SELECT i, j, mean_risk, n_assets, gi_z, class, polygon FROM hotspots"):
            gj = json.dumps({"type": "Polygon", "coordinates": [json.loads(r[6])]})
            cur.execute("INSERT INTO infra.hotspots VALUES (%s,%s,%s,%s,%s,%s, ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))",
                        (*r[:6], gj))
        pg.commit()
    print("PostGIS load complete.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
