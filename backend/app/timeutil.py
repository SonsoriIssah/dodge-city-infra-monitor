"""Timestamps of the API: one output format, one parser (build contract 10.1).

Every timestamp in every response is ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, whole seconds). In Python it is produced
by ``iso_z``; in SQL by the equivalent ``to_char`` expression from ``sql_iso_z``. Datetimes received from
clients are ISO 8601; a value without a UTC offset is UTC.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

ISO_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ISO_Z_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
SQL_ISO_Z_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
# A "+" of a UTC offset arrives as a space when the query string was not percent-encoded
# ("...T12:00:00+00:00" -> "...T12:00:00 00:00"); this pattern recognises that case.
_UNENCODED_OFFSET = re.compile(r"(T\d{2}(?::?\d{2}){0,2}(?:\.\d+)?) (\d{2}(?::?\d{2})?)$")


def to_utc(value: datetime) -> datetime:
    """The same moment as an aware UTC datetime; a naive value is taken as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_z(value: datetime | None) -> str | None:
    """Format a timestamp as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); None stays None."""
    if value is None:
        return None
    return to_utc(value).strftime(ISO_Z_FORMAT)


def sql_iso_z(expression: str) -> str:
    """SQL expression that formats a timestamptz expression exactly like ``iso_z``.

    ``expression`` is a column reference written in the query module (never request input).
    """
    return f"to_char({expression} AT TIME ZONE 'UTC', '{SQL_ISO_Z_FORMAT}')"


def parse_datetime(text: str) -> datetime:
    """Parse an ISO 8601 date or datetime into an aware UTC datetime; ``ValueError`` when it is not one."""
    cleaned = _UNENCODED_OFFSET.sub(r"\1+\2", text.strip())
    if not cleaned:
        raise ValueError("expected an ISO 8601 datetime, for example 2026-09-15T12:00:00Z")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        raise ValueError(
            "expected an ISO 8601 datetime, for example 2026-09-15T12:00:00Z (a value without an offset is UTC)"
        ) from None
    return to_utc(parsed)
