"""Spatial analysis and the Derived Asset Health Score (build contract section 9), PostGIS-first.

    status.py      THE single implementation of the time axis, ``as_of`` handling, sensor status and KPIs
    health.py      Derived Asset Health Score: formula (pure function) and hourly computation
    clustering.py  spatio-temporal co-occurrence clusters of anomalies
    risk_zones.py  hexagonal grid and hourly risk score per cell
    spatial.py     reusable proximity / nearest / density queries

Everything here is derived from simulated sensor readings and prototype anomaly detection; none of it is an
assessment of the real condition of an asset.
"""
