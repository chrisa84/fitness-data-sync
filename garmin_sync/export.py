"""
Export functions: write normalised data to JSON or CSV files.

All functions call queries.py for data — no direct SQL here.
Never call Garmin. Never write to sync tables (sync_runs, sync_cursor, raw_payload).
"""

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

from garmin_sync import queries


def _default_range(
    from_date: str | None, to_date: str | None
) -> tuple[str, str]:
    return from_date or "1900-01-01", to_date or date.today().isoformat()


def export_activities_json(
    conn: sqlite3.Connection,
    output_path: Path,
    from_date: str | None = None,
    to_date: str | None = None,
) -> int:
    """Write activities to a JSON file. Returns row count written."""
    from_d, to_d = _default_range(from_date, to_date)
    rows = queries.get_recent_activities(conn, limit=None, from_date=from_d, to_date=to_d)
    output_path.write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8"
    )
    return len(rows)


def export_health_json(
    conn: sqlite3.Connection,
    output_path: Path,
    from_date: str | None = None,
    to_date: str | None = None,
) -> int:
    """Write daily health (all tables joined) to a JSON file. Returns row count written."""
    from_d, to_d = _default_range(from_date, to_date)
    rows = queries.get_daily_health(conn, from_d, to_d)
    output_path.write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8"
    )
    return len(rows)


def export_activities_csv(
    conn: sqlite3.Connection,
    output_path: Path,
    from_date: str | None = None,
    to_date: str | None = None,
) -> int:
    """Write activities to a CSV file. Returns row count written."""
    from_d, to_d = _default_range(from_date, to_date)
    rows = queries.get_recent_activities(conn, limit=None, from_date=from_d, to_date=to_d)
    if not rows:
        output_path.write_text("", encoding="utf-8")
        return 0
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_health_csv(
    conn: sqlite3.Connection,
    output_path: Path,
    from_date: str | None = None,
    to_date: str | None = None,
) -> int:
    """Write daily health data to a CSV file. Returns row count written."""
    from_d, to_d = _default_range(from_date, to_date)
    rows = queries.get_daily_health(conn, from_d, to_d)
    if not rows:
        output_path.write_text("", encoding="utf-8")
        return 0
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
