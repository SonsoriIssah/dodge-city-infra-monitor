"""SQL of the API. Every statement is parameterised; every function takes an explicit connection.

    common.py     row helpers, rounding rules, GeoJSON builders, NotFoundError
    meta.py       /health, /meta, /statistics
    assets.py     /assets, /assets/{id}, /assets/{id}/health
    sensors.py    /sensors, /sensors/{id}, /sensor-readings
    anomalies.py  /anomalies, /anomalies/{id}, /simulation-events
    spatial.py    /spatial/*
    layers.py     /layers/*
    playback.py   /playback bundle and its in-process cache

The functions return plain dictionaries in the response shape (timestamps already formatted, numbers already
rounded); the routers only validate parameters and send the result.
"""
