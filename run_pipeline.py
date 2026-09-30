"""End-to-end pipeline: OSM ingest → asset inventory → IoT simulation → GeoAI → DB → dashboard.

    python run_pipeline.py            # uses cached OSM data if present
    python run_pipeline.py --refresh  # re-download OpenStreetMap data
"""
import json
import sys
import time

from pipeline import build_assets, config, database, export_dashboard, fetch_osm, geoai, simulate_iot


def main():
    t0 = time.time()
    print("[1/6] Fetching OpenStreetMap base data")
    osm = fetch_osm.fetch(force="--refresh" in sys.argv)

    print("[2/6] Building asset inventory + sensor network")
    assets, sensors, source = build_assets.build(osm)

    print("[3/6] Simulating IoT telemetry")
    times, readings, labels, events = simulate_iot.simulate(assets, sensors)

    print("[4/6] Running GeoAI anomaly detection")
    incidents, metrics, z, expected, iso = geoai.detect(assets, sensors, times, readings, labels, events)
    geoai.score_assets(assets, sensors, incidents)
    hot = geoai.hotspots(assets)
    print(f"  {len(incidents)} incidents | event recall {metrics['event_recall']:.0%} | "
          f"incident precision {metrics['incident_precision']:.0%} | "
          f"diagnosis accuracy {metrics['diagnosis_accuracy']:.0%} | "
          f"zone event caught: {metrics['zone_event_detected']}")
    print(f"  {sum(1 for h in hot if h['class'] == 'hot')} Gi* hot-spot cells")

    print("[5/6] Writing spatial database")
    database.save(assets, sensors, times, readings, expected, z, iso, incidents, events, hot)

    print("[6/6] Exporting dashboard data")
    export_dashboard.export(assets, sensors, times, readings, expected, z, iso, incidents, metrics, events, hot, source)
    (config.DATA_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Done in {time.time() - t0:.1f}s -> open dashboard/index.html")


if __name__ == "__main__":
    main()
