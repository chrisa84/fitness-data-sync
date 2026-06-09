"""
Tests for idempotent upserts.

Verifies:
  - Inserting the same raw_payload twice produces one row.
  - Inserting a raw_payload with a changed hash updates the row.
  - Inserting the same activity twice produces one row.
  - Changing a Garmin payload and re-syncing updates raw_payload and activity.
"""

import hashlib
import json
import sqlite3

import pytest

from garmin_sync import repositories as repo
from garmin_sync.normalise import normalise_activity


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


class TestRawPayloadUpsert:
    def test_insert_same_payload_twice_creates_one_row(self, db_conn, sample_activity):
        payload = sample_activity
        garmin_id = str(payload["activityId"])
        pj = _json(payload)
        ph = _hash(payload)

        repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj, payload_hash=ph,
        )
        repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj, payload_hash=ph,
        )

        count = db_conn.execute(
            "SELECT COUNT(*) FROM raw_payload WHERE garmin_id=?", (garmin_id,)
        ).fetchone()[0]
        assert count == 1

    def test_changed_payload_updates_hash(self, db_conn, sample_activity):
        garmin_id = str(sample_activity["activityId"])
        pj1 = _json(sample_activity)
        ph1 = _hash(sample_activity)

        raw_id1, changed1 = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj1, payload_hash=ph1,
        )
        assert changed1 is True

        # Garmin corrects a field.
        modified = {**sample_activity, "averageHR": 155}
        pj2 = _json(modified)
        ph2 = _hash(modified)
        assert ph2 != ph1

        raw_id2, changed2 = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj2, payload_hash=ph2,
        )
        assert changed2 is True
        assert raw_id1 == raw_id2  # Same row, updated in place.

        stored_hash = db_conn.execute(
            "SELECT payload_hash FROM raw_payload WHERE garmin_id=?", (garmin_id,)
        ).fetchone()["payload_hash"]
        assert stored_hash == ph2

    def test_unchanged_payload_returns_not_changed(self, db_conn, sample_activity):
        garmin_id = str(sample_activity["activityId"])
        pj = _json(sample_activity)
        ph = _hash(sample_activity)

        repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj, payload_hash=ph,
        )
        _, changed = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj, payload_hash=ph,
        )
        assert changed is False

    def test_two_different_activities_create_two_rows(self, db_conn, sample_activity, sample_activity_sparse):
        for act in (sample_activity, sample_activity_sparse):
            gid = str(act["activityId"])
            pj = _json(act)
            ph = _hash(act)
            repo.upsert_raw_payload_by_garmin_id(
                db_conn, source="gc", data_type="activity_summary",
                garmin_id=gid, payload_json=pj, payload_hash=ph,
            )
        count = db_conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0]
        assert count == 2


class TestActivityUpsert:
    def _insert_activity(self, db_conn, activity: dict) -> int:
        garmin_id = str(activity["activityId"])
        pj = _json(activity)
        ph = _hash(activity)
        raw_id, _ = repo.upsert_raw_payload_by_garmin_id(
            db_conn, source="gc", data_type="activity_summary",
            garmin_id=garmin_id, payload_json=pj, payload_hash=ph,
        )
        normalised = normalise_activity(activity)
        assert normalised is not None
        normalised["raw_payload_id"] = raw_id
        repo.upsert_activity(db_conn, normalised)  # type: ignore[arg-type]
        return raw_id

    def test_upsert_same_activity_twice_creates_one_row(self, db_conn, sample_activity):
        self._insert_activity(db_conn, sample_activity)
        self._insert_activity(db_conn, sample_activity)
        count = db_conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        assert count == 1

    def test_changed_payload_updates_activity_row(self, db_conn, sample_activity):
        self._insert_activity(db_conn, sample_activity)

        modified = {**sample_activity, "averageHR": 160}
        self._insert_activity(db_conn, modified)

        count = db_conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        assert count == 1

        avg_hr = db_conn.execute(
            "SELECT avg_hr FROM activity WHERE activity_id=?",
            (str(sample_activity["activityId"]),),
        ).fetchone()["avg_hr"]
        assert avg_hr == 160

    def test_two_activities_create_two_rows(self, db_conn, sample_activity, sample_activity_sparse):
        self._insert_activity(db_conn, sample_activity)
        self._insert_activity(db_conn, sample_activity_sparse)
        count = db_conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        assert count == 2
