"""Tests for Phase 5b performance metrics: normalise, repositories, and queries."""

import pytest

from garmin_sync.normalise import (
    normalise_lactate_threshold,
    normalise_race_prediction,
    normalise_endurance_score,
    normalise_hill_score_entry,
    normalise_training_status,
    normalise_training_readiness,
    normalise_fitness_age,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def raw_payload_id(db_conn):
    """Insert a dummy raw_payload row and return its id."""
    cur = db_conn.execute(
        "INSERT INTO raw_payload (source, data_type, date, fetched_at, payload_json, payload_hash) "
        "VALUES ('garmin_connect', 'test', '2025-01-01', '2025-01-01T00:00:00+00:00', '{}', 'abc')"
    )
    db_conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Normalise tests — no DB needed
# ---------------------------------------------------------------------------


def test_normalise_lactate_threshold_merges_arrays():
    raw = {
        "speed":      [{"updatedDate": "2025-01-10", "value": 0.33, "series": "running"}],
        "heart_rate": [{"updatedDate": "2025-01-10", "value": 165}],
        "power":      [{"updatedDate": "2025-01-10", "value": 420}],
    }
    rows = normalise_lactate_threshold(raw, 1)
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-01-10"
    assert rows[0]["threshold_hr"] == 165
    assert rows[0]["threshold_speed_value"] == pytest.approx(0.33)
    assert rows[0]["threshold_power_w"] == pytest.approx(420)
    assert rows[0]["series"] == "running"


def test_normalise_lactate_threshold_empty():
    assert normalise_lactate_threshold({}, 1) == []
    assert normalise_lactate_threshold(None, 1) == []


def test_normalise_lactate_threshold_multiple_dates():
    raw = {
        "speed": [
            {"updatedDate": "2025-01-10", "value": 0.33, "series": "running"},
            {"updatedDate": "2025-01-20", "value": 0.35, "series": "running"},
        ],
        "heart_rate": [
            {"updatedDate": "2025-01-10", "value": 165},
            {"updatedDate": "2025-01-20", "value": 167},
        ],
        "power": [],
    }
    rows = normalise_lactate_threshold(raw, 1)
    assert len(rows) == 2
    dates = {r["date"] for r in rows}
    assert dates == {"2025-01-10", "2025-01-20"}


def test_normalise_lactate_threshold_falls_back_to_from():
    raw = {
        "speed": [{"from": "2025-01-10", "value": 0.33, "series": "running"}],
        "heart_rate": [],
        "power": [],
    }
    rows = normalise_lactate_threshold(raw, 1)
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-01-10"


def test_normalise_race_prediction_basic():
    entry = {
        "calendarDate": "2025-11-10",
        "time5K": 1479,
        "time10K": 3175,
        "timeHalfMarathon": 7078,
        "timeMarathon": 16245,
    }
    row = normalise_race_prediction(entry, 1)
    assert row is not None
    assert row["date"] == "2025-11-10"
    assert row["race_5k_s"] == 1479
    assert row["race_10k_s"] == 3175
    assert row["race_half_s"] == 7078
    assert row["race_full_s"] == 16245


def test_normalise_race_prediction_missing_date():
    assert normalise_race_prediction({}, 1) is None
    assert normalise_race_prediction({"time5K": 1500}, 1) is None


def test_normalise_endurance_score_dto_and_groupmap():
    raw = {
        "enduranceScoreDTO": {
            "calendarDate": "2025-11-14",
            "overallScore": 5671,
            "classification": 2,
        },
        "groupMap": {
            "2025-11-08": {"groupAverage": 5643, "groupMax": 5683},
            "2025-11-01": {"groupAverage": 5600, "groupMax": 5650},
        },
    }
    rows = normalise_endurance_score(raw, 1)
    # 1 from DTO + 2 from groupMap
    assert len(rows) == 3
    dto_row = next(r for r in rows if r["date"] == "2025-11-14")
    assert dto_row["score"] == 5671
    assert dto_row["classification"] == 2
    week_row = next(r for r in rows if r["date"] == "2025-11-08")
    assert week_row["score"] == 5643
    assert week_row["classification"] is None


def test_normalise_endurance_score_empty():
    assert normalise_endurance_score({}, 1) == []
    assert normalise_endurance_score(None, 1) == []


def test_normalise_hill_score_entry_basic():
    entry = {
        "calendarDate": "2025-11-14",
        "overallScore": 50,
        "strengthScore": 12,
        "enduranceScore": 48,
        "hillScoreClassificationId": 3,
    }
    row = normalise_hill_score_entry(entry, 1)
    assert row is not None
    assert row["date"] == "2025-11-14"
    assert row["overall_score"] == 50
    assert row["strength_score"] == 12
    assert row["hill_endurance_score"] == 48
    assert row["classification"] == 3


def test_normalise_hill_score_entry_missing_date():
    assert normalise_hill_score_entry({}, 1) is None


def test_normalise_training_status_extracts_primary_device():
    raw = {
        "mostRecentVO2Max": {
            "generic": {"calendarDate": "2025-11-12", "vo2MaxPreciseValue": 48.6, "vo2MaxValue": 49.0}
        },
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "3451984205": {
                    "trainingStatus": 7,
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 306,
                        "dailyTrainingLoadChronic": 257,
                        "dailyAcuteChronicWorkloadRatio": 1.1,
                    },
                    "primaryTrainingDevice": True,
                }
            }
        },
        "mostRecentTrainingLoadBalance": {
            "metricsTrainingLoadBalanceDTOMap": {
                "3451984205": {
                    "monthlyLoadAerobicLow": 677.6,
                    "monthlyLoadAerobicHigh": 195.7,
                    "monthlyLoadAnaerobic": 85.7,
                    "trainingBalanceFeedbackPhrase": "AEROBIC_HIGH_SHORTAGE",
                    "primaryTrainingDevice": True,
                }
            }
        },
    }
    row = normalise_training_status(raw, "2025-11-12", 1)
    assert row is not None
    assert row["date"] == "2025-11-12"
    assert row["vo2max"] == pytest.approx(49.0)
    assert row["vo2max_precise"] == pytest.approx(48.6)
    assert row["training_status_code"] == 7
    assert row["training_status_phrase"] == "PRODUCTIVE_1"
    assert row["acute_load"] == 306
    assert row["chronic_load"] == 257
    assert row["acwr"] == pytest.approx(1.1)
    assert row["load_aerobic_low"] == pytest.approx(677.6)
    assert row["load_feedback"] == "AEROBIC_HIGH_SHORTAGE"


def test_normalise_training_status_empty():
    assert normalise_training_status({}, "2025-01-01", 1) is None
    assert normalise_training_status(None, "2025-01-01", 1) is None


def test_normalise_training_readiness_primary_is_non_morning():
    """Primary row uses first non-AFTER_WAKEUP_RESET entry; morning fields use AFTER_WAKEUP_RESET."""
    raw_list = [
        {
            "calendarDate": "2025-11-14",
            "inputContext": "INTRADAY",
            "score": 50,
            "level": "MODERATE",
            "feedbackShort": "MODERATE",
            "recoveryTime": 5,
            "acwrFactorPercent": 60,
            "acuteLoad": 200,
            "hrvFactorPercent": 70,
            "sleepHistoryFactorPercent": 55,
            "stressHistoryFactorPercent": 48,
            "primaryActivityTracker": True,
        },
        {
            "calendarDate": "2025-11-14",
            "inputContext": "AFTER_WAKEUP_RESET",
            "score": 78,
            "level": "HIGH",
            "feedbackShort": "WELL_RECOVERED",
            "recoveryTime": 1,
            "acwrFactorPercent": 83,
            "acuteLoad": 306,
            "hrvFactorPercent": 100,
            "primaryActivityTracker": True,
        },
    ]
    row = normalise_training_readiness(raw_list, "2025-11-14", 1)
    assert row is not None
    # Primary entry is INTRADAY (first non-morning entry)
    assert row["score"] == 50
    assert row["level"] == "MODERATE"
    assert row["recovery_time_min"] == 5
    assert row["sleep_factor_pct"] == 55
    assert row["stress_factor_pct"] == 48
    # Morning fields from AFTER_WAKEUP_RESET
    assert row["morning_readiness_score"] == 78
    assert row["morning_readiness_level"] == "HIGH"
    assert row["morning_recovery_time_min"] == 1


def test_normalise_training_readiness_falls_back_to_first():
    raw_list = [
        {
            "calendarDate": "2025-11-14",
            "inputContext": "INTRADAY",
            "score": 55,
            "level": "MODERATE",
            "feedbackShort": "SOME_FATIGUE",
            "recoveryTime": 8,
            "acwrFactorPercent": 72,
            "acuteLoad": 350,
            "hrvFactorPercent": 80,
            "primaryActivityTracker": True,
        }
    ]
    row = normalise_training_readiness(raw_list, "2025-11-14", 1)
    assert row is not None
    assert row["score"] == 55
    assert row["level"] == "MODERATE"


def test_normalise_training_readiness_empty():
    assert normalise_training_readiness([], "2025-01-01", 1) is None
    assert normalise_training_readiness(None, "2025-01-01", 1) is None


def test_normalise_fitness_age_basic():
    raw = {"chronologicalAge": 41, "fitnessAge": 37.12, "achievableFitnessAge": 35.39}
    row = normalise_fitness_age(raw, "2025-11-14", 1)
    assert row is not None
    assert row["date"] == "2025-11-14"
    assert row["fitness_age"] == pytest.approx(37.12)
    assert row["achievable_fitness_age"] == pytest.approx(35.39)
    assert row["chronological_age"] == 41


def test_normalise_fitness_age_empty():
    assert normalise_fitness_age({}, "2025-01-01", 1) is None
    assert normalise_fitness_age(None, "2025-01-01", 1) is None


# ---------------------------------------------------------------------------
# Repository idempotency tests
# ---------------------------------------------------------------------------


def test_upsert_lactate_threshold_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-01-10",
        "threshold_hr": 165,
        "threshold_speed_value": 0.33,
        "threshold_power_w": 420.0,
        "series": "running",
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-01-10T00:00:00+00:00",
    }
    repo.upsert_lactate_threshold(db_conn, row)
    repo.upsert_lactate_threshold(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM lactate_threshold").fetchone()[0]
    assert count == 1


def test_upsert_race_prediction_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-06-01",
        "race_5k_s": 1500,
        "race_10k_s": 3200,
        "race_half_s": 7000,
        "race_full_s": 16000,
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    repo.upsert_race_prediction(db_conn, row)
    repo.upsert_race_prediction(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM race_predictions").fetchone()[0]
    assert count == 1


def test_upsert_endurance_score_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-06-01",
        "score": 5671,
        "classification": 2,
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    repo.upsert_endurance_score(db_conn, row)
    repo.upsert_endurance_score(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM endurance_score").fetchone()[0]
    assert count == 1


def test_upsert_hill_score_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-06-01",
        "overall_score": 50,
        "strength_score": 12,
        "hill_endurance_score": 48,
        "classification": 3,
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    repo.upsert_hill_score(db_conn, row)
    repo.upsert_hill_score(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM hill_score").fetchone()[0]
    assert count == 1


def test_upsert_training_status_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-06-01",
        "vo2max": 49.0,
        "vo2max_precise": 48.6,
        "training_status_code": 7,
        "training_status_phrase": "PRODUCTIVE_1",
        "load_aerobic_low": 677.6,
        "load_aerobic_high": 195.7,
        "load_anaerobic": 85.7,
        "load_feedback": "BALANCED",
        "acute_load": 306,
        "chronic_load": 257,
        "acwr": 1.1,
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    repo.upsert_training_status(db_conn, row)
    repo.upsert_training_status(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM training_status").fetchone()[0]
    assert count == 1


def test_upsert_training_readiness_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-06-01",
        "score": 78,
        "level": "HIGH",
        "recovery_time_min": 1,
        "acwr_factor_pct": 83,
        "acute_load": 306,
        "hrv_factor_pct": 100,
        "sleep_factor_pct": 55,
        "stress_factor_pct": 60,
        "feedback_short": "WELL_RECOVERED",
        "morning_readiness_score": 72,
        "morning_readiness_level": "MODERATE",
        "morning_recovery_time_min": 120,
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    repo.upsert_training_readiness(db_conn, row)
    repo.upsert_training_readiness(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM training_readiness").fetchone()[0]
    assert count == 1


def test_upsert_fitness_age_idempotent(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-06-01",
        "fitness_age": 37.12,
        "achievable_fitness_age": 35.39,
        "chronological_age": 41,
        "raw_payload_id": raw_payload_id,
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    repo.upsert_fitness_age(db_conn, row)
    repo.upsert_fitness_age(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM fitness_age").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Query tests — empty tables must return []
# ---------------------------------------------------------------------------


def test_get_lactate_threshold_trend_empty(db_conn):
    from garmin_sync.queries import get_lactate_threshold_trend
    assert get_lactate_threshold_trend(db_conn, days=365) == []


def test_get_race_predictions_trend_empty(db_conn):
    from garmin_sync.queries import get_race_predictions_trend
    assert get_race_predictions_trend(db_conn, days=365) == []


def test_get_endurance_score_trend_empty(db_conn):
    from garmin_sync.queries import get_endurance_score_trend
    assert get_endurance_score_trend(db_conn, days=365) == []


def test_get_hill_score_trend_empty(db_conn):
    from garmin_sync.queries import get_hill_score_trend
    assert get_hill_score_trend(db_conn, days=365) == []


def test_get_training_status_trend_empty(db_conn):
    from garmin_sync.queries import get_training_status_trend
    assert get_training_status_trend(db_conn, days=90) == []


def test_get_training_readiness_trend_empty(db_conn):
    from garmin_sync.queries import get_training_readiness_trend
    assert get_training_readiness_trend(db_conn, days=90) == []


def test_get_vo2max_trend_empty(db_conn):
    from garmin_sync.queries import get_vo2max_trend
    assert get_vo2max_trend(db_conn, days=180) == []


def test_get_performance_summary_empty(db_conn):
    from garmin_sync.queries import get_performance_summary
    assert get_performance_summary(db_conn, days=90) == []


# ---------------------------------------------------------------------------
# Query tests — with data
# ---------------------------------------------------------------------------


def test_get_race_predictions_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_race_predictions_trend
    repo.upsert_race_prediction(db_conn, {
        "date": "2025-06-01", "race_5k_s": 1500, "race_10k_s": 3200,
        "race_half_s": 7000, "race_full_s": 16000,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_race_predictions_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["race_5k_s"] == 1500


def test_get_lactate_threshold_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_lactate_threshold_trend
    repo.upsert_lactate_threshold(db_conn, {
        "date": "2025-06-01", "threshold_hr": 165, "threshold_speed_value": 0.33,
        "threshold_power_w": 420.0, "series": "running",
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_lactate_threshold_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["threshold_hr"] == 165
    assert rows[0]["series"] == "running"


def test_get_endurance_score_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_endurance_score_trend
    repo.upsert_endurance_score(db_conn, {
        "date": "2025-06-01", "score": 5671, "classification": 2,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_endurance_score_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["score"] == 5671


def test_get_hill_score_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_hill_score_trend
    repo.upsert_hill_score(db_conn, {
        "date": "2025-06-01", "overall_score": 50, "strength_score": 12,
        "hill_endurance_score": 48, "classification": 3,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_hill_score_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["overall_score"] == 50
    assert rows[0]["hill_endurance_score"] == 48


def test_get_training_readiness_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_training_readiness_trend
    repo.upsert_training_readiness(db_conn, {
        "date": "2025-06-01", "score": 78, "level": "HIGH",
        "recovery_time_min": 225, "acwr_factor_pct": 83, "acute_load": 306,
        "hrv_factor_pct": 100, "sleep_factor_pct": 55, "stress_factor_pct": 60,
        "feedback_short": "WELL_RECOVERED",
        "morning_readiness_score": 65, "morning_readiness_level": "MODERATE",
        "morning_recovery_time_min": 120,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_training_readiness_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["score"] == 78
    assert rows[0]["level"] == "HIGH"
    assert rows[0]["recovery_time_min"] == 225
    assert rows[0]["morning_readiness_score"] == 65


def test_get_vo2max_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_vo2max_trend
    repo.upsert_training_status(db_conn, {
        "date": "2025-06-01", "vo2max": 49.0, "vo2max_precise": 48.6,
        "training_status_code": 7, "training_status_phrase": "PRODUCTIVE_1",
        "load_aerobic_low": 100.0, "load_aerobic_high": 50.0, "load_anaerobic": 20.0,
        "load_feedback": "BALANCED", "acute_load": 300, "chronic_load": 250, "acwr": 1.2,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_vo2max_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["vo2max"] == pytest.approx(49.0)
    assert rows[0]["vo2max_precise"] == pytest.approx(48.6)


def test_get_performance_summary_partial_data(db_conn, raw_payload_id):
    """Only training_status populated — no crash, returns rows."""
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_performance_summary
    repo.upsert_training_status(db_conn, {
        "date": "2025-06-01", "vo2max": 49.0, "vo2max_precise": 48.6,
        "training_status_code": 7, "training_status_phrase": "PRODUCTIVE_1",
        "load_aerobic_low": 100.0, "load_aerobic_high": 50.0, "load_anaerobic": 20.0,
        "load_feedback": "BALANCED", "acute_load": 300, "chronic_load": 250, "acwr": 1.2,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_performance_summary(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["vo2max"] == pytest.approx(49.0)
    assert rows[0]["readiness_score"] is None  # not populated
    assert rows[0]["endurance_score"] is None  # not populated


def test_get_performance_summary_all_tables(db_conn, raw_payload_id):
    """All three tables populated for the same date — join works correctly."""
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_performance_summary
    repo.upsert_training_status(db_conn, {
        "date": "2025-06-01", "vo2max": 49.0, "vo2max_precise": 48.6,
        "training_status_code": 7, "training_status_phrase": "PRODUCTIVE_1",
        "load_aerobic_low": 100.0, "load_aerobic_high": 50.0, "load_anaerobic": 20.0,
        "load_feedback": "BALANCED", "acute_load": 300, "chronic_load": 250, "acwr": 1.2,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    repo.upsert_training_readiness(db_conn, {
        "date": "2025-06-01", "score": 78, "level": "HIGH",
        "recovery_time_min": 225, "acwr_factor_pct": 83, "acute_load": 306,
        "hrv_factor_pct": 100, "sleep_factor_pct": 55, "stress_factor_pct": 60,
        "feedback_short": "WELL_RECOVERED",
        "morning_readiness_score": 65, "morning_readiness_level": "MODERATE",
        "morning_recovery_time_min": 120,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    repo.upsert_endurance_score(db_conn, {
        "date": "2025-06-01", "score": 5671, "classification": 2,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_performance_summary(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["vo2max"] == pytest.approx(49.0)
    assert rows[0]["readiness_score"] == 78
    assert rows[0]["endurance_score"] == 5671


def test_get_training_status_trend_with_data(db_conn, raw_payload_id):
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_training_status_trend
    repo.upsert_training_status(db_conn, {
        "date": "2025-06-01", "vo2max": 49.0, "vo2max_precise": 48.6,
        "training_status_code": 7, "training_status_phrase": "PRODUCTIVE_1",
        "load_aerobic_low": 100.0, "load_aerobic_high": 50.0, "load_anaerobic": 20.0,
        "load_feedback": "BALANCED", "acute_load": 300, "chronic_load": 250, "acwr": 1.2,
        "raw_payload_id": raw_payload_id, "updated_at": "2025-06-01T00:00:00+00:00",
    })
    rows = get_training_status_trend(db_conn, from_date="2025-01-01", to_date="2026-12-31")
    assert len(rows) == 1
    assert rows[0]["training_status_phrase"] == "PRODUCTIVE_1"
    assert rows[0]["acwr"] == pytest.approx(1.2)


def test_upsert_lactate_threshold_updates_on_conflict(db_conn, raw_payload_id):
    """Upserting with same date but different HR should update the row."""
    from garmin_sync import repositories as repo
    row = {
        "date": "2025-01-10", "threshold_hr": 165, "threshold_speed_value": 0.33,
        "threshold_power_w": 420.0, "series": "running",
        "raw_payload_id": raw_payload_id, "updated_at": "2025-01-10T00:00:00+00:00",
    }
    repo.upsert_lactate_threshold(db_conn, row)
    row["threshold_hr"] = 170
    repo.upsert_lactate_threshold(db_conn, row)
    count = db_conn.execute("SELECT COUNT(*) FROM lactate_threshold").fetchone()[0]
    assert count == 1
    stored_hr = db_conn.execute("SELECT threshold_hr FROM lactate_threshold WHERE date='2025-01-10'").fetchone()[0]
    assert stored_hr == 170


def test_date_range_filtering(db_conn, raw_payload_id):
    """Rows outside the requested date range are not returned."""
    from garmin_sync import repositories as repo
    from garmin_sync.queries import get_race_predictions_trend
    # Insert two rows at different dates
    for d, v in [("2020-01-01", 1800), ("2025-06-01", 1500)]:
        repo.upsert_race_prediction(db_conn, {
            "date": d, "race_5k_s": v, "race_10k_s": v * 2,
            "race_half_s": v * 5, "race_full_s": v * 11,
            "raw_payload_id": raw_payload_id, "updated_at": f"{d}T00:00:00+00:00",
        })
    # Request only the 2025 range — 2020 row should be excluded
    rows = get_race_predictions_trend(db_conn, from_date="2025-01-01", to_date="2025-12-31")
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-06-01"
