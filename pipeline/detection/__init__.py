"""Prototype Anomaly Detection: a retrospective batch analysis of the stored sensor readings.

Baselines and scales are estimated from the whole data window. Playback replays those results hour by hour;
it does not reproduce what a streaming detector would have known at that hour.

    baseline.py   work domain, local-hour profile, peer adjustment, robust z (pure numpy)
    detectors.py  threshold, robust z-score, rolling median, Isolation Forest (hour flags and scores)
    events.py     merging flagged hours into events, persistence rule, score, severity, signature
    explain.py    plain-English explanation of an event (descriptive only)
    evaluate.py   self-consistency check against the simulator's injected events
    runner.py     database in -> database out (one transaction)
"""
