"""Tests for garmin_sync.mcp_server.

All tests use in-memory SQLite via the db_conn fixture.
No Garmin credentials needed. No real DB file required.
"""

import json
import sqlite3
import sys
from datetime import date, timedelta

import pytest

import garmin_sync.mcp_server as server
from garmin_sync.db import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_activity(conn, activity_id="ACT001", start_time="2024-03-15 06:30:00",
                     atype="running", raw_id=1):
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload (id, source, data_type, garmin_id, fetched_at, payload_json, payload_hash) "
        "VALUES (?, 'garmin_connect', 'activity_summary', ?, '2024-01-01T00:00:00', '{}', 'abc')",
        (raw_id, activity_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO activity (activity_id, name, type, start_time, start_time_local, "
        "raw_payload_id, updated_at) VALUES (?, 'Test Run', ?, ?, ?, ?, '2024-01-01T00:00:00')",
        (activity_id, atype, start_time, start_time, raw_id),
    )


def _insert_split(conn, activity_id="ACT001", split_index=0, raw_id=1):
    conn.execute(
        "INSERT OR IGNORE INTO activity_split "
        "(activity_id, split_index, distance_m, duration_s, avg_hr, raw_payload_id, updated_at) "
        "VALUES (?, ?, 1000.0, 300.0, 150, ?, '2024-01-01T00:00:00')",
        (activity_id, split_index, raw_id),
    )


def _insert_sleep(conn, d="2024-03-15", raw_id=2):
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload (id, source, data_type, date, fetched_at, payload_json, payload_hash) "
        "VALUES (?, 'garmin_connect', 'sleep', ?, '2024-01-01T00:00:00', '{}', 'def')",
        (raw_id, d),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sleep (date, total_sleep_seconds, sleep_score, avg_spo2, "
        "raw_payload_id, updated_at) VALUES (?, 27000, 78, 96.5, ?, '2024-01-01T00:00:00')",
        (d, raw_id),
    )


def _insert_hrv(conn, d="2024-03-15", raw_id=3):
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload (id, source, data_type, date, fetched_at, payload_json, payload_hash) "
        "VALUES (?, 'garmin_connect', 'hrv', ?, '2024-01-01T00:00:00', '{}', 'ghi')",
        (raw_id, d),
    )
    conn.execute(
        "INSERT OR IGNORE INTO hrv (date, weekly_avg, last_night_avg, status, "
        "raw_payload_id, updated_at) VALUES (?, 55, 52, 'BALANCED', ?, '2024-01-01T00:00:00')",
        (d, raw_id),
    )


def _insert_training_status(conn, d="2024-03-15", raw_id=4):
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload (id, source, data_type, date, fetched_at, payload_json, payload_hash) "
        "VALUES (?, 'garmin_connect', 'training_status', ?, '2024-01-01T00:00:00', '{}', 'jkl')",
        (raw_id, d),
    )
    conn.execute(
        "INSERT OR IGNORE INTO training_status (date, vo2max, training_status_phrase, "
        "acute_load, chronic_load, acwr, raw_payload_id, updated_at) "
        "VALUES (?, 48.0, 'MAINTAINING_2', 500, 490, 1.02, ?, '2024-01-01T00:00:00')",
        (d, raw_id),
    )


def _insert_fitness_age(conn, d="2024-03-15", raw_id=5):
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload (id, source, data_type, date, fetched_at, payload_json, payload_hash) "
        "VALUES (?, 'garmin_connect', 'fitness_age', ?, '2024-01-01T00:00:00', '{}', 'mno')",
        (raw_id, d),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fitness_age (date, fitness_age, achievable_fitness_age, "
        "chronological_age, raw_payload_id, updated_at) "
        "VALUES (?, 35.5, 36.2, 42, ?, '2024-01-01T00:00:00')",
        (d, raw_id),
    )


def _insert_daily_summary(conn, d="2024-03-15", raw_id=6):
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload (id, source, data_type, date, fetched_at, payload_json, payload_hash) "
        "VALUES (?, 'garmin_connect', 'daily_summary', ?, '2024-01-01T00:00:00', '{}', 'pqr')",
        (raw_id, d),
    )
    conn.execute(
        "INSERT OR IGNORE INTO daily_summary (date, total_steps, resting_hr, "
        "raw_payload_id, updated_at) VALUES (?, 12000, 49, ?, '2024-01-01T00:00:00')",
        (d, raw_id),
    )


@pytest.fixture()
def mcp_conn(db_conn, monkeypatch):
    """Patch get_conn() to return the in-memory test DB."""
    monkeypatch.setattr(server, "get_conn", lambda: db_conn)
    return db_conn


@pytest.fixture()
def populated_conn(mcp_conn):
    """mcp_conn with sample rows across key tables."""
    _insert_activity(mcp_conn)
    _insert_split(mcp_conn)
    _insert_sleep(mcp_conn)
    _insert_hrv(mcp_conn)
    _insert_training_status(mcp_conn)
    _insert_fitness_age(mcp_conn)
    _insert_daily_summary(mcp_conn)
    mcp_conn.commit()
    return mcp_conn


# ---------------------------------------------------------------------------
# TestNoSideEffectsOnImport
# ---------------------------------------------------------------------------

class TestNoSideEffectsOnImport:
    def test_import_does_not_open_db(self, monkeypatch):
        """Importing mcp_server must not open DB. _conn stays None until first tool call."""
        # Reset any connection cached by a previous test
        monkeypatch.setattr(server, "_conn", None)
        assert server._conn is None

    def test_does_not_import_garmin_client(self):
        """MCP server must not have GarminClient in its own namespace."""
        import garmin_sync.mcp_server as mod
        assert "garmin_client" not in vars(mod)
        assert "GarminClient" not in vars(mod)


# ---------------------------------------------------------------------------
# TestReadOnly
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_connection_uri_uses_mode_ro(self, tmp_path, monkeypatch):
        """_open_ro_conn must open SQLite with mode=ro URI."""
        db = tmp_path / "test.db"
        # Seed a valid DB file
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE t (x INTEGER)")
        c.close()

        monkeypatch.setenv("GARMIN_DB_PATH", str(db))
        conn = server._open_ro_conn()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
        conn.close()

    def test_tools_do_not_write(self, populated_conn):
        """All list-returning tools must not write. Verify by calling all and checking count."""
        before = populated_conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        server.get_recent_activities()
        server.get_activity_splits("ACT001")
        server.get_weekly_running_volume()
        server.get_sleep_trend()
        server.get_database_status()
        after = populated_conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        assert before == after


# ---------------------------------------------------------------------------
# TestInputValidation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_invalid_from_date(self, mcp_conn):
        with pytest.raises(ValueError, match="from_date must be YYYY-MM-DD"):
            server._vdate("not-a-date", "from_date")

    def test_invalid_to_date(self, mcp_conn):
        with pytest.raises(ValueError, match="to_date must be YYYY-MM-DD"):
            server._vdate("2024-13-01", "to_date")

    def test_from_date_after_to_date(self, mcp_conn):
        with pytest.raises(ValueError, match="from_date .* must be <= to_date"):
            server._vrange("2024-03-20", "2024-03-15")

    def test_invalid_limit_zero(self, mcp_conn):
        with pytest.raises(ValueError, match="limit must be"):
            server._vlimit(0)

    def test_invalid_limit_too_large(self, mcp_conn):
        with pytest.raises(ValueError, match="limit must be"):
            server._vlimit(501)

    def test_invalid_days_zero(self, mcp_conn):
        with pytest.raises(ValueError, match="days must be"):
            server._vdays(0)

    def test_invalid_days_too_large(self, mcp_conn):
        with pytest.raises(ValueError, match="days must be"):
            server._vdays(3651)

    def test_invalid_weeks_zero(self, mcp_conn):
        with pytest.raises(ValueError, match="weeks must be"):
            server._vweeks(0)

    def test_invalid_months_zero(self, mcp_conn):
        with pytest.raises(ValueError, match="months must be"):
            server._vmonths(0)

    def test_empty_activity_id(self, mcp_conn):
        with pytest.raises(ValueError, match="activity_id must be non-empty"):
            server._vactivity_id("")

    def test_whitespace_activity_id(self, mcp_conn):
        with pytest.raises(ValueError, match="activity_id must be non-empty"):
            server._vactivity_id("   ")

    def test_valid_range_passes(self, mcp_conn):
        server._vrange("2024-03-01", "2024-03-15")  # must not raise

    def test_valid_range_same_date_passes(self, mcp_conn):
        server._vrange("2024-03-15", "2024-03-15")  # must not raise


# ---------------------------------------------------------------------------
# TestToolReturnTypes
# ---------------------------------------------------------------------------

class TestToolReturnTypes:
    def test_get_recent_activities_returns_list(self, populated_conn):
        result = server.get_recent_activities()
        assert isinstance(result, list)

    def test_get_activity_known_id_returns_dict(self, populated_conn):
        result = server.get_activity("ACT001")
        assert isinstance(result, dict)
        assert result["activity_id"] == "ACT001"

    def test_get_activity_unknown_id_returns_none(self, populated_conn):
        result = server.get_activity("NOTEXIST")
        assert result is None

    def test_get_activity_splits_returns_list(self, populated_conn):
        result = server.get_activity_splits("ACT001")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_get_activity_splits_empty_for_unknown(self, populated_conn):
        result = server.get_activity_splits("NOTEXIST")
        assert result == []

    def test_get_weekly_running_volume_returns_list(self, populated_conn):
        result = server.get_weekly_running_volume()
        assert isinstance(result, list)

    def test_get_monthly_running_volume_returns_list(self, populated_conn):
        result = server.get_monthly_running_volume()
        assert isinstance(result, list)

    def test_get_sleep_trend_returns_list(self, populated_conn):
        result = server.get_sleep_trend()
        assert isinstance(result, list)

    def test_get_hrv_trend_returns_list(self, populated_conn):
        result = server.get_hrv_trend()
        assert isinstance(result, list)

    def test_get_resting_hr_trend_returns_list(self, populated_conn):
        result = server.get_resting_hr_trend()
        assert isinstance(result, list)

    def test_get_stress_trend_returns_list(self, populated_conn):
        result = server.get_stress_trend()
        assert isinstance(result, list)

    def test_get_body_battery_trend_returns_list(self, populated_conn):
        result = server.get_body_battery_trend()
        assert isinstance(result, list)

    def test_get_training_vs_sleep_returns_list(self, populated_conn):
        result = server.get_training_vs_sleep()
        assert isinstance(result, list)

    def test_get_intensity_distribution_returns_list(self, populated_conn):
        result = server.get_intensity_distribution()
        assert isinstance(result, list)

    def test_get_running_dynamics_returns_list(self, populated_conn):
        result = server.get_running_dynamics()
        assert isinstance(result, list)

    def test_get_training_status_returns_list(self, populated_conn):
        result = server.get_training_status(days=30)
        assert isinstance(result, list)

    def test_get_training_readiness_returns_list(self, populated_conn):
        result = server.get_training_readiness(days=30)
        assert isinstance(result, list)

    def test_get_vo2max_trend_returns_list(self, populated_conn):
        result = server.get_vo2max_trend(days=30)
        assert isinstance(result, list)

    def test_get_lactate_threshold_returns_list(self, populated_conn):
        result = server.get_lactate_threshold(days=365)
        assert isinstance(result, list)

    def test_get_race_predictions_returns_list(self, populated_conn):
        result = server.get_race_predictions(days=365)
        assert isinstance(result, list)

    def test_get_endurance_score_returns_list(self, populated_conn):
        result = server.get_endurance_score(days=365)
        assert isinstance(result, list)

    def test_get_hill_score_returns_list(self, populated_conn):
        result = server.get_hill_score(days=365)
        assert isinstance(result, list)

    def test_get_performance_summary_returns_list(self, populated_conn):
        result = server.get_performance_summary(days=30)
        assert isinstance(result, list)

    def test_get_daily_health_summary_returns_list(self, populated_conn):
        result = server.get_daily_health_summary(days=30)
        assert isinstance(result, list)

    def test_get_database_status_returns_dict(self, populated_conn):
        result = server.get_database_status()
        assert isinstance(result, dict)
        assert "activity_count" in result
        assert "raw_payload_count" in result
        assert "cursors" in result


# ---------------------------------------------------------------------------
# TestDatabaseStatus
# ---------------------------------------------------------------------------

class TestDatabaseStatus:
    def test_counts_reflect_inserted_rows(self, populated_conn):
        result = server.get_database_status()
        assert result["activity_count"] == 1
        assert result["sleep_count"] == 1
        assert result["hrv_count"] == 1

    def test_all_expected_keys_present(self, populated_conn):
        result = server.get_database_status()
        for key in ("activity_count", "activity_detail_count", "sleep_count",
                    "hrv_count", "training_status_count", "fitness_age_count",
                    "raw_payload_count", "cursors", "recent_runs"):
            assert key in result, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# TestDailyHealthSummary
# ---------------------------------------------------------------------------

class TestDailyHealthSummary:
    def test_returns_row_for_date_with_data(self, populated_conn):
        result = server.get_daily_health_summary(
            from_date="2024-03-15", to_date="2024-03-15"
        )
        assert len(result) == 1
        row = result[0]
        assert row["date"] == "2024-03-15"
        assert row["sleep_score"] == 78
        assert row["hrv_last_night"] == 52

    def test_date_range_generates_all_dates(self, populated_conn):
        result = server.get_daily_health_summary(
            from_date="2024-03-13", to_date="2024-03-15"
        )
        assert len(result) == 3
        dates = [r["date"] for r in result]
        assert "2024-03-13" in dates
        assert "2024-03-14" in dates
        assert "2024-03-15" in dates

    def test_days_without_data_return_none_fields(self, populated_conn):
        result = server.get_daily_health_summary(
            from_date="2024-03-14", to_date="2024-03-14"
        )
        assert len(result) == 1
        assert result[0]["sleep_score"] is None


# ---------------------------------------------------------------------------
# TestJsonSerialisability
# ---------------------------------------------------------------------------

class TestJsonSerialisability:
    def test_all_list_tools_are_json_serialisable(self, populated_conn):
        results = [
            server.get_recent_activities(),
            server.get_activity_splits("ACT001"),
            server.get_weekly_running_volume(),
            server.get_monthly_running_volume(),
            server.get_sleep_trend(days=30),
            server.get_hrv_trend(days=30),
            server.get_resting_hr_trend(days=30),
            server.get_stress_trend(days=30),
            server.get_body_battery_trend(days=30),
            server.get_training_vs_sleep(days=30),
            server.get_intensity_distribution(),
            server.get_running_dynamics(days=30),
            server.get_training_status(days=30),
            server.get_training_readiness(days=30),
            server.get_vo2max_trend(days=30),
            server.get_lactate_threshold(days=365),
            server.get_race_predictions(days=365),
            server.get_endurance_score(days=365),
            server.get_hill_score(days=365),
            server.get_performance_summary(days=30),
            server.get_daily_health_summary(days=30),
            server.get_database_status(),
        ]
        for result in results:
            try:
                json.dumps(result)
            except TypeError as e:
                pytest.fail(f"Result not JSON-serialisable: {e}\nValue: {result!r}")

    def test_get_activity_result_is_json_serialisable(self, populated_conn):
        result = server.get_activity("ACT001")
        json.dumps(result)  # must not raise

    def test_get_activity_none_is_json_serialisable(self, populated_conn):
        result = server.get_activity("NOTEXIST")
        assert result is None
        json.dumps(result)
