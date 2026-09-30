"""Simulate hourly IoT telemetry with realistic daily cycles and injected faults.

Each injected fault is recorded as ground truth so the GeoAI detector can be
scored (precision / recall) rather than just eyeballed.
"""
import math
import random
from datetime import datetime, timedelta

from . import config


def _hours():
    t0 = datetime.fromisoformat(config.SIM_START)
    return [t0 + timedelta(hours=h) for h in range(config.SIM_HOURS)]


def _baseline(sensor, asset, t, regional, rng):
    h, dow = t.hour, t.weekday()
    st = sensor["type"]
    p = asset["props"]
    if st == "water_pressure":
        base = 68 if p.get("pressure_zone", "Zone A") == "Zone A" else 61
        base += (sensor["lat"] - config.CENTER[0]) * -400  # lower ground → higher static head
        demand = 4.5 * math.exp(-((h - 7) ** 2) / 4) + 3.5 * math.exp(-((h - 19) ** 2) / 5)
        return base - demand + rng.gauss(0, 0.7)
    if st == "streetlight_power":
        on = h >= 19 or h < 6 or (h == 6 and t.month >= 10)
        w = p.get("wattage", 90)
        return (w * rng.uniform(0.96, 1.03)) if on else rng.uniform(1.5, 3.0)
    if st == "traffic_volume":
        peak = math.exp(-((h - 8) ** 2) / 3) + 1.15 * math.exp(-((h - 17) ** 2) / 4) + 0.55 * math.exp(-((h - 12.5) ** 2) / 6)
        night = 0.06
        weekend = 0.62 if dow >= 5 else 1.0
        hourly = p.get("aadt", 3000) / 14.0
        return max(0, hourly * (night + peak) * weekend * rng.uniform(0.88, 1.12))
    if st == "structural_tilt":
        thermal = 0.06 * math.sin((h - 9) / 24 * 2 * math.pi)
        return sensor["_tilt0"] + thermal + rng.gauss(0, 0.012)
    if st == "air_quality":
        traffic_bump = 3.0 * (math.exp(-((h - 8) ** 2) / 3) + math.exp(-((h - 17) ** 2) / 4))
        return max(1, regional + traffic_bump + rng.gauss(0, 1.1))
    raise ValueError(st)


FAULTS = {
    "water_pressure": [("main_break_leak", 6, 40), ("pressure_transient", 1, 3)],
    "streetlight_power": [("lamp_outage", 12, 72), ("day_burner", 24, 96), ("power_surge", 2, 8)],
    "traffic_volume": [("road_closure", 4, 14), ("incident_congestion", 2, 5)],
    "structural_tilt": [("foundation_settlement", 30, 150)],
    "air_quality": [("local_pollution_event", 3, 10)],
}


def _apply_fault(kind, value, sensor, asset, t, k, dur, severity):
    """Return the faulted reading; k = hours since fault start."""
    if kind == "main_break_leak":
        return value - severity * (14 + 10 * min(1, k / 3))
    if kind == "pressure_transient":
        return value + severity * 22 * (1 if k % 2 == 0 else -0.6)
    if kind == "lamp_outage":
        return 0.0 if value > 10 else value
    if kind == "day_burner":
        return asset["props"].get("wattage", 90) * 0.98 if value < 10 else value
    if kind == "power_surge":
        return value * (1.5 + 0.4 * severity) if value > 10 else value + 45 * severity
    if kind == "road_closure":
        return value * 0.04
    if kind == "incident_congestion":
        return value * (2.2 + severity) + 250
    if kind == "foundation_settlement":
        return value + severity * 0.012 * k
    if kind == "local_pollution_event":
        return value + severity * 70 * math.sin(math.pi * (k + 0.5) / dur)
    return value


def simulate(assets, sensors):
    rng = random.Random(config.RANDOM_SEED + 1)
    by_id = {a["id"]: a for a in assets}
    hours = _hours()
    n = len(hours)

    # Regional air quality: slowly varying weather-driven signal shared by all nodes
    regional, v = [], 8.0
    for i in range(n):
        v += rng.gauss(0, 0.6) + (8.0 - v) * 0.05
        if 200 <= i < 214:  # regional dust event (wind) -> everybody rises: NOT a local fault
            v += 2.5
        regional.append(max(2, v))

    for s in sensors:
        s["_tilt0"] = rng.uniform(-0.8, 0.8)

    readings, labels, events = {}, {}, []
    for s in sensors:
        asset = by_id[s["asset_id"]]
        vals = [_baseline(s, asset, hours[i], regional[i], rng) for i in range(n)]
        lab = [None] * n

        # Inject 0-2 faults per sensor; worse-condition assets fail more often
        cond = asset["props"].get("condition", asset["props"].get("pci", 70))
        p_fault = 0.25 + (100 - cond) / 180
        n_faults = sum(rng.random() < p_fault for _ in range(2))
        for _ in range(n_faults):
            kind, dmin, dmax = rng.choice(FAULTS[s["type"]])
            dur = rng.randint(dmin, dmax)
            start = rng.randint(24, n - dur - 1)
            if any(lab[start - 2: start + dur + 2]):
                continue
            sev = rng.uniform(0.7, 1.4)
            for k in range(dur):
                i = start + k
                vals[i] = _apply_fault(kind, vals[i], s, asset, hours[i], k, dur, sev)
                lab[i] = kind
            events.append({"sensor_id": s["id"], "asset_id": s["asset_id"], "kind": kind,
                           "start": hours[start].isoformat(), "end": hours[start + dur - 1].isoformat(),
                           "start_idx": start, "end_idx": start + dur - 1, "severity": round(sev, 2)})

        # Communication dropouts (missing data, not an infrastructure fault)
        if rng.random() < 0.15:
            g0 = rng.randint(0, n - 12)
            for i in range(g0, g0 + rng.randint(2, 10)):
                vals[i] = None
                lab[i] = None
        readings[s["id"]] = [None if x is None else round(x, 3) for x in vals]
        labels[s["id"]] = lab

    # Zone-wide pump trip: every Zone B pressure sensor drops together for 4 h
    pump_idx = 24 * 9 + 14
    zone_b = [s for s in sensors if s["type"] == "water_pressure"
              and by_id[s["asset_id"]]["props"].get("pressure_zone") == "Zone B"]
    for s in zone_b:
        for i in range(pump_idx, pump_idx + 4):
            if readings[s["id"]][i] is not None and labels[s["id"]][i] is None:
                readings[s["id"]][i] = round(readings[s["id"]][i] - 18, 3)
                labels[s["id"]][i] = "pump_station_trip"
    if zone_b:
        events.append({"sensor_id": "*ZONE_B*", "asset_id": None, "kind": "pump_station_trip",
                       "start": hours[pump_idx].isoformat(), "end": hours[pump_idx + 3].isoformat(),
                       "start_idx": pump_idx, "end_idx": pump_idx + 3, "severity": 1.0,
                       "affected_sensors": len(zone_b)})

    for s in sensors:
        s.pop("_tilt0", None)
    print(f"  simulated {len(sensors)} sensors x {n} h = {len(sensors) * n:,} readings, {len(events)} injected faults")
    return [h.isoformat() for h in hours], readings, labels, events
