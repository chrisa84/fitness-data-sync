"""
Tests for activity detail sync (phase 2).

Verifies:
  - raw activity_detail upsert idempotency
  - changed detail payload updates hash
  - lap upsert idempotency
  - missing laps does not crash
  - failed detail fetch for one activity does not stop the whole sync
  - detail cursor updates on each activity
  - status includes detail and lap counts
"""

import hashlib
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from garmin_sync import normalise, repositories as repo
from garmin_sync.sync_engine import DetailSyncEngine, _process_one_detail, _Stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _hash(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _insert_summary(db_conn, activity_id: str) -> None:
    """Insert a minimal activity summary row so detail queries have something to join."""
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


def _make_detail(activity_id: str, avg_hr: int = 150, num_laps: int = 2) -> dict:
    return {
        "activityId": int(activity_id),
        "activityName": f"Run {activity_id}",
        "laps": [
            {
                "lapIndex": i,
                "startTimeGMT": f"2024-03-15 0{6+i}:30:00",
                "distance": 1000.0 * (i + 1),
                "duration": 300.0,
                "movingDuration": 295.0,
                "averageHR": avg_hr,
                "maxHR": avg_hr + 20,
                "averageRunCadence": 170.0,
                "gainElevation": 5.0,
                "lossElevation": 3.0,
            }
            for i in range(num_laps)
        ],
    }


# ---------------------------------------------------------------------------
# Raw payload idempotency
# ---------------------------------------------------------------------------

class TestDetailRawPayloadIdempotency:
    def test_insert_same_detail_twice_creates_one_row(self, db_conn):
        detail = _make_detail("111")
        pj = _json(detail)
        ph = _hash(detail)

        repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="111", payload_json=pj, payload_hash=ph,
        )
        repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="111", payload_json=pj, payload_hash=ph,
        )

        count = db_conn.execute(
            "SELECT COUNT(*) FROM raw_payload WHERE data_type='activity_detail' AND garmin_id='111'"
        ).fetchone()[0]
        assert count == 1

    def test_changed_detail_updates_hash(self, db_conn):
        detail = _make_detail("222")
        pj1 = _json(detail)
        ph1 = _hash(detail)
        repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="222", payload_json=pj1, payload_hash=ph1,
        )

        modified = {**detail, "activityName": "Updated"}
        pj2 = _json(modified)
        ph2 = _hash(modified)
        _, changed = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="222", payload_json=pj2, payload_hash=ph2,
        )
        assert changed is True

        stored = db_conn.execute(
            "SELECT payload_hash FROM raw_payload WHERE data_type='activity_detail' AND garmin_id='222'"
        ).fetchone()["payload_hash"]
        assert stored == ph2

    def test_summary_and_detail_are_separate_rows(self, db_conn):
        """Same garmin_id but different data_type → two distinct raw_payload rows."""
        d = {"activityId": 333}
        for dt in ("activity_summary", "activity_detail"):
            pj = _json(d)
            ph = _hash(d)
            repo.upsert_raw_payload_by_garmin_id(
                db_conn, source="gc", data_type=dt,
                garmin_id="333", payload_json=pj, payload_hash=ph,
            )
        count = db_conn.execute("SELECT COUNT(*) FROM raw_payload WHERE garmin_id='333'").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# Lap idempotency
# ---------------------------------------------------------------------------

class TestLapIdempotency:
    def _store_laps(self, db_conn, activity_id: str, detail: dict) -> None:
        pj = _json(detail)
        ph = _hash(detail)
        raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id=activity_id, payload_json=pj, payload_hash=ph,
        )
        laps = normalise.normalise_activity_laps(detail, activity_id, raw_id)
        repo.replace_activity_laps(db_conn, activity_id, laps)

    def test_replace_laps_is_idempotent(self, db_conn):
        detail = _make_detail("444", num_laps=3)
        self._store_laps(db_conn, "444", detail)
        self._store_laps(db_conn, "444", detail)
        count = db_conn.execute(
            "SELECT COUNT(*) FROM activity_lap WHERE activity_id='444'"
        ).fetchone()[0]
        assert count == 3

    def test_changed_payload_replaces_laps(self, db_conn):
        detail_v1 = _make_detail("555", num_laps=2)
        self._store_laps(db_conn, "555", detail_v1)

        detail_v2 = _make_detail("555", num_laps=4)
        self._store_laps(db_conn, "555", detail_v2)

        count = db_conn.execute(
            "SELECT COUNT(*) FROM activity_lap WHERE activity_id='555'"
        ).fetchone()[0]
        assert count == 4

    def test_no_laps_does_not_crash(self, db_conn):
        detail = {"activityId": 666, "activityName": "No Laps"}
        pj = _json(detail)
        ph = _hash(detail)
        raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="666", payload_json=pj, payload_hash=ph,
        )
        laps = normalise.normalise_activity_laps(detail, "666", raw_id)
        assert laps == []
        repo.replace_activity_laps(db_conn, "666", laps)
        count = db_conn.execute(
            "SELECT COUNT(*) FROM activity_lap WHERE activity_id='666'"
        ).fetchone()[0]
        assert count == 0

    def test_laps_with_missing_optional_fields(self, db_conn):
        detail = {
            "activityId": 777,
            "laps": [{"lapIndex": 0}],  # no distance, HR, etc.
        }
        pj = _json(detail)
        ph = _hash(detail)
        raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="777", payload_json=pj, payload_hash=ph,
        )
        laps = normalise.normalise_activity_laps(detail, "777", raw_id)
        assert len(laps) == 1
        assert laps[0]["distance_m"] is None
        assert laps[0]["avg_hr"] is None
        repo.replace_activity_laps(db_conn, "777", laps)


# ---------------------------------------------------------------------------
# Activities needing detail
# ---------------------------------------------------------------------------

class TestActivitiesNeedingDetail:
    def test_returns_activities_without_detail(self, db_conn):
        _insert_summary(db_conn, "10")
        _insert_summary(db_conn, "20")
        ids = repo.get_activities_needing_detail(db_conn)
        assert set(ids) == {"10", "20"}

    def test_excludes_activities_with_detail(self, db_conn):
        _insert_summary(db_conn, "10")
        _insert_summary(db_conn, "20")

        # Give activity "10" a detail row.
        pj = _json({"activityId": 10})
        ph = _hash({"activityId": 10})
        raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_detail",
            garmin_id="10", payload_json=pj, payload_hash=ph,
        )
        repo.upsert_activity_detail(db_conn, {
            "activity_id": "10", "raw_payload_id": raw_id,
            "has_splits": 0, "has_laps": 0, "sample_count": None,
            "updated_at": "2024-01-01T00:00:00",
        })

        ids = repo.get_activities_needing_detail(db_conn)
        assert ids == ["20"]

    def test_refresh_existing_returns_all(self, db_conn):
        _insert_summary(db_conn, "10")
        _insert_summary(db_conn, "20")
        ids = repo.get_activities_needing_detail(db_conn, refresh_existing=True)
        assert set(ids) == {"10", "20"}

    def test_limit_is_respected(self, db_conn):
        for i in range(5):
            _insert_summary(db_conn, str(i))
        ids = repo.get_activities_needing_detail(db_conn, limit=3)
        assert len(ids) == 3


# ---------------------------------------------------------------------------
# Detail sync engine behaviour
# ---------------------------------------------------------------------------

class TestDetailSyncEngine:
    def _make_engine(self, db_conn, client_mock, dry_run=False):
        from garmin_sync.config import Config
        config = Config(
            garmin_email="test@test.com",
            garmin_password="pw",
            garmin_request_delay_seconds=0,
        )
        return DetailSyncEngine(config, db_conn, client_mock, dry_run=dry_run)

    def test_failed_fetch_for_one_activity_continues(self, db_conn):
        _insert_summary(db_conn, "10")
        _insert_summary(db_conn, "20")
        _insert_summary(db_conn, "30")

        client = MagicMock()
        # Activity "20" raises a network error; others succeed.
        def side_effect(activity_id):
            if activity_id == "20":
                raise ConnectionError("timeout")
            return _make_detail(activity_id)

        client.get_activity_detail.side_effect = side_effect

        engine = self._make_engine(db_conn, client)
        result = engine.sync_recent_details(limit=3)

        # 2 fetched successfully, 1 skipped (error).
        assert result["fetched"] == 2
        assert result["skipped"] == 1

    def test_rate_limit_stops_sync(self, db_conn):
        _insert_summary(db_conn, "10")
        _insert_summary(db_conn, "20")

        from garmin_sync.rate_limit import RateLimitExceeded
        client = MagicMock()
        client.get_activity_detail.side_effect = RateLimitExceeded("429")

        engine = self._make_engine(db_conn, client)
        with pytest.raises(RateLimitExceeded):
            engine.sync_recent_details(limit=2)

    def test_dry_run_does_not_write_to_db(self, db_conn):
        _insert_summary(db_conn, "10")

        client = MagicMock()
        client.get_activity_detail.return_value = _make_detail("10")

        engine = self._make_engine(db_conn, client, dry_run=True)
        engine.sync_recent_details(limit=1)

        count = db_conn.execute("SELECT COUNT(*) FROM raw_payload WHERE data_type='activity_detail'").fetchone()[0]
        assert count == 0

    def test_rerun_does_not_duplicate_detail_rows(self, db_conn):
        _insert_summary(db_conn, "10")

        client = MagicMock()
        client.get_activity_detail.return_value = _make_detail("10")

        engine = self._make_engine(db_conn, client)
        engine.sync_recent_details(limit=1, refresh_existing=True)
        engine.sync_recent_details(limit=1, refresh_existing=True)

        count = db_conn.execute("SELECT COUNT(*) FROM activity_detail").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Cursor tracking
# ---------------------------------------------------------------------------

class TestDetailCursor:
    def test_cursor_updated_after_each_activity(self, db_conn):
        _insert_summary(db_conn, "10")
        _insert_summary(db_conn, "20")

        client = MagicMock()
        client.get_activity_detail.return_value = _make_detail("10")

        from garmin_sync.config import Config
        config = Config(garmin_email="t@t.com", garmin_password="pw", garmin_request_delay_seconds=0)
        engine = DetailSyncEngine(config, db_conn, client)
        engine.sync_recent_details(limit=1)

        cursor = repo.get_cursor(db_conn, "activity_detail")
        assert cursor is not None
        assert cursor["last_successful_activity_id"] is not None


# ---------------------------------------------------------------------------
# Status includes detail counts
# ---------------------------------------------------------------------------

class TestStatusIncludesDetailCounts:
    def test_status_has_detail_and_lap_counts(self, db_conn):
        status = repo.get_sync_status(db_conn)
        assert "activity_detail_count" in status
        assert "activity_lap_count" in status
        assert status["activity_detail_count"] == 0
        assert status["activity_lap_count"] == 0
