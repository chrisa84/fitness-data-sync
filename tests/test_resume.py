"""
Tests for sync cursor tracking and resume behaviour.

Verifies:
  - Cursor is written after a page of activities.
  - A restarted backfill reads the cursor and starts at the correct offset.
  - Mid-page crash (cursor not updated) is safe to re-run (upserts are idempotent).
"""

import hashlib
import json

import pytest

from garmin_sync import repositories as repo


def _make_activity(activity_id: int) -> dict:
    return {"activityId": activity_id, "activityName": f"Run {activity_id}"}


def _json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _hash(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _store(db_conn, activity: dict) -> None:
    gid = str(activity["activityId"])
    repo.upsert_raw_payload_by_garmin_id(
        db_conn, source="gc", data_type="activity_summary",
        garmin_id=gid, payload_json=_json(activity), payload_hash=_hash(activity),
    )


class TestCursorTracking:
    def test_cursor_is_none_before_any_sync(self, db_conn):
        cursor = repo.get_cursor(db_conn, "activity_summary")
        assert cursor is None

    def test_update_cursor_creates_row(self, db_conn):
        repo.update_cursor(db_conn, "activity_summary", last_offset=100)
        cursor = repo.get_cursor(db_conn, "activity_summary")
        assert cursor is not None
        assert cursor["last_offset"] == 100

    def test_update_cursor_increments_offset(self, db_conn):
        repo.update_cursor(db_conn, "activity_summary", last_offset=100)
        repo.update_cursor(db_conn, "activity_summary", last_offset=200)
        cursor = repo.get_cursor(db_conn, "activity_summary")
        assert cursor["last_offset"] == 200

    def test_update_cursor_stores_activity_id(self, db_conn):
        repo.update_cursor(
            db_conn, "activity_summary",
            last_offset=50,
            last_successful_activity_id="98765",
        )
        cursor = repo.get_cursor(db_conn, "activity_summary")
        assert cursor["last_successful_activity_id"] == "98765"

    def test_cursor_per_data_type_are_independent(self, db_conn):
        repo.update_cursor(db_conn, "activity_summary", last_offset=10)
        repo.update_cursor(db_conn, "daily_summary", last_offset=20)
        assert repo.get_cursor(db_conn, "activity_summary")["last_offset"] == 10
        assert repo.get_cursor(db_conn, "daily_summary")["last_offset"] == 20


class TestResumeFromCursor:
    def test_resume_reads_cursor_offset(self, db_conn):
        """Simulate: backfill crashed at offset 100. Re-run should start at 100."""
        # Write cursor as if first 100 activities were already stored.
        repo.update_cursor(db_conn, "activity_summary", last_offset=100)
        cursor = repo.get_cursor(db_conn, "activity_summary")
        start_offset = cursor["last_offset"] if cursor else 0
        assert start_offset == 100

    def test_midpage_crash_is_safe(self, db_conn):
        """
        Simulate a crash mid-page: first two activities stored, cursor NOT updated.
        On re-run, re-store all three activities in the page. No duplicates.
        """
        page = [_make_activity(1001), _make_activity(1002), _make_activity(1003)]
        # First run: store first two, crash before cursor update.
        _store(db_conn, page[0])
        _store(db_conn, page[1])
        # No cursor update — simulate crash.

        # Second run: re-fetch same page and store all three.
        for act in page:
            _store(db_conn, act)

        count = db_conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0]
        assert count == 3  # No duplicates.

    def test_completed_page_cursor_not_lost_on_rerun(self, db_conn):
        """Cursor from a completed page should survive a second run."""
        repo.update_cursor(db_conn, "activity_summary", last_offset=50, last_successful_activity_id="5050")
        # Simulate second run reading the cursor.
        cursor = repo.get_cursor(db_conn, "activity_summary")
        assert cursor["last_offset"] == 50
        assert cursor["last_successful_activity_id"] == "5050"
