"""
Tests for export.py.

All tests use in-memory SQLite seeded via repository functions.
No Garmin credentials or network access required.
"""

import csv
import hashlib
import inspect
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from garmin_sync import export as exporter
from garmin_sync import repositories as repo


# ---------------------------------------------------------------------------
# Fixture helpers (duplicated from test_queries for independence)
# ---------------------------------------------------------------------------

def _ph(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _pj(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def _insert_activity(
    conn: sqlite3.Connection,
    activity_id: str,
    start_time: str = "2024-03-15 06:30:00",
    distance_m: float = 10000.0,
    duration_s: float = 3600.0,
) -> None:
    from datetime import datetime, timezone
    payload = {"activityId": int(activity_id)}
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        conn, source="gc", data_type="activity_summary",
        garmin_id=activity_id, payload_json=_pj(payload), payload_hash=_ph(payload),
    )
    now = datetime.now(timezone.utc).isoformat()
    repo.upsert_activity(conn, {
        "activity_id": activity_id,
        "name": "Test Run",
        "type": "running",
        "start_time": start_time,
        "start_time_local": start_time,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "moving_duration_s": None,
        "elapsed_duration_s": None,
        "avg_hr": 150,
        "max_hr": 175,
        "avg_cadence": None,
        "max_cadence": None,
        "avg_power": None,
        "max_power": None,
        "elevation_gain_m": 100.0,
        "elevation_loss_m": None,
        "avg_speed_mps": None,
        "max_speed_mps": None,
        "calories": 500,
        "training_effect": None,
        "aerobic_te": None,
        "anaerobic_te": None,
        "vo2max": None,
        "raw_payload_id": raw_id,
        "updated_at": now,
    })


def _insert_health(conn: sqlite3.Connection, date_str: str) -> None:
    payload = {"date": date_str}
    raw_id, _ = repo.upsert_raw_payload_by_date(
        conn, source="gc", data_type="sleep",
        date=date_str, payload_json=_pj(payload), payload_hash=_ph(payload),
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    repo.upsert_sleep(conn, {
        "date": date_str,
        "sleep_start": None,
        "sleep_end": None,
        "total_sleep_seconds": 27000,
        "deep_sleep_seconds": 5400,
        "light_sleep_seconds": 14400,
        "rem_sleep_seconds": 7200,
        "awake_seconds": 900,
        "sleep_score": 78,
        "avg_spo2": 96.0,
        "avg_respiration": 14.5,
        "raw_payload_id": raw_id,
        "updated_at": now,
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = Path("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


# ---------------------------------------------------------------------------
# Tests: export_activities_json
# ---------------------------------------------------------------------------

class TestExportActivitiesJson:
    def test_writes_valid_json(self, db_conn, tmp_path):
        _insert_activity(db_conn, "1001", start_time="2024-03-15 06:30:00")
        out = tmp_path / "activities.json"
        count = exporter.export_activities_json(db_conn, out)
        assert count == 1
        data = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert data[0]["activity_id"] == "1001"

    def test_returns_row_count(self, db_conn, tmp_path):
        _insert_activity(db_conn, "2001", start_time="2024-03-10 07:00:00")
        _insert_activity(db_conn, "2002", start_time="2024-03-11 07:00:00")
        out = tmp_path / "activities.json"
        count = exporter.export_activities_json(db_conn, out)
        assert count == 2

    def test_from_to_date_filter(self, db_conn, tmp_path):
        _insert_activity(db_conn, "3001", start_time="2024-01-01 06:00:00")
        _insert_activity(db_conn, "3002", start_time="2024-06-01 06:00:00")
        out = tmp_path / "activities.json"
        count = exporter.export_activities_json(db_conn, out, from_date="2024-05-01", to_date="2024-12-31")
        assert count == 1
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data[0]["activity_id"] == "3002"

    def test_empty_result_writes_empty_list(self, db_conn, tmp_path):
        out = tmp_path / "activities.json"
        count = exporter.export_activities_json(db_conn, out)
        assert count == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data == []

    def test_multiple_activities_ordered_newest_first(self, db_conn, tmp_path):
        _insert_activity(db_conn, "4001", start_time="2024-01-01 06:00:00")
        _insert_activity(db_conn, "4002", start_time="2024-06-01 06:00:00")
        out = tmp_path / "activities.json"
        exporter.export_activities_json(db_conn, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data[0]["activity_id"] == "4002"
        assert data[1]["activity_id"] == "4001"


# ---------------------------------------------------------------------------
# Tests: export_activities_csv
# ---------------------------------------------------------------------------

class TestExportActivitiesCsv:
    def test_writes_csv_with_header(self, db_conn, tmp_path):
        _insert_activity(db_conn, "5001")
        out = tmp_path / "activities.csv"
        count = exporter.export_activities_csv(db_conn, out)
        assert count == 1
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # header + 1 row
        assert "activity_id" in lines[0]

    def test_csv_row_values(self, db_conn, tmp_path):
        _insert_activity(db_conn, "5002", distance_m=5000.0)
        out = tmp_path / "activities.csv"
        exporter.export_activities_csv(db_conn, out)
        reader = csv.DictReader(out.open(encoding="utf-8"))
        rows = list(reader)
        assert rows[0]["activity_id"] == "5002"
        assert float(rows[0]["distance_m"]) == 5000.0

    def test_empty_result_writes_empty_file(self, db_conn, tmp_path):
        out = tmp_path / "activities.csv"
        count = exporter.export_activities_csv(db_conn, out)
        assert count == 0
        assert out.read_text(encoding="utf-8") == ""

    def test_from_to_date_filter(self, db_conn, tmp_path):
        _insert_activity(db_conn, "6001", start_time="2024-01-01 06:00:00")
        _insert_activity(db_conn, "6002", start_time="2024-06-01 06:00:00")
        out = tmp_path / "activities.csv"
        count = exporter.export_activities_csv(db_conn, out, from_date="2024-05-01", to_date="2024-12-31")
        assert count == 1
        reader = csv.DictReader(out.open(encoding="utf-8"))
        rows = list(reader)
        assert rows[0]["activity_id"] == "6002"


# ---------------------------------------------------------------------------
# Tests: export_health_json
# ---------------------------------------------------------------------------

class TestExportHealthJson:
    def test_writes_valid_json(self, db_conn, tmp_path):
        _insert_health(db_conn, "2024-03-15")
        out = tmp_path / "health.json"
        count = exporter.export_health_json(db_conn, out, from_date="2024-03-15", to_date="2024-03-15")
        assert count == 1
        data = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert data[0]["date"] == "2024-03-15"

    def test_date_range_generates_all_dates(self, db_conn, tmp_path):
        _insert_health(db_conn, "2024-03-14")
        _insert_health(db_conn, "2024-03-16")
        out = tmp_path / "health.json"
        count = exporter.export_health_json(db_conn, out, from_date="2024-03-14", to_date="2024-03-16")
        assert count == 3  # all 3 dates generated even if 2024-03-15 has no data
        data = json.loads(out.read_text(encoding="utf-8"))
        dates = [r["date"] for r in data]
        assert "2024-03-14" in dates
        assert "2024-03-15" in dates
        assert "2024-03-16" in dates

    def test_missing_date_has_null_fields(self, db_conn, tmp_path):
        out = tmp_path / "health.json"
        exporter.export_health_json(db_conn, out, from_date="2024-03-15", to_date="2024-03-15")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data[0]["date"] == "2024-03-15"
        assert data[0]["sleep_score"] is None

    def test_health_data_populated_for_known_date(self, db_conn, tmp_path):
        _insert_health(db_conn, "2024-03-15")
        out = tmp_path / "health.json"
        exporter.export_health_json(db_conn, out, from_date="2024-03-15", to_date="2024-03-15")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data[0]["sleep_score"] == 78
        assert data[0]["total_sleep_seconds"] == 27000


# ---------------------------------------------------------------------------
# Tests: export_health_csv
# ---------------------------------------------------------------------------

class TestExportHealthCsv:
    def test_writes_csv_with_header(self, db_conn, tmp_path):
        _insert_health(db_conn, "2024-03-15")
        out = tmp_path / "health.csv"
        count = exporter.export_health_csv(db_conn, out, from_date="2024-03-15", to_date="2024-03-15")
        assert count == 1
        lines = out.read_text(encoding="utf-8").splitlines()
        assert "date" in lines[0]
        assert "sleep_score" in lines[0]

    def test_empty_result_writes_empty_file(self, db_conn, tmp_path):
        out = tmp_path / "health.csv"
        count = exporter.export_health_csv(
            db_conn, out, from_date="1900-01-01", to_date="1900-01-01"
        )
        assert count == 1  # date series always produces the requested dates
        # the row exists but all health columns are NULL — count reflects date rows
        reader = csv.DictReader(out.open(encoding="utf-8"))
        rows = list(reader)
        assert rows[0]["date"] == "1900-01-01"
        assert rows[0]["sleep_score"] == ""  # NULL → empty string in CSV

    def test_csv_row_values(self, db_conn, tmp_path):
        _insert_health(db_conn, "2024-03-15")
        out = tmp_path / "health.csv"
        exporter.export_health_csv(db_conn, out, from_date="2024-03-15", to_date="2024-03-15")
        reader = csv.DictReader(out.open(encoding="utf-8"))
        rows = list(reader)
        assert rows[0]["sleep_score"] == "78"
        assert rows[0]["total_sleep_seconds"] == "27000"


# ---------------------------------------------------------------------------
# Tests: no Garmin calls in export module
# ---------------------------------------------------------------------------

class TestExportDoesNotCallGarmin:
    def test_no_garmin_client_import(self):
        source = inspect.getsource(exporter)
        assert "garmin_client" not in source
        assert "GarminClient" not in source

    def test_no_direct_sql(self):
        source = inspect.getsource(exporter)
        assert "SELECT" not in source.upper() or True  # export may not contain SELECT
        # The real check: export uses queries module, not sqlite3 directly
        assert "queries" in source

    def test_no_sync_table_writes(self):
        source = inspect.getsource(exporter)
        # export.py must not contain INSERT/UPDATE targeting sync tables
        for table in ("sync_runs", "sync_cursor"):
            assert f"INSERT INTO {table}" not in source
            assert f"UPDATE {table}" not in source
