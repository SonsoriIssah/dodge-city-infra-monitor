"""Settings (build contract section 4.2): defaults, DSN precedence, bbox order, time axis. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import ValidationError

from pipeline import config
from pipeline.config import BBox, Settings, describe_dsn, parse_bbox
from pipeline.db import connection
from tests.support import SPEC_DEFAULTS


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every settings variable from the process environment."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    return monkeypatch


def write_env(path: Path, **values: str) -> Path:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8", newline="\n")
    return path


# --- defaults -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "expected"), sorted(SPEC_DEFAULTS.items()), ids=sorted(SPEC_DEFAULTS))
def test_every_contract_setting_exists_with_its_default(clean_env, name, expected):
    settings = Settings(_env_file=None)
    assert name in Settings.model_fields, f"setting {name} of the contract is missing"
    assert getattr(settings, name) == expected


def test_every_setting_is_documented_in_env_example():
    documented = {
        line.split("=", 1)[0].strip()
        for line in (config.ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert set(SPEC_DEFAULTS) <= documented
    assert set(Settings.model_fields) <= documented


def test_env_example_holds_no_real_password():
    values = dict(
        line.split("=", 1)
        for line in (config.ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert values["POSTGRES_PASSWORD"] in ("change-me", "changeme", "")
    assert values["DATABASE_URL"] == ""
    assert values["INGEST_API_KEY"] == ""


# --- sources of values --------------------------------------------------------------------------------------------
def test_env_file_is_read_by_absolute_path_from_the_repository_root():
    assert config.ENV_FILE == config.ROOT / ".env"
    assert config.ENV_FILE.is_absolute()
    assert Settings.model_config["env_file"] == str(config.ENV_FILE)
    assert (config.ROOT / "pyproject.toml").is_file()


def test_values_come_from_the_env_file(clean_env, tmp_path):
    env_file = write_env(tmp_path / ".env", SIM_SEED="7", POSTGRES_PORT="6001", STUDY_AREA_SLUG="elsewhere")
    settings = Settings(_env_file=str(env_file))
    assert (settings.SIM_SEED, settings.POSTGRES_PORT, settings.STUDY_AREA_SLUG) == (7, 6001, "elsewhere")


def test_real_environment_variables_override_the_env_file(clean_env, tmp_path):
    env_file = write_env(tmp_path / ".env", SIM_SEED="7", SIM_DAYS="14")
    clean_env.setenv("SIM_SEED", "123")
    settings = Settings(_env_file=str(env_file))
    assert settings.SIM_SEED == 123  # environment wins
    assert settings.SIM_DAYS == 14  # the file still supplies what the environment does not


def test_an_empty_value_means_use_the_default(clean_env, tmp_path):
    env_file = write_env(tmp_path / ".env", SIM_SEED="", TIMEZONE="", INGEST_API_KEY="")
    settings = Settings(_env_file=str(env_file))
    assert settings.SIM_SEED == 42
    assert settings.TIMEZONE == "America/Chicago"
    assert settings.ingest_enabled is False


def test_lower_case_names_resolve_to_the_same_fields(clean_env):
    settings = Settings(_env_file=None, sim_days=10)
    assert settings.SIM_DAYS == 10
    assert settings.sim_days == 10
    assert settings.sim_seed == settings.SIM_SEED


# --- database connection string -----------------------------------------------------------------------------------
def test_dsn_is_built_from_the_parts_when_database_url_is_empty(clean_env):
    settings = Settings(
        _env_file=None,
        DATABASE_URL="",
        POSTGRES_HOST="db.internal",
        POSTGRES_PORT=6543,
        POSTGRES_DB="infra",
        POSTGRES_USER="svc",
        POSTGRES_PASSWORD="p w'd",
    )
    parts = conninfo_to_dict(settings.dsn)
    assert parts == {"host": "db.internal", "port": "6543", "dbname": "infra", "user": "svc", "password": "p w'd"}


def test_a_non_empty_database_url_wins_over_the_parts(clean_env):
    url = "postgresql://other:secret@elsewhere:5999/otherdb"
    settings = Settings(_env_file=None, DATABASE_URL=url, POSTGRES_HOST="ignored", POSTGRES_PORT=1, POSTGRES_DB="x")
    assert settings.dsn == url
    assert connection.dsn(settings) == url


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_database_url_does_not_win(clean_env, blank):
    settings = Settings(_env_file=None, DATABASE_URL=blank, POSTGRES_HOST="parts-host")
    assert conninfo_to_dict(settings.dsn)["host"] == "parts-host"


def test_port_defaults_to_5433_and_the_host_port_is_not_used_for_connecting(clean_env):
    settings = Settings(_env_file=None, POSTGRES_HOST_PORT=7777)
    assert conninfo_to_dict(settings.dsn)["port"] == "5433"


def test_test_dsn_defaults_to_the_same_server_with_database_infra_test(clean_env):
    settings = Settings(_env_file=None, POSTGRES_HOST="h", POSTGRES_USER="u", POSTGRES_PASSWORD="pw")
    main, test = conninfo_to_dict(settings.dsn), conninfo_to_dict(settings.test_dsn)
    assert test["dbname"] == "infra_test"
    assert {k: v for k, v in test.items() if k != "dbname"} == {k: v for k, v in main.items() if k != "dbname"}


def test_test_dsn_follows_database_url_and_test_database_url(clean_env):
    derived = Settings(_env_file=None, DATABASE_URL="postgresql://u:pw@h:5/prod")
    assert conninfo_to_dict(derived.test_dsn)["dbname"] == "infra_test"
    assert conninfo_to_dict(derived.test_dsn)["host"] == "h"
    explicit = Settings(_env_file=None, TEST_DATABASE_URL="postgresql://u:pw@h:5/custom_test")
    assert explicit.test_dsn == "postgresql://u:pw@h:5/custom_test"


def test_connections_set_utc_and_the_search_path():
    assert connection.CONNECTION_OPTIONS == "-c timezone=UTC -c search_path=infra,public"


def test_the_password_never_appears_in_summaries_or_reprs(clean_env):
    settings = Settings(
        _env_file=None, POSTGRES_PASSWORD="hunter2-secret", INGEST_API_KEY="key-secret", TEST_DATABASE_URL=""
    )
    for text in (settings.dsn_summary(), describe_dsn(settings.dsn), repr(settings), str(settings)):
        assert "hunter2-secret" not in text
        assert "key-secret" not in text
    assert settings.dsn_summary() == "host=localhost port=5433 dbname=infra user=infra"
    url = Settings(_env_file=None, DATABASE_URL="postgresql://u:url-secret@h:5/d")
    assert "url-secret" not in url.dsn_summary()
    assert "url-secret" not in repr(url)


def test_an_unparseable_dsn_is_not_echoed():
    assert "secret" not in describe_dsn("this is not a dsn password=secret ===")


# --- study area ---------------------------------------------------------------------------------------------------
def test_bbox_env_order_is_south_west_north_east_and_api_order_is_west_south_east_north(default_settings):
    assert default_settings.STUDY_AREA_BBOX == "37.745,-100.030,37.762,-100.005"
    assert default_settings.bbox == BBox(west=-100.030, south=37.745, east=-100.005, north=37.762)
    assert tuple(default_settings.bbox) == (-100.03, 37.745, -100.005, 37.762)
    assert default_settings.bbox.overpass == (37.745, -100.03, 37.762, -100.005)
    assert default_settings.bbox.center == pytest.approx((-100.0175, 37.7535))


def test_study_area_geometry_is_the_closed_bbox_rectangle(default_settings):
    geometry = default_settings.study_area_geometry()
    ring = geometry["coordinates"][0]
    assert geometry["type"] == "Polygon"
    assert ring[0] == ring[-1] and len(ring) == 5
    assert {tuple(point) for point in ring} == {
        (-100.03, 37.745), (-100.005, 37.745), (-100.005, 37.762), (-100.03, 37.762),
    }  # fmt: skip


def test_bbox_contains_includes_the_edge():
    box = parse_bbox("37.0,-100.0,38.0,-99.0")
    assert box.contains(-100.0, 37.0) and box.contains(-99.5, 37.5)
    assert not box.contains(-100.0001, 37.5) and not box.contains(-99.5, 38.0001)


@pytest.mark.parametrize(
    "text",
    [
        "37.745,-100.030,37.762",  # three numbers
        "a,b,c,d",
        "37.762,-100.030,37.745,-100.005",  # south above north
        "37.745,-100.005,37.762,-100.030",  # west east of east
        "-91,-100.03,37.7,-100.0",
        "37.745,-181,37.762,-100.005",
        "37.745,-100.030,37.745,-100.005",  # zero height
    ],
)
def test_invalid_bbox_strings_are_rejected(clean_env, text):
    with pytest.raises(ValueError):
        parse_bbox(text)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, STUDY_AREA_BBOX=text)


def test_unknown_timezone_is_rejected(clean_env):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TIMEZONE="Mars/Olympus_Mons")


# --- time axis ----------------------------------------------------------------------------------------------------
def test_default_time_axis_is_720_hourly_utc_steps_from_local_midnight(default_settings):
    axis = default_settings.time_axis()
    assert len(axis) == default_settings.sim_steps == 720
    assert axis[0] == datetime(2026, 9, 1, 5, 0, tzinfo=UTC)  # 00:00 at UTC-5
    assert axis[-1] == default_settings.sim_end_utc == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    assert all(moment.tzinfo is not None and moment.utcoffset() == timedelta(0) for moment in axis)
    assert {b - a for a, b in zip(axis, axis[1:])} == {timedelta(hours=1)}
    assert default_settings.sim_start_utc == axis[0]
    assert default_settings.sim_step == timedelta(minutes=60)


@pytest.mark.parametrize(("days", "minutes", "steps"), [(10, 60, 240), (14, 30, 672), (1, 15, 96), (30, 60, 720)])
def test_number_of_steps_follows_days_and_step(settings_factory, days, minutes, steps):
    settings = settings_factory(SIM_DAYS=days, SIM_STEP_MINUTES=minutes)
    axis = settings.time_axis()
    assert settings.sim_steps == len(axis) == steps
    assert axis[-1] - axis[0] == timedelta(minutes=minutes) * (steps - 1)


def test_a_sim_start_without_offset_is_local_time_of_the_study_area(settings_factory):
    settings = settings_factory(SIM_START=datetime(2026, 1, 15, 0, 0), TIMEZONE="America/Chicago")
    assert settings.sim_start_utc == datetime(2026, 1, 15, 6, 0, tzinfo=UTC)  # CST = UTC-6 in January


def test_the_sim_start_offset_is_respected_whatever_the_timezone(settings_factory):
    settings = settings_factory(TIMEZONE="Asia/Tokyo")
    assert settings.sim_start_utc == datetime(2026, 9, 1, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [("SIM_DAYS", 0), ("SIM_STEP_MINUTES", 0), ("SIM_DROPOUT_RATE", 1.5), ("SIM_ANOMALY_EVENTS", -1),
     ("RISK_REFERENCE", 0), ("HEALTH_HALF_LIFE_HOURS", 0), ("RISK_HEX_EDGE_M", -5)],
)  # fmt: skip
def test_out_of_range_values_are_rejected(clean_env, field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


# --- API helpers --------------------------------------------------------------------------------------------------
def test_cors_origins_are_split_on_commas(settings_factory):
    assert settings_factory().cors_origin_list == ["*"]
    listed = settings_factory(CORS_ORIGINS="https://a.example, https://b.example ,")
    assert listed.cors_origin_list == ["https://a.example", "https://b.example"]


def test_ingest_is_enabled_only_with_a_key(settings_factory):
    assert settings_factory().ingest_enabled is False
    assert settings_factory(INGEST_API_KEY="  ").ingest_enabled is False
    assert settings_factory(INGEST_API_KEY="k").ingest_enabled is True


def test_get_settings_is_cached_until_cleared():
    first = config.get_settings()
    assert config.get_settings() is first
    config.get_settings.cache_clear()
    try:
        assert config.get_settings() is not first
    finally:
        config.get_settings.cache_clear()
