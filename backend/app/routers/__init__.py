"""HTTP endpoints of the API, grouped by resource. Paths are at the root, exactly as in the build contract.

    service.py    /health  /meta  /statistics
    assets.py     /assets  /assets/{asset_id}  /assets/{asset_id}/health
    sensors.py    /sensors  /sensors/{sensor_id}  /sensor-readings
    anomalies.py  /anomalies  /anomalies/{anomaly_id}  /simulation-events
    spatial.py    /spatial/*
    layers.py     /layers/*
    playback.py   /playback
    ingest.py     POST /ingest/readings

Every endpoint is a plain ``def`` (the database access is synchronous) and answers with canonical JSON.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.routers import anomalies, assets, ingest, layers, playback, sensors, service, spatial

API_ROUTERS: tuple[APIRouter, ...] = (
    service.router,
    assets.router,
    sensors.router,
    anomalies.router,
    spatial.router,
    layers.router,
    playback.router,
    ingest.router,
)
