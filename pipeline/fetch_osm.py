"""Download OpenStreetMap base data (buildings, streets, rail, utilities) for the study area.

Results are cached to data/raw/osm.json so the pipeline can re-run offline.
"""
import json
import time
import urllib.parse
import urllib.request

from . import config


def _query():
    s, w, n, e = config.BBOX
    bb = f"({s},{w},{n},{e})"
    return f"""
[out:json][timeout:120];
(
  way["building"]{bb};
  way["highway"]{bb};
  way["railway"]{bb};
  node["emergency"="fire_hydrant"]{bb};
  node["highway"="street_lamp"]{bb};
  node["man_made"~"water_tower|storage_tank"]{bb};
  way["man_made"~"water_tower|storage_tank|wastewater_plant"]{bb};
  node["power"~"substation|transformer"]{bb};
  way["power"~"substation|line|minor_line"]{bb};
);
out geom tags;
"""


def fetch(force=False):
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = config.RAW_DIR / "osm.json"
    if cache.exists() and not force:
        print(f"  using cached OSM data: {cache.name}")
        return json.loads(cache.read_text(encoding="utf-8"))

    body = urllib.parse.urlencode({"data": _query()}).encode()
    last_err = None
    for url in config.OVERPASS_ENDPOINTS:
        for attempt in range(2):
            try:
                print(f"  querying {url} (attempt {attempt + 1})")
                req = urllib.request.Request(url, data=body, headers={"User-Agent": config.USER_AGENT})
                with urllib.request.urlopen(req, timeout=150) as r:
                    data = json.loads(r.read())
                cache.write_text(json.dumps(data), encoding="utf-8")
                print(f"  downloaded {len(data.get('elements', []))} OSM elements")
                return data
            except Exception as exc:  # network errors, 429/504 from busy mirrors
                last_err = exc
                time.sleep(3)
    print(f"  WARNING: OSM download failed ({last_err}); falling back to synthetic street grid")
    return None
