"""Turn OSM base data into an infrastructure asset inventory + an IoT sensor network.

Real geometry (buildings, streets, rail) comes from OpenStreetMap. Buried/unmapped
utility assets (water mains, hydrants, streetlights) are *derived* from the street
network the way municipal utilities are usually laid out, and given plausible
attributes (material, install year, condition). Everything synthetic is flagged
with `synthetic: true` so it is never confused with authoritative data.
"""
import math
import random

from . import config, geo

DRIVABLE = {
    "motorway": 5, "trunk": 5, "primary": 4, "secondary": 4, "tertiary": 3,
    "unclassified": 2, "residential": 2, "service": 1, "living_street": 1,
    "motorway_link": 4, "trunk_link": 4, "primary_link": 3, "secondary_link": 3, "tertiary_link": 2,
}
AADT_BY_CLASS = {5: 14000, 4: 9000, 3: 4500, 2: 1200, 1: 300}
CRITICAL_BUILDINGS = {"hospital": 5, "school": 4, "fire_station": 5, "police": 5, "government": 4,
                      "civic": 4, "public": 4, "church": 3, "commercial": 3, "retail": 3,
                      "industrial": 3, "warehouse": 2, "apartments": 3, "house": 2, "residential": 2}


def _levels(tags, area, rng):
    if "height" in tags:
        try:
            return float(str(tags["height"]).split()[0]), False
        except ValueError:
            pass
    if "building:levels" in tags:
        try:
            return float(tags["building:levels"]) * 3.6, False
        except ValueError:
            pass
    btype = tags.get("building", "yes")
    if btype in ("house", "residential", "garage", "shed", "detached"):
        return rng.uniform(4, 7), True
    if btype in ("industrial", "warehouse", "grain_silo", "silo"):
        return rng.uniform(8, 18), True
    # Downtown commercial blocks: 1-3 storeys, larger footprints trend taller
    storeys = 1 + min(3, int(area / 900)) + (1 if rng.random() < 0.25 else 0)
    return storeys * 4.0, True


def _condition_from_age(age, rng, spread=12):
    base = 100 - age * 1.1
    return max(5, min(100, round(base + rng.gauss(0, spread), 1)))


def _material(year):
    if year < 1960:
        return "Cast Iron"
    if year < 1990:
        return "Ductile Iron"
    return "PVC C900"


def _synthetic_street_grid(rng):
    """Fallback street grid centred on downtown if OSM cannot be reached."""
    lat0, lon0 = config.CENTER
    ways = []
    dlat, dlon = 0.0018, 0.0023
    for i in range(-4, 5):
        lat = lat0 + i * dlat
        ways.append({"type": "way", "id": 900000 + i, "tags": {"highway": "primary" if i == 0 else "residential",
                     "name": "Wyatt Earp Blvd" if i == 0 else f"Street {i + 5}"},
                     "geometry": [{"lat": lat, "lon": lon0 + j * dlon} for j in range(-5, 6)]})
        lon = lon0 + i * dlon
        ways.append({"type": "way", "id": 910000 + i, "tags": {"highway": "secondary" if i == 0 else "residential",
                     "name": "Central Ave" if i == 0 else f"Avenue {i + 5}"},
                     "geometry": [{"lat": lat0 + j * dlat, "lon": lon} for j in range(-4, 5)]})
    for i in range(-4, 4):
        for j in range(-5, 5):
            for k in range(rng.randint(2, 5)):
                cx = lon0 + (j + 0.2 + 0.6 * rng.random()) * dlon
                cy = lat0 + (i + 0.2 + 0.6 * rng.random()) * dlat
                w, h = rng.uniform(0.00008, 0.0002), rng.uniform(0.00006, 0.00015)
                ring = [(cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h), (cx - w, cy - h)]
                ways.append({"type": "way", "id": 800000 + i * 1000 + j * 10 + k,
                             "tags": {"building": rng.choice(["commercial", "house", "retail", "yes"])},
                             "geometry": [{"lon": x, "lat": y} for x, y in ring]})
    return {"elements": ways}


def build(osm):
    rng = random.Random(config.RANDOM_SEED)
    source = "OpenStreetMap"
    if osm is None:
        osm = _synthetic_street_grid(rng)
        source = "synthetic"

    assets = []
    now_year = int(config.SIM_START[:4])

    def add(kind, geometry, name, props, synthetic):
        aid = f"{kind[:3].upper()}-{sum(1 for a in assets if a['type'] == kind) + 1:04d}"
        assets.append({"id": aid, "type": kind, "name": name, "geometry": geometry,
                       "props": {**props, "synthetic": synthetic, "source": "derived" if synthetic else source}})
        return assets[-1]

    roads = []
    for el in osm["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "way" and "geometry" in el:
            coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if "building" in tags and len(coords) >= 4:
                area = geo.polygon_area_m2(coords[:-1])
                if area < 25:
                    continue
                height, est = _levels(tags, area, rng)
                year = int(tags.get("start_date", "0")[:4] or 0) or rng.choice(
                    [rng.randint(1885, 1930), rng.randint(1930, 1975), rng.randint(1975, 2020)])
                btype = tags.get("building", "yes")
                if btype == "yes":
                    btype = tags.get("amenity") or tags.get("shop") and "retail" or "commercial"
                add("building", {"type": "Polygon", "coordinates": [geo.round_coords(coords)]},
                    tags.get("name", ""),
                    {"osm_id": el["id"], "building_type": btype, "height_m": round(height, 1),
                     "height_estimated": est, "footprint_m2": round(area), "year_built": year,
                     # historic stock is periodically renovated: condition follows effective age
                     "condition": _condition_from_age(min(now_year - year, rng.randint(8, 70)), rng, 12),
                     "criticality": CRITICAL_BUILDINGS.get(btype, 2)}, False)
            elif tags.get("highway") in DRIVABLE and len(coords) >= 2:
                cls = DRIVABLE[tags["highway"]]
                length = geo.line_length(coords)
                year = rng.randint(1995, 2024)
                a = add("road", {"type": "LineString", "coordinates": geo.round_coords(coords)},
                        tags.get("name", tags["highway"].title()),
                        {"osm_id": el["id"], "highway": tags["highway"], "road_class": cls,
                         "lanes": int(tags.get("lanes", 2 if cls < 4 else 4)) if str(tags.get("lanes", "2")).isdigit() else 2,
                         "surface": tags.get("surface", "asphalt"), "length_m": round(length, 1),
                         "last_resurfaced": year, "pci": _condition_from_age((now_year - year) * 2.2, rng, 10),
                         "aadt": int(AADT_BY_CLASS[cls] * rng.uniform(0.7, 1.3)), "criticality": cls}, False)
                roads.append(a)
            elif "railway" in tags and tags["railway"] in ("rail", "siding", "spur", "yard"):
                add("rail", {"type": "LineString", "coordinates": geo.round_coords(coords)},
                    tags.get("name", "BNSF Railway"),
                    {"osm_id": el["id"], "railway": tags["railway"], "operator": tags.get("operator", "BNSF"),
                     "length_m": round(geo.line_length(coords), 1), "criticality": 4}, False)
        elif el["type"] == "node":
            if tags.get("emergency") == "fire_hydrant":
                add("hydrant", {"type": "Point", "coordinates": [el["lon"], el["lat"]]}, "Hydrant",
                    {"osm_id": el["id"], "flow_gpm": rng.choice([500, 750, 1000, 1500]), "criticality": 4}, False)
            elif tags.get("highway") == "street_lamp":
                add("streetlight", {"type": "Point", "coordinates": [el["lon"], el["lat"]]}, "Streetlight",
                    {"osm_id": el["id"], "lamp": "LED", "wattage": 90, "criticality": 2}, False)

    # --- Derived utility network -------------------------------------------------
    osm_lamps = sum(1 for a in assets if a["type"] == "streetlight")
    for r in roads:
        cls = r["props"]["road_class"]
        coords = r["geometry"]["coordinates"]
        if cls >= 2 and r["props"]["length_m"] > 40:
            year = rng.choice([rng.randint(1925, 1959), rng.randint(1960, 1989), rng.randint(1990, 2022)])
            age = now_year - year
            material = _material(year)
            breaks = max(0, int(rng.gauss(age / 25 if material == "Cast Iron" else age / 60, 1)))
            main = add("water_main", {"type": "LineString", "coordinates": geo.round_coords(geo.offset_line(coords, 4.5))},
                       f"Main under {r['name']}",
                       {"road_id": r["id"], "diameter_in": {5: 16, 4: 12, 3: 10, 2: 8}.get(cls, 6),
                        "material": material, "install_year": year, "length_m": r["props"]["length_m"],
                        "break_history": breaks, "condition": _condition_from_age(age * 0.9 + breaks * 6, rng, 8),
                        "criticality": min(5, cls + 1), "pressure_zone": "Zone A" if coords[0][1] > config.CENTER[0] else "Zone B"}, True)
            for pt in geo.interpolate_along(main["geometry"]["coordinates"], config.HYDRANT_SPACING_M, 30):
                add("hydrant", {"type": "Point", "coordinates": geo.round_coords(list(pt))},
                    "Hydrant", {"main_id": main["id"], "pressure_zone": main["props"]["pressure_zone"], "flow_gpm": rng.choice([500, 750, 1000, 1500]),
                                "criticality": 4}, True)
        if cls >= 2 and osm_lamps < 50:
            for pt in geo.interpolate_along(coords, config.STREETLIGHT_SPACING_M * (1 if cls >= 3 else 1.6), 10):
                year = rng.randint(2000, 2024)
                led = year >= 2015 or rng.random() < 0.3
                add("streetlight", {"type": "Point", "coordinates": geo.round_coords(list(pt))}, "Streetlight",
                    {"road_id": r["id"], "lamp": "LED" if led else "HPS", "wattage": 90 if led else 250,
                     "install_year": year, "pole_height_m": 9 if cls >= 3 else 7.5,
                     "condition": _condition_from_age((now_year - year) * 2, rng, 10), "criticality": 2}, True)

    sensors = _place_sensors(assets, rng)
    print(f"  assets: " + ", ".join(f"{k}={sum(1 for a in assets if a['type'] == k)}"
                                     for k in sorted({a['type'] for a in assets})))
    print(f"  sensors: {len(sensors)}")
    return assets, sensors, source


SENSOR_SPECS = {
    # type: (asset type, unit, description)
    "water_pressure": ("hydrant", "psi", "Hydrant-mounted pressure logger"),
    "streetlight_power": ("streetlight", "W", "Smart lighting controller"),
    "traffic_volume": ("road", "veh/h", "Radar traffic counter"),
    "structural_tilt": ("building", "mrad", "MEMS inclinometer"),
    "air_quality": ("road", "µg/m³ PM2.5", "Low-cost PM2.5 node"),
}


def _anchor_point(asset):
    g = asset["geometry"]
    if g["type"] == "Point":
        return g["coordinates"]
    if g["type"] == "LineString":
        c = g["coordinates"]
        return c[len(c) // 2]
    return list(geo.polygon_centroid(g["coordinates"][0][:-1]))


def _spread_sample(candidates, n, rng):
    """Greedy farthest-point sampling so sensors cover the whole area."""
    if len(candidates) <= n:
        return candidates
    chosen = [rng.choice(candidates)]
    pts = {a["id"]: _anchor_point(a) for a in candidates}
    dmin = {a["id"]: float("inf") for a in candidates}
    while len(chosen) < n:
        last = pts[chosen[-1]["id"]]
        best, best_d = None, -1
        for a in candidates:
            p = pts[a["id"]]
            d = dmin[a["id"]] = min(dmin[a["id"]], geo.haversine(*p, *last))
            if d > best_d:
                best, best_d = a, d
        chosen.append(best)
    return chosen


def _place_sensors(assets, rng):
    sensors = []
    counts = {"water_pressure": config.MAX_SENSORS_PER_TYPE, "streetlight_power": config.MAX_SENSORS_PER_TYPE,
              "traffic_volume": 28, "structural_tilt": 22, "air_quality": 14}
    for stype, n in counts.items():
        atype, unit, desc = SENSOR_SPECS[stype]
        pool = [a for a in assets if a["type"] == atype]
        if stype == "traffic_volume" or stype == "air_quality":
            pool = [a for a in pool if a["props"]["road_class"] >= 2 and a["props"]["length_m"] > 60]
        if stype == "structural_tilt":
            pool = sorted(pool, key=lambda a: (-a["props"]["criticality"], -a["props"]["footprint_m2"]))[: n * 4]
        for i, a in enumerate(_spread_sample(pool, n, rng)):
            lon, lat = _anchor_point(a)
            sensors.append({"id": f"{stype[:2].upper()}{stype.split('_')[1][:1].upper()}-{i + 1:03d}",
                            "type": stype, "unit": unit, "description": desc, "asset_id": a["id"],
                            "lon": round(lon, 6), "lat": round(lat, 6),
                            "install_date": f"20{rng.randint(21, 25)}-0{rng.randint(1, 9)}-15",
                            "battery_pct": rng.randint(35, 100) if stype != "streetlight_power" else None})
    return sensors
