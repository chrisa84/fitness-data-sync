"""
Tests for health data sync (phase 3).

Verifies:
  - raw_payload upsert by date (idempotency, hash change detection)
  - normalisation for all 6 health data types
  - HealthSyncEngine: successful sync, partial failures, dry-run, rate-limit stop
  - Cursor tracks last_successful_date
  - Status includes all health table counts
"""

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from garmin_sync import normalise, repositories as repo
from garmin_sync.sync_engine import HealthSyncEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _hash(payload) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _make_engine(db_conn, client_mock, dry_run=False):
    from garmin_sync.config import Config
    config = Config(
        garmin_email="test@test.com",
        garmin_password="pw",
        garmin_request_delay_seconds=0,
    )
    return HealthSyncEngine(config, db_conn, client_mock, dry_run=dry_run)


def _make_client(date_str="2024-03-15"):
    client = MagicMock()
    client.get_user_summary.return_value = {
        "calendarDate": date_str,
        "totalSteps": 8000,
        "dailyStepGoal": 10000,
        "totalDistanceMeters": 6400.0,
        "activeKilocalories": 450,
        "bmrKilocalories": 1900,
        "totalKilocalories": 2350,
        "averageHeartRateInBeatsPerMinute": 68,
        "maxHeartRateInBeatsPerMinute": 155,
        "restingHeartRateInBeatsPerMinute": 52,
        "averageStressLevel": 35,
        "maxStressLevel": 80,
        "moderateIntensityMinutes": 30,
        "vigorousIntensityMinutes": 15,
        "intensityMinutesGoal": 150,
        "floorsAscended": 5.0,
        "floorsDescended": 3.0,
    }
    client.get_sleep_data.return_value = {
        "dailySleepDTO": {
            "calendarDate": date_str,
            "sleepStartTimestampGMT": 1710384600000,
            "sleepEndTimestampGMT": 1710413400000,
            "sleepTimeSeconds": 28800,
            "deepSleepSeconds": 7200,
            "lightSleepSeconds": 14400,
            "remSleepSeconds": 5400,
            "awakeSleepSeconds": 1800,
            "overallSleepScore": {"value": 78},
            "averageSpo2Value": 96.5,
            "averageRespirationValue": 14.5,
        }
    }
    client.get_hrv_data.return_value = {
        "hrvSummary": {
            "calendarDate": date_str,
            "weeklyAvg": 45,
            "lastNight": 48,
            "lastNight5MinHigh": 60,
            "baseline": {"balancedLow": 44, "balancedUpper": 52},
            "status": "BALANCED",
        }
    }
    client.get_stress_data.return_value = {
        "calendarDate": date_str,
        "avgStressLevel": 35,
        "maxStressLevel": 80,
        "stressDuration": 28800,
        "restStressDuration": 18000,
        "lowStressDuration": 7200,
        "mediumStressDuration": 3600,
        "highStressDuration": 1800,
    }
    client.get_body_battery.return_value = [
        {
            "date": date_str,
            "charged": 45,
            "drained": 52,
            "startingValue": 80,
            "endingValue": 73,
        }
    ]
    client.get_heart_rates.return_value = {
        "calendarDate": date_str,
        "restingHeartRate": 52,
        "maxHeartRate": 155,
        "minHeartRate": 44,
    }
    return client


# ---------------------------------------------------------------------------
# upsert_raw_payload_by_date
# ---------------------------------------------------------------------------

class TestUpsertRawPayloadByDate:
    def test_insert_same_payload_twice_creates_one_row(self, db_conn):
        pj = _json({"calendarDate": "2024-03-15"})
        ph = _hash({"calendarDate": "2024-03-15"})
        repo.upsert_raw_payload_by_date(
            db_conn, source="gc", data_type="daily_summary",
            date="2024-03-15", payload_json=pj, payload_hash=ph,
        )
        repo.upsert_raw_payload_by_date(
            db_conn, source="gc", data_type="daily_summary",
            date="2024-03-15", payload_json=pj, payload_hash=ph,
        )
        count = db_conn.execute(
            "SELECT COUNT(*) FROM raw_payload WHERE data_type='daily_summary' AND date='2024-03-15'"
        ).fetchone()[0]
        assert count == 1

    def test_changed_payload_updates_hash(self, db_conn):
        p1 = {"steps": 8000}
        repo.upsert_raw_payload_by_date(
            db_conn, source="gc", data_type="daily_summary",
            date="2024-03-15", payload_json=_json(p1), payload_hash=_hash(p1),
        )
        p2 = {"steps": 9000}
        _, changed = repo.upsert_raw_payload_by_date(
            db_conn, source="gc", data_type="daily_summary",
            date="2024-03-15", payload_json=_json(p2), payload_hash=_hash(p2),
        )
        assert changed is True

    def test_same_date_different_data_types_are_separate_rows(self, db_conn):
        for dt in ("daily_summary", "sleep", "hrv"):
            p = {"type": dt}
            repo.upsert_raw_payload_by_date(
                db_conn, source="gc", data_type=dt,
                date="2024-03-15", payload_json=_json(p), payload_hash=_hash(p),
            )
        count = db_conn.execute(
            "SELECT COUNT(*) FROM raw_payload WHERE date='2024-03-15'"
        ).fetchone()[0]
        assert count == 3

    def test_returns_correct_row_id(self, db_conn):
        p = {"steps": 1000}
        row_id, _ = repo.upsert_raw_payload_by_date(
            db_conn, source="gc", data_type="daily_summary",
            date="2024-03-15", payload_json=_json(p), payload_hash=_hash(p),
        )
        assert row_id > 0


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

class TestNormaliseDailySummary:
    def test_full_payload(self):
        raw = {
            "totalSteps": 8000,
            "dailyStepGoal": 10000,
            "totalDistanceMeters": 6400.0,
            "activeKilocalories": 450,
            "bmrKilocalories": 1900,
            "totalKilocalories": 2350,
            "averageHeartRateInBeatsPerMinute": 68,
            "maxHeartRateInBeatsPerMinute": 155,
            "restingHeartRateInBeatsPerMinute": 52,
            "averageStressLevel": 35,
            "maxStressLevel": 80,
            "moderateIntensityMinutes": 30,
            "vigorousIntensityMinutes": 15,
            "intensityMinutesGoal": 150,
            "floorsAscended": 5.0,
            "floorsDescended": 3.0,
        }
        row = normalise.normalise_daily_summary(raw, "2024-03-15", 1)
        assert row is not None
        assert row["date"] == "2024-03-15"
        assert row["total_steps"] == 8000
        assert row["resting_hr"] == 52
        assert row["raw_payload_id"] == 1

    def test_empty_dict_returns_none(self):
        assert normalise.normalise_daily_summary({}, "2024-03-15", 1) is None

    def test_missing_fields_produce_none(self):
        row = normalise.normalise_daily_summary({"totalSteps": 5000}, "2024-03-15", 1)
        assert row is not None
        assert row["total_steps"] == 5000
        assert row["resting_hr"] is None


class TestNormaliseSleep:
    def test_full_payload_with_nested_dto(self):
        raw = {
            "dailySleepDTO": {
                "sleepTimeSeconds": 28800,
                "deepSleepSeconds": 7200,
                "lightSleepSeconds": 14400,
                "remSleepSeconds": 5400,
                "awakeSleepSeconds": 1800,
                "overallSleepScore": {"value": 78},
                "averageSpo2Value": 96.5,
                "averageRespirationValue": 14.5,
                "sleepStartTimestampGMT": 1710384600000,
                "sleepEndTimestampGMT": 1710413400000,
            }
        }
        row = normalise.normalise_sleep(raw, "2024-03-15", 1)
        assert row is not None
        assert row["date"] == "2024-03-15"
        assert row["total_sleep_seconds"] == 28800
        assert row["sleep_score"] == 78
        assert row["sleep_start"] is not None

    def test_empty_dict_returns_none(self):
        assert normalise.normalise_sleep({}, "2024-03-15", 1) is None

    def test_integer_sleep_score(self):
        raw = {"dailySleepDTO": {"overallSleepScore": 72}}
        row = normalise.normalise_sleep(raw, "2024-03-15", 1)
        assert row["sleep_score"] == 72


class TestNormaliseHrv:
    def test_full_payload(self):
        raw = {
            "hrvSummary": {
                "weeklyAvg": 45,
                "lastNight": 48,
                "lastNight5MinHigh": 60,
                "baseline": {"balancedLow": 44, "balancedUpper": 52},
                "status": "BALANCED",
            }
        }
        row = normalise.normalise_hrv(raw, "2024-03-15", 1)
        assert row is not None
        assert row["weekly_avg"] == 45
        assert row["baseline_low"] == 44
        assert row["status"] == "BALANCED"

    def test_empty_dict_returns_none(self):
        assert normalise.normalise_hrv({}, "2024-03-15", 1) is None


class TestNormaliseStress:
    def test_full_payload(self):
        raw = {
            "avgStressLevel": 35,
            "maxStressLevel": 80,
            "stressDuration": 28800,
            "restStressDuration": 18000,
            "lowStressDuration": 7200,
            "mediumStressDuration": 3600,
            "highStressDuration": 1800,
        }
        row = normalise.normalise_stress(raw, "2024-03-15", 1)
        assert row is not None
        assert row["avg_stress_level"] == 35
        assert row["high_stress_duration_seconds"] == 1800

    def test_empty_dict_returns_none(self):
        assert normalise.normalise_stress({}, "2024-03-15", 1) is None


class TestNormaliseBodyBattery:
    def test_list_payload_extracts_matching_date(self):
        raw = [
            {"date": "2024-03-14", "charged": 30, "drained": 40},
            {"date": "2024-03-15", "charged": 45, "drained": 52, "startingValue": 80, "endingValue": 73},
        ]
        row = normalise.normalise_body_battery(raw, "2024-03-15", 1)
        assert row is not None
        assert row["charged"] == 45
        assert row["starting_value"] == 80

    def test_empty_list_returns_none(self):
        assert normalise.normalise_body_battery([], "2024-03-15", 1) is None

    def test_dict_payload(self):
        raw = {"charged": 45, "drained": 52}
        row = normalise.normalise_body_battery(raw, "2024-03-15", 1)
        assert row is not None
        assert row["charged"] == 45


class TestNormaliseHeartRate:
    def test_full_payload(self):
        raw = {"restingHeartRate": 52, "maxHeartRate": 155, "minHeartRate": 44}
        row = normalise.normalise_heart_rate(raw, "2024-03-15", 1)
        assert row is not None
        assert row["resting_hr"] == 52
        assert row["max_hr"] == 155

    def test_empty_dict_returns_none(self):
        assert normalise.normalise_heart_rate({}, "2024-03-15", 1) is None


# ---------------------------------------------------------------------------
# HealthSyncEngine
# ---------------------------------------------------------------------------

class TestHealthSyncEngineSuccess:
    def test_sync_recent_health_stores_all_types(self, db_conn):
        client = _make_client("2024-03-15")
        engine = _make_engine(db_conn, client)
        engine.sync_recent_health(days=1)

        assert db_conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM stress").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM body_battery").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0] == 1

    def test_sync_health_date_range(self, db_conn):
        client = _make_client()
        for method in ("get_user_summary", "get_sleep_data", "get_hrv_data",
                        "get_stress_data", "get_body_battery", "get_heart_rates"):
            getattr(client, method).return_value = (
                [{"date": "x", "charged": 1}] if method == "get_body_battery"
                else {"calendarDate": "x"}
            )

        engine = _make_engine(db_conn, client)
        engine.sync_health(from_date="2024-03-13", to_date="2024-03-15")

        assert client.get_user_summary.call_count == 3

    def test_rerun_does_not_duplicate_rows(self, db_conn):
        client = _make_client("2024-03-15")
        engine = _make_engine(db_conn, client)
        engine.sync_recent_health(days=1)
        engine.sync_recent_health(days=1)

        assert db_conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 1

    def test_dry_run_does_not_write(self, db_conn):
        client = _make_client("2024-03-15")
        engine = _make_engine(db_conn, client, dry_run=True)
        engine.sync_recent_health(days=1)

        assert db_conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0] == 0


class TestHealthSyncEngineFailures:
    def test_one_data_type_failure_continues(self, db_conn):
        client = _make_client("2024-03-15")
        client.get_hrv_data.side_effect = ConnectionError("timeout")

        engine = _make_engine(db_conn, client)
        result = engine.sync_recent_health(days=1)

        # HRV failed, other 5 succeeded.
        assert db_conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 1
        assert result["skipped"] >= 1

    def test_rate_limit_stops_sync(self, db_conn):
        from garmin_sync.rate_limit import RateLimitExceeded
        client = MagicMock()
        client.get_user_summary.side_effect = RateLimitExceeded("429")

        engine = _make_engine(db_conn, client)
        with pytest.raises(RateLimitExceeded):
            engine.sync_recent_health(days=1)

    def test_empty_response_is_skipped_gracefully(self, db_conn):
        client = _make_client("2024-03-15")
        client.get_hrv_data.return_value = {}

        engine = _make_engine(db_conn, client)
        engine.sync_recent_health(days=1)

        assert db_conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Cursor tracking
# ---------------------------------------------------------------------------

class TestHealthCursor:
    def test_cursor_updated_after_sync(self, db_conn):
        client = _make_client("2024-03-15")
        engine = _make_engine(db_conn, client)
        engine.sync_recent_health(days=1)

        cursor = repo.get_cursor(db_conn, "health")
        assert cursor is not None
        assert cursor["last_successful_date"] is not None

    def test_cursor_advances_through_range(self, db_conn):
        client = _make_client()
        for method in ("get_user_summary", "get_sleep_data", "get_hrv_data",
                        "get_stress_data", "get_body_battery", "get_heart_rates"):
            getattr(client, method).return_value = (
                [{"date": "x"}] if method == "get_body_battery" else {"x": 1}
            )

        engine = _make_engine(db_conn, client)
        engine.sync_health(from_date="2024-03-13", to_date="2024-03-15")

        cursor = repo.get_cursor(db_conn, "health")
        assert cursor["last_successful_date"] == "2024-03-13"  # reversed iter: oldest is last


# ---------------------------------------------------------------------------
# Status includes health counts
# ---------------------------------------------------------------------------

class TestStatusIncludesHealthCounts:
    def test_all_health_fields_present_and_zero(self, db_conn):
        status = repo.get_sync_status(db_conn)
        for key in (
            "daily_summary_count", "sleep_count", "hrv_count",
            "stress_count", "body_battery_count", "heart_rate_count",
        ):
            assert key in status, f"Missing key: {key}"
            assert status[key] == 0
