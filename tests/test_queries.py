"""
Tests for queries.py.

All tests use in-memory SQLite seeded with fixture data via repository
functions. No Garmin credentials or network access required.
"""

import hashlib
import json
import sqlite3
from datetime import date, timedelta

import pytest

from garmin_sync import queries, repositories as repo


# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

def _ph(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _pj(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def _insert_activity(
    db_conn: sqlite3.Connection,
    activity_id: str,
    type_: str = "running",
    start_time: str = "2024-03-15 06:30:00",
    distance_m: float = 10000.0,
    duration_s: float = 3600.0,
    avg_hr: int = 150,
    elevation_gain_m: float = 100.0,
    name: str = "Test Run",
) -> str:
    payload = {"activityId": int(activity_id)}
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_summary",
        garmin_id=activity_id, payload_json=_pj(payload), payload_hash=_ph(payload),
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "activity_id": activity_id,
        "name": name,
        "type": type_,
        "start_time": start_time,
        "start_time_local": start_time,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "moving_duration_s": duration_s,
        "elapsed_duration_s": duration_s,
        "avg_hr": avg_hr,
        "max_hr": avg_hr + 20,
        "avg_cadence": 170.0,
        "max_cadence": 185.0,
        "avg_power": None,
        "max_power": None,
        "elevation_gain_m": elevation_gain_m,
        "elevation_loss_m": elevation_gain_m * 0.9,
        "avg_speed_mps": 3.0,
        "max_speed_mps": 4.5,
        "calories": 600,
        "training_effect": 3.5,
        "aerobic_te": 3.5,
        "anaerobic_te": 0.5,
        "vo2max": 52.0,
        "raw_payload_id": raw_id,
        "updated_at": now,
    }
    repo.upsert_activity(db_conn, row)
    return activity_id


def _insert_lap(
    db_conn: sqlite3.Connection,
    activity_id: str,
    lap_index: int = 0,
    distance_m: float = 5000.0,
    duration_s: float = 1800.0,
    avg_hr: int = 150,
) -> None:
    payload = {"activityId": int(activity_id), "lapIndex": lap_index}
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_detail",
        garmin_id=activity_id, payload_json=_pj(payload), payload_hash=_ph(payload),
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    repo.replace_activity_laps(db_conn, activity_id, [{
        "activity_id": activity_id,
        "lap_index": lap_index,
        "start_time": "2024-03-15 06:30:00",
        "distance_m": distance_m,
        "duration_s": duration_s,
        "moving_duration_s": duration_s,
        "avg_hr": avg_hr,
        "max_hr": avg_hr + 15,
        "avg_cadence": 170.0,
        "avg_power": None,
        "elevation_gain_m": 50.0,
        "elevation_loss_m": 45.0,
        "raw_payload_id": raw_id,
        "updated_at": now,
    }])


def _insert_health(
    db_conn: sqlite3.Connection,
    date_str: str,
    steps: int = 8000,
    sleep_s: int = 28800,
    sleep_score: int = 75,
    hrv_last_night: int = 48,
    hrv_status: str = "BALANCED",
    resting_hr: int = 52,
    avg_stress: int = 35,
    battery_end: int = 73,
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    def _raw_id(dt: str) -> int:
        p = {"date": date_str, "type": dt}
        raw_id, _ = repo.upsert_raw_payload_by_date(
            db_conn, source="gc", data_type=dt,
            date=date_str, payload_json=_pj(p), payload_hash=_ph(p),
        )
        return raw_id

    repo.upsert_daily_summary(db_conn, {
        "date": date_str, "total_steps": steps, "step_goal": 10000,
        "total_distance_m": steps * 0.8, "active_calories": 450,
        "resting_calories": 1900, "total_calories": 2350,
        "avg_hr": 68, "max_hr": 155, "resting_hr": resting_hr,
        "avg_stress_level": avg_stress, "max_stress_level": avg_stress * 2,
        "moderate_intensity_minutes": 30, "vigorous_intensity_minutes": 15,
        "intensity_minutes_goal": 150, "floors_ascended": 5.0, "floors_descended": 3.0,
        "raw_payload_id": _raw_id("daily_summary"), "updated_at": now,
    })
    repo.upsert_sleep(db_conn, {
        "date": date_str, "sleep_start": None, "sleep_end": None,
        "total_sleep_seconds": sleep_s,
        "deep_sleep_seconds": sleep_s // 4, "light_sleep_seconds": sleep_s // 2,
        "rem_sleep_seconds": sleep_s // 4, "awake_seconds": 1800,
        "sleep_score": sleep_score, "avg_spo2": 96.5, "avg_respiration": 14.5,
        "raw_payload_id": _raw_id("sleep"), "updated_at": now,
    })
    repo.upsert_hrv(db_conn, {
        "date": date_str, "weekly_avg": 45, "last_night_avg": hrv_last_night,
        "last_night_5min_high": hrv_last_night + 12,
        "baseline_low": 42, "baseline_high": 52, "status": hrv_status,
        "raw_payload_id": _raw_id("hrv"), "updated_at": now,
    })
    repo.upsert_stress(db_conn, {
        "date": date_str, "avg_stress_level": avg_stress,
        "max_stress_level": avg_stress * 2, "stress_duration_seconds": 28800,
        "rest_stress_duration_seconds": 18000, "low_stress_duration_seconds": 7200,
        "medium_stress_duration_seconds": 3600, "high_stress_duration_seconds": 1800,
        "raw_payload_id": _raw_id("stress"), "updated_at": now,
    })
    repo.upsert_body_battery(db_conn, {
        "date": date_str, "charged": 45, "drained": 52,
        "starting_value": 80, "ending_value": battery_end,
        "raw_payload_id": _raw_id("body_battery"), "updated_at": now,
    })
    repo.upsert_heart_rate(db_conn, {
        "date": date_str, "resting_hr": resting_hr, "max_hr": 155, "min_hr": 44,
        "raw_payload_id": _raw_id("heart_rate"), "updated_at": now,
    })


def _today_minus(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat() + " 07:00:00"


def _today_date_minus(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# get_recent_activities
# ---------------------------------------------------------------------------

class TestGetRecentActivities:
    def test_returns_activities_newest_first(self, db_conn):
        _insert_activity(db_conn, "1", start_time=_today_minus(5))
        _insert_activity(db_conn, "2", start_time=_today_minus(2))
        _insert_activity(db_conn, "3", start_time=_today_minus(10))

        rows = queries.get_recent_activities(db_conn, limit=10)
        ids = [r["activity_id"] for r in rows]
        assert ids == ["2", "1", "3"]

    def test_limit_is_respected(self, db_conn):
        for i in range(5):
            _insert_activity(db_conn, str(i), start_time=_today_minus(i))
        rows = queries.get_recent_activities(db_conn, limit=3)
        assert len(rows) == 3

    def test_limit_none_returns_all(self, db_conn):
        for i in range(4):
            _insert_activity(db_conn, str(i), start_time=_today_minus(i))
        rows = queries.get_recent_activities(db_conn, limit=None)
        assert len(rows) == 4

    def test_activity_type_filter(self, db_conn):
        _insert_activity(db_conn, "1", type_="running", start_time=_today_minus(1))
        _insert_activity(db_conn, "2", type_="cycling", start_time=_today_minus(2))
        rows = queries.get_recent_activities(db_conn, activity_type="cycling")
        assert len(rows) == 1
        assert rows[0]["type"] == "cycling"

    def test_from_date_filter(self, db_conn):
        _insert_activity(db_conn, "1", start_time=_today_minus(1))
        _insert_activity(db_conn, "2", start_time=_today_minus(10))
        cutoff = _today_date_minus(5)
        rows = queries.get_recent_activities(db_conn, from_date=cutoff)
        ids = [r["activity_id"] for r in rows]
        assert "1" in ids
        assert "2" not in ids

    def test_to_date_filter(self, db_conn):
        _insert_activity(db_conn, "1", start_time=_today_minus(1))
        _insert_activity(db_conn, "2", start_time=_today_minus(10))
        cutoff = _today_date_minus(5)
        rows = queries.get_recent_activities(db_conn, to_date=cutoff)
        ids = [r["activity_id"] for r in rows]
        assert "2" in ids
        assert "1" not in ids

    def test_empty_db_returns_empty_list(self, db_conn):
        assert queries.get_recent_activities(db_conn) == []

    def test_result_includes_detail_columns(self, db_conn):
        _insert_activity(db_conn, "1", start_time=_today_minus(1))
        rows = queries.get_recent_activities(db_conn)
        assert "has_laps" in rows[0]
        assert "sample_count" in rows[0]

    def test_does_not_call_garmin(self, db_conn):
        import garmin_sync.queries as q
        import inspect
        src = inspect.getsource(q)
        assert "garmin_client" not in src
        assert "GarminClient" not in src


# ---------------------------------------------------------------------------
# get_activity / get_activity_laps
# ---------------------------------------------------------------------------

class TestGetActivity:
    def test_returns_activity_by_id(self, db_conn):
        _insert_activity(db_conn, "42", start_time=_today_minus(1))
        row = queries.get_activity(db_conn, "42")
        assert row is not None
        assert row["activity_id"] == "42"

    def test_returns_none_for_missing_id(self, db_conn):
        assert queries.get_activity(db_conn, "999") is None

    def test_includes_detail_columns(self, db_conn):
        _insert_activity(db_conn, "42", start_time=_today_minus(1))
        row = queries.get_activity(db_conn, "42")
        assert "has_laps" in row

    def test_get_activity_laps_returns_laps(self, db_conn):
        from datetime import datetime, timezone
        _insert_activity(db_conn, "42", start_time=_today_minus(1))
        payload = {"activityId": 42}
        raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="42", payload_json=_pj(payload), payload_hash=_ph(payload),
        )
        now = datetime.now(timezone.utc).isoformat()
        repo.replace_activity_laps(db_conn, "42", [
            {"activity_id": "42", "lap_index": 0, "start_time": "2024-03-15 06:30:00",
             "distance_m": 5000.0, "duration_s": 1800.0, "moving_duration_s": 1800.0,
             "avg_hr": 150, "max_hr": 165, "avg_cadence": 170.0, "avg_power": None,
             "elevation_gain_m": 50.0, "elevation_loss_m": 45.0,
             "raw_payload_id": raw_id, "updated_at": now},
            {"activity_id": "42", "lap_index": 1, "start_time": "2024-03-15 07:00:00",
             "distance_m": 5000.0, "duration_s": 1800.0, "moving_duration_s": 1800.0,
             "avg_hr": 155, "max_hr": 170, "avg_cadence": 172.0, "avg_power": None,
             "elevation_gain_m": 50.0, "elevation_loss_m": 45.0,
             "raw_payload_id": raw_id, "updated_at": now},
        ])
        laps = queries.get_activity_laps(db_conn, "42")
        assert len(laps) == 2
        assert laps[0]["lap_index"] == 0

    def test_get_activity_laps_empty(self, db_conn):
        _insert_activity(db_conn, "42", start_time=_today_minus(1))
        assert queries.get_activity_laps(db_conn, "42") == []


# ---------------------------------------------------------------------------
# get_weekly_running_volume
# ---------------------------------------------------------------------------

class TestGetWeeklyRunningVolume:
    def test_groups_runs_by_week(self, db_conn):
        # Two runs this week, one last week
        _insert_activity(db_conn, "1", type_="running", start_time=_today_minus(1), distance_m=10000)
        _insert_activity(db_conn, "2", type_="running", start_time=_today_minus(2), distance_m=8000)
        _insert_activity(db_conn, "3", type_="running", start_time=_today_minus(8), distance_m=12000)

        rows = queries.get_weekly_running_volume(db_conn, weeks=4)
        assert len(rows) >= 1
        total_runs = sum(r["run_count"] for r in rows)
        assert total_runs == 3

    def test_excludes_non_running_types(self, db_conn):
        _insert_activity(db_conn, "1", type_="running", start_time=_today_minus(1), distance_m=10000)
        _insert_activity(db_conn, "2", type_="cycling", start_time=_today_minus(2), distance_m=30000)

        rows = queries.get_weekly_running_volume(db_conn, weeks=4)
        total_runs = sum(r["run_count"] for r in rows)
        assert total_runs == 1

    def test_weeks_limit(self, db_conn):
        for i in range(1, 6):
            _insert_activity(
                db_conn, str(i), type_="running",
                start_time=_today_minus(i * 7), distance_m=10000,
            )
        rows = queries.get_weekly_running_volume(db_conn, weeks=3)
        assert len(rows) <= 3

    def test_returns_distance_and_duration(self, db_conn):
        _insert_activity(db_conn, "1", type_="running", start_time=_today_minus(1),
                         distance_m=10000, duration_s=3600)
        rows = queries.get_weekly_running_volume(db_conn, weeks=2)
        assert rows[0]["total_distance_m"] == pytest.approx(10000)
        assert rows[0]["total_duration_s"] == pytest.approx(3600)

    def test_includes_trail_running(self, db_conn):
        _insert_activity(db_conn, "1", type_="trail_running", start_time=_today_minus(1),
                         distance_m=15000)
        rows = queries.get_weekly_running_volume(db_conn, weeks=2)
        assert len(rows) == 1 and rows[0]["run_count"] == 1

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_weekly_running_volume(db_conn) == []


# ---------------------------------------------------------------------------
# get_monthly_running_volume
# ---------------------------------------------------------------------------

class TestGetMonthlyRunningVolume:
    def test_groups_by_month(self, db_conn):
        this_month = date.today().strftime("%Y-%m")
        _insert_activity(db_conn, "1", type_="running",
                         start_time=date.today().isoformat() + " 07:00:00", distance_m=10000)
        _insert_activity(db_conn, "2", type_="running",
                         start_time=date.today().isoformat() + " 09:00:00", distance_m=8000)

        rows = queries.get_monthly_running_volume(db_conn, months=3)
        this_m = next((r for r in rows if r["month"] == this_month), None)
        assert this_m is not None
        assert this_m["run_count"] == 2
        assert this_m["total_distance_m"] == pytest.approx(18000)

    def test_months_limit(self, db_conn):
        for i in range(1, 5):
            _insert_activity(
                db_conn, str(i), type_="running",
                start_time=_today_minus(i * 30), distance_m=10000,
            )
        rows = queries.get_monthly_running_volume(db_conn, months=2)
        assert len(rows) <= 2

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_monthly_running_volume(db_conn) == []


# ---------------------------------------------------------------------------
# get_sleep_trend
# ---------------------------------------------------------------------------

class TestGetSleepTrend:
    def test_returns_recent_sleep_oldest_first(self, db_conn):
        for i in range(3, 0, -1):
            _insert_health(db_conn, _today_date_minus(i))

        rows = queries.get_sleep_trend(db_conn, days=10)
        assert len(rows) == 3
        # oldest first
        assert rows[0]["date"] < rows[1]["date"] < rows[2]["date"]

    def test_excludes_old_records(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1))   # inside window
        _insert_health(db_conn, _today_date_minus(60))  # outside window

        rows = queries.get_sleep_trend(db_conn, days=30)
        assert len(rows) == 1

    def test_returns_expected_fields(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1), sleep_s=27000, sleep_score=80)
        rows = queries.get_sleep_trend(db_conn, days=5)
        row = rows[0]
        assert row["total_sleep_seconds"] == 27000
        assert row["sleep_score"] == 80
        assert "deep_sleep_seconds" in row
        assert "rem_sleep_seconds" in row

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_sleep_trend(db_conn) == []


# ---------------------------------------------------------------------------
# get_hrv_trend
# ---------------------------------------------------------------------------

class TestGetHrvTrend:
    def test_returns_hrv_records(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1), hrv_last_night=52, hrv_status="BALANCED")
        _insert_health(db_conn, _today_date_minus(2), hrv_last_night=44, hrv_status="LOW")

        rows = queries.get_hrv_trend(db_conn, days=10)
        assert len(rows) == 2
        assert rows[0]["status"] == "LOW"   # oldest first
        assert rows[1]["status"] == "BALANCED"

    def test_excludes_old_records(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1))
        _insert_health(db_conn, _today_date_minus(60))

        rows = queries.get_hrv_trend(db_conn, days=30)
        assert len(rows) == 1

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_hrv_trend(db_conn) == []


# ---------------------------------------------------------------------------
# get_resting_hr_trend
# ---------------------------------------------------------------------------

class TestGetRestingHrTrend:
    def test_returns_resting_hr(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1), resting_hr=52)
        rows = queries.get_resting_hr_trend(db_conn, days=5)
        assert len(rows) == 1
        assert rows[0]["resting_hr"] == 52

    def test_joins_daily_summary_resting_hr(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1), resting_hr=52)
        rows = queries.get_resting_hr_trend(db_conn, days=5)
        # summary_resting_hr comes from the LEFT JOIN with daily_summary
        assert "summary_resting_hr" in rows[0]

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_resting_hr_trend(db_conn) == []


# ---------------------------------------------------------------------------
# get_stress_trend
# ---------------------------------------------------------------------------

class TestGetStressTrend:
    def test_returns_stress_fields(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1), avg_stress=42)
        rows = queries.get_stress_trend(db_conn, days=5)
        assert len(rows) == 1
        assert rows[0]["avg_stress_level"] == 42
        assert "high_stress_duration_seconds" in rows[0]

    def test_excludes_old_records(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1))
        _insert_health(db_conn, _today_date_minus(60))
        rows = queries.get_stress_trend(db_conn, days=30)
        assert len(rows) == 1

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_stress_trend(db_conn) == []


# ---------------------------------------------------------------------------
# get_body_battery_trend
# ---------------------------------------------------------------------------

class TestGetBodyBatteryTrend:
    def test_returns_battery_fields(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1), battery_end=73)
        rows = queries.get_body_battery_trend(db_conn, days=5)
        assert len(rows) == 1
        assert rows[0]["ending_value"] == 73
        assert "charged" in rows[0]
        assert "drained" in rows[0]

    def test_excludes_old_records(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1))
        _insert_health(db_conn, _today_date_minus(60))
        rows = queries.get_body_battery_trend(db_conn, days=30)
        assert len(rows) == 1

    def test_empty_returns_empty_list(self, db_conn):
        assert queries.get_body_battery_trend(db_conn) == []


# ---------------------------------------------------------------------------
# get_training_vs_sleep
# ---------------------------------------------------------------------------

class TestGetTrainingVsSleep:
    def test_returns_combined_row(self, db_conn):
        d = _today_date_minus(1)
        _insert_health(db_conn, d, sleep_s=28800, sleep_score=78)
        _insert_activity(db_conn, "1", type_="running",
                         start_time=d + " 07:00:00", distance_m=10000)

        rows = queries.get_training_vs_sleep(db_conn, days=5)
        assert len(rows) == 1
        row = rows[0]
        assert row["total_sleep_seconds"] == 28800
        assert row["run_count"] == 1
        assert row["total_distance_m"] == pytest.approx(10000)

    def test_rest_day_shows_zero_runs(self, db_conn):
        d = _today_date_minus(1)
        _insert_health(db_conn, d, sleep_s=32400)

        rows = queries.get_training_vs_sleep(db_conn, days=5)
        assert len(rows) == 1
        assert rows[0]["run_count"] == 0
        assert rows[0]["total_distance_m"] is None

    def test_excludes_dates_without_sleep(self, db_conn):
        # Activity with no corresponding sleep record
        _insert_activity(db_conn, "1", type_="running",
                         start_time=_today_minus(1), distance_m=10000)
        rows = queries.get_training_vs_sleep(db_conn, days=5)
        assert rows == []

    def test_excludes_cycling_from_load(self, db_conn):
        d = _today_date_minus(1)
        _insert_health(db_conn, d)
        _insert_activity(db_conn, "1", type_="cycling",
                         start_time=d + " 07:00:00", distance_m=30000)

        rows = queries.get_training_vs_sleep(db_conn, days=5)
        assert rows[0]["run_count"] == 0

    def test_excludes_old_records(self, db_conn):
        _insert_health(db_conn, _today_date_minus(1))
        _insert_health(db_conn, _today_date_minus(100))
        rows = queries.get_training_vs_sleep(db_conn, days=30)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# get_daily_health
# ---------------------------------------------------------------------------

class TestGetDailyHealth:
    def test_generates_complete_date_series(self, db_conn):
        rows = queries.get_daily_health(db_conn, "2024-03-13", "2024-03-15")
        assert len(rows) == 3
        assert rows[0]["date"] == "2024-03-13"
        assert rows[2]["date"] == "2024-03-15"

    def test_joins_all_health_tables(self, db_conn):
        _insert_health(db_conn, "2024-03-15", steps=9000, sleep_s=28800, resting_hr=52)
        rows = queries.get_daily_health(db_conn, "2024-03-15", "2024-03-15")
        row = rows[0]
        assert row["total_steps"] == 9000
        assert row["total_sleep_seconds"] == 28800
        assert row["resting_hr"] == 52
        assert row["hrv_last_night"] is not None
        assert row["battery_end"] is not None

    def test_missing_data_produces_none_not_missing_row(self, db_conn):
        # Date with no health data at all
        rows = queries.get_daily_health(db_conn, "2024-01-01", "2024-01-01")
        assert len(rows) == 1
        assert rows[0]["total_steps"] is None
        assert rows[0]["sleep_score"] is None

    def test_single_date_range(self, db_conn):
        rows = queries.get_daily_health(db_conn, "2024-03-15", "2024-03-15")
        assert len(rows) == 1
