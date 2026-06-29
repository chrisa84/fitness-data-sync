"""
Tests for intraday health time-series sync (Phase 7).

Covers:
  - normalise_intraday_heart_rate extracts heartRateValues pairs
  - normalise_intraday_stress handles negative stress levels (→ NULL)
  - normalise_intraday_steps extracts 15-min blocks
  - normalise_intraday_respiration extracts respirationValues pairs
  - replace_intraday_* functions are idempotent
  - IntradaySyncEngine.sync_intraday stores rows and skips unchanged dates
  - Single fetcher failure does not abort the whole date
"""

import hashlib
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from garmin_sync import normalise, repositories as repo
from garmin_sync.sync_engine import IntradaySyncEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _hash(payload) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _make_hr_raw(n: int = 3) -> dict:
    base_ms = 1710460200000  # 2024-03-15 00:10:00 UTC
    return {
        "calendarDate": "2024-03-15",
        "restingHeartRate": 55,
        "minHeartRate": 44,
        "maxHeartRate": 120,
        "heartRateValues": [
            [base_ms + i * 60000, 60 + i]
            for i in range(n)
        ],
    }


def _make_stress_raw(n: int = 3) -> dict:
    base_ms = 1710460200000
    return {
        "calendarDate": "2024-03-15",
        "stressValuesArray": [
            [base_ms + i * 240000, 30 + i if i != 1 else -1]  # second entry is -1 (rest)
            for i in range(n)
        ],
    }


def _make_steps_raw(n: int = 3) -> list:
    return [
        {
            "startGMT": f"2024-03-15 {6 + i}:00:00",
            "endGMT": f"2024-03-15 {6 + i}:15:00",
            "steps": 100 * (i + 1),
            "primaryActivityLevel": "active" if i % 2 == 0 else "sedentary",
        }
        for i in range(n)
    ]


def _make_respiration_raw(n: int = 3) -> dict:
    base_ms = 1710460200000
    return {
        "calendarDate": "2024-03-15",
        "respirationValues": [
            [base_ms + i * 60000, 14.5 + i * 0.5]
            for i in range(n)
        ],
    }


# ---------------------------------------------------------------------------
# normalise_intraday_heart_rate
# ---------------------------------------------------------------------------

def test_normalise_intraday_hr_extracts_values():
    raw = _make_hr_raw(3)
    rows = normalise.normalise_intraday_heart_rate(raw, "2024-03-15", raw_payload_id=1)
    assert len(rows) == 3
    assert rows[0]["heart_rate"] == 60
    assert rows[1]["heart_rate"] == 61
    assert rows[0]["date"] == "2024-03-15"
    assert rows[0]["timestamp_utc"] is not None
    assert rows[0]["raw_payload_id"] == 1


def test_normalise_intraday_hr_empty():
    rows = normalise.normalise_intraday_heart_rate({}, "2024-03-15", raw_payload_id=1)
    assert rows == []


def test_normalise_intraday_hr_null_bpm_kept():
    raw = {
        "heartRateValues": [[1710460200000, None], [1710460260000, 65]],
    }
    rows = normalise.normalise_intraday_heart_rate(raw, "2024-03-15", raw_payload_id=1)
    assert len(rows) == 2
    assert rows[0]["heart_rate"] is None
    assert rows[1]["heart_rate"] == 65


# ---------------------------------------------------------------------------
# normalise_intraday_stress
# ---------------------------------------------------------------------------

def test_normalise_intraday_stress_extracts_values():
    raw = _make_stress_raw(3)
    rows = normalise.normalise_intraday_stress(raw, "2024-03-15", raw_payload_id=1)
    assert len(rows) == 3
    assert rows[0]["stress_level"] == 30
    assert rows[1]["stress_level"] is None  # -1 → NULL
    assert rows[2]["stress_level"] == 32


def test_normalise_intraday_stress_empty():
    rows = normalise.normalise_intraday_stress({}, "2024-03-15", raw_payload_id=1)
    assert rows == []


# ---------------------------------------------------------------------------
# normalise_intraday_steps
# ---------------------------------------------------------------------------

def test_normalise_intraday_steps_extracts_blocks():
    raw = _make_steps_raw(3)
    rows = normalise.normalise_intraday_steps(raw, "2024-03-15", raw_payload_id=1)
    assert len(rows) == 3
    assert rows[0]["steps"] == 100
    assert rows[0]["activity_level"] == "active"
    assert rows[1]["activity_level"] == "sedentary"
    assert rows[0]["date"] == "2024-03-15"


def test_normalise_intraday_steps_not_a_list():
    rows = normalise.normalise_intraday_steps({}, "2024-03-15", raw_payload_id=1)
    assert rows == []


# ---------------------------------------------------------------------------
# normalise_intraday_respiration
# ---------------------------------------------------------------------------

def test_normalise_intraday_respiration_extracts_values():
    raw = _make_respiration_raw(3)
    rows = normalise.normalise_intraday_respiration(raw, "2024-03-15", raw_payload_id=1)
    assert len(rows) == 3
    assert rows[0]["breaths_per_min"] == pytest.approx(14.5)
    assert rows[2]["breaths_per_min"] == pytest.approx(15.5)


def test_normalise_intraday_respiration_empty():
    rows = normalise.normalise_intraday_respiration({}, "2024-03-15", raw_payload_id=1)
    assert rows == []


# ---------------------------------------------------------------------------
# replace_intraday_* (repository idempotency)
# ---------------------------------------------------------------------------

def test_replace_intraday_heart_rate_idempotent(db_conn: sqlite3.Connection):
    pj = _json({"x": 1})
    raw_id, _ = repo.upsert_raw_payload_by_date(
        db_conn, source="gc", data_type="intraday_hr",
        date="2024-03-15", payload_json=pj, payload_hash=_hash({"x": 1}),
    )
    rows = [
        {"date": "2024-03-15", "timestamp_utc": f"2024-03-15T00:{i:02d}:00+00:00",
         "heart_rate": 60 + i, "raw_payload_id": raw_id}
        for i in range(3)
    ]
    repo.replace_intraday_heart_rate(db_conn, "2024-03-15", rows)
    repo.replace_intraday_heart_rate(db_conn, "2024-03-15", rows)

    count = db_conn.execute(
        "SELECT COUNT(*) FROM intraday_heart_rate WHERE date='2024-03-15'"
    ).fetchone()[0]
    assert count == 3


def test_replace_intraday_stress_idempotent(db_conn: sqlite3.Connection):
    pj = _json({"x": 1})
    raw_id, _ = repo.upsert_raw_payload_by_date(
        db_conn, source="gc", data_type="intraday_stress",
        date="2024-03-15", payload_json=pj, payload_hash=_hash({"x": 1}),
    )
    rows = [
        {"date": "2024-03-15", "timestamp_utc": f"2024-03-15T00:{i:02d}:00+00:00",
         "stress_level": 30 + i, "raw_payload_id": raw_id}
        for i in range(2)
    ]
    repo.replace_intraday_stress(db_conn, "2024-03-15", rows)
    repo.replace_intraday_stress(db_conn, "2024-03-15", rows)

    count = db_conn.execute(
        "SELECT COUNT(*) FROM intraday_stress WHERE date='2024-03-15'"
    ).fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# IntradaySyncEngine integration
# ---------------------------------------------------------------------------

def _make_mock_client():
    client = MagicMock()
    client.get_heart_rates.return_value = _make_hr_raw(5)
    client.get_all_day_stress.return_value = _make_stress_raw(5)
    client.get_steps_data.return_value = _make_steps_raw(5)
    client.get_respiration_data.return_value = _make_respiration_raw(5)
    return client


def test_sync_intraday_stores_all_types(db_conn: sqlite3.Connection):
    client = _make_mock_client()
    engine = IntradaySyncEngine(MagicMock(), db_conn, client)
    result = engine.sync_intraday(days=1)

    assert result["stored"] > 0

    hr_count = db_conn.execute("SELECT COUNT(*) FROM intraday_heart_rate").fetchone()[0]
    stress_count = db_conn.execute("SELECT COUNT(*) FROM intraday_stress").fetchone()[0]
    steps_count = db_conn.execute("SELECT COUNT(*) FROM intraday_steps").fetchone()[0]
    resp_count = db_conn.execute("SELECT COUNT(*) FROM intraday_respiration").fetchone()[0]

    assert hr_count == 5
    assert stress_count > 0  # some may be NULL stress but still stored
    assert steps_count == 5
    assert resp_count == 5


def test_sync_intraday_skips_unchanged(db_conn: sqlite3.Connection):
    client = _make_mock_client()
    engine = IntradaySyncEngine(MagicMock(), db_conn, client)
    engine.sync_intraday(days=1)
    result = engine.sync_intraday(days=1)

    # Second run: payload unchanged, should be all skips
    assert result["skipped"] > 0
    assert result["updated"] == 0


def test_sync_intraday_continues_on_fetcher_failure(db_conn: sqlite3.Connection):
    client = MagicMock()
    client.get_heart_rates.side_effect = RuntimeError("network error")
    client.get_all_day_stress.return_value = _make_stress_raw(3)
    client.get_steps_data.return_value = _make_steps_raw(3)
    client.get_respiration_data.return_value = _make_respiration_raw(3)

    engine = IntradaySyncEngine(MagicMock(), db_conn, client)
    result = engine.sync_intraday(days=1)

    # HR failed but the rest stored
    steps_count = db_conn.execute("SELECT COUNT(*) FROM intraday_steps").fetchone()[0]
    assert steps_count == 3
    assert result["skipped"] >= 1
