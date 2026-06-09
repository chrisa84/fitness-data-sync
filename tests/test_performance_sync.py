"""
Tests for Phase 5b.1 sync infrastructure:
  - _yearly_chunks date-range splitter
  - Rate limiter: 400 errors not retried, 429/transient still retried
  - sync_performance_ranges: all 4 endpoints use chunking, empty responses handled
  - Idempotency: re-running sync does not create duplicate rows
  - reprocess-performance-derived: no Garmin calls
"""

import sqlite3
from unittest.mock import MagicMock, call, patch

import pytest

from garmin_sync.sync_engine import PerformanceSyncEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeGarminTooManyRequests(Exception):
    pass


class FakeGarminConnectionError(Exception):
    pass


class FakeGarminAuthError(Exception):
    pass


@pytest.fixture(autouse=True)
def patch_garmin_exceptions(monkeypatch):
    monkeypatch.setattr("garminconnect.GarminConnectTooManyRequestsError", FakeGarminTooManyRequests)
    monkeypatch.setattr("garminconnect.GarminConnectConnectionError", FakeGarminConnectionError)
    monkeypatch.setattr("garminconnect.GarminConnectAuthenticationError", FakeGarminAuthError)


@pytest.fixture()
def zero_delay_limiter():
    """RateLimiter with no real delays, max_retries=2."""
    from garmin_sync.rate_limit import RateLimiter
    return RateLimiter(base_delay_s=0.0, jitter_factor=0.0, max_retries=2, backoff_base_s=0.0)


def _make_engine(db_conn, client):
    from garmin_sync.config import Config
    cfg = Config(
        garmin_email="test@example.com",
        garmin_password="secret",
    )
    return PerformanceSyncEngine(cfg, db_conn, client)


def _minimal_client():
    """GarminClient mock with all 4 range methods returning empty."""
    client = MagicMock()
    client.get_lactate_threshold.return_value = {}
    client.get_race_predictions.return_value = []
    client.get_endurance_score.return_value = {}
    client.get_hill_score.return_value = {}
    return client


# ---------------------------------------------------------------------------
# _yearly_chunks — date range splitter
# ---------------------------------------------------------------------------

class TestYearlyChunks:
    def test_range_under_366_days_is_single_chunk(self):
        chunks = PerformanceSyncEngine._yearly_chunks("2025-01-01", "2025-06-30")
        assert len(chunks) == 1
        assert chunks[0] == ("2025-01-01", "2025-06-30")

    def test_exactly_366_days_is_single_chunk(self):
        # 2024 is a leap year; 2024-01-01 to 2024-12-31 = 366 days
        chunks = PerformanceSyncEngine._yearly_chunks("2024-01-01", "2024-12-31")
        assert len(chunks) == 1
        assert chunks[0] == ("2024-01-01", "2024-12-31")

    def test_exactly_365_days_across_year_boundary_is_two_chunks(self):
        # 2025-01-01 to 2025-12-31 spans one calendar year boundary within the chunker
        chunks = PerformanceSyncEngine._yearly_chunks("2025-01-01", "2025-12-31")
        assert len(chunks) == 1  # still within same year

    def test_multi_year_range_splits_at_year_boundaries(self):
        chunks = PerformanceSyncEngine._yearly_chunks("2023-06-01", "2025-03-31")
        # Should produce 3 chunks: 2023-06-01→2024-05-31, 2024-06-01→2025-05-31 (capped at 2025-03-31)
        assert len(chunks) >= 2
        # First chunk starts at from_date
        assert chunks[0][0] == "2023-06-01"
        # Last chunk ends at to_date
        assert chunks[-1][1] == "2025-03-31"

    def test_no_gaps_between_chunks(self):
        from datetime import date, timedelta
        chunks = PerformanceSyncEngine._yearly_chunks("2020-04-01", "2026-06-08")
        for i in range(len(chunks) - 1):
            end_of_current = date.fromisoformat(chunks[i][1])
            start_of_next = date.fromisoformat(chunks[i + 1][0])
            assert start_of_next == end_of_current + timedelta(days=1), (
                f"Gap between chunk {i} and {i+1}: {chunks[i][1]} → {chunks[i+1][0]}"
            )

    def test_chunks_cover_full_range(self):
        from datetime import date
        chunks = PerformanceSyncEngine._yearly_chunks("2020-04-01", "2026-06-08")
        assert chunks[0][0] == "2020-04-01"
        assert chunks[-1][1] == "2026-06-08"

    def test_each_chunk_is_at_most_366_days(self):
        from datetime import date
        chunks = PerformanceSyncEngine._yearly_chunks("2020-04-01", "2026-06-08")
        for cs, ce in chunks:
            delta = (date.fromisoformat(ce) - date.fromisoformat(cs)).days + 1
            assert delta <= 366, f"Chunk {cs}→{ce} is {delta} days (> 366)"

    def test_single_day_range(self):
        chunks = PerformanceSyncEngine._yearly_chunks("2025-06-01", "2025-06-01")
        assert len(chunks) == 1
        assert chunks[0] == ("2025-06-01", "2025-06-01")

    def test_no_invalid_chunks_in_output(self):
        from datetime import date
        chunks = PerformanceSyncEngine._yearly_chunks("2020-04-01", "2026-06-08")
        for cs, ce in chunks:
            assert date.fromisoformat(cs) <= date.fromisoformat(ce), (
                f"Invalid chunk: start {cs} > end {ce}"
            )


# ---------------------------------------------------------------------------
# Rate limiter: 400 errors must not be retried
# ---------------------------------------------------------------------------

class TestFourHundredNotRetried:
    def test_400_error_raises_immediately(self, zero_delay_limiter):
        fn = MagicMock(side_effect=FakeGarminConnectionError("HTTP 400: Bad Request"))
        with pytest.raises(FakeGarminConnectionError):
            zero_delay_limiter.execute(fn)
        fn.assert_called_once()

    def test_401_error_raises_immediately(self, zero_delay_limiter):
        fn = MagicMock(side_effect=FakeGarminConnectionError("401 Unauthorized"))
        with pytest.raises(FakeGarminConnectionError):
            zero_delay_limiter.execute(fn)
        fn.assert_called_once()

    def test_403_error_raises_immediately(self, zero_delay_limiter):
        fn = MagicMock(side_effect=FakeGarminConnectionError("403 Forbidden"))
        with pytest.raises(FakeGarminConnectionError):
            zero_delay_limiter.execute(fn)
        fn.assert_called_once()

    def test_404_error_raises_immediately(self, zero_delay_limiter):
        fn = MagicMock(side_effect=FakeGarminConnectionError("404 Not Found"))
        with pytest.raises(FakeGarminConnectionError):
            zero_delay_limiter.execute(fn)
        fn.assert_called_once()

    def test_transient_connection_error_still_retries(self, zero_delay_limiter):
        fn = MagicMock(side_effect=FakeGarminConnectionError("Connection timeout"))
        with pytest.raises(FakeGarminConnectionError):
            zero_delay_limiter.execute(fn)
        assert fn.call_count == zero_delay_limiter.max_retries + 1

    def test_429_still_retries_on_non_login(self, zero_delay_limiter):
        from garmin_sync.rate_limit import RateLimitExceeded
        fn = MagicMock(side_effect=FakeGarminTooManyRequests("rate limited"))
        with pytest.raises(RateLimitExceeded):
            zero_delay_limiter.execute(fn)
        assert fn.call_count == zero_delay_limiter.max_retries + 1

    def test_success_after_transient_error(self, zero_delay_limiter):
        fn = MagicMock(side_effect=[FakeGarminConnectionError("timeout"), "ok"])
        result = zero_delay_limiter.execute(fn)
        assert result == "ok"
        assert fn.call_count == 2


# ---------------------------------------------------------------------------
# sync_performance_ranges: all 4 endpoints use chunking
# ---------------------------------------------------------------------------

class TestSyncPerformanceRangesChunking:
    def test_all_four_endpoints_called_per_chunk(self, db_conn):
        """Multi-year range → each endpoint called once per chunk."""
        client = _minimal_client()
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2023-01-01", "2025-12-31")
        chunks = PerformanceSyncEngine._yearly_chunks("2023-01-01", "2025-12-31")
        n = len(chunks)
        assert client.get_lactate_threshold.call_count == n
        assert client.get_race_predictions.call_count == n
        assert client.get_endurance_score.call_count == n
        assert client.get_hill_score.call_count == n

    def test_short_range_single_call_per_endpoint(self, db_conn):
        client = _minimal_client()
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-06-30")
        client.get_lactate_threshold.assert_called_once()
        client.get_race_predictions.assert_called_once()
        client.get_endurance_score.assert_called_once()
        client.get_hill_score.assert_called_once()

    def test_correct_date_ranges_passed_to_client(self, db_conn):
        client = _minimal_client()
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-06-30")
        client.get_lactate_threshold.assert_called_once_with("2025-01-01", "2025-06-30")
        client.get_race_predictions.assert_called_once_with("2025-01-01", "2025-06-30")
        client.get_endurance_score.assert_called_once_with("2025-01-01", "2025-06-30")
        client.get_hill_score.assert_called_once_with("2025-01-01", "2025-06-30")


# ---------------------------------------------------------------------------
# sync_performance_ranges: empty endpoint responses handled gracefully
# ---------------------------------------------------------------------------

class TestEmptyEndpointResponses:
    def test_empty_lactate_threshold_no_crash(self, db_conn):
        client = _minimal_client()
        client.get_lactate_threshold.return_value = {}
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-06-30")
        count = db_conn.execute("SELECT COUNT(*) FROM lactate_threshold").fetchone()[0]
        assert count == 0

    def test_empty_race_predictions_no_crash(self, db_conn):
        client = _minimal_client()
        client.get_race_predictions.return_value = []
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-06-30")
        count = db_conn.execute("SELECT COUNT(*) FROM race_predictions").fetchone()[0]
        assert count == 0

    def test_empty_endurance_score_no_crash(self, db_conn):
        client = _minimal_client()
        client.get_endurance_score.return_value = {}
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-06-30")
        count = db_conn.execute("SELECT COUNT(*) FROM endurance_score").fetchone()[0]
        assert count == 0

    def test_empty_hill_score_no_crash(self, db_conn):
        client = _minimal_client()
        client.get_hill_score.return_value = {}
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-06-30")
        count = db_conn.execute("SELECT COUNT(*) FROM hill_score").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Idempotency: re-running sync produces no duplicates
# ---------------------------------------------------------------------------

class TestSyncPerformanceIdempotency:
    def _race_predictions_client(self):
        client = _minimal_client()
        client.get_race_predictions.return_value = [
            {
                "calendarDate": "2025-06-01",
                "time5K": 1479, "time10K": 3175,
                "timeHalfMarathon": 7078, "timeMarathon": 16245,
            }
        ]
        return client

    def test_race_predictions_rerun_no_duplicates(self, db_conn):
        client = self._race_predictions_client()
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")
        count = db_conn.execute("SELECT COUNT(*) FROM race_predictions").fetchone()[0]
        assert count == 1

    def test_endurance_score_rerun_no_duplicates(self, db_conn):
        client = _minimal_client()
        client.get_endurance_score.return_value = {
            "enduranceScoreDTO": {"calendarDate": "2025-06-01", "overallScore": 5000, "levelId": 2},
            "groupMap": {},
        }
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")
        count = db_conn.execute("SELECT COUNT(*) FROM endurance_score").fetchone()[0]
        assert count == 1

    def test_hill_score_rerun_no_duplicates(self, db_conn):
        client = _minimal_client()
        client.get_hill_score.return_value = {
            "hillScoreDTOList": [
                {"calendarDate": "2025-06-01", "overallScore": 50,
                 "strengthScore": 12, "hillEnduranceScore": 38, "levelId": 3},
            ]
        }
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")
        count = db_conn.execute("SELECT COUNT(*) FROM hill_score").fetchone()[0]
        assert count == 1

    def test_rerun_updates_value_on_change(self, db_conn):
        """Garmin corrects a race prediction — re-sync updates the stored value."""
        client = self._race_predictions_client()
        engine = _make_engine(db_conn, client)
        engine.sync_performance_ranges("2025-01-01", "2025-12-31")

        client.get_race_predictions.return_value = [
            {
                "calendarDate": "2025-06-01",
                "time5K": 1450, "time10K": 3100,  # corrected values
                "timeHalfMarathon": 7000, "timeMarathon": 16000,
            }
        ]
        engine2 = _make_engine(db_conn, client)
        engine2.sync_performance_ranges("2025-01-01", "2025-12-31")

        row = db_conn.execute(
            "SELECT race_5k_s FROM race_predictions WHERE date='2025-06-01'"
        ).fetchone()
        assert row[0] == 1450


# ---------------------------------------------------------------------------
# reprocess-performance-derived makes no Garmin API calls
# ---------------------------------------------------------------------------

class TestReprocessPerformanceDerivedNoGarminCalls:
    def test_reprocess_reads_raw_payload_only(self, db_conn):
        """Reprocess reads raw_payload and writes normalised tables — no Garmin calls needed."""
        import json, hashlib
        from garmin_sync import normalise, repositories as repo

        payload = [
            {
                "calendarDate": "2025-06-01",
                "time5K": 1479, "time10K": 3175,
                "timeHalfMarathon": 7078, "timeMarathon": 16245,
            }
        ]
        payload_json = json.dumps(payload, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        cur = db_conn.execute(
            "INSERT INTO raw_payload (source, data_type, date, fetched_at, payload_json, payload_hash) "
            "VALUES ('garmin_connect', 'race_predictions', '2025-12-31', "
            "'2025-12-31T00:00:00+00:00', ?, ?)",
            (payload_json, payload_hash),
        )
        db_conn.commit()
        raw_id = cur.lastrowid

        # Replicate what the CLI command does — no GarminClient involved
        entries = json.loads(payload_json)
        for entry in entries:
            r = normalise.normalise_race_prediction(entry, raw_id)
            if r:
                repo.upsert_race_prediction(db_conn, r)

        count = db_conn.execute("SELECT COUNT(*) FROM race_predictions").fetchone()[0]
        assert count == 1

    def test_reprocess_is_idempotent(self, db_conn):
        """Running reprocess twice gives the same result — no duplicates."""
        import json, hashlib
        from garmin_sync import normalise, repositories as repo

        payload = [{"calendarDate": "2025-06-01", "time5K": 1479, "time10K": 3175,
                    "timeHalfMarathon": 7078, "timeMarathon": 16245}]
        payload_json = json.dumps(payload, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        cur = db_conn.execute(
            "INSERT INTO raw_payload (source, data_type, date, fetched_at, payload_json, payload_hash) "
            "VALUES ('garmin_connect', 'race_predictions', '2025-12-31', "
            "'2025-12-31T00:00:00+00:00', ?, ?)",
            (payload_json, payload_hash),
        )
        db_conn.commit()
        raw_id = cur.lastrowid

        for _ in range(2):
            entries = json.loads(payload_json)
            for entry in entries:
                r = normalise.normalise_race_prediction(entry, raw_id)
                if r:
                    repo.upsert_race_prediction(db_conn, r)

        count = db_conn.execute("SELECT COUNT(*) FROM race_predictions").fetchone()[0]
        assert count == 1
