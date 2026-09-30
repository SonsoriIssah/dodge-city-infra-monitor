"""Bundle pipeline outputs into dashboard/data/data.js (works from file:// – no server needed)."""
import json
from collections import Counter
from datetime import datetime, timezone

from . import config


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def export(assets, sensors, times, readings, expected, z, iso, incidents, metrics, events, hot, source):
    features = [{"type": "Feature", "id": i, "geometry": a["geometry"],
                 "properties": {"id": a["id"], "type": a["type"], "name": a["name"],
                                **{k: v for k, v in a["props"].items() if not isinstance(v, (dict, list))}}}
                for i, a in enumerate(assets)]
    sensor_rows = []
    for s in sensors:
        sid = s["id"]
        sensor_rows.append({**s, "values": [_r(v) for v in readings[sid]],
                            "expected": [_r(v) for v in expected[sid]],
                            "z": [_r(v, 1) for v in z[sid]]})
    summary = {
        "asset_counts": Counter(a["type"] for a in assets),
        "sensor_counts": Counter(s["type"] for s in sensors),
        "incident_counts": Counter(i["diagnosis"] for i in incidents),
        "high_risk_assets": sum(1 for a in assets if a["props"].get("risk_class") == "high"),
        "avg_pci": round(sum(a["props"]["pci"] for a in assets if a["type"] == "road") /
                         max(1, sum(1 for a in assets if a["type"] == "road")), 1),
    }
    payload = {
        "meta": {"title": "Dodge City, KS – Urban Infrastructure Monitoring", "source": source,
                 "bbox": config.BBOX, "center": config.CENTER,
                 "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "times": times},
        "assets": {"type": "FeatureCollection", "features": features},
        "sensors": sensor_rows, "incidents": incidents, "metrics": metrics,
        "ground_truth": events, "hotspots": hot, "summary": summary,
    }
    config.DASHBOARD_DATA.parent.mkdir(parents=True, exist_ok=True)
    text = "window.INFRA_DATA = " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n"
    config.DASHBOARD_DATA.write_text(text, encoding="utf-8")
    print(f"  wrote {config.DASHBOARD_DATA.relative_to(config.ROOT)} ({len(text) / 1e6:.1f} MB)")
