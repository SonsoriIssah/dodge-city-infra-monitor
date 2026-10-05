"""Deterministic simulator of the sensor network: "Simulated Sensor Data" (build contract section 7).

Nothing here is a measurement. The simulator produces, for every placed sensor, one value per time step of
the configured window, plus the ground truth of what it injected:

* Simulated weather reaches the sensors only through **shared regional drivers** (air-temperature anomaly,
  solar / cloud factor, rain). Every sensor responds with its own offset, gains and lag, so sensors of one
  placement class move together when the weather changes. That is what lets the detector tell a regional
  rain event or hot spell (benign) from a change on one sensor (abnormal).
* **Benign unusual behaviour** (vibration bursts, slow pressure drift, rain, a hot spell) stays within
  3.5 sigma of the sensor's own noise in the detector's work domain (ln for vibration, native units
  otherwise), after the regional part shared with its placement class is removed.
* **Injected abnormal events** are at least 8 sigma at their peak (short events) or at least 4 sigma for
  6 hours or more (sustained events and ramps).

Determinism: regional drivers and the event schedule are seeded from ``SIM_SEED``; every sensor has its own
generator seeded from ``zlib.crc32(sensor_id) ^ SIM_SEED``. The same seed and sensors give the same readings.
Daily and weekly patterns follow the local time of the study area (``settings.TIMEZONE``); timestamps are
timezone-aware UTC.
"""

from __future__ import annotations

import logging
import math
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any

import numpy as np

from pipeline import geo
from pipeline.config import Settings
from pipeline.models import TAG_NBI_BRIDGE, TAG_SHOWCASE, InjectedEvent, Reading, SensorSpec
from pipeline.sensors.thresholds import threshold_for

logger = logging.getLogger(__name__)

SOURCE_NAME = "simulator"
VALUE_DECIMALS = 4

# --- the rule that ties the simulator to the detector (section 7 "Rule") --------------------------------------
BENIGN_MAX_SIGMA = 3.5
SHORT_MIN_SIGMA = 8.0
SUSTAINED_MIN_SIGMA = 4.0
SUSTAINED_MIN_HOURS = 6.0
SIGMA_RULE_MARGIN = 1.02  # an event that the rule (not its range) sizes is made 2 % larger than the bound
# Scale floors of the detector's robust z-score (section 8.3), per sensor type, in work-domain units.
WORK_SIGMA_FLOOR: dict[str, float] = {"temperature": 0.5, "vibration": 0.10, "moisture": 0.5, "pressure": 0.5}
SMALL_CLASS_SIZE = 4  # temperature classes with fewer sensors have no peer median ...
SMALL_CLASS_TEMPERATURE_FLOOR = 1.5  # ... and a wider floor

# --- schedule constants ---------------------------------------------------------------------------------------
REFERENCE_EVENT_TOTAL = 40
QUIET_HOURS = 72.0  # no injected event in the lead-in
QUIET_WINDOW_DIVISOR = 4  # ... which never takes more than a quarter of a short window
MAX_EVENTS_PER_SENSOR = 2
MAX_EVENT_HOURS_PER_SENSOR = 144.0
EVENT_GAP_HOURS = 12.0  # between two events of one sensor, and between an event's end and the last timestamp
EXCLUSIVE_CLASS_SIZE = 8  # in smaller placement classes only one sensor has an event at a time
SLICE_ATTEMPTS = 4  # start times tried per slice of the window before the next slice is used
COLOCATED_SIZE = 4
COLOCATED_MIN_ASSETS = 3
COLOCATED_RADIUS_M = 150.0
COLOCATED_WIDER_RADII_M = (200.0, 300.0, 600.0)  # tried, with a warning, when no group exists within 150 m
COLOCATED_MAX_NEIGHBOURS = 14  # nearest sensors considered around each sensor (bounds the search)
COLOCATED_START_HOURS = (72.0, 36.0)  # the group starts between 72 h and 36 h before the last timestamp
COLOCATED_ENDED_MAX_HOURS = 24.0  # group members that do not run to the end last at most this long
RAMP_LEAD_HOURS = 60.0  # ramps that are ongoing at the end start at least this long before it
STEP_LEAD_HOURS = 24.0  # step-type events that are ongoing at the end start at least this long before it
# The events that are still running at the last timestamp: hours before the end at which they start, and size.
FINAL_BRIDGE_LEAD_HOURS = (STEP_LEAD_HOURS + 2.0, 40.0)
FINAL_BRIDGE_MIN_FACTOR = 3.0  # upper part of the "sustained high vibration" range: clearly visible
FINAL_DROP_LEAD_HOURS = (STEP_LEAD_HOURS, STEP_LEAD_HOURS + 4.0)
FINAL_MOISTURE_LEAD_HOURS = (STEP_LEAD_HOURS + 6.0, 54.0)
FINAL_MOISTURE_PCT = (10.0, 14.0)
FINAL_DRIFT_LEAD_HOURS = (RAMP_LEAD_HOURS, 84.0)
FINAL_DRIFT_C = (7.0, 10.0)
WARNING_CLEARANCE = 0.5  # the final moisture event stays this far below the warning limit (native units)
RECENT_HOURS = 24.0  # ... measured against the sensor's highest value of the last day
FINAL_OUTAGE_HOURS = (30.0, 6.0)
DROPOUT_HOURS = (2, 10)
DROPOUT_ATTEMPTS = 50  # positions tried for a random gap before the sensor is left without one
DROPOUT_EVENT_MARGIN_STEPS = 2  # a random gap keeps this distance from the protected part of an event
CRITICAL_DROP_TARGET_PSI = 16.5  # level the designated pressure drop falls to (below crit_low = 20 psi)
PRESSURE_DROP_FLOOR_PSI = 26.0  # every other pressure drop stays above this level

# --- regional drivers -----------------------------------------------------------------------------------------
AIR_MEAN_START_C = 22.5
AIR_MEAN_END_C = 16.5
AIR_REFERENCE_C = (AIR_MEAN_START_C + AIR_MEAN_END_C) / 2.0
DIURNAL_HALF_RANGE_C = 7.0
DIURNAL_PEAK_HOUR = 16.0
FRONT_SD_C = 3.5
FRONT_PHI_PER_HOUR = 0.985
FRONT_LIMIT_C = 8.0  # soft limit of the front anomaly (keeps benign weather below the warning thresholds)
HOT_SPELL_C = 7.0
HOT_SPELL_HOURS = 36.0
HOT_SPELL_RAMP_HOURS = 6.0
HOT_SPELL_AT = 0.40  # position in the window (fraction)
HOT_SPELL_MIN_WINDOW_FACTOR = 4  # no hot spell in a window shorter than four times its length
REGIONAL_JITTER_STEPS = 3  # regional events start up to this many steps before / after their nominal position
DIURNAL_CLEAR_SKY_SHARE = 0.5  # share of the daily air-temperature swing that disappears under full cloud
SOLAR_RISE_HOUR = 7.0  # local hour at which the sun factor leaves zero ...
SOLAR_DAY_HOURS = 12.0  # ... and the length of the half-sine that follows (peak 13:00 local)
LAG_KERNEL_TAUS = 6.0  # length of the thermal-lag filter in time constants
# Rain events by window length: three in a window of three weeks or more (the default), fewer in shorter
# windows so that the dry-down periods do not cover the whole window. Positions are fractions of the window.
RAIN_AT: dict[int, tuple[float, ...]] = {3: (0.27, 0.52, 0.76), 2: (0.25, 0.55), 1: (0.30,)}
RAIN_MIN_DAYS = {3: 21.0, 2: 14.0, 1: 4.0}
RAIN_DEPTH_MM = ((9.0, 12.0), (15.0, 20.0), (22.0, 26.0))  # one event per band, order drawn from the seed
RAIN_HOURS = 5.0
RAIN_EFFECT_HOURS = 72.0  # benign window recorded after the rain stops (most of the dry-down)
RAIN_GUARD_HOURS = 6.0  # injected moisture events also keep this distance before a rain event
RAIN_SHORT_EFFECT_HOURS = 24.0  # relaxed exclusion used only when a window is too short for the full one
CLOUD_FACTOR = 0.35
CLOUD_LEAD_HOURS = 6.0
CLOUD_TAIL_HOURS = 18.0
CLOUD_RAMP_HOURS = 3.0
SPINUP_HOURS = 96.0  # drivers are simulated this long before the window so that lagged responses have settled

# --- sensor models --------------------------------------------------------------------------------------------
TEMPERATURE_NOISE_RANGE_C = (0.3, 0.6)
# Response of each placement class to the regional drivers: gain on the air anomaly, solar gain (deg C at full
# sun), thermal lag (h). A sensor responds with its own sensitivity (one factor on both gains), its own offset
# and its own lag, so the sensors of one class respond (nearly) proportionally to the weather.
# Also: share of the mean air temperature in the base level, noise sd, daily load heat (equipment only).
TEMPERATURE_MODELS: dict[str, dict[str, Any]] = {
    "bridge_deck": {"gain": 1.075, "solar": 10.5, "lag": 2.0, "sensitivity": (0.93, 1.07), "offset": (-0.5, 0.5),
                    "air_share": 1.0, "noise": 0.5, "load": None},
    "road_surface": {"gain": 1.125, "solar": 15.5, "lag": 1.0, "sensitivity": (0.93, 1.07), "offset": (0.5, 1.5),
                     "air_share": 1.0, "noise": 0.55, "load": None},
    "building_envelope": {"gain": 0.35, "solar": 1.0, "lag": 10.0, "sensitivity": (0.75, 1.25),
                          "offset": (11.0, 13.0), "air_share": 0.5, "noise": 0.3, "load": None},
    "equipment": {"gain": 0.8, "solar": 3.0, "lag": 3.0, "sensitivity": (0.95, 1.05), "offset": (15.0, 20.0),
                  "air_share": 1.0, "noise": 0.55, "load": (3.0, 5.0)},
}  # fmt: skip
TEMPERATURE_SOLAR_SPREAD = 0.03  # sensor-specific variation of the solar share
TEMPERATURE_LAG_SPREAD = 0.05  # sensor-specific variation of the thermal lag
TEMPERATURE_NOISE_SPREAD = (0.9, 1.1)  # sensor-specific factor on the class's noise sd
EQUIPMENT_LOAD_PEAK_HOUR = 17.0  # local hour of the daily load peak of the equipment ...
EQUIPMENT_LOAD_WIDTH = 18.0  # ... and its width (denominator of the Gaussian exponent, h^2)
# Weekday peak-hour level per placement class; the daily pattern scales it down at other hours.
VIBRATION_LEVEL_MM_S: dict[str, tuple[float, float]] = {
    "bridge_deck": (0.8, 2.0),
    "building_structure": (0.05, 0.30),
    "road_pavement": (0.3, 0.8),
}
VIBRATION_NOISE_LN = 0.15
VIBRATION_WEEKEND_FACTOR = 0.7
VIBRATION_BURST_FACTOR = (1.3, 1.5)
VIBRATION_BURSTS_PER_MONTH = 6.0
HOURS_PER_MONTH = 720.0
BURST_CLEARANCE_HOURS = 3.0  # benign bursts keep this distance from injected events and from each other
# Traffic-like daily activity: a night level plus Gaussian peaks (amplitude, local hour, width in h^2) for the
# morning, the evening and midday.
TRAFFIC_NIGHT_LEVEL = 0.35
TRAFFIC_PEAKS = ((0.60, 8.0, 8.0), (0.75, 17.0, 10.0), (0.40, 12.5, 8.0))
MOISTURE_BASELINE_PCT = (14.0, 26.0)
MOISTURE_NOISE_PCT = 0.15
MOISTURE_DIURNAL_PCT = 0.2
MOISTURE_DIURNAL_PEAK_HOUR = 15.0
MOISTURE_RISE_HOURS = 6.0
MOISTURE_TAU_SPREAD = 0.10
# dry-down time constant (h) and response in % volumetric water content per mm of rain
MOISTURE_MODELS: dict[str, dict[str, Any]] = {
    "road_subgrade": {"tau": 60.0, "gain": (0.20, 0.27)},
    "abutment_backfill": {"tau": 90.0, "gain": (0.18, 0.25)},
    "foundation_perimeter": {"tau": 120.0, "gain": (0.167, 0.22)},
}
MOISTURE_LIMITS_PCT = (1.0, 60.0)
PRESSURE_LEVEL_PSI = (55.0, 75.0)
PRESSURE_DEMAND = (0.6, 1.3)  # sensor-specific factor on the demand dips
# Daily demand dips: (depth in psi at demand factor 1, local hour, width in h^2) for the morning and the evening.
PRESSURE_DIPS = ((4.0, 7.0, 4.0), (3.0, 19.0, 5.0))
PRESSURE_NOISE_PSI = 0.5
PRESSURE_DRIFT_SD_PSI = 0.6
PRESSURE_DRIFT_PHI_PER_HOUR = 0.99
PRESSURE_DRIFT_LIMIT_PSI = 1.2  # soft limit (2 sd) of the benign drift, so that drift plus noise stays benign
PRESSURE_MIN_PSI = 1.0


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One kind of injected abnormal event."""

    event_type: str
    sensor_type: str
    count: int  # events of this kind per REFERENCE_EVENT_TOTAL
    hours: tuple[float, float]  # duration range
    magnitude: tuple[float, float]  # native units; vibration: multiplicative factor
    shape: str  # factor | step | ramp | half_sine | rise_hold
    short: bool  # True: at least 8 sigma at the peak; False: at least 4 sigma for 6 h or more
    sign: int = 1
    label: str = ""


# ``hours`` is the whole duration of an event. For a moisture increase it includes the rise over
# MOISTURE_RISE_HOURS, as in the reference prototype the constants were tested with.
EVENT_SPECS: tuple[EventSpec, ...] = (
    EventSpec("vibration_spike", "vibration", 6, (1, 2), (4.0, 8.0), "factor", True, 1, "short elevated vibration"),
    EventSpec("sustained_high_vibration", "vibration", 6, (6, 48), (2.2, 3.5), "factor", False, 1,
              "sustained high vibration"),
    EventSpec("moisture_increase", "moisture", 7, (12, 72), (6.0, 14.0), "rise_hold", False, 1,
              "moisture increase without rain"),
    EventSpec("pressure_drop", "pressure", 5, (4, 24), (12.0, 40.0), "step", True, -1, "pressure drop"),
    EventSpec("pressure_spike", "pressure", 4, (1, 3), (15.0, 30.0), "step", True, 1, "short pressure excursion"),
    EventSpec("pressure_decline", "pressure", 3, (48, 96), (8.0, 15.0), "ramp", False, -1,
              "gradual pressure decline"),
    EventSpec("temperature_spike", "temperature", 5, (3, 8), (8.0, 15.0), "half_sine", True, 1,
              "abnormal temperature rise"),
    EventSpec("temperature_drift", "temperature", 4, (48, 120), (5.0, 10.0), "ramp", False, 1, "temperature drift"),
)  # fmt: skip
EVENT_SPEC_BY_TYPE: dict[str, EventSpec] = {spec.event_type: spec for spec in EVENT_SPECS}
# Event of each sensor type that can still be running at the last timestamp / that ends well before it.
_ONGOING_TYPE = {
    "vibration": "sustained_high_vibration",
    "moisture": "moisture_increase",
    "pressure": "pressure_decline",
    "temperature": "temperature_drift",
}
_ENDED_TYPES = {
    "vibration": ("sustained_high_vibration", "vibration_spike"),
    "moisture": ("moisture_increase",),
    "pressure": ("pressure_drop", "pressure_spike"),
    "temperature": ("temperature_spike", "temperature_drift"),
}

REGIONAL_RAIN = "regional_rain"
REGIONAL_HOT_SPELL = "regional_hot_spell"
BENIGN_BURST = "vibration_burst"


# --- results ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EventDetail:
    """An event on one sensor together with what the simulator added to the signal.

    ``deviation`` is the added signal per time step in the detector's work domain (ln of the factor for
    vibration, native units otherwise); ``sigma`` is the sensor's work-domain noise scale.
    """

    event: InjectedEvent
    start_index: int
    end_index: int
    deviation: np.ndarray
    sigma: float
    short: bool
    role: str  # scheduled | final_hour | colocated | colocated_final_hour | benign
    step_hours: float = 1.0

    @property
    def peak_sigma(self) -> float:
        """Largest deviation of the event in units of the sensor's noise sigma."""
        return float(np.max(np.abs(self.deviation)) / self.sigma)

    def sustained_hours(self, sigma_level: float = SUSTAINED_MIN_SIGMA) -> float:
        """Hours of the event with a deviation of at least ``sigma_level`` sigma."""
        return float(np.count_nonzero(np.abs(self.deviation) >= sigma_level * self.sigma - 1e-9) * self.step_hours)


@dataclass(slots=True)
class SimulationResult:
    """Everything one simulation run produced."""

    timestamps: list[datetime]
    sensors: list[SensorSpec]
    values: dict[str, np.ndarray]  # NaN = the sensor did not report at that step
    events: list[EventDetail]  # injected abnormal events, in start order
    benign_bursts: list[EventDetail]  # benign local events (vibration bursts); not stored as ground truth
    regional_events: list[InjectedEvent]  # benign regional events: rain, hot spell
    regional_sigma: list[dict[str, float]]  # per regional event: sensor id -> size of its sensor-specific part
    noise_sigma: dict[str, float]  # sd of the sensor's own noise, work domain
    work_sigma: dict[str, float]  # max(noise_sigma, detector scale floor)
    dropouts: dict[str, list[tuple[int, int]]] = field(default_factory=dict)  # inclusive index ranges
    colocated_group: list[str] = field(default_factory=list)

    def ground_truth(self) -> list[InjectedEvent]:
        """Rows for ``infra.simulation_events``: injected abnormal events, then the benign regional events."""
        return [detail.event for detail in self.events] + list(self.regional_events)

    def readings(self, start: datetime | None = None, end: datetime | None = None) -> Iterator[Reading]:
        """Readings with ``start <= ts <= end`` (both optional), sensor by sensor in time order."""
        keep = [
            index
            for index, ts in enumerate(self.timestamps)
            if (start is None or ts >= start) and (end is None or ts <= end)
        ]
        for sensor in self.sensors:
            series = self.values[sensor.sensor_id]
            for index in keep:
                value = series[index]
                if not math.isnan(value):
                    yield Reading(
                        sensor.sensor_id, self.timestamps[index], round(float(value), VALUE_DECIMALS), sensor.unit
                    )

    def reading_count(self) -> int:
        """Number of readings (time steps that are not dropouts)."""
        return int(sum(np.count_nonzero(~np.isnan(series)) for series in self.values.values()))

    def ongoing_at_end(self) -> list[EventDetail]:
        """Injected events still running at the last timestamp."""
        last = len(self.timestamps) - 1
        return [detail for detail in self.events if detail.end_index == last]

    def offline_at_end(self) -> list[str]:
        """Sensors without a reading at the last timestamp."""
        return [sid for sid, series in self.values.items() if math.isnan(series[-1])]

    def sigma_rule_violations(self) -> list[str]:
        """Ground truth that breaks the benign / abnormal sigma rule (empty when the rule holds).

        Benign bursts and the sensor-specific part of benign regional events must stay within 3.5 sigma;
        injected short events must reach 8 sigma, sustained ones 4 sigma for at least 6 hours.
        """
        problems: list[str] = []
        for detail in self.events:
            where = f"{detail.event.event_type} on {detail.event.sensor_id}"
            if detail.short and detail.peak_sigma < SHORT_MIN_SIGMA:
                problems.append(f"{where}: peak {detail.peak_sigma:.1f} sigma is below {SHORT_MIN_SIGMA:.0f}")
            if not detail.short and detail.sustained_hours() < SUSTAINED_MIN_HOURS:
                problems.append(
                    f"{where}: {detail.sustained_hours():.0f} h at {SUSTAINED_MIN_SIGMA:.0f} sigma, "
                    f"needs {SUSTAINED_MIN_HOURS:.0f} h"
                )
        for detail in self.benign_bursts:
            if detail.peak_sigma > BENIGN_MAX_SIGMA:
                problems.append(f"benign burst on {detail.event.sensor_id}: {detail.peak_sigma:.1f} sigma")
        for event, sizes in zip(self.regional_events, self.regional_sigma):
            for sensor_id, size in sizes.items():
                if size > BENIGN_MAX_SIGMA:
                    problems.append(f"{event.event_type}: sensor-specific part on {sensor_id} is {size:.1f} sigma")
        return problems


# --- time axis and regional drivers -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TimeAxis:
    """The simulation window: UTC timestamps plus the local clock the daily and weekly patterns follow."""

    timestamps: tuple[datetime, ...]
    step_hours: float
    hour_of_day: np.ndarray  # local, fractional
    weekend: np.ndarray  # local Saturday / Sunday
    lead_hour_of_day: np.ndarray  # local hour of day of the spin-up steps before the window

    @property
    def n(self) -> int:
        """Number of time steps."""
        return len(self.timestamps)

    @property
    def lead(self) -> int:
        """Number of spin-up steps before the window."""
        return len(self.lead_hour_of_day)

    def steps(self, hours: float) -> int:
        """Whole number of time steps for a duration in hours (at least one)."""
        return max(1, int(round(hours / self.step_hours)))


def build_time_axis(settings: Settings) -> TimeAxis:
    """Time axis of the configured window (SIM_START, SIM_DAYS, SIM_STEP_MINUTES)."""
    timestamps = tuple(settings.time_axis())
    local = [ts.astimezone(settings.tz) for ts in timestamps]
    step_hours = settings.SIM_STEP_MINUTES / 60.0
    lead = max(1, int(round(SPINUP_HOURS / step_hours)))
    before = [(timestamps[0] - (lead - i) * settings.sim_step).astimezone(settings.tz) for i in range(lead)]
    return TimeAxis(
        timestamps=timestamps,
        step_hours=step_hours,
        hour_of_day=np.array([t.hour + t.minute / 60.0 for t in local], dtype=float),
        weekend=np.array([t.weekday() >= 5 for t in local], dtype=bool),
        lead_hour_of_day=np.array([t.hour + t.minute / 60.0 for t in before], dtype=float),
    )


@dataclass(frozen=True, slots=True)
class RegionalDrivers:
    """Simulated weather shared by every sensor (never presented as observed weather).

    The series start ``lead`` spin-up steps before the window; the event indices refer to the window.
    """

    lead: int
    air: np.ndarray  # simulated air temperature, deg C
    solar: np.ndarray  # 0..1 sun factor after cloud
    rain: np.ndarray  # mm per time step
    hot_component: np.ndarray  # part of the air anomaly that belongs to the hot spell
    rain_events: tuple[tuple[int, int, float], ...]  # (start index, steps, depth in mm)
    hot_spell: tuple[int, int] | None  # (start index, end index inclusive)


def _trapezoid(n: int, start: int, stop: int, ramp: int) -> np.ndarray:
    """Weights 0..1 that rise over ``ramp`` steps from ``start``, hold, and fall to 0 at ``stop`` (cosine edges)."""
    index = np.arange(n, dtype=float)
    ramp = max(ramp, 1)
    rise = np.clip((index - start + 1) / ramp, 0.0, 1.0)
    fall = np.clip((stop - index) / ramp, 0.0, 1.0)
    return 0.5 * (1.0 - np.cos(np.pi * np.minimum(rise, fall)))


def _ar1(rng: np.random.Generator, n: int, phi: float, sd: float) -> np.ndarray:
    """Stationary AR(1) series with standard deviation ``sd``."""
    shocks = rng.normal(0.0, sd * math.sqrt(max(1.0 - phi * phi, 0.0)), n)
    series = np.zeros(n)
    series[0] = rng.normal(0.0, sd)
    for i in range(1, n):
        series[i] = phi * series[i - 1] + shocks[i]
    return series


def build_drivers(axis: TimeAxis, seed: int) -> RegionalDrivers:
    """Regional drivers for the window: air temperature, sun / cloud factor, rain (seeded from SIM_SEED)."""
    rng = np.random.default_rng(seed & 0xFFFFFFFF)
    n, lead = axis.n, axis.lead
    total = lead + n
    hour_of_day = np.concatenate([axis.lead_hour_of_day, axis.hour_of_day])
    position = np.clip((np.arange(total) - lead) / max(n - 1, 1), 0.0, 1.0)
    seasonal = AIR_MEAN_START_C + (AIR_MEAN_END_C - AIR_MEAN_START_C) * position
    front = _ar1(rng, total, FRONT_PHI_PER_HOUR**axis.step_hours, FRONT_SD_C)
    front = FRONT_LIMIT_C * np.tanh(front / FRONT_LIMIT_C)

    hot_spell: tuple[int, int] | None = None
    hot_component = np.zeros(total)
    hot_steps = axis.steps(HOT_SPELL_HOURS)
    jitter = (-REGIONAL_JITTER_STEPS, REGIONAL_JITTER_STEPS + 1)
    if n >= HOT_SPELL_MIN_WINDOW_FACTOR * hot_steps:
        hot_start = int(round(HOT_SPELL_AT * n)) + int(rng.integers(*jitter))
        hot_stop = min(hot_start + hot_steps, n)
        weight = _trapezoid(total, lead + hot_start, lead + hot_stop, axis.steps(HOT_SPELL_RAMP_HOURS))
        hot_component = weight * (HOT_SPELL_C - front)  # the air anomaly becomes +7 deg C during the spell
        hot_spell = (hot_start, hot_stop - 1)

    rain = np.zeros(total)
    cover = np.zeros(total)
    rain_events: list[tuple[int, int, float]] = []
    rain_steps = axis.steps(RAIN_HOURS)
    bands = [RAIN_DEPTH_MM[i] for i in rng.permutation(len(RAIN_DEPTH_MM))]
    days = n * axis.step_hours / 24.0
    positions = next((RAIN_AT[k] for k in (3, 2, 1) if days >= RAIN_MIN_DAYS[k]), ())
    for at, band in zip(positions, bands):
        start = int(round(at * n)) + int(rng.integers(*jitter))
        depth = float(rng.uniform(*band))
        if start < 0 or start + rain_steps > n:
            continue
        rain[lead + start : lead + start + rain_steps] += depth / rain_steps
        cover = np.maximum(
            cover,
            _trapezoid(total, lead + start - axis.steps(CLOUD_LEAD_HOURS),
                       lead + start + axis.steps(CLOUD_TAIL_HOURS), axis.steps(CLOUD_RAMP_HOURS)),
        )  # fmt: skip
        rain_events.append((start, rain_steps, depth))
    cloud = 1.0 - (1.0 - CLOUD_FACTOR) * cover

    diurnal = DIURNAL_HALF_RANGE_C * np.cos((hour_of_day - DIURNAL_PEAK_HOUR) / 24.0 * 2.0 * np.pi)
    swing = (1.0 - DIURNAL_CLEAR_SKY_SHARE) + DIURNAL_CLEAR_SKY_SHARE * cloud  # smaller daily swing under cloud
    air = seasonal + front + hot_component + diurnal * swing
    solar = np.clip(np.sin(np.pi * (hour_of_day - SOLAR_RISE_HOUR) / SOLAR_DAY_HOURS), 0.0, None) * cloud
    return RegionalDrivers(
        lead=lead, air=air, solar=solar, rain=rain, hot_component=hot_component, rain_events=tuple(rain_events),
        hot_spell=hot_spell,
    )  # fmt: skip


def _lagged(series: np.ndarray, lag_hours: float, step_hours: float) -> np.ndarray:
    """Exponentially lagged copy of a driver (thermal inertia with time constant ``lag_hours``).

    Causal: the value at step i is a weighted mean of the driver at i, i-1, ... with weights exp(-k / tau).
    """
    tau = lag_hours / step_hours
    length = max(int(math.ceil(LAG_KERNEL_TAUS * tau)), 2)
    kernel = np.exp(-np.arange(length) / tau)
    kernel /= kernel.sum()
    padded = np.concatenate([np.full(length - 1, series[0]), series])
    return np.convolve(padded, kernel, mode="valid")  # convolve flips the kernel: weight k applies to step i - k


def _wetness(rain: np.ndarray, tau_hours: float, step_hours: float) -> np.ndarray:
    """Wetness state: rain accumulates and dries down exponentially with time constant ``tau_hours``."""
    decay = math.exp(-step_hours / tau_hours)
    state = np.zeros(len(rain))
    for i in range(1, len(rain)):
        state[i] = state[i - 1] * decay + rain[i]
    return state


def _gaussian_bump(hour_of_day: np.ndarray, peak_hour: float, width: float) -> np.ndarray:
    """Bell-shaped daily bump: 1 at ``peak_hour`` (local), ``width`` is the denominator of the exponent."""
    return np.exp(-((hour_of_day - peak_hour) ** 2) / width)


def _traffic_pattern(hour_of_day: np.ndarray) -> np.ndarray:
    pattern = np.full(hour_of_day.shape, TRAFFIC_NIGHT_LEVEL)
    for amplitude, peak_hour, width in TRAFFIC_PEAKS:
        pattern = pattern + amplitude * _gaussian_bump(hour_of_day, peak_hour, width)
    return pattern


_TRAFFIC_PEAK = float(_traffic_pattern(np.linspace(0.0, 24.0, 2401)).max())


def traffic_shape(hour_of_day: np.ndarray) -> np.ndarray:
    """Traffic-like daily activity pattern: 1.0 at the afternoon peak, about 0.3 at night."""
    return _traffic_pattern(hour_of_day) / _TRAFFIC_PEAK


# --- per-sensor signals -----------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Signal:
    """Baseline signal of one sensor and what the scheduler needs to know about it."""

    spec: SensorSpec
    rng: np.random.Generator
    values: np.ndarray
    noise_sigma: float
    info: dict[str, float] = field(default_factory=dict)
    regional: dict[str, tuple[float, float]] = field(default_factory=dict)  # driver -> (gain, own time constant)


def sensor_rng(sensor_id: str, seed: int) -> np.random.Generator:
    """The sensor's own generator: ``zlib.crc32(sensor_id) ^ SIM_SEED``."""
    return np.random.default_rng(zlib.crc32(sensor_id.encode("utf-8")) ^ (seed & 0xFFFFFFFF))


def _temperature_signal(
    spec: SensorSpec, axis: TimeAxis, drivers: RegionalDrivers, rng: np.random.Generator
) -> _Signal:
    model = TEMPERATURE_MODELS.get(spec.placement)
    if model is None:
        raise ValueError(f"no temperature model for placement {spec.placement!r} ({spec.sensor_id})")
    sensitivity = float(rng.uniform(*model["sensitivity"]))
    spread = float(rng.uniform(1.0 - TEMPERATURE_SOLAR_SPREAD, 1.0 + TEMPERATURE_SOLAR_SPREAD))
    gain = sensitivity * model["gain"]
    solar_gain = sensitivity * model["solar"] * spread
    lag = model["lag"] * float(rng.uniform(1.0 - TEMPERATURE_LAG_SPREAD, 1.0 + TEMPERATURE_LAG_SPREAD))
    offset = float(rng.uniform(*model["offset"]))
    noise = float(np.clip(model["noise"] * rng.uniform(*TEMPERATURE_NOISE_SPREAD), *TEMPERATURE_NOISE_RANGE_C))
    lead = drivers.lead
    values = (
        AIR_REFERENCE_C * model["air_share"]
        + offset
        + gain * _lagged(drivers.air - AIR_REFERENCE_C, lag, axis.step_hours)[lead:]
        + solar_gain * _lagged(drivers.solar, lag, axis.step_hours)[lead:]
    )
    if model["load"] is not None:  # daily load heat of the equipment, on top of its constant offset
        load_shape = _gaussian_bump(axis.hour_of_day, EQUIPMENT_LOAD_PEAK_HOUR, EQUIPMENT_LOAD_WIDTH)
        values = values + float(rng.uniform(*model["load"])) * load_shape
    values = values + rng.normal(0.0, noise, axis.n)
    return _Signal(spec, rng, values, noise, {"gain": gain, "solar": solar_gain, "lag": lag}, {"air": (gain, lag)})


def _vibration_signal(spec: SensorSpec, axis: TimeAxis, rng: np.random.Generator) -> _Signal:
    bounds = VIBRATION_LEVEL_MM_S.get(spec.placement)
    if bounds is None:
        raise ValueError(f"no vibration model for placement {spec.placement!r} ({spec.sensor_id})")
    level = float(rng.uniform(*bounds))
    weekly = np.where(axis.weekend, VIBRATION_WEEKEND_FACTOR, 1.0)
    values = level * traffic_shape(axis.hour_of_day) * weekly * np.exp(rng.normal(0.0, VIBRATION_NOISE_LN, axis.n))
    return _Signal(spec, rng, values, VIBRATION_NOISE_LN, {"level": level})


def _moisture_signal(spec: SensorSpec, axis: TimeAxis, drivers: RegionalDrivers, rng: np.random.Generator) -> _Signal:
    model = MOISTURE_MODELS.get(spec.placement)
    if model is None:
        raise ValueError(f"no moisture model for placement {spec.placement!r} ({spec.sensor_id})")
    baseline = float(rng.uniform(*MOISTURE_BASELINE_PCT))
    gain = float(rng.uniform(*model["gain"]))
    tau = model["tau"] * float(rng.uniform(1.0 - MOISTURE_TAU_SPREAD, 1.0 + MOISTURE_TAU_SPREAD))
    daily = MOISTURE_DIURNAL_PCT * np.cos((axis.hour_of_day - MOISTURE_DIURNAL_PEAK_HOUR) / 24.0 * 2.0 * np.pi)
    values = (
        baseline
        + gain * _wetness(drivers.rain, tau, axis.step_hours)[drivers.lead :]
        + daily
        + rng.normal(0.0, MOISTURE_NOISE_PCT, axis.n)
    )
    return _Signal(spec, rng, values, MOISTURE_NOISE_PCT, {"baseline": baseline, "tau": tau}, {"rain": (gain, tau)})


def _pressure_signal(spec: SensorSpec, axis: TimeAxis, rng: np.random.Generator) -> _Signal:
    level = float(rng.uniform(*PRESSURE_LEVEL_PSI))
    demand = float(rng.uniform(*PRESSURE_DEMAND))
    dips = demand * sum(depth * _gaussian_bump(axis.hour_of_day, hour, width) for depth, hour, width in PRESSURE_DIPS)
    drift = _ar1(rng, axis.n, PRESSURE_DRIFT_PHI_PER_HOUR**axis.step_hours, PRESSURE_DRIFT_SD_PSI)
    drift = PRESSURE_DRIFT_LIMIT_PSI * np.tanh(drift / PRESSURE_DRIFT_LIMIT_PSI)
    values = level - dips + drift + rng.normal(0.0, PRESSURE_NOISE_PSI, axis.n)
    sigma = math.hypot(PRESSURE_NOISE_PSI, PRESSURE_DRIFT_SD_PSI)  # white noise plus the benign slow drift
    deepest = max(depth for depth, _hour, _width in PRESSURE_DIPS) * demand
    return _Signal(spec, rng, values, sigma, {"level": level, "max_dip": deepest})


def _build_signal(spec: SensorSpec, axis: TimeAxis, drivers: RegionalDrivers, seed: int) -> _Signal:
    threshold_for(spec.sensor_type, spec.placement)  # KeyError for an undefined (type, placement) class
    rng = sensor_rng(spec.sensor_id, seed)
    if spec.sensor_type == "temperature":
        return _temperature_signal(spec, axis, drivers, rng)
    if spec.sensor_type == "vibration":
        return _vibration_signal(spec, axis, rng)
    if spec.sensor_type == "moisture":
        return _moisture_signal(spec, axis, drivers, rng)
    if spec.sensor_type == "pressure":
        return _pressure_signal(spec, axis, rng)
    raise ValueError(f"unknown sensor type {spec.sensor_type!r} ({spec.sensor_id})")


def work_sigmas(signals: Sequence[_Signal]) -> dict[str, float]:
    """Noise scale of every sensor as the detector sees it: its own noise, but not below the detector's floor."""
    class_size: dict[tuple[str, str], int] = {}
    for signal in signals:
        key = (signal.spec.sensor_type, signal.spec.placement)
        class_size[key] = class_size.get(key, 0) + 1
    sigmas: dict[str, float] = {}
    for signal in signals:
        spec = signal.spec
        floor = WORK_SIGMA_FLOOR[spec.sensor_type]
        if spec.sensor_type == "temperature" and class_size[(spec.sensor_type, spec.placement)] < SMALL_CLASS_SIZE:
            floor = SMALL_CLASS_TEMPERATURE_FLOOR
        sigmas[spec.sensor_id] = max(signal.noise_sigma, floor)
    return sigmas


# --- event shapes -----------------------------------------------------------------------------------------------
def unit_profile(shape: str, steps: int, axis: TimeAxis) -> np.ndarray:
    """Shape of an event over its time steps, scaled to a peak of at most 1."""
    k = np.arange(steps, dtype=float)
    if shape in ("factor", "step"):
        return np.ones(steps)
    if shape == "ramp":
        return (k + 1.0) / steps
    if shape == "half_sine":
        return np.sin(np.pi * (k + 0.5) / steps)
    if shape == "rise_hold":
        return np.minimum(1.0, (k + 1.0) / axis.steps(MOISTURE_RISE_HOURS))
    raise ValueError(f"unknown event shape {shape!r}")


def minimum_work_magnitude(spec: EventSpec, profile: np.ndarray, sigma: float, axis: TimeAxis) -> float:
    """Smallest work-domain magnitude that satisfies the sigma rule for this event shape and duration."""
    if spec.short:
        return SHORT_MIN_SIGMA * sigma / float(profile.max())
    needed = min(axis.steps(SUSTAINED_MIN_HOURS), len(profile))
    return SUSTAINED_MIN_SIGMA * sigma / float(np.sort(profile)[::-1][needed - 1])


def scaled_event_mix(total: int) -> dict[str, int]:
    """Number of events per kind for a total: the reference mix scaled proportionally (largest remainders)."""
    exact = {spec.event_type: spec.count * total / REFERENCE_EVENT_TOTAL for spec in EVENT_SPECS}
    counts = {name: int(math.floor(value)) for name, value in exact.items()}
    order = sorted(exact, key=lambda name: (-(exact[name] - counts[name]), list(exact).index(name)))
    for name in order[: max(total - sum(counts.values()), 0)]:
        counts[name] += 1
    return counts


# --- co-located group -------------------------------------------------------------------------------------------
def find_colocated_group(
    sensors: Sequence[SensorSpec], excluded_assets: frozenset[str] = frozenset(), radius_m: float = COLOCATED_RADIUS_M
) -> list[SensorSpec]:
    """Four sensors on at least three assets, all within ``radius_m`` of each other.

    Among the candidates the group with the most sensor types, then the most assets, then the most sensors
    on showcase assets, then the smallest extent is chosen. The radius is widened when no group exists at
    150 m; an empty list means the network has no four such sensors at all.
    """
    pool = sorted((s for s in sensors if s.asset_id not in excluded_assets), key=lambda s: s.sensor_id)
    for radius in (radius_m, *(wider for wider in COLOCATED_WIDER_RADII_M if wider > radius_m)):
        best: tuple[tuple[float, ...], list[SensorSpec]] | None = None
        for anchor_index, anchor in enumerate(pool):
            near = [
                (geo.haversine(anchor.lon, anchor.lat, other.lon, other.lat), other)
                for other in pool[anchor_index + 1 :]
            ]
            neighbours = [other for distance, other in sorted(near, key=lambda item: (item[0], item[1].sensor_id))
                          if distance <= radius][:COLOCATED_MAX_NEIGHBOURS]  # fmt: skip
            for trio in combinations(neighbours, COLOCATED_SIZE - 1):
                group = [anchor, *trio]
                if len({s.asset_id for s in group}) < COLOCATED_MIN_ASSETS:
                    continue
                extent = max(geo.haversine(a.lon, a.lat, b.lon, b.lat) for a, b in combinations(group, 2))
                if extent > radius:
                    continue
                score = (
                    float(len({s.sensor_type for s in group})),
                    float(len({s.asset_id for s in group})),
                    float(sum(TAG_SHOWCASE in s.asset_tags for s in group)),
                    -extent,
                )
                if best is None or score > best[0]:
                    best = (score, group)
        if best is not None:
            if radius > radius_m:
                logger.warning("no co-located sensor group within %.0f m; using one within %.0f m", radius_m, radius)
            return best[1]
    return []


# --- event schedule ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Planned:
    spec: EventSpec
    sensor: SensorSpec
    start: int
    steps: int
    magnitude: float  # native units, unsigned; vibration: factor
    role: str

    @property
    def end(self) -> int:
        """Index of the event's last time step (inclusive)."""
        return self.start + self.steps - 1


class _Scheduler:
    """Places the injected events on sensors and on the time axis (seeded, deterministic)."""

    def __init__(
        self,
        signals: dict[str, _Signal],
        sigmas: dict[str, float],
        axis: TimeAxis,
        drivers: RegionalDrivers,
        total: int,
        rng: np.random.Generator,
    ) -> None:
        self.signals = signals
        self.sigmas = sigmas
        self.axis = axis
        self.rng = rng
        self.last = axis.n - 1
        # "Moisture increase without rain": an injected moisture event keeps clear of every rain event and of
        # its dry-down (level 0); a shorter exclusion (level 1) is used only when the window leaves no room.
        self.rain_blocks = [np.zeros(axis.n, dtype=bool) for _ in range(2)]
        for start, steps, _depth in drivers.rain_events:
            begin = max(start - axis.steps(RAIN_GUARD_HOURS), 0)
            for block, effect in zip(self.rain_blocks, (RAIN_EFFECT_HOURS, RAIN_SHORT_EFFECT_HOURS)):
                block[begin : start + steps + axis.steps(effect)] = True
        self.quiet = min(axis.steps(QUIET_HOURS), axis.n // QUIET_WINDOW_DIVISOR)
        self.gap = axis.steps(EVENT_GAP_HOURS)
        self.remaining = scaled_event_mix(total)
        self.planned: list[_Planned] = []
        self.reserved: set[str] = set()  # sensors with a hand-placed event take no scheduled event
        self.group: list[SensorSpec] = []
        self.quantiles: dict[str, list[float]] = {}  # stratified magnitude positions of the scheduled events
        self.sensors = sorted((s.spec for s in signals.values()), key=lambda s: s.sensor_id)
        self.class_size: dict[tuple[str, str], int] = {}
        for sensor in self.sensors:
            key = (sensor.sensor_type, sensor.placement)
            self.class_size[key] = self.class_size.get(key, 0) + 1

    # -- helpers
    def _uniform_int(self, low: float, high: float) -> int:
        low_i, high_i = int(math.ceil(low)), int(math.floor(high))
        return int(self.rng.integers(low_i, max(low_i, high_i) + 1))

    def _lead_start(self, low_hours: float, high_hours: float) -> int:
        """Start index between ``high_hours`` and ``low_hours`` before the last timestamp (never in the lead-in)."""
        lead = self._uniform_int(self.axis.steps(low_hours), self.axis.steps(high_hours))
        return max(self.last - lead, self.quiet)

    def fits(self, sensor: SensorSpec, start: int, steps: int, relax: int = 0) -> bool:
        """True when the sensor may take an event there.

        At most 2 events and 144 event-hours per sensor, no overlap with its other event, one event at a time
        in a small placement class, and no rain during a moisture event (``relax`` 1 shortens that exclusion,
        2 drops it).
        """
        end = start + steps - 1
        rain_matters = sensor.sensor_type == "moisture" and relax < len(self.rain_blocks)
        if rain_matters and self.rain_blocks[relax][max(start, 0) : end + 1].any():
            return False
        mine = [p for p in self.planned if p.sensor.sensor_id == sensor.sensor_id]
        if len(mine) >= MAX_EVENTS_PER_SENSOR:
            return False
        if (sum(p.steps for p in mine) + steps) * self.axis.step_hours > MAX_EVENT_HOURS_PER_SENSOR:
            return False
        if any(start <= p.end + self.gap and end >= p.start - self.gap for p in mine):
            return False
        key = (sensor.sensor_type, sensor.placement)
        if self.class_size[key] < EXCLUSIVE_CLASS_SIZE:
            for p in self.planned:
                same_class = (p.sensor.sensor_type, p.sensor.placement) == key
                if same_class and start <= p.end + self.gap and end >= p.start - self.gap:
                    return False
        return True

    def _quantile(self, event_type: str) -> float:
        """Position of the next magnitude in its range: stratified for scheduled events, else uniform."""
        waiting = self.quantiles.get(event_type)
        return waiting.pop() if waiting else float(self.rng.uniform())

    def magnitude(
        self, spec: EventSpec, sensor: SensorSpec, steps: int, bounds: tuple[float, float] | None = None
    ) -> float:
        """Draw the event's magnitude from its range, within the sensor's physical limits and the sigma rule."""
        low, high = bounds or spec.magnitude
        info = self.signals[sensor.sensor_id].info
        if spec.event_type == "pressure_drop" and bounds is None:
            high = max(low, min(high, info["level"] - info["max_dip"] - PRESSURE_DROP_FLOOR_PSI))
        position = self._quantile(spec.event_type) if bounds is None else float(self.rng.uniform())
        value = low + position * (high - low)
        profile = unit_profile(spec.shape, steps, self.axis)
        needed = minimum_work_magnitude(spec, profile, self.sigmas[sensor.sensor_id], self.axis) * SIGMA_RULE_MARGIN
        if spec.shape == "factor":
            return max(value, math.exp(needed))
        return max(value, needed)

    def add(self, spec: EventSpec, sensor: SensorSpec, start: int, steps: int, magnitude: float, role: str) -> None:
        """Record a planned event; a sensor with a hand-placed (non-scheduled) event takes no scheduled one."""
        self.planned.append(_Planned(spec, sensor, start, steps, magnitude, role))
        self.remaining[spec.event_type] -= 1
        if role != "scheduled":
            self.reserved.add(sensor.sensor_id)

    def _available(self, event_type: str) -> bool:
        return self.remaining.get(event_type, 0) > 0

    def _pick(self, candidates: Sequence[SensorSpec]) -> SensorSpec:
        return candidates[int(self.rng.integers(len(candidates)))]

    def _to_end(self, spec: EventSpec, sensor: SensorSpec, start: int, role: str, bounds: tuple[float, float]) -> bool:
        steps = self.last - start + 1
        if steps < 1 or not any(self.fits(sensor, start, steps, relax) for relax in (0, 1, 2)):
            return False
        self.add(spec, sensor, start, steps, self.magnitude(spec, sensor, steps, bounds), role)
        return True

    # -- the events that are still running at the last timestamp
    def place_bridge_vibration(self) -> None:
        """Sustained high vibration on the highway bridge matched to the bridge inventory, ongoing at the end."""
        spec = EVENT_SPEC_BY_TYPE["sustained_high_vibration"]
        if not self._available(spec.event_type):
            return
        vibration = [s for s in self.sensors if s.sensor_type == "vibration"]
        for pool in (
            [s for s in vibration if TAG_NBI_BRIDGE in s.asset_tags],
            [s for s in vibration if s.placement == "bridge_deck" and s.asset_type == "bridge"],
            [s for s in vibration if s.placement == "bridge_deck"],
            vibration,
        ):
            if pool:
                start = self._lead_start(*FINAL_BRIDGE_LEAD_HOURS)
                self._to_end(spec, pool[0], start, "final_hour", (FINAL_BRIDGE_MIN_FACTOR, spec.magnitude[1]))
                return

    def select_colocated_group(self) -> None:
        """Choose the sensors of the co-located group (their events are placed by ``place_colocated_group``)."""
        early = self.last - self.axis.steps(COLOCATED_START_HOURS[0])
        if early < self.quiet:
            logger.warning("window too short for the co-located group; the events are scheduled individually")
            return
        used_assets = frozenset(p.sensor.asset_id for p in self.planned)
        self.group = find_colocated_group(self.sensors, used_assets)
        if not self.group:
            logger.warning("no co-located sensor group can be formed; the events are scheduled individually")

    def place_colocated_group(self) -> None:
        """Four events on neighbouring assets that start within 36 h of each other; two run to the end."""
        group = self.group
        if not group or sum(self.remaining.values()) < len(group):
            self.group = []
            return
        early = self.last - self.axis.steps(COLOCATED_START_HOURS[0])
        late = self.last - self.axis.steps(COLOCATED_START_HOURS[1])
        order = [group[i] for i in self.rng.permutation(len(group))]
        ongoing: list[SensorSpec] = []
        for sensor in order:  # two members keep running to the last timestamp
            spec = EVENT_SPEC_BY_TYPE[_ONGOING_TYPE[sensor.sensor_type]]
            if len(ongoing) == 2 or not self._available(spec.event_type):
                continue
            if spec.shape == "ramp":
                start = self._uniform_int(early, self.last - self.axis.steps(RAMP_LEAD_HOURS))
            elif spec.event_type == "sustained_high_vibration":
                start = self._uniform_int(max(early, self.last - self.axis.steps(spec.hours[1]) + 1), late)
            else:
                start = self._uniform_int(early, late)
            low = (spec.magnitude[0] + spec.magnitude[1]) / 2.0  # upper half of the range: clearly visible
            if self._to_end(spec, sensor, start, "colocated_final_hour", (low, spec.magnitude[1])):
                ongoing.append(sensor)
        for sensor in order:  # the others start in the same 36 h and end well before the last timestamp
            if sensor in ongoing:
                continue
            for event_type in _ENDED_TYPES[sensor.sensor_type]:
                spec = EVENT_SPEC_BY_TYPE[event_type]
                if not self._available(event_type):
                    continue
                start = self._uniform_int(early, late)
                longest = min(self.axis.steps(spec.hours[1]), self.last - self.gap - start + 1)
                shortest = min(self.axis.steps(spec.hours[0]), longest)
                cap = self.axis.steps(COLOCATED_ENDED_MAX_HOURS)
                steps = self._uniform_int(shortest, max(shortest, min(longest, cap)))
                if steps >= 1 and any(self.fits(sensor, start, steps, relax) for relax in (0, 1, 2)):
                    self.add(spec, sensor, start, steps, self.magnitude(spec, sensor, steps), "colocated")
                    break
        self.group = [s for s in group if any(p.sensor.sensor_id == s.sensor_id for p in self.planned)]

    def place_critical_pressure_drop(self) -> None:
        """A pressure drop sized to fall below the critical low limit, ongoing at the end."""
        spec = EVENT_SPEC_BY_TYPE["pressure_drop"]
        if not self._available(spec.event_type):
            return
        group_assets = {s.asset_id for s in self.group}
        pool = [s for s in self.sensors if s.sensor_type == "pressure" and s.sensor_id not in self.reserved
                and s.asset_id not in group_assets]  # fmt: skip
        if not pool:
            return
        sensor = min(pool, key=lambda s: (self.signals[s.sensor_id].info["level"], s.sensor_id))
        drop = max(spec.magnitude[0], self.signals[sensor.sensor_id].info["level"] - CRITICAL_DROP_TARGET_PSI)
        start = self._lead_start(*FINAL_DROP_LEAD_HOURS)
        self._to_end(spec, sensor, start, "final_hour", (drop, drop))

    def place_final(
        self,
        event_type: str,
        lead_hours: tuple[float, float],
        bounds: tuple[float, float],
        ceiling: float | None = None,
    ) -> None:
        """One more event of a kind that is ongoing at the end, on a sensor away from the co-located group.

        With a ``ceiling`` the event is sized to stay below that level (the sensor's warning limit), on a
        sensor that has room for it.
        """
        spec = EVENT_SPEC_BY_TYPE[event_type]
        if not self._available(event_type):
            return
        busy_assets = {p.sensor.asset_id for p in self.planned} | {s.asset_id for s in self.group}
        pool = [s for s in self.sensors if s.sensor_type == spec.sensor_type and s.asset_id not in busy_assets]
        roomy = [s for s in pool if self.class_size[(s.sensor_type, s.placement)] >= EXCLUSIVE_CLASS_SIZE]
        start = self._lead_start(*lead_hours)
        candidates: list[SensorSpec] = []
        for relax in (0, 1, 2):
            candidates = [s for s in (roomy or pool) if self.fits(s, start, self.last - start + 1, relax)]
            if candidates:
                break
        if not candidates:
            return
        if ceiling is None:
            self._to_end(spec, self._pick(candidates), start, "final_hour", bounds)
            return
        recent = self.axis.steps(RECENT_HOURS)
        room = {
            s.sensor_id: ceiling - WARNING_CLEARANCE - float(np.max(self.signals[s.sensor_id].values[-recent:]))
            for s in candidates
        }
        spacious = [s for s in candidates if room[s.sensor_id] >= bounds[1]]
        sensor = self._pick(spacious) if spacious else max(candidates, key=lambda s: (room[s.sensor_id], s.sensor_id))
        high = max(spec.magnitude[0], min(bounds[1], room[sensor.sensor_id]))
        self._to_end(spec, sensor, start, "final_hour", (min(bounds[0], high), high))

    # -- everything else: one event per equal slice of the window after the lead-in
    def place_scheduled(self) -> None:
        """Place the remaining events: start times stratified over the window, sensors and sizes drawn."""
        kinds = [spec for spec in EVENT_SPECS for _ in range(max(self.remaining[spec.event_type], 0))]
        kinds = [kinds[i] for i in self.rng.permutation(len(kinds))]
        count = len(kinds)
        if count == 0:
            return
        upper = self.last - self.gap  # last index an event may occupy
        if upper <= self.quiet:
            logger.warning("window too short to schedule %d injected event(s)", count)
            return
        for spec in EVENT_SPECS:  # magnitudes cover each kind's range evenly instead of clustering by chance
            share = sum(1 for kind in kinds if kind.event_type == spec.event_type)
            positions = [(k + float(self.rng.uniform())) / share for k in range(share)]
            self.quantiles[spec.event_type] = [positions[i] for i in self.rng.permutation(share)]
        width = (upper - self.quiet + 1) / count
        slices = [(self.quiet + k * width, self.quiet + (k + 1) * width) for k in range(count)]
        durations = [self._uniform_int(self.axis.steps(s.hours[0]), self.axis.steps(s.hours[1])) for s in kinds]
        free = set(range(count))
        left_out: dict[str, int] = {}
        for index in sorted(range(count), key=lambda i: -durations[i]):  # the longest events choose first
            spec, steps = kinds[index], durations[index]
            eligible = [k for k in sorted(free) if math.ceil(slices[k][0]) + steps - 1 <= upper]
            if not eligible:  # shorten the event so that it fits the earliest free slice
                eligible = [min(free)]
                steps = max(1, upper - math.ceil(slices[eligible[0]][0]) + 1)
            pool = [s for s in self.sensors if s.sensor_type == spec.sensor_type and s.sensor_id not in self.reserved]
            placed = False
            for relax in (0, 1, 2) if spec.sensor_type == "moisture" else (0,):
                for slot in [eligible[i] for i in self.rng.permutation(len(eligible))]:
                    low = math.ceil(slices[slot][0])
                    high = min(math.ceil(slices[slot][1]) - 1, upper - steps + 1)
                    for _attempt in range(SLICE_ATTEMPTS):  # a slice may be only partly usable (rain, busy sensors)
                        start = self._uniform_int(low, max(low, high))
                        candidates = [s for s in pool if self.fits(s, start, steps, relax)]
                        if candidates:
                            sensor = self._pick(candidates)
                            self.add(spec, sensor, start, steps, self.magnitude(spec, sensor, steps), "scheduled")
                            free.discard(slot)
                            placed = True
                            break
                    if placed:
                        break
                if placed:
                    break
            if not placed:
                left_out[spec.event_type] = left_out.get(spec.event_type, 0) + 1
        if left_out:
            logger.warning(
                "%d injected event(s) could not be placed on a free sensor and are left out: %s",
                sum(left_out.values()), ", ".join(f"{name} {count}" for name, count in sorted(left_out.items())),
            )  # fmt: skip


def plan_events(
    signals: dict[str, _Signal],
    sigmas: dict[str, float],
    axis: TimeAxis,
    drivers: RegionalDrivers,
    settings: Settings,
    rng: np.random.Generator,
) -> _Scheduler:
    """Schedule every injected event (final-hour set, co-located group, then the stratified rest)."""
    scheduler = _Scheduler(signals, sigmas, axis, drivers, settings.SIM_ANOMALY_EVENTS, rng)
    if settings.SIM_ANOMALY_EVENTS <= 0 or not signals:
        return scheduler
    # The events that are still running at the last timestamp come first (they must exist even for a small
    # SIM_ANOMALY_EVENTS), on sensors away from the co-located group; then the group, then the rest.
    scheduler.place_bridge_vibration()
    scheduler.select_colocated_group()
    scheduler.place_critical_pressure_drop()
    moisture_warning = threshold_for("moisture", "road_subgrade").warn_high
    scheduler.place_final("moisture_increase", FINAL_MOISTURE_LEAD_HOURS, FINAL_MOISTURE_PCT, ceiling=moisture_warning)
    scheduler.place_final("temperature_drift", FINAL_DRIFT_LEAD_HOURS, FINAL_DRIFT_C)
    scheduler.place_colocated_group()
    scheduler.place_scheduled()
    return scheduler


# --- injection --------------------------------------------------------------------------------------------------
def _describe_event(planned: _Planned, axis: TimeAxis, ongoing: bool) -> str:
    spec = planned.spec
    hours = planned.steps * axis.step_hours
    unit = planned.sensor.unit
    if spec.shape == "factor":
        size = f"{planned.magnitude:.1f} times the normal level"
    elif spec.shape == "ramp":
        size = f"gradual change reaching {spec.sign * planned.magnitude:+.1f} {unit}"
    else:
        size = f"{spec.sign * planned.magnitude:+.1f} {unit}"
    tail = "ongoing at the end of the simulated window" if ongoing else f"{hours:.0f} h"
    text = f"Injected simulated event: {spec.label}, {size}, {tail}"
    if planned.role.startswith("colocated"):
        text += "; part of the co-located group"
    return text


def _inject(planned: _Planned, signal: _Signal, sigma: float, axis: TimeAxis) -> EventDetail:
    """Add the event to the sensor's signal and return its ground truth."""
    spec = planned.spec
    profile = unit_profile(spec.shape, planned.steps, axis)
    window = slice(planned.start, planned.start + planned.steps)
    if spec.shape == "factor":
        deviation = math.log(planned.magnitude) * profile
        signal.values[window] *= np.exp(deviation)
    else:
        deviation = spec.sign * planned.magnitude * profile
        signal.values[window] += deviation
    last = axis.n - 1
    event = InjectedEvent(
        event_type=spec.event_type,
        is_anomaly=True,
        started_at=axis.timestamps[planned.start],
        ended_at=axis.timestamps[planned.end],
        sensor_id=planned.sensor.sensor_id,
        asset_id=planned.sensor.asset_id,
        sensor_type=planned.sensor.sensor_type,
        magnitude=round(planned.magnitude if spec.shape == "factor" else spec.sign * planned.magnitude, 3),
        description=_describe_event(planned, axis, planned.end == last),
    )
    return EventDetail(event, planned.start, planned.end, deviation, sigma, spec.short, planned.role, axis.step_hours)


def _add_bursts(signal: _Signal, planned: Sequence[_Planned], axis: TimeAxis, sigma: float) -> list[EventDetail]:
    """Benign one-step vibration bursts (about six per sensor-month), away from the sensor's injected events."""
    rng = signal.rng
    expected = VIBRATION_BURSTS_PER_MONTH * axis.n * axis.step_hours / HOURS_PER_MONTH
    blocked = np.zeros(axis.n, dtype=bool)
    margin = axis.steps(BURST_CLEARANCE_HOURS)
    for event in planned:
        blocked[max(event.start - margin, 0) : event.end + margin + 1] = True
    bursts: list[EventDetail] = []
    for index in sorted(int(i) for i in rng.integers(0, axis.n, int(rng.poisson(expected)))):
        factor = float(rng.uniform(*VIBRATION_BURST_FACTOR))
        if blocked[index]:
            continue
        blocked[max(index - margin, 0) : index + margin + 1] = True  # bursts never sit next to each other
        signal.values[index] *= factor
        event = InjectedEvent(
            event_type=BENIGN_BURST,
            is_anomaly=False,
            started_at=axis.timestamps[index],
            ended_at=axis.timestamps[index],
            sensor_id=signal.spec.sensor_id,
            asset_id=signal.spec.asset_id,
            sensor_type="vibration",
            magnitude=round(factor, 3),
            description=f"Benign simulated burst: {factor:.2f} times the normal level for one reading",
        )
        bursts.append(
            EventDetail(event, index, index, np.array([math.log(factor)]), sigma, True, "benign", axis.step_hours)
        )
    return bursts


# --- benign regional events -------------------------------------------------------------------------------------
def _mismatch_sigma(own: np.ndarray, shared: np.ndarray, sigma: float) -> float:
    """Largest part of a sensor's response that a multiple of its class's shared response does not explain."""
    energy = float(np.dot(shared, shared))
    if energy <= 0.0:
        return 0.0
    beta = float(np.dot(own, shared)) / energy
    return float(np.max(np.abs(own - beta * shared)) / sigma)


def _regional_events(
    signals: Sequence[_Signal], sigmas: dict[str, float], drivers: RegionalDrivers, axis: TimeAxis
) -> tuple[list[InjectedEvent], list[dict[str, float]]]:
    """Ground truth of the benign regional events and, per sensor, the size of its sensor-specific part.

    A regional event moves every sensor of a placement class together. What is specific to one sensor is the
    difference between its own response (own gain and time constant) and the best multiple of the response
    of its class (nominal time constant); that difference, in units of the sensor's noise sigma, is the size
    that matters to a detector that compares sensors with their peers.
    """
    events: list[InjectedEvent] = []
    sizes: list[dict[str, float]] = []
    last = axis.n - 1
    for start, steps, depth in drivers.rain_events:
        end = min(start + steps - 1 + axis.steps(RAIN_EFFECT_HOURS), last)
        only = np.zeros(axis.n)
        only[start : start + steps] = depth / steps
        size: dict[str, float] = {}
        for signal in signals:
            if "rain" not in signal.regional:
                continue
            gain, tau = signal.regional["rain"]
            nominal = MOISTURE_MODELS[signal.spec.placement]["tau"]
            own = gain * _wetness(only, tau, axis.step_hours)
            shared = _wetness(only, nominal, axis.step_hours)
            size[signal.spec.sensor_id] = _mismatch_sigma(own, shared, sigmas[signal.spec.sensor_id])
        events.append(
            InjectedEvent(
                event_type=REGIONAL_RAIN,
                is_anomaly=False,
                started_at=axis.timestamps[start],
                ended_at=axis.timestamps[end],
                sensor_type="moisture",
                magnitude=round(depth, 1),
                description=(
                    f"Simulated regional rain: {depth:.0f} mm over {steps * axis.step_hours:.0f} h, followed by "
                    "the dry-down. Benign: moisture rises at every moisture sensor."
                ),
            )
        )
        sizes.append(size)
    if drivers.hot_spell is not None:
        start, end = drivers.hot_spell
        size = {}
        for signal in signals:
            if "air" not in signal.regional:
                continue
            gain, lag = signal.regional["air"]
            nominal = TEMPERATURE_MODELS[signal.spec.placement]["lag"]
            own = gain * _lagged(drivers.hot_component, lag, axis.step_hours)[drivers.lead :]
            shared = _lagged(drivers.hot_component, nominal, axis.step_hours)[drivers.lead :]
            size[signal.spec.sensor_id] = _mismatch_sigma(own, shared, sigmas[signal.spec.sensor_id])
        events.append(
            InjectedEvent(
                event_type=REGIONAL_HOT_SPELL,
                is_anomaly=False,
                started_at=axis.timestamps[start],
                ended_at=axis.timestamps[end],
                sensor_type="temperature",
                magnitude=HOT_SPELL_C,
                description=(
                    f"Simulated regional hot spell: air temperature about {HOT_SPELL_C:.0f} °C above the "
                    f"seasonal mean for {(end - start + 1) * axis.step_hours:.0f} h. Benign: every temperature "
                    "sensor warms."
                ),
            )
        )
        sizes.append(size)
    return events, sizes


# --- dropouts ---------------------------------------------------------------------------------------------------
def plan_dropouts(
    sensors: Sequence[SensorSpec], planned: Sequence[_Planned], axis: TimeAxis, rate: float, rng: np.random.Generator
) -> dict[str, list[tuple[int, int]]]:
    """Missing-reading gaps: two outages that run through the last timestamp plus random short gaps.

    The final outages (30 h and 6 h) are on sensors without an injected event, preferably the only sensor of
    their asset. A random gap of 2 to 10 hours never hides a short event or the onset of a long one.
    """
    last = axis.n - 1
    gaps: dict[str, list[tuple[int, int]]] = {}
    if not sensors:
        return gaps
    ordered = sorted(sensors, key=lambda s: s.sensor_id)
    per_asset: dict[str, int] = {}
    for sensor in ordered:
        per_asset[sensor.asset_id] = per_asset.get(sensor.asset_id, 0) + 1
    event_sensors = {p.sensor.sensor_id for p in planned}
    event_assets = {p.sensor.asset_id for p in planned}
    quiet = [s for s in ordered if s.sensor_id not in event_sensors and s.asset_id not in event_assets]
    sole = [s for s in quiet if per_asset[s.asset_id] == 1]
    pool = sole if len(sole) >= len(FINAL_OUTAGE_HOURS) else (quiet or ordered)
    picks: list[SensorSpec] = []
    for index in rng.permutation(len(pool)):  # sensors of different types, so the outages are easy to tell apart
        if len(picks) < len(FINAL_OUTAGE_HOURS) and all(pool[index].sensor_type != p.sensor_type for p in picks):
            picks.append(pool[index])
    picks += [s for s in pool if s not in picks][: len(FINAL_OUTAGE_HOURS) - len(picks)]
    for sensor, hours in zip(picks, FINAL_OUTAGE_HOURS):
        gaps[sensor.sensor_id] = [(max(last - axis.steps(hours) + 1, 0), last)]

    others = [s for s in ordered if s.sensor_id not in gaps]
    count = min(int(round(rate * len(ordered))), len(others))
    guard = axis.steps(EVENT_GAP_HOURS)
    onset = axis.steps(SUSTAINED_MIN_HOURS)
    for position in sorted(int(i) for i in rng.permutation(len(others))[:count]):
        sensor = others[position]
        protected = np.zeros(axis.n, dtype=bool)
        for event in planned:
            if event.sensor.sensor_id != sensor.sensor_id:
                continue
            stop = event.end if event.steps <= guard else event.start + onset
            margin = DROPOUT_EVENT_MARGIN_STEPS
            protected[max(event.start - margin, 0) : min(stop + margin, last) + 1] = True
        for _attempt in range(DROPOUT_ATTEMPTS):
            length = int(rng.integers(axis.steps(DROPOUT_HOURS[0]), axis.steps(DROPOUT_HOURS[1]) + 1))
            high = last - guard - length
            if high <= 0:
                break
            start = int(rng.integers(0, high + 1))
            if not protected[start : start + length].any():
                gaps[sensor.sensor_id] = [(start, start + length - 1)]
                break
    return gaps


# --- entry point ------------------------------------------------------------------------------------------------
def simulate(sensors: Sequence[SensorSpec], settings: Settings) -> SimulationResult:
    """Simulate every sensor over the configured window and return the readings with their ground truth."""
    axis = build_time_axis(settings)
    seed = settings.SIM_SEED & 0xFFFFFFFF
    ordered = sorted(sensors, key=lambda s: s.sensor_id)
    if len({s.sensor_id for s in ordered}) != len(ordered):
        raise ValueError("sensor ids must be unique")
    drivers = build_drivers(axis, seed)
    signals = {spec.sensor_id: _build_signal(spec, axis, drivers, seed) for spec in ordered}
    sigmas = work_sigmas(list(signals.values()))

    schedule_rng = np.random.default_rng([seed, 1])
    scheduler = plan_events(signals, sigmas, axis, drivers, settings, schedule_rng)
    planned = sorted(scheduler.planned, key=lambda p: (p.start, p.sensor.sensor_id))

    bursts: list[EventDetail] = []
    for sensor_id, signal in signals.items():
        if signal.spec.sensor_type == "vibration":
            mine = [p for p in planned if p.sensor.sensor_id == sensor_id]
            bursts.extend(_add_bursts(signal, mine, axis, sigmas[sensor_id]))
    details = [_inject(p, signals[p.sensor.sensor_id], sigmas[p.sensor.sensor_id], axis) for p in planned]
    regional_events, regional_sigma = _regional_events(list(signals.values()), sigmas, drivers, axis)

    # Physical limits (never reached by the constants above; a guard against absurd values).
    for signal in signals.values():
        if signal.spec.sensor_type == "moisture":
            np.clip(signal.values, *MOISTURE_LIMITS_PCT, out=signal.values)
        elif signal.spec.sensor_type == "pressure":
            np.clip(signal.values, PRESSURE_MIN_PSI, None, out=signal.values)
        elif signal.spec.sensor_type == "vibration":
            np.clip(signal.values, 10.0**-VALUE_DECIMALS, None, out=signal.values)

    dropouts = plan_dropouts(ordered, planned, axis, settings.SIM_DROPOUT_RATE, np.random.default_rng([seed, 2]))
    for sensor_id, ranges in dropouts.items():
        for start, end in ranges:
            signals[sensor_id].values[start : end + 1] = np.nan

    result = SimulationResult(
        timestamps=list(axis.timestamps),
        sensors=ordered,
        values={sensor_id: signal.values for sensor_id, signal in signals.items()},
        events=details,
        benign_bursts=bursts,
        regional_events=regional_events,
        regional_sigma=regional_sigma,
        noise_sigma={sensor_id: signal.noise_sigma for sensor_id, signal in signals.items()},
        work_sigma=sigmas,
        dropouts=dropouts,
        colocated_group=[s.sensor_id for s in scheduler.group],
    )
    logger.info(
        "simulated %d sensors x %d steps: %d readings, %d injected events (%d ongoing at the end), "
        "%d benign regional events, %d sensors with gaps",
        len(ordered), axis.n, result.reading_count(), len(details), len(result.ongoing_at_end()),
        len(regional_events), len(dropouts),
    )  # fmt: skip
    return result
