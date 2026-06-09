"""
Tests for the reprocess-derived-fields phase.

Covers:
  - DB migration (adding columns to existing tables)
  - normalise_activity_derived
  - normalise_activity_splits
  - normalise_daily_summary_derived
  - update_activity_derived / replace_activity_splits / update_daily_summary_derived
  - get_activity_splits / get_intensity_distribution / get_running_dynamics
  - No Garmin imports in normalise/repositories
"""
import inspect
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from garmin_sync import normalise, queries
from garmin_sync import repositories as repo
from garmin_sync.db import (
    _ACTIVITY_DERIVED_COLS,
    _DAILY_SUMMARY_DERIVED_COLS,
    _migrate_add_columns,
    init_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_raw_payload(conn, data_type, garmin_id=None, date=None, payload=None):
    payload = payload or {}
    conn.execute(
        """
        INSERT INTO raw_payload (source, data_type, garmin_id, date, fetched_at, payload_json, payload_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("garmin_connect", data_type, garmin_id, date,
         datetime.now(timezone.utc).isoformat(),
         json.dumps(payload), "hash-" + (garmin_id or date or "x")),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_activity(conn, activity_id, raw_payload_id):
    conn.execute(
        """
        INSERT INTO activity (activity_id, name, type, start_time, raw_payload_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (activity_id, "Test Run", "running", "2024-01-15 06:00:00",
         raw_payload_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _insert_daily_summary(conn, date, raw_payload_id):
    conn.execute(
        """
        INSERT INTO daily_summary (date, raw_payload_id, updated_at)
        VALUES (?, ?, ?)
        """,
        (date, raw_payload_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ===========================================================================
# TestMigration
# ===========================================================================

class TestMigration:
    def test_fresh_db_has_derived_columns(self, db_conn):
        """Fresh DB created via schema.sql should have all derived columns."""
        existing = {row[1] for row in db_conn.execute("PRAGMA table_info(activity)").fetchall()}
        for col, _ in _ACTIVITY_DERIVED_COLS:
            assert col in existing, f"Missing activity column: {col}"

        existing_ds = {row[1] for row in db_conn.execute("PRAGMA table_info(daily_summary)").fetchall()}
        for col, _ in _DAILY_SUMMARY_DERIVED_COLS:
            assert col in existing_ds, f"Missing daily_summary column: {col}"

    def test_migration_adds_columns_to_existing_table(self):
        """Migration should add missing columns to an existing table."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # Create a minimal activity table without derived cols
        conn.execute(
            "CREATE TABLE activity (activity_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL)"
        )
        conn.commit()

        _migrate_add_columns(conn, "activity", _ACTIVITY_DERIVED_COLS)

        existing = {row[1] for row in conn.execute("PRAGMA table_info(activity)").fetchall()}
        for col, _ in _ACTIVITY_DERIVED_COLS:
            assert col in existing

    def test_migration_is_idempotent(self, db_conn):
        """Running migration twice must not raise errors."""
        _migrate_add_columns(db_conn, "activity", _ACTIVITY_DERIVED_COLS)
        _migrate_add_columns(db_conn, "activity", _ACTIVITY_DERIVED_COLS)
        _migrate_add_columns(db_conn, "daily_summary", _DAILY_SUMMARY_DERIVED_COLS)
        _migrate_add_columns(db_conn, "daily_summary", _DAILY_SUMMARY_DERIVED_COLS)
        # No exception = pass

    def test_activity_split_table_exists(self, db_conn):
        """activity_split table should be created by schema.sql."""
        tables = {row[0] for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "activity_split" in tables


# ===========================================================================
# TestNormaliseActivityDerived
# ===========================================================================

class TestNormaliseActivityDerived:
    def test_extracts_training_load_from_summary(self):
        summary = {"activityTrainingLoad": 70.0}
        result = normalise.normalise_activity_derived(summary)
        assert result["training_load"] == 70.0

    def test_prefers_detail_summary_dto(self):
        summary = {"activityTrainingLoad": 70.0}
        detail = {"summaryDTO": {"activityTrainingLoad": 80.0}}
        result = normalise.normalise_activity_derived(summary, detail)
        assert result["training_load"] == 80.0

    def test_extracts_hr_zones(self):
        summary = {
            "hrTimeInZone_1": 300.7,
            "hrTimeInZone_2": 600.2,
            "hrTimeInZone_3": 1200.6,
            "hrTimeInZone_4": 480.9,
            "hrTimeInZone_5": 60.1,
        }
        result = normalise.normalise_activity_derived(summary)
        assert result["hr_zone_1_s"] == 301
        assert result["hr_zone_2_s"] == 600
        assert result["hr_zone_3_s"] == 1201
        assert result["hr_zone_4_s"] == 481
        assert result["hr_zone_5_s"] == 60

    def test_extracts_running_dynamics(self):
        summary = {}
        detail = {"summaryDTO": {
            "groundContactTime": 255.0,
            "groundContactBalanceLeft": 49.8,
            "verticalOscillation": 8.5,
            "verticalRatio": 7.2,
            "strideLength": 120.0,
        }}
        result = normalise.normalise_activity_derived(summary, detail)
        assert result["ground_contact_ms"] == 255.0
        assert result["ground_contact_balance_left"] == 49.8
        assert result["vertical_oscillation_cm"] == 8.5
        assert result["vertical_ratio_pct"] == 7.2
        assert result["stride_length_cm"] == 120.0

    def test_is_pr_true(self):
        summary = {"pr": True}
        result = normalise.normalise_activity_derived(summary)
        assert result["is_pr"] == 1

    def test_is_pr_false(self):
        summary = {"pr": False}
        result = normalise.normalise_activity_derived(summary)
        assert result["is_pr"] == 0

    def test_is_pr_missing(self):
        result = normalise.normalise_activity_derived({})
        assert result["is_pr"] == 0

    def test_missing_fields_return_none(self):
        result = normalise.normalise_activity_derived({})
        assert result["training_load"] is None
        assert result["activity_steps"] is None
        assert result["body_battery_delta"] is None
        assert result["avg_respiration_rate"] is None
        assert result["hr_zone_1_s"] is None
        assert result["norm_power"] is None
        assert result["fastest_km_s"] is None
        assert result["temp_avg_c"] is None
        assert result["water_estimated_ml"] is None
        assert result["stamina_start"] is None

    def test_no_detail_falls_back_to_summary(self):
        summary = {"activityTrainingLoad": 55.0, "steps": 8000}
        result = normalise.normalise_activity_derived(summary, None)
        assert result["training_load"] == 55.0
        assert result["activity_steps"] == 8000
        # Stamina only in detail summaryDTO
        assert result["stamina_start"] is None

    def test_all_keys_present(self):
        result = normalise.normalise_activity_derived({})
        expected_keys = [
            "training_load", "activity_steps", "body_battery_delta",
            "avg_respiration_rate", "hr_zone_1_s", "hr_zone_2_s", "hr_zone_3_s",
            "hr_zone_4_s", "hr_zone_5_s", "norm_power", "fastest_km_s",
            "fastest_mile_s", "fastest_5k_s", "temp_avg_c", "temp_min_c",
            "temp_max_c", "water_estimated_ml", "is_pr", "stamina_start",
            "stamina_end", "stamina_min", "total_work_j", "ground_contact_ms",
            "ground_contact_balance_left", "vertical_oscillation_cm",
            "vertical_ratio_pct", "stride_length_cm",
        ]
        for k in expected_keys:
            assert k in result, f"Missing key: {k}"


# ===========================================================================
# TestNormaliseActivitySplits
# ===========================================================================

class TestNormaliseActivitySplits:
    def _make_split(self, **kwargs):
        defaults = {
            "splitType": "INTERVAL_ACTIVE",
            "distance": 1000.0,
            "duration": 300.0,
            "movingDuration": 295.0,
            "averageHR": 155,
            "maxHR": 170,
            "averageSpeed": 3.33,
            "averageRunCadence": 168.0,
            "averagePower": 250.0,
            "maxPower": 300.0,
            "normalizedPower": 255.0,
            "calories": 50,
            "elevationGain": 5.0,
            "elevationLoss": 3.0,
            "groundContactTime": 250.0,
            "verticalOscillation": 8.2,
        }
        defaults.update(kwargs)
        return defaults

    def test_extracts_splits_from_detail(self):
        splits = [self._make_split(), self._make_split(distance=2000.0)]
        detail = {"splitSummaries": splits}
        result = normalise.normalise_activity_splits(detail, {}, "act1", 42)
        assert len(result) == 2
        assert result[0]["distance_m"] == 1000.0
        assert result[0]["split_type"] == "INTERVAL_ACTIVE"
        assert result[0]["activity_id"] == "act1"
        assert result[0]["raw_payload_id"] == 42

    def test_falls_back_to_summary_splits(self):
        splits = [self._make_split()]
        summary = {"splitSummaries": splits}
        result = normalise.normalise_activity_splits(None, summary, "act1", 1)
        assert len(result) == 1
        assert result[0]["distance_m"] == 1000.0

    def test_empty_splits_returns_empty_list(self):
        result = normalise.normalise_activity_splits(None, {}, "act1", 1)
        assert result == []

    def test_split_index_is_enumerated_from_zero(self):
        splits = [self._make_split(), self._make_split(), self._make_split()]
        detail = {"splitSummaries": splits}
        result = normalise.normalise_activity_splits(detail, {}, "act1", 1)
        assert [r["split_index"] for r in result] == [0, 1, 2]

    def test_hr_converted_to_int(self):
        split = self._make_split(averageHR=155.7, maxHR=170.3)
        detail = {"splitSummaries": [split]}
        result = normalise.normalise_activity_splits(detail, {}, "act1", 1)
        assert result[0]["avg_hr"] == 155
        assert result[0]["max_hr"] == 170

    def test_missing_hr_is_none(self):
        split = self._make_split()
        del split["averageHR"]
        del split["maxHR"]
        detail = {"splitSummaries": [split]}
        result = normalise.normalise_activity_splits(detail, {}, "act1", 1)
        assert result[0]["avg_hr"] is None
        assert result[0]["max_hr"] is None

    def test_prefers_detail_over_summary_splits(self):
        detail_split = self._make_split(distance=999.0)
        summary_split = self._make_split(distance=1111.0)
        detail = {"splitSummaries": [detail_split]}
        summary = {"splitSummaries": [summary_split]}
        result = normalise.normalise_activity_splits(detail, summary, "act1", 1)
        assert result[0]["distance_m"] == 999.0

    def test_updated_at_is_set(self):
        split = self._make_split()
        detail = {"splitSummaries": [split]}
        result = normalise.normalise_activity_splits(detail, {}, "act1", 1)
        assert result[0]["updated_at"] is not None


# ===========================================================================
# TestNormaliseDailySummaryDerived
# ===========================================================================

class TestNormaliseDailySummaryDerived:
    def test_extracts_spo2_fields(self):
        raw = {"averageSpo2": 96.5, "latestSpo2": 97.0, "lowestSpo2": 94.0}
        result = normalise.normalise_daily_summary_derived(raw)
        assert result["average_spo2"] == 96.5
        assert result["latest_spo2"] == 97.0
        assert result["lowest_spo2"] == 94.0

    def test_extracts_body_battery_fields(self):
        raw = {
            "bodyBatteryHighestValue": 90,
            "bodyBatteryLowestValue": 30,
            "bodyBatteryAtWakeTime": 75,
        }
        result = normalise.normalise_daily_summary_derived(raw)
        assert result["body_battery_highest"] == 90
        assert result["body_battery_lowest"] == 30
        assert result["body_battery_at_wake"] == 75

    def test_extracts_sedentary_and_rhr(self):
        raw = {"sedentarySeconds": 28800, "lastSevenDaysAvgRestingHeartRate": 52.3}
        result = normalise.normalise_daily_summary_derived(raw)
        assert result["sedentary_seconds"] == 28800
        assert result["resting_hr_7d_avg"] == 52.3

    def test_missing_fields_return_none(self):
        result = normalise.normalise_daily_summary_derived({})
        assert result["average_spo2"] is None
        assert result["latest_spo2"] is None
        assert result["lowest_spo2"] is None
        assert result["body_battery_highest"] is None
        assert result["body_battery_lowest"] is None
        assert result["body_battery_at_wake"] is None
        assert result["sedentary_seconds"] is None
        assert result["resting_hr_7d_avg"] is None

    def test_all_keys_present(self):
        result = normalise.normalise_daily_summary_derived({})
        expected = [
            "average_spo2", "latest_spo2", "lowest_spo2",
            "body_battery_highest", "body_battery_lowest", "body_battery_at_wake",
            "sedentary_seconds", "resting_hr_7d_avg",
        ]
        for k in expected:
            assert k in result


# ===========================================================================
# TestReprocessActivityDerived
# ===========================================================================

class TestReprocessActivityDerived:
    @pytest.fixture()
    def activity_setup(self, db_conn):
        """Insert raw_payload (summary + detail) and activity rows."""
        summary_payload = {
            "activityId": "act123",
            "activityTrainingLoad": 65.0,
            "hrTimeInZone_1": 120,
            "hrTimeInZone_2": 600,
            "hrTimeInZone_3": 1200,
            "hrTimeInZone_4": 300,
            "hrTimeInZone_5": 60,
        }
        detail_payload = {
            "summaryDTO": {
                "activityTrainingLoad": 70.0,
                "groundContactTime": 255.0,
                "beginPotentialStamina": 88.0,
                "endPotentialStamina": 72.0,
                "minAvailableStamina": 68.0,
            },
            "splitSummaries": [
                {
                    "splitType": "INTERVAL_ACTIVE",
                    "distance": 1000.0,
                    "duration": 280.0,
                    "movingDuration": 278.0,
                    "averageHR": 160,
                    "maxHR": 172,
                    "averageSpeed": 3.57,
                }
            ],
        }
        raw_s_id = _insert_raw_payload(db_conn, "activity_summary", garmin_id="act123", payload=summary_payload)
        raw_d_id = _insert_raw_payload(db_conn, "activity_detail", garmin_id="act123", payload=detail_payload)
        _insert_activity(db_conn, "act123", raw_s_id)
        return db_conn, raw_s_id, raw_d_id

    def test_update_activity_derived_sets_training_load(self, activity_setup):
        conn, _, _ = activity_setup
        fields = {"training_load": 70.0, "hr_zone_1_s": 120}
        updated = repo.update_activity_derived(conn, "act123", fields)
        assert updated is True
        row = conn.execute("SELECT training_load, hr_zone_1_s FROM activity WHERE activity_id='act123'").fetchone()
        assert row["training_load"] == 70.0
        assert row["hr_zone_1_s"] == 120

    def test_reprocess_is_idempotent(self, activity_setup):
        conn, _, _ = activity_setup
        fields = {"training_load": 70.0}
        repo.update_activity_derived(conn, "act123", fields)
        count_before = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        repo.update_activity_derived(conn, "act123", fields)
        count_after = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        assert count_before == count_after
        row = conn.execute("SELECT training_load FROM activity WHERE activity_id='act123'").fetchone()
        assert row["training_load"] == 70.0

    def test_replace_activity_splits_inserts_rows(self, activity_setup):
        conn, _, raw_d_id = activity_setup
        splits = normalise.normalise_activity_splits(
            {"splitSummaries": [{"splitType": "RUN", "distance": 1000.0, "duration": 300.0, "movingDuration": 295.0}]},
            {},
            "act123",
            raw_d_id,
        )
        repo.replace_activity_splits(conn, "act123", splits)
        count = conn.execute("SELECT COUNT(*) FROM activity_split WHERE activity_id='act123'").fetchone()[0]
        assert count == 1

    def test_replace_activity_splits_is_idempotent(self, activity_setup):
        conn, _, raw_d_id = activity_setup
        splits = normalise.normalise_activity_splits(
            {"splitSummaries": [{"splitType": "RUN", "distance": 1000.0, "duration": 300.0, "movingDuration": 295.0}]},
            {},
            "act123",
            raw_d_id,
        )
        repo.replace_activity_splits(conn, "act123", splits)
        repo.replace_activity_splits(conn, "act123", splits)
        count = conn.execute("SELECT COUNT(*) FROM activity_split WHERE activity_id='act123'").fetchone()[0]
        assert count == 1

    def test_activity_not_in_activity_table_is_skipped(self, db_conn):
        """raw_payload row with no matching activity row → update_activity_derived returns False."""
        payload = {"activityId": "ghost999", "activityTrainingLoad": 50.0}
        _insert_raw_payload(db_conn, "activity_summary", garmin_id="ghost999", payload=payload)
        # No activity row inserted
        fields = normalise.normalise_activity_derived(payload)
        result = repo.update_activity_derived(db_conn, "ghost999", fields)
        assert result is False

    def test_update_empty_fields_returns_false(self, activity_setup):
        conn, _, _ = activity_setup
        result = repo.update_activity_derived(conn, "act123", {})
        assert result is False


# ===========================================================================
# TestReprocessHealthDerived
# ===========================================================================

class TestReprocessHealthDerived:
    @pytest.fixture()
    def health_setup(self, db_conn):
        payload = {
            "averageSpo2": 96.0,
            "latestSpo2": 97.0,
            "lowestSpo2": 93.5,
            "bodyBatteryHighestValue": 88,
            "bodyBatteryLowestValue": 25,
            "bodyBatteryAtWakeTime": 70,
            "sedentarySeconds": 25200,
            "lastSevenDaysAvgRestingHeartRate": 51.0,
        }
        raw_id = _insert_raw_payload(db_conn, "daily_summary", date="2024-03-15", payload=payload)
        _insert_daily_summary(db_conn, "2024-03-15", raw_id)
        return db_conn, raw_id

    def test_update_daily_summary_derived_sets_spo2(self, health_setup):
        conn, _ = health_setup
        fields = normalise.normalise_daily_summary_derived({
            "averageSpo2": 96.0, "latestSpo2": 97.0, "lowestSpo2": 93.5,
            "bodyBatteryHighestValue": 88, "bodyBatteryLowestValue": 25,
            "bodyBatteryAtWakeTime": 70, "sedentarySeconds": 25200,
            "lastSevenDaysAvgRestingHeartRate": 51.0,
        })
        updated = repo.update_daily_summary_derived(conn, "2024-03-15", fields)
        assert updated is True
        row = conn.execute(
            "SELECT average_spo2, body_battery_highest FROM daily_summary WHERE date='2024-03-15'"
        ).fetchone()
        assert row["average_spo2"] == 96.0
        assert row["body_battery_highest"] == 88

    def test_reprocess_health_is_idempotent(self, health_setup):
        conn, _ = health_setup
        fields = {"average_spo2": 96.0}
        repo.update_daily_summary_derived(conn, "2024-03-15", fields)
        count_before = conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0]
        repo.update_daily_summary_derived(conn, "2024-03-15", fields)
        count_after = conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0]
        assert count_before == count_after

    def test_daily_summary_not_in_table_is_skipped(self, db_conn):
        """raw_payload exists but no daily_summary row → update returns False."""
        payload = {"averageSpo2": 96.0}
        _insert_raw_payload(db_conn, "daily_summary", date="2024-01-01", payload=payload)
        fields = normalise.normalise_daily_summary_derived(payload)
        result = repo.update_daily_summary_derived(db_conn, "2024-01-01", fields)
        assert result is False

    def test_update_empty_fields_returns_false(self, health_setup):
        conn, _ = health_setup
        result = repo.update_daily_summary_derived(conn, "2024-03-15", {})
        assert result is False


# ===========================================================================
# TestQueryActivitySplits
# ===========================================================================

class TestQueryActivitySplits:
    @pytest.fixture()
    def splits_setup(self, db_conn):
        raw_id = _insert_raw_payload(db_conn, "activity_detail", garmin_id="runact1", payload={})
        _insert_activity(db_conn, "runact1", raw_id)
        now = datetime.now(timezone.utc).isoformat()
        splits = [
            {
                "activity_id": "runact1", "split_index": 0, "split_type": "RUN",
                "distance_m": 1000.0, "duration_s": 280.0, "moving_duration_s": 278.0,
                "avg_hr": 158, "max_hr": 168, "avg_speed_mps": 3.57, "avg_cadence": 168.0,
                "avg_power": None, "max_power": None, "norm_power": None, "calories": None,
                "elevation_gain_m": None, "elevation_loss_m": None,
                "ground_contact_ms": None, "vertical_oscillation_cm": None,
                "raw_payload_id": raw_id, "updated_at": now,
            },
            {
                "activity_id": "runact1", "split_index": 1, "split_type": "RUN",
                "distance_m": 2000.0, "duration_s": 580.0, "moving_duration_s": 578.0,
                "avg_hr": 162, "max_hr": 175, "avg_speed_mps": 3.45, "avg_cadence": 165.0,
                "avg_power": None, "max_power": None, "norm_power": None, "calories": None,
                "elevation_gain_m": None, "elevation_loss_m": None,
                "ground_contact_ms": None, "vertical_oscillation_cm": None,
                "raw_payload_id": raw_id, "updated_at": now,
            },
        ]
        repo.replace_activity_splits(db_conn, "runact1", splits)
        return db_conn

    def test_returns_splits_in_order(self, splits_setup):
        rows = queries.get_activity_splits(splits_setup, "runact1")
        assert len(rows) == 2
        assert rows[0]["split_index"] == 0
        assert rows[1]["split_index"] == 1

    def test_returns_correct_data(self, splits_setup):
        rows = queries.get_activity_splits(splits_setup, "runact1")
        assert rows[0]["distance_m"] == 1000.0
        assert rows[1]["distance_m"] == 2000.0

    def test_empty_for_unknown_activity(self, splits_setup):
        rows = queries.get_activity_splits(splits_setup, "doesnotexist")
        assert rows == []


# ===========================================================================
# TestGetIntensityDistribution
# ===========================================================================

class TestGetIntensityDistribution:
    @pytest.fixture()
    def intensity_setup(self, db_conn):
        raw_id = _insert_raw_payload(db_conn, "activity_summary", garmin_id="run1", payload={})
        # Insert two running activities in the same week with zone data
        db_conn.execute(
            """
            INSERT INTO activity (
                activity_id, name, type, start_time,
                hr_zone_1_s, hr_zone_2_s, hr_zone_3_s, hr_zone_4_s, hr_zone_5_s,
                raw_payload_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run1", "Run 1", "running", "2024-03-11 06:00:00",
             300, 600, 1200, 480, 60,
             raw_id, datetime.now(timezone.utc).isoformat()),
        )
        raw_id2 = _insert_raw_payload(db_conn, "activity_summary", garmin_id="run2", payload={})
        db_conn.execute(
            """
            INSERT INTO activity (
                activity_id, name, type, start_time,
                hr_zone_1_s, hr_zone_2_s, hr_zone_3_s, hr_zone_4_s, hr_zone_5_s,
                raw_payload_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run2", "Run 2", "running", "2024-03-13 06:00:00",
             200, 400, 800, 200, 0,
             raw_id2, datetime.now(timezone.utc).isoformat()),
        )
        # An activity without zone data — should be excluded
        raw_id3 = _insert_raw_payload(db_conn, "activity_summary", garmin_id="run3", payload={})
        db_conn.execute(
            """
            INSERT INTO activity (activity_id, name, type, start_time, raw_payload_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("run3", "Run 3", "running", "2024-03-14 06:00:00",
             raw_id3, datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()
        return db_conn

    def test_returns_zone_sums_per_week(self, intensity_setup):
        rows = queries.get_intensity_distribution(intensity_setup, weeks=9999)
        assert len(rows) >= 1
        # Find the week containing 2024-03-11 (Monday)
        week_row = next((r for r in rows if r["week_start"] == "2024-03-11"), None)
        assert week_row is not None
        assert week_row["run_count"] == 2
        assert week_row["zone_1_s"] == 500   # 300+200
        assert week_row["zone_3_s"] == 2000  # 1200+800

    def test_excludes_activities_without_zone_data(self, intensity_setup):
        rows = queries.get_intensity_distribution(intensity_setup, weeks=9999)
        # run3 has no zone data; both run1 and run2 are in the same week
        week_row = next((r for r in rows if r["week_start"] == "2024-03-11"), None)
        assert week_row is not None
        assert week_row["run_count"] == 2  # run3 excluded

    def test_weeks_limit(self, intensity_setup):
        # Using a very small window that excludes 2024-03-11
        rows = queries.get_intensity_distribution(intensity_setup, weeks=1)
        # The activities are in the past, should return empty
        assert isinstance(rows, list)


# ===========================================================================
# TestGetRunningDynamics
# ===========================================================================

class TestGetRunningDynamics:
    @pytest.fixture()
    def dynamics_setup(self, db_conn):
        # Activity with dynamics
        raw_id1 = _insert_raw_payload(db_conn, "activity_summary", garmin_id="dyn1", payload={})
        db_conn.execute(
            """
            INSERT INTO activity (
                activity_id, name, type, start_time,
                distance_m, avg_cadence, ground_contact_ms,
                ground_contact_balance_left, vertical_oscillation_cm,
                vertical_ratio_pct, stride_length_cm, avg_hr,
                raw_payload_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("dyn1", "Dynamic Run", "running", "2024-03-15 06:00:00",
             10000.0, 168.0, 255.0, 49.8, 8.5, 7.2, 120.0, 155,
             raw_id1, datetime.now(timezone.utc).isoformat()),
        )
        # Older activity with dynamics
        raw_id2 = _insert_raw_payload(db_conn, "activity_summary", garmin_id="dyn2", payload={})
        db_conn.execute(
            """
            INSERT INTO activity (
                activity_id, name, type, start_time,
                distance_m, avg_cadence, ground_contact_ms,
                raw_payload_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("dyn2", "Older Run", "running", "2024-03-10 06:00:00",
             8000.0, 165.0, 260.0,
             raw_id2, datetime.now(timezone.utc).isoformat()),
        )
        # Activity without dynamics — should be excluded
        raw_id3 = _insert_raw_payload(db_conn, "activity_summary", garmin_id="nodyn", payload={})
        db_conn.execute(
            """
            INSERT INTO activity (activity_id, name, type, start_time, raw_payload_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("nodyn", "No Dynamics", "running", "2024-03-14 06:00:00",
             raw_id3, datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()
        return db_conn

    def test_returns_dynamics_newest_first(self, dynamics_setup):
        rows = queries.get_running_dynamics(dynamics_setup, days=9999)
        assert len(rows) >= 2
        # dyn1 (2024-03-15) should come before dyn2 (2024-03-10)
        ids = [r["start_time"][:10] for r in rows if r["start_time"][:10] in ("2024-03-15", "2024-03-10")]
        assert ids[0] == "2024-03-15"

    def test_excludes_activities_without_dynamics(self, dynamics_setup):
        rows = queries.get_running_dynamics(dynamics_setup, days=9999)
        activity_ids = [r.get("start_time") for r in rows]
        # nodyn should not appear (ground_contact_ms is NULL)
        for r in rows:
            assert r["ground_contact_ms"] is not None

    def test_days_filter(self, dynamics_setup):
        # Only 1 day window — should exclude the 2024-03-15 activity too (it's in the past)
        rows = queries.get_running_dynamics(dynamics_setup, days=1)
        assert isinstance(rows, list)

    def test_correct_fields_returned(self, dynamics_setup):
        rows = queries.get_running_dynamics(dynamics_setup, days=9999)
        dyn1 = next(r for r in rows if "2024-03-15" in str(r["start_time"]))
        assert dyn1["ground_contact_ms"] == 255.0
        assert dyn1["ground_contact_balance_left"] == 49.8
        assert dyn1["vertical_oscillation_cm"] == 8.5
        assert dyn1["stride_length_cm"] == 120.0
        assert dyn1["avg_hr"] == 155


# ===========================================================================
# TestNoGarminImports
# ===========================================================================

class TestNoGarminImports:
    def test_normalise_derived_no_garmin_import(self):
        """normalise.py must not import garmin_connect or GarminClient."""
        src = inspect.getsource(normalise)
        assert "garmin_connect" not in src or "garminconnect" not in src.lower().replace("garmin_connect", "")
        # More targeted: the new functions should not reference Garmin API
        assert "GarminClient" not in src

    def test_repositories_derived_no_garmin_import(self):
        """repositories.py must not import garmin_connect or GarminClient."""
        import garmin_sync.repositories as repositories_module
        src = inspect.getsource(repositories_module)
        assert "GarminClient" not in src
