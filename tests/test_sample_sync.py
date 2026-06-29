"""
Tests for activity sample sync (time-series HR, pace, GPS, cadence, etc.).

Verifies:
  - normalise_activity_samples extracts metrics correctly via descriptor map
  - GPS falls back to polyline when lat/lon not in metrics
  - empty payload returns empty list
  - replace_activity_samples is idempotent
  - get_activities_needing_samples skips activities that already have samples
  - failed fetch for one activity does not stop the whole sync
"""

import hashlib
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from garmin_sync import normalise, repositories as repo
from garmin_sync.sync_engine import SampleSyncEngine, _process_one_samples, _Stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _hash(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _insert_activity(db_conn: sqlite3.Connection, activity_id: str) -> None:
    pj = _json({"activityId": activity_id})
    ph = _hash({"activityId": activity_id})
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_summary",
        garmin_id=activity_id, payload_json=pj, payload_hash=ph,
    )
    db_conn.execute(
        """
        INSERT INTO activity (activity_id, raw_payload_id, updated_at)
        VALUES (?, ?, '2024-01-01T00:00:00')
        ON CONFLICT(activity_id) DO NOTHING
        """,
        (activity_id, raw_id),
    )
    db_conn.commit()


def _make_raw_samples(activity_id: str, n: int = 3) -> dict:
    """Minimal get_activity_details() response with HR and speed metrics."""
    return {
        "activityId": int(activity_id),
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "directTimestamp"},
            {"metricsIndex": 1, "key": "directHeartRate"},
            {"metricsIndex": 2, "key": "directSpeed"},
            {"metricsIndex": 3, "key": "directCadence"},
            {"metricsIndex": 4, "key": "directDistance"},
        ],
        "activityDetailMetrics": [
            {
                "startGMT": f"2024-03-15 06:30:{i:02d}",
                "metrics": [
                    1710484200000 + i * 1000,  # ts_ms
                    140 + i,                    # heart_rate
                    3.0 + i * 0.1,             # speed_mps
                    170 + i,                    # cadence
                    i * 10.0,                  # distance_m
                ],
            }
            for i in range(n)
        ],
    }


def _make_raw_samples_with_gps(activity_id: str, n: int = 3) -> dict:
    """Response with lat/lon in metricDescriptors."""
    base = _make_raw_samples(activity_id, n)
    base["metricDescriptors"].extend([
        {"metricsIndex": 5, "key": "directLatitude"},
        {"metricsIndex": 6, "key": "directLongitude"},
    ])
    for i, entry in enumerate(base["activityDetailMetrics"]):
        entry["metrics"].extend([51.5 + i * 0.001, -0.12 + i * 0.001])
    return base


def _make_raw_samples_polyline_gps(activity_id: str, n: int = 3) -> dict:
    """Response without lat/lon in metrics but with geoPolylineDTO."""
    base = _make_raw_samples(activity_id, n)
    base["geoPolylineDTO"] = {
        "polyline": [
            {"lat": 51.5 + i * 0.001, "lon": -0.12 + i * 0.001, "altitude": 10.0 + i}
            for i in range(n)
        ]
    }
    return base


# ---------------------------------------------------------------------------
# normalise_activity_samples
# ---------------------------------------------------------------------------

def test_normalise_extracts_metrics():
    raw = _make_raw_samples("111", n=3)
    rows = normalise.normalise_activity_samples(raw, "111", raw_payload_id=1)
    assert len(rows) == 3
    assert rows[0]["heart_rate"] == 140
    assert rows[1]["speed_mps"] == pytest.approx(3.1, abs=0.01)
    assert rows[2]["cadence"] == 172
    assert rows[0]["distance_m"] == pytest.approx(0.0)
    assert rows[1]["distance_m"] == pytest.approx(10.0)


def test_normalise_gps_from_metrics():
    raw = _make_raw_samples_with_gps("222", n=2)
    rows = normalise.normalise_activity_samples(raw, "222", raw_payload_id=1)
    assert rows[0]["lat"] == pytest.approx(51.5)
    assert rows[0]["lon"] == pytest.approx(-0.12)
    assert rows[1]["lat"] == pytest.approx(51.501)


def test_normalise_gps_from_polyline_fallback():
    raw = _make_raw_samples_polyline_gps("333", n=3)
    rows = normalise.normalise_activity_samples(raw, "333", raw_payload_id=1)
    assert rows[0]["lat"] == pytest.approx(51.5)
    assert rows[0]["lon"] == pytest.approx(-0.12)
    assert rows[0]["altitude_m"] == pytest.approx(10.0)


def test_normalise_polyline_not_used_when_length_differs():
    raw = _make_raw_samples("444", n=3)
    raw["geoPolylineDTO"] = {
        "polyline": [{"lat": 99.0, "lon": 99.0}]  # different length
    }
    rows = normalise.normalise_activity_samples(raw, "444", raw_payload_id=1)
    assert rows[0]["lat"] is None
    assert rows[0]["lon"] is None


def test_normalise_empty_payload():
    rows = normalise.normalise_activity_samples({}, "555", raw_payload_id=1)
    assert rows == []


def test_normalise_missing_metric_values_are_none():
    raw = {
        "activityId": 666,
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "directHeartRate"},
            {"metricsIndex": 1, "key": "directSpeed"},
        ],
        "activityDetailMetrics": [
            {"startGMT": "2024-01-01 10:00:00", "metrics": [None, None]},
        ],
    }
    rows = normalise.normalise_activity_samples(raw, "666", raw_payload_id=1)
    assert rows[0]["heart_rate"] is None
    assert rows[0]["speed_mps"] is None


# ---------------------------------------------------------------------------
# replace_activity_samples (repository)
# ---------------------------------------------------------------------------

def test_replace_activity_samples_idempotent(db_conn: sqlite3.Connection):
    _insert_activity(db_conn, "777")
    pj = _json({"x": 1})
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_samples",
        garmin_id="777", payload_json=pj, payload_hash=_hash({"x": 1}),
    )
    samples = [
        {"activity_id": "777", "sample_index": i, "timestamp_utc": None,
         "distance_m": None, "heart_rate": 150, "speed_mps": 3.0,
         "cadence": None, "power_w": None, "altitude_m": None,
         "lat": None, "lon": None, "respiration_rate": None,
         "ground_contact_ms": None, "ground_contact_balance_left": None,
         "vertical_oscillation_cm": None,
         "vertical_ratio_pct": None, "stride_length_cm": None,
         "raw_payload_id": raw_id}
        for i in range(3)
    ]

    repo.replace_activity_samples(db_conn, "777", samples)
    repo.replace_activity_samples(db_conn, "777", samples)

    count = db_conn.execute(
        "SELECT COUNT(*) FROM activity_sample WHERE activity_id='777'"
    ).fetchone()[0]
    assert count == 3


# ---------------------------------------------------------------------------
# get_activities_needing_samples
# ---------------------------------------------------------------------------

def test_activities_needing_samples_excludes_already_synced(db_conn: sqlite3.Connection):
    _insert_activity(db_conn, "aaa")
    _insert_activity(db_conn, "bbb")

    pj = _json({"x": 1})
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_samples",
        garmin_id="aaa", payload_json=pj, payload_hash=_hash({"x": 1}),
    )
    db_conn.execute(
        "INSERT INTO activity_sample (activity_id, sample_index, raw_payload_id) VALUES ('aaa', 0, ?)",
        (raw_id,),
    )
    db_conn.commit()

    needing = repo.get_activities_needing_samples(db_conn)
    assert "bbb" in needing
    assert "aaa" not in needing


def test_activities_needing_samples_refresh_existing(db_conn: sqlite3.Connection):
    _insert_activity(db_conn, "ccc")
    pj = _json({"x": 1})
    raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_samples",
        garmin_id="ccc", payload_json=pj, payload_hash=_hash({"x": 1}),
    )
    db_conn.execute(
        "INSERT INTO activity_sample (activity_id, sample_index, raw_payload_id) VALUES ('ccc', 0, ?)",
        (raw_id,),
    )
    db_conn.commit()

    needing = repo.get_activities_needing_samples(db_conn, refresh_existing=True)
    assert "ccc" in needing


# ---------------------------------------------------------------------------
# SampleSyncEngine integration
# ---------------------------------------------------------------------------

def test_sync_engine_stores_samples(db_conn: sqlite3.Connection):
    _insert_activity(db_conn, "999")
    raw = _make_raw_samples("999", n=5)

    mock_client = MagicMock()
    mock_client.get_activity_samples.return_value = raw

    engine = SampleSyncEngine(MagicMock(), db_conn, mock_client)
    result = engine.sync_recent_samples(limit=10)

    assert result["fetched"] == 1
    assert result["stored"] == 1

    count = db_conn.execute(
        "SELECT COUNT(*) FROM activity_sample WHERE activity_id='999'"
    ).fetchone()[0]
    assert count == 5


def test_sync_engine_skips_unchanged(db_conn: sqlite3.Connection):
    _insert_activity(db_conn, "888")
    raw = _make_raw_samples("888", n=2)

    mock_client = MagicMock()
    mock_client.get_activity_samples.return_value = raw

    engine = SampleSyncEngine(MagicMock(), db_conn, mock_client)
    engine.sync_recent_samples(limit=10)
    result = engine.sync_recent_samples(limit=10, refresh_existing=True)

    assert result["fetched"] == 1
    assert result["skipped"] == 1  # hash unchanged


def test_sync_engine_continues_on_single_failure(db_conn: sqlite3.Connection):
    _insert_activity(db_conn, "1001")
    _insert_activity(db_conn, "1002")

    mock_client = MagicMock()
    mock_client.get_activity_samples.side_effect = [
        RuntimeError("network error"),
        _make_raw_samples("1002", n=1),
    ]

    engine = SampleSyncEngine(MagicMock(), db_conn, mock_client)
    result = engine.sync_recent_samples(limit=10)

    assert result["skipped"] >= 1
    count = db_conn.execute(
        "SELECT COUNT(*) FROM activity_sample WHERE activity_id='1002'"
    ).fetchone()[0]
    assert count == 1
