"""Guard: the test session never writes to the main database (it runs as the last test of the session).

The fingerprint of the main database - the row count of every table, the md5 of all readings and of all
anomalies, and the latest detection run - is taken by the ``main_database_guard`` fixture before the test
database is created. This module compares it again after every other test has run; the fixture repeats the
comparison once more when the session is torn down.
"""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.conftest import main_fingerprint

pytestmark = pytest.mark.db


def test_the_suite_works_on_a_separate_database_whose_name_ends_with_test(database_targets, test_db):
    assert test_db.name == database_targets.test_name
    assert test_db.name.endswith("_test")
    assert test_db.name != database_targets.main_name
    # every stage, the API and the exporter get their connection from these settings
    settings_database = conninfo_to_dict(test_db.settings.dsn).get("dbname")  # (kept out of the assert: no DSN in a report)
    assert settings_database == test_db.name


def test_main_database_is_unchanged_after_the_whole_session(database_targets, main_database_guard, test_db):
    before, after = main_database_guard, main_fingerprint(database_targets)
    assert after == before
    if "counts" in before:  # the main database is populated: say what was compared
        assert before["counts"]["sensor_readings"] == after["counts"]["sensor_readings"]
        assert before["readings_md5"] == after["readings_md5"]
