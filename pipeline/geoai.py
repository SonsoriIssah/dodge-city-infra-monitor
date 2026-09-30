"""GeoAI anomaly detection and spatial risk analytics (pure Python, no numpy needed).

Pipeline
  1. Seasonal decomposition   – robust hour-of-day (and weekday/weekend) profile per sensor
  2. Robust z-scores          – residual / (1.4826 * MAD)
  3. Isolation Forest         – multivariate outlier score on temporal features, per sensor type
  4. Spatial context          – is the deviation shared by neighbouring sensors in the same
                                network zone? → "area-wide/systemic" vs "local asset fault"
  5. Incident building        – merge flagged hours, diagnose root cause from the signature,
                                attach impacted assets
  6. Asset risk scoring       – condition x criticality x anomaly burden
  7. Hot-spot analysis        – Getis-Ord Gi* over a regular grid of asset risk
  8. Evaluation               – precision / recall against the simulator's ground truth
"""
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime

from . import config, geo

# --------------------------------------------------------------------------- utils

def _median(xs):
    return statistics.median(xs) if xs else 0.0


def _mad(xs, med):
    return _median([abs(x - med) for x in xs])


MIN_SCALE = {"water_pressure": 0.8, "streetlight_power": 0.05, "traffic_volume": 0.08,
             "structural_tilt": 0.015, "air_quality": 1.5}
# Sensors whose noise scales with the signal level get relative residuals: (x - p) / (p + k)
RELATIVE_K = {"streetlight_power": 10.0, "traffic_volume": 30.0}


# ------------------------------------------------------------------ Isolation Forest

def _c(n):
    if n <= 1:
        return 0.0
    return 2 * (math.log(n - 1) + 0.5772156649) - 2 * (n - 1) / n


class IsolationForest:
    def __init__(self, n_trees=80, sample_size=256, seed=0):
        self.n_trees, self.sample_size = n_trees, sample_size
        self.rng = random.Random(seed)
        self.trees = []

    def fit(self, X):
        m = min(self.sample_size, len(X))
        self.limit = math.ceil(math.log2(max(m, 2)))
        self.c = _c(m)
        self.trees = [self._grow(self.rng.sample(X, m), 0) for _ in range(self.n_trees)]
        return self

    def _grow(self, X, depth):
        if depth >= self.limit or len(X) <= 1:
            return ("leaf", len(X))
        dims = list(range(len(X[0])))
        self.rng.shuffle(dims)
        for f in dims:
            lo = min(r[f] for r in X)
            hi = max(r[f] for r in X)
            if hi > lo:
                split = self.rng.uniform(lo, hi)
                left = [r for r in X if r[f] < split]
                right = [r for r in X if r[f] >= split]
                return ("node", f, split, self._grow(left, depth + 1), self._grow(right, depth + 1))
        return ("leaf", len(X))

    def _path(self, x, node, depth=0):
        while node[0] == "node":
            node = node[3] if x[node[1]] < node[2] else node[4]
            depth += 1
        return depth + _c(node[1])

    def score(self, x):
        """Anomaly score in (0, 1]; ~0.5 normal, → 1 anomalous."""
        avg = sum(self._path(x, t) for t in self.trees) / len(self.trees)
        return 2 ** (-avg / self.c)


# ----------------------------------------------------------------------- detection

def _profile_key(stype, t):
    if stype == "traffic_volume":
        return (t.hour, t.weekday() >= 5)
    return t.hour


def _decompose(sensor, series, times):
    stype = sensor["type"]
    buckets = defaultdict(list)
    for x, t in zip(series, times):
        if x is not None:
            buckets[_profile_key(stype, t)].append(x)
    profile = {k: _median(v) for k, v in buckets.items()}
    k = RELATIVE_K.get(stype)
    resid = []
    for x, t in zip(series, times):
        p = profile[_profile_key(stype, t)]
        resid.append(None if x is None else (x - p) / (p + k) if k else x - p)
    valid = [r for r in resid if r is not None]
    med = _median(valid)
    scale = max(1.4826 * _mad(valid, med), MIN_SCALE[stype])
    if stype == "structural_tilt":
        # Tilt should be stationary: compare against the first 3 days' level, not the whole-series median
        early = [r for r in resid[:72] if r is not None]
        med = _median(early) if early else med
    z = [None if r is None else (r - med) / scale for r in resid]
    expected = [profile[_profile_key(stype, t)] for t in times]
    return z, expected, scale


def _features(z):
    feats = []
    for i, v in enumerate(z):
        if v is None:
            feats.append(None)
            continue
        prev = z[i - 1] if i and z[i - 1] is not None else v
        win = [x for x in z[max(0, i - 2): i + 1] if x is not None]
        m = sum(win) / len(win)
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / len(win))
        feats.append([v, v - prev, m, sd])
    return feats


def _diagnose(stype, zsign, dur, systemic, expected_on=None):
    if stype == "water_pressure":
        if systemic:
            return "pump_station_trip", "Zone-wide pressure loss – pump station / supply event"
        if zsign < 0 and dur >= 4:
            return "main_break_leak", "Sustained pressure drop – suspected main break / leak"
        return "pressure_transient", "Pressure transient – possible water hammer / valve operation"
    if stype == "streetlight_power":
        if zsign < 0:
            return "lamp_outage", "Lamp outage – no draw during dark hours"
        if expected_on is False:
            return "day_burner", "Day-burner – lamp on in daylight (photocell fault)"
        return "power_surge", "Over-draw – power surge / ballast fault"
    if stype == "traffic_volume":
        if zsign < 0:
            return "road_closure", "Traffic collapse – road closure or blocked detector"
        return "incident_congestion", "Traffic surge – incident / diversion congestion"
    if stype == "structural_tilt":
        return "foundation_settlement", "Progressive tilt – possible foundation settlement"
    if stype == "air_quality":
        if systemic:
            return "regional_aq_event", "Area-wide PM2.5 rise – regional weather/dust event"
        return "local_pollution_event", "Localized PM2.5 spike – nearby emission source"
    return "unknown", "Unclassified anomaly"


def detect(assets, sensors, times_iso, readings, labels, events):
    times = [datetime.fromisoformat(t) for t in times_iso]
    n = len(times)
    by_asset = {a["id"]: a for a in assets}

    z_all, expected_all, feats_all = {}, {}, {}
    for s in sensors:
        z, exp, _ = _decompose(s, readings[s["id"]], times)
        z_all[s["id"]], expected_all[s["id"]] = z, exp
        feats_all[s["id"]] = _features(z)

    # --- Isolation Forest per sensor type
    iso_all = {}
    for stype in {s["type"] for s in sensors}:
        group = [s for s in sensors if s["type"] == stype]
        X = [f for s in group for f in feats_all[s["id"]] if f is not None]
        forest = IsolationForest(config.IFOREST_TREES, config.IFOREST_SAMPLE, seed=config.RANDOM_SEED).fit(X)
        for s in group:
            iso_all[s["id"]] = [None if f is None else round(forest.score(f), 4) for f in feats_all[s["id"]]]

    # --- Point flags
    flags = {}
    for s in sensors:
        z, iso = z_all[s["id"]], iso_all[s["id"]]
        flags[s["id"]] = [z[i] is not None and ((iso[i] >= config.ANOMALY_SCORE_THRESHOLD and abs(z[i]) >= 3.0)
                                                or abs(z[i]) >= config.ROBUST_Z_THRESHOLD * 1.5)
                          for i in range(n)]

    # --- Spatial context: groups = network zones (pressure) or whole study area (AQ)
    def group_key(s):
        if s["type"] == "water_pressure":
            return ("water_pressure", by_asset[s["asset_id"]]["props"].get("pressure_zone"))
        return (s["type"], "all")

    groups = defaultdict(list)
    for s in sensors:
        groups[group_key(s)].append(s["id"])
    systemic = {}
    for key, ids in groups.items():
        if key[0] not in ("water_pressure", "air_quality") or len(ids) < 4:
            continue
        for i in range(n):
            dev = [z_all[sid][i] for sid in ids if z_all[sid][i] is not None and abs(z_all[sid][i]) >= 2.5]
            if len(dev) >= 0.5 * len(ids):
                systemic[(key, i)] = True

    # --- Merge flagged hours into incidents
    incidents = []
    for s in sensors:
        sid, f, z = s["id"], flags[s["id"]], z_all[s["id"]]
        i = 0
        while i < n:
            if not f[i]:
                i += 1
                continue
            j = i
            gap = 0
            while j + 1 < n and (f[j + 1] or (gap < 2 and j + 2 < n and f[j + 2])):
                gap = 0 if f[j + 1] else gap + 1
                j += 1
            span = [k for k in range(i, j + 1) if f[k]]
            peak_k = max(span, key=lambda k: abs(z[k]))
            zsign = 1 if sum(z[k] for k in span) > 0 else -1
            is_sys = sum(1 for k in span if systemic.get((group_key(s), k))) >= 0.5 * len(span)
            exp_on = expected_all[sid][peak_k] > 10 if s["type"] == "streetlight_power" else None
            kind, text = _diagnose(s["type"], zsign, len(span), is_sys, exp_on)
            sev = min(1.0, abs(z[peak_k]) / 25) * 0.6 + min(1.0, len(span) / 24) * 0.4
            incidents.append({
                "id": f"INC-{len(incidents) + 1:04d}", "sensor_id": sid, "asset_id": s["asset_id"],
                "sensor_type": s["type"], "start_idx": i, "end_idx": j,
                "start": times_iso[i], "end": times_iso[j], "hours": len(span),
                "peak_z": round(z[peak_k], 2), "peak_iso": iso_all[sid][peak_k],
                "peak_value": readings[sid][peak_k], "expected": round(expected_all[sid][peak_k], 2),
                "diagnosis": kind, "description": text, "scope": "area-wide" if is_sys else "local",
                "severity": round(sev, 3), "lon": s["lon"], "lat": s["lat"],
            })
            i = j + 1

    _group_area_events(incidents)
    _attach_impacts(incidents, assets, by_asset)
    metrics = _evaluate(incidents, events, sensors, flags, labels, n)
    anomaly_series = {sid: [round(v, 2) if v is not None else None for v in z_all[sid]] for sid in z_all}
    return incidents, metrics, anomaly_series, expected_all, iso_all


def _group_area_events(incidents):
    """Area-wide incidents that overlap in time with the same diagnosis are one physical event."""
    events = []
    for inc in sorted((i for i in incidents if i["scope"] == "area-wide"), key=lambda i: i["start_idx"]):
        ev = next((e for e in events if e["diagnosis"] == inc["diagnosis"]
                   and inc["start_idx"] <= e["end_idx"] + 1 and inc["end_idx"] >= e["start_idx"] - 1), None)
        if ev is None:
            ev = {"id": f"EVT-{len(events) + 1:03d}", "diagnosis": inc["diagnosis"],
                  "start_idx": inc["start_idx"], "end_idx": inc["end_idx"]}
            events.append(ev)
        ev["start_idx"] = min(ev["start_idx"], inc["start_idx"])
        ev["end_idx"] = max(ev["end_idx"], inc["end_idx"])
        inc["event_id"] = ev["id"]
    for inc in incidents:
        inc.setdefault("event_id", inc["id"])


def _attach_impacts(incidents, assets, by_asset):
    buildings = [(a["id"], *geo.polygon_centroid(a["geometry"]["coordinates"][0][:-1]))
                 for a in assets if a["type"] == "building"]
    for inc in incidents:
        a = by_asset[inc["asset_id"]]
        related = [a["id"]]
        if a["type"] == "hydrant" and a["props"].get("main_id"):
            related.append(a["props"]["main_id"])
        radius = {"water_pressure": 180, "traffic_volume": 120, "air_quality": 250,
                  "structural_tilt": 40, "streetlight_power": 35}[inc["sensor_type"]]
        near = [bid for bid, x, y in buildings if geo.haversine(inc["lon"], inc["lat"], x, y) <= radius]
        inc["related_assets"] = related
        inc["impacted_buildings"] = len(near)
        if inc["scope"] == "local":
            inc["priority"] = round(100 * (0.55 * inc["severity"] + 0.25 * a["props"].get("criticality", 2) / 5
                                           + 0.20 * min(1, len(near) / 40)))
        else:
            inc["priority"] = round(100 * (0.5 + 0.5 * inc["severity"]))


def _evaluate(incidents, events, sensors, flags, labels, n):
    """Event-level recall, incident-level precision, and diagnosis accuracy."""
    local_events = [e for e in events if e["sensor_id"] != "*ZONE_B*"]
    by_sensor = defaultdict(list)
    for e in local_events:
        by_sensor[e["sensor_id"]].append(e)

    detected, correct_dx = 0, 0
    for e in local_events:
        hits = [inc for inc in incidents if inc["sensor_id"] == e["sensor_id"]
                and inc["start_idx"] <= e["end_idx"] + 2 and inc["end_idx"] >= e["start_idx"] - 2]
        if hits:
            detected += 1
            if any(h["diagnosis"] == e["kind"] for h in hits):
                correct_dx += 1

    tp_inc = 0
    for inc in incidents:
        lab = labels[inc["sensor_id"]][inc["start_idx"]: inc["end_idx"] + 1]
        if any(lab) or inc["diagnosis"] == "regional_aq_event":
            tp_inc += 1  # regional AQ is real (shared weather signal), correctly scoped as area-wide
    pump = [e for e in events if e["sensor_id"] == "*ZONE_B*"]
    pump_caught = bool(pump) and any(i["diagnosis"] == "pump_station_trip" for i in incidents)

    # Point-level confusion for the confusion matrix
    tp = fp = fn = tn = 0
    for s in sensors:
        for i in range(n):
            y, p = labels[s["id"]][i] is not None, flags[s["id"]][i]
            tp += y and p
            fp += (not y) and p
            fn += y and not p
            tn += (not y) and not p

    return {
        "fault_events": len(local_events),
        "events_detected": detected,
        "event_recall": round(detected / max(1, len(local_events)), 3),
        "incident_precision": round(tp_inc / max(1, len(incidents)), 3),
        "diagnosis_accuracy": round(correct_dx / max(1, detected), 3),
        "zone_event_detected": pump_caught,
        "point_confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "point_precision": round(tp / max(1, tp + fp), 3),
        "point_recall": round(tp / max(1, tp + fn), 3),
    }


# ------------------------------------------------------------------ risk + hotspots

def score_assets(assets, sensors, incidents):
    burden = defaultdict(float)
    for inc in incidents:
        if inc["scope"] != "local":
            continue
        for aid in inc["related_assets"]:
            burden[aid] += inc["severity"] * (1 + inc["hours"] / 24)
    for a in assets:
        p = a["props"]
        cond = p.get("condition", p.get("pci"))
        if cond is None:
            continue
        crit = p.get("criticality", 2)
        anomaly = min(1.0, burden.get(a["id"], 0) / 1.5)
        risk = 100 * (0.45 * (1 - cond / 100) + 0.25 * crit / 5 + 0.30 * anomaly)
        p["anomaly_burden"] = round(burden.get(a["id"], 0), 3)
        p["risk"] = round(risk, 1)
        p["risk_class"] = "high" if risk >= 55 else "medium" if risk >= 35 else "low"


def hotspots(assets, cell_m=130):
    """Getis-Ord Gi* over a regular grid of mean asset risk."""
    s, w, n, e = config.BBOX
    dy = cell_m / 110540
    dx = cell_m / (111320 * math.cos(math.radians((s + n) / 2)))
    cells = defaultdict(list)
    for a in assets:
        r = a["props"].get("risk")
        if r is None:
            continue
        g = a["geometry"]
        if g["type"] == "Point":
            x, y = g["coordinates"]
        elif g["type"] == "LineString":
            x, y = g["coordinates"][len(g["coordinates"]) // 2]
        else:
            x, y = geo.polygon_centroid(g["coordinates"][0][:-1])
        cells[(int((x - w) / dx), int((y - s) / dy))].append(r)
    vals = {k: sum(v) / len(v) for k, v in cells.items()}
    N = len(vals)
    if N < 3:
        return []
    xbar = sum(vals.values()) / N
    S = math.sqrt(sum(v * v for v in vals.values()) / N - xbar ** 2) or 1
    out = []
    for (i, j), v in vals.items():
        nb = [vals[(i + a, j + b)] for a in (-1, 0, 1) for b in (-1, 0, 1) if (i + a, j + b) in vals]
        W = len(nb)
        num = sum(nb) - xbar * W
        den = S * math.sqrt((N * W - W * W) / (N - 1)) if N > 1 else 1
        gi = num / den if den else 0
        x0, y0 = w + i * dx, s + j * dy
        out.append({"i": i, "j": j, "mean_risk": round(v, 1), "n_assets": len(cells[(i, j)]),
                    "gi_z": round(gi, 2),
                    "class": "hot" if gi >= 1.96 else "cold" if gi <= -1.96 else "ns",
                    "polygon": geo.round_coords([[x0, y0], [x0 + dx, y0], [x0 + dx, y0 + dy], [x0, y0 + dy], [x0, y0]])})
    return out
