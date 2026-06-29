"""
ActivitySyncEngine: orchestrates fetching, storing, and normalising activities.

Flow per activity:
  1. Fetch raw dict from GarminClient.
  2. Upsert into raw_payload (keyed by garmin_id).
  3. If payload changed (hash differs): normalise and upsert activity row.
  4. If payload unchanged: skip normalisation (no-op).

Backfill cursor is updated after each full page, not per activity.
Crash mid-page: cursor points to start of that page; re-fetch is safe (upserts are idempotent).

Offset pagination note:
  Garmin returns activities most-recent-first (start=0 is newest).
  Backfill uses increasing offsets to walk backwards in time.
  KNOWN LIMITATION: if new activities arrive during backfill, offsets drift.
  Recommended: run backfill once, then use sync-recent-activities for ongoing sync.
"""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from garmin_sync import repositories as repo
from garmin_sync import normalise
from garmin_sync.config import Config
from garmin_sync.garmin_client import GarminClient
from garmin_sync.models import SyncResult

logger = logging.getLogger(__name__)

_DATA_TYPE = "activity_summary"


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class _Stats:
    fetched: int = 0
    stored: int = 0
    skipped: int = 0
    updated: int = 0

    def to_result(self) -> SyncResult:
        return SyncResult(
            fetched=self.fetched,
            stored=self.stored,
            skipped=self.skipped,
            updated=self.updated,
        )


def _process_activity(
    conn: sqlite3.Connection,
    activity: dict,
    stats: _Stats,
    dry_run: bool,
) -> None:
    activity_id = str(activity.get("activityId", ""))
    if not activity_id:
        logger.warning("Activity missing activityId, skipping.")
        stats.skipped += 1
        return

    payload_json = _canonical_json(activity)
    payload_hash = _sha256(payload_json)

    if dry_run:
        logger.info("[dry-run] would store activity %s", activity_id)
        stats.fetched += 1
        return

    raw_id, was_changed = repo.upsert_raw_payload_by_garmin_id(
        conn,
        source="garmin_connect",
        data_type=_DATA_TYPE,
        garmin_id=activity_id,
        payload_json=payload_json,
        payload_hash=payload_hash,
    )

    stats.fetched += 1

    if was_changed:
        normalised = normalise.normalise_activity(activity)
        if normalised is None:
            logger.warning(
                "Normalisation returned None for activity %s — raw payload kept.",
                activity_id,
            )
            stats.stored += 1
            return

        normalised["raw_payload_id"] = raw_id
        repo.upsert_activity(conn, normalised)  # type: ignore[arg-type]
        stats.stored += 1
        stats.updated += 1
        logger.debug("Stored/updated activity %s", activity_id)
    else:
        stats.skipped += 1
        logger.debug("Activity %s unchanged (hash match), skipped.", activity_id)


class ActivitySyncEngine:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: GarminClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        self._client = client
        self._dry_run = dry_run

    def sync_recent_activities(self, limit: int = 20) -> SyncResult:
        """
        Fetch the most recent N activities and upsert them.

        Always starts at offset 0 (most recent). Does not use or update
        the backfill cursor. Safe to run repeatedly — idempotent by activity_id.
        """
        logger.info("sync-recent-activities: fetching %d activities (offset 0)", limit)
        stats = _Stats()

        run_id = repo.create_sync_run(self._conn, "sync-recent-activities")
        try:
            activities = self._client.get_activities(start=0, limit=limit)
            logger.info("Fetched %d activities from Garmin.", len(activities))

            for activity in activities:
                _process_activity(self._conn, activity, stats, self._dry_run)

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "sync-recent-activities done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched,
                stats.stored,
                stats.skipped,
                stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()

    def backfill_activities(self, page_size: int | None = None) -> SyncResult:
        """
        Fetch all activities page by page, resuming from cursor if present.

        Uses sync_cursor.last_offset to track progress.
        Cursor is written after each full page — crash mid-page is safe
        because re-fetching the page is idempotent.

        KNOWN LIMITATION: offset-based pagination drifts if new activities
        arrive during the backfill. Run backfill once, then use
        sync-recent-activities for ongoing incremental sync.
        """
        page_size = page_size or self._config.garmin_backfill_page_size
        stats = _Stats()

        cursor = repo.get_cursor(self._conn, _DATA_TYPE)
        start_offset = cursor["last_offset"] if cursor and cursor.get("last_offset") else 0

        logger.info(
            "backfill-activities: resuming from offset %d, page_size=%d",
            start_offset,
            page_size,
        )

        run_id = repo.create_sync_run(self._conn, "backfill-activities")
        try:
            while True:
                logger.info("Fetching page at offset %d (limit %d).", start_offset, page_size)
                activities = self._client.get_activities(start=start_offset, limit=page_size)

                if not activities:
                    logger.info("Empty page at offset %d — backfill complete.", start_offset)
                    break

                last_activity_id: str | None = None
                for activity in activities:
                    _process_activity(self._conn, activity, stats, self._dry_run)
                    last_activity_id = str(activity.get("activityId", "")) or last_activity_id

                if not self._dry_run:
                    start_offset += len(activities)
                    repo.update_cursor(
                        self._conn,
                        _DATA_TYPE,
                        last_offset=start_offset,
                        last_successful_activity_id=last_activity_id,
                    )
                    logger.debug("Cursor updated to offset %d.", start_offset)

                if len(activities) < page_size:
                    logger.info("Partial page (%d < %d) — backfill complete.", len(activities), page_size)
                    break

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "backfill-activities done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched,
                stats.stored,
                stats.skipped,
                stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()


# ---------------------------------------------------------------------------
# Activity detail sync
# ---------------------------------------------------------------------------

_DETAIL_DATA_TYPE = "activity_detail"


def _process_one_detail(
    conn: sqlite3.Connection,
    client: GarminClient,
    activity_id: str,
    stats: _Stats,
    dry_run: bool,
) -> None:
    """Fetch and store detail for a single activity. Raises on auth/rate errors."""
    raw = client.get_activity_detail(activity_id)

    if not raw:
        logger.warning("Empty detail response for activity %s, skipping.", activity_id)
        stats.skipped += 1
        return

    payload_json = _canonical_json(raw)
    payload_hash = _sha256(payload_json)

    if dry_run:
        logger.info("[dry-run] would store detail for activity %s", activity_id)
        stats.fetched += 1
        return

    raw_id, was_changed = repo.upsert_raw_payload_by_garmin_id(
        conn,
        source="garmin_connect",
        data_type=_DETAIL_DATA_TYPE,
        garmin_id=activity_id,
        payload_json=payload_json,
        payload_hash=payload_hash,
    )

    stats.fetched += 1

    if was_changed:
        detail_row = normalise.normalise_activity_detail(raw)
        if detail_row is None:
            logger.warning("Detail normalisation failed for activity %s — raw kept.", activity_id)
            stats.stored += 1
            return

        detail_row["raw_payload_id"] = raw_id
        repo.upsert_activity_detail(conn, detail_row)

        laps: list[dict] = []
        try:
            laps = normalise.normalise_activity_laps(raw, activity_id, raw_id)
            repo.replace_activity_laps(conn, activity_id, laps)
        except Exception as exc:
            logger.warning(
                "Lap normalisation failed for activity %s (%s) — detail row kept.", activity_id, exc
            )

        stats.stored += 1
        stats.updated += 1
        logger.debug("Stored detail for activity %s (%d laps).", activity_id, len(laps))
    else:
        stats.skipped += 1
        logger.debug("Detail for activity %s unchanged (hash match), skipped.", activity_id)


class DetailSyncEngine:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: GarminClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        self._client = client
        self._dry_run = dry_run

    def sync_recent_details(
        self, limit: int = 20, refresh_existing: bool = False
    ) -> SyncResult:
        """
        Fetch detail payloads for up to `limit` activities.

        By default picks the most recent activities that have no detail row yet.
        --refresh-existing: re-fetch even if detail exists (hash change detection applies).

        Individual fetch failures are logged and skipped.
        Auth and rate-limit errors stop the entire run.
        """
        activity_ids = repo.get_activities_needing_detail(
            self._conn, limit=limit, refresh_existing=refresh_existing
        )
        logger.info(
            "sync-activity-details: %d activities to process (limit=%d, refresh=%s)",
            len(activity_ids), limit, refresh_existing,
        )

        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "sync-activity-details")

        try:
            for activity_id in activity_ids:
                try:
                    _process_one_detail(
                        self._conn, self._client, activity_id, stats, self._dry_run
                    )
                    if not self._dry_run:
                        repo.update_cursor(
                            self._conn, _DETAIL_DATA_TYPE,
                            last_successful_activity_id=activity_id,
                        )
                except Exception as exc:
                    from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
                    if isinstance(exc, (LoginRateLimitedError, RateLimitExceeded)):
                        raise
                    logger.warning(
                        "Detail fetch failed for activity %s: %s — continuing.", activity_id, exc
                    )
                    stats.skipped += 1

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "sync-activity-details done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched, stats.stored, stats.skipped, stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()

    def backfill_details(self, refresh_existing: bool = False) -> SyncResult:
        """
        Fetch detail payloads for all activities without one, newest first.

        Resumable: activities already in activity_detail are skipped via LEFT JOIN.
        Individual fetch failures are logged and skipped.
        Auth and rate-limit errors stop the run; progress is preserved.
        """
        activity_ids = repo.get_activities_needing_detail(
            self._conn, limit=None, refresh_existing=refresh_existing
        )
        total = len(activity_ids)
        logger.info(
            "backfill-activity-details: %d activities to process (refresh=%s)",
            total, refresh_existing,
        )

        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "backfill-activity-details")

        try:
            for i, activity_id in enumerate(activity_ids, 1):
                if i % 50 == 0 or i == total:
                    logger.info("Progress: %d/%d activities processed.", i, total)

                try:
                    _process_one_detail(
                        self._conn, self._client, activity_id, stats, self._dry_run
                    )
                    if not self._dry_run:
                        repo.update_cursor(
                            self._conn, _DETAIL_DATA_TYPE,
                            last_successful_activity_id=activity_id,
                        )
                except Exception as exc:
                    from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
                    if isinstance(exc, (LoginRateLimitedError, RateLimitExceeded)):
                        raise
                    logger.warning(
                        "Detail fetch failed for activity %s: %s — continuing.", activity_id, exc
                    )
                    stats.skipped += 1

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "backfill-activity-details done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched, stats.stored, stats.skipped, stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()


# ---------------------------------------------------------------------------
# Health data sync
# ---------------------------------------------------------------------------

_HEALTH_CURSOR = "health"


@dataclass
class _HealthFetcher:
    data_type: str
    fetch: Callable
    normalise_fn: Callable
    upsert: Callable


class HealthSyncEngine:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: GarminClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        self._client = client
        self._dry_run = dry_run

        self._fetchers: list[_HealthFetcher] = [
            _HealthFetcher("daily_summary", client.get_user_summary, normalise.normalise_daily_summary, repo.upsert_daily_summary),
            _HealthFetcher("sleep", client.get_sleep_data, normalise.normalise_sleep, repo.upsert_sleep),
            _HealthFetcher("hrv", client.get_hrv_data, normalise.normalise_hrv, repo.upsert_hrv),
            _HealthFetcher("stress", client.get_stress_data, normalise.normalise_stress, repo.upsert_stress),
            _HealthFetcher("body_battery", lambda d: client.get_body_battery(d, d), normalise.normalise_body_battery, repo.upsert_body_battery),
            _HealthFetcher("heart_rate", client.get_heart_rates, normalise.normalise_heart_rate, repo.upsert_heart_rate),
        ]

    def _date_range(self, from_date: str, to_date: str) -> list[str]:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        if end < start:
            return []
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    def _process_health_date(self, date_str: str, stats: _Stats) -> None:
        """Fetch and store all health data types for one calendar date."""
        for fetcher in self._fetchers:
            try:
                raw = fetcher.fetch(date_str)
                if not raw:
                    logger.debug("Empty %s response for %s.", fetcher.data_type, date_str)
                    continue

                payload_json = _canonical_json(raw)
                payload_hash = _sha256(payload_json)

                if self._dry_run:
                    logger.info("[dry-run] would store %s for %s", fetcher.data_type, date_str)
                    stats.fetched += 1
                    continue

                raw_id, was_changed = repo.upsert_raw_payload_by_date(
                    self._conn,
                    source="garmin_connect",
                    data_type=fetcher.data_type,
                    date=date_str,
                    payload_json=payload_json,
                    payload_hash=payload_hash,
                )
                stats.fetched += 1

                if was_changed:
                    row = fetcher.normalise_fn(raw, date_str, raw_id)
                    if row is not None:
                        fetcher.upsert(self._conn, row)
                    stats.stored += 1
                    stats.updated += 1
                else:
                    stats.skipped += 1

            except Exception as exc:
                from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
                if isinstance(exc, (LoginRateLimitedError, RateLimitExceeded)):
                    raise
                logger.warning(
                    "Health fetch failed for %s on %s: %s — continuing.",
                    fetcher.data_type, date_str, exc,
                )
                stats.skipped += 1

    def sync_health(self, from_date: str, to_date: str) -> SyncResult:
        """
        Fetch all health data types for every date in [from_date, to_date].

        Both dates inclusive (YYYY-MM-DD). Always re-syncs all dates —
        Garmin corrects health data retroactively.
        Individual data-type failures are logged and skipped.
        """
        dates = list(reversed(self._date_range(from_date, to_date)))
        total = len(dates)
        logger.info("sync-health: %d dates from %s back to %s", total, to_date, from_date)

        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "sync-health")

        try:
            for i, date_str in enumerate(dates, 1):
                logger.info("Health [%d/%d] %s", i, total, date_str)
                self._process_health_date(date_str, stats)
                if not self._dry_run:
                    repo.update_cursor(self._conn, _HEALTH_CURSOR, last_successful_date=date_str)

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "sync-health done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched, stats.stored, stats.skipped, stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()

    def sync_recent_health(self, days: int = 7) -> SyncResult:
        """
        Fetch health data for the last N days (today and N-1 preceding days).

        Always re-syncs even if data already exists — Garmin corrects sleep/HRV
        data retroactively so re-fetching recent days is the correct strategy.
        """
        today = date.today()
        from_date = (today - timedelta(days=days - 1)).isoformat()
        to_date = today.isoformat()
        logger.info("sync-recent-health: %d days (%s to %s)", days, from_date, to_date)

        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "sync-recent-health")

        date_list = self._date_range(from_date, to_date)
        total = len(date_list)
        try:
            for i, date_str in enumerate(date_list, 1):
                logger.info("Health [%d/%d] %s", i, total, date_str)
                self._process_health_date(date_str, stats)
                if not self._dry_run:
                    repo.update_cursor(self._conn, _HEALTH_CURSOR, last_successful_date=date_str)

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "sync-recent-health done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched, stats.stored, stats.skipped, stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()


# ---------------------------------------------------------------------------
# Performance ranges sync (Phase 5b.1)
# ---------------------------------------------------------------------------


class PerformanceSyncEngine:
    """Syncs range-based performance metrics (Phase 5b.1).

    All four range endpoints enforce a 366-day maximum window per API call.
    Every endpoint is chunked into yearly windows automatically.
    Each call is idempotent (upserts by date) — no cursor needed.
    """

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: GarminClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        self._client = client
        self._dry_run = dry_run

    def sync_performance_ranges(self, from_date: str, to_date: str) -> SyncResult:
        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "sync-performance-ranges")
        try:
            self._sync_lactate_threshold(from_date, to_date, stats)
            self._sync_race_predictions(from_date, to_date, stats)
            self._sync_endurance_score(from_date, to_date, stats)
            self._sync_hill_score(from_date, to_date, stats)
            repo.finish_sync_run(self._conn, run_id, "completed")
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", str(exc))
            raise
        return stats.to_result()

    @staticmethod
    def _yearly_chunks(from_date: str, to_date: str) -> list[tuple[str, str]]:
        """Split a date range into ≤366-day chunks aligned to calendar years."""
        from datetime import date as _date, timedelta as _td
        start = _date.fromisoformat(from_date)
        end = _date.fromisoformat(to_date)
        chunks = []
        chunk_start = start
        while chunk_start <= end:
            try:
                chunk_end = min(chunk_start.replace(year=chunk_start.year + 1) - _td(days=1), end)
            except ValueError:
                chunk_end = end
            chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
            chunk_start = chunk_end + _td(days=1)
        return chunks

    def _store_range_payload(self, data_type: str, date_key: str, raw: object) -> tuple[int, bool]:
        payload_json = _canonical_json(raw)
        payload_hash = _sha256(payload_json)
        return repo.upsert_raw_payload_by_date(
            self._conn,
            source="garmin_connect",
            data_type=data_type,
            date=date_key,
            payload_json=payload_json,
            payload_hash=payload_hash,
        )

    def _sync_lactate_threshold(self, from_date: str, to_date: str, stats: _Stats) -> None:
        total_stored = 0
        for cs, ce in self._yearly_chunks(from_date, to_date):
            logger.info("Fetching lactate_threshold %s to %s", cs, ce)
            raw = self._client.get_lactate_threshold(cs, ce)
            if not raw:
                stats.fetched += 1
                continue
            if self._dry_run:
                logger.info("[dry-run] would store lactate_threshold for %s to %s", cs, ce)
                stats.fetched += 1
                continue
            raw_id, _ = self._store_range_payload("lactate_threshold", ce, raw)
            rows = normalise.normalise_lactate_threshold(raw, raw_id)
            for row in rows:
                repo.upsert_lactate_threshold(self._conn, row)
            stats.fetched += 1
            total_stored += len(rows)
        stats.stored += total_stored
        logger.info("Stored %d lactate_threshold entries total.", total_stored)

    def _sync_race_predictions(self, from_date: str, to_date: str, stats: _Stats) -> None:
        total_stored = 0
        for cs, ce in self._yearly_chunks(from_date, to_date):
            logger.info("Fetching race_predictions %s to %s", cs, ce)
            entries = self._client.get_race_predictions(cs, ce)
            if not entries:
                stats.fetched += 1
                continue
            if self._dry_run:
                logger.info("[dry-run] would store %d race_prediction entries for %s to %s", len(entries), cs, ce)
                stats.fetched += 1
                continue
            raw_id, _ = self._store_range_payload("race_predictions", ce, entries)
            for entry in entries:
                row = normalise.normalise_race_prediction(entry, raw_id)
                if row:
                    repo.upsert_race_prediction(self._conn, row)
                    total_stored += 1
            stats.fetched += 1
        stats.stored += total_stored
        logger.info("Stored %d race_prediction entries total.", total_stored)

    def _sync_endurance_score(self, from_date: str, to_date: str, stats: _Stats) -> None:
        total_stored = 0
        for cs, ce in self._yearly_chunks(from_date, to_date):
            logger.info("Fetching endurance_score %s to %s", cs, ce)
            raw = self._client.get_endurance_score(cs, ce)
            if not raw:
                stats.fetched += 1
                continue
            if self._dry_run:
                logger.info("[dry-run] would store endurance_score for %s to %s", cs, ce)
                stats.fetched += 1
                continue
            raw_id, _ = self._store_range_payload("endurance_score", ce, raw)
            rows = normalise.normalise_endurance_score(raw, raw_id)
            for row in rows:
                repo.upsert_endurance_score(self._conn, row)
            stats.fetched += 1
            total_stored += len(rows)
        stats.stored += total_stored
        logger.info("Stored %d endurance_score entries total.", total_stored)

    def _sync_hill_score(self, from_date: str, to_date: str, stats: _Stats) -> None:
        total_stored = 0
        for cs, ce in self._yearly_chunks(from_date, to_date):
            logger.info("Fetching hill_score %s to %s", cs, ce)
            raw = self._client.get_hill_score(cs, ce)
            if not raw:
                stats.fetched += 1
                continue
            if self._dry_run:
                logger.info("[dry-run] would store hill_score for %s to %s", cs, ce)
                stats.fetched += 1
                continue
            raw_id, _ = self._store_range_payload("hill_score", ce, raw)
            entries = raw.get("hillScoreDTOList") or []
            stored = 0
            for entry in entries:
                row = normalise.normalise_hill_score_entry(entry, raw_id)
                if row:
                    repo.upsert_hill_score(self._conn, row)
                    stored += 1
            stats.fetched += 1
            total_stored += stored
        stats.stored += total_stored
        logger.info("Stored %d hill_score entries total.", total_stored)


# ---------------------------------------------------------------------------
# Per-day performance sync (Phase 5b.2)
# ---------------------------------------------------------------------------

_PERF_DAY_CURSOR = "performance_day"


class PerformanceDaySyncEngine:
    """Syncs per-day performance metrics: training_status, training_readiness, max_metrics.

    Newest-first iteration with cursor-based resume. Each date is processed
    atomically; cursor is written after each successful date.
    """

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: GarminClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        self._client = client
        self._dry_run = dry_run

    def _date_range(self, from_date: str, to_date: str) -> list[str]:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        if end < start:
            return []
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    def _process_day(self, date_str: str, stats: _Stats) -> None:
        """Fetch and store all per-day performance data for one date."""
        fetchers = [
            ("training_status",    self._client.get_training_status,    normalise.normalise_training_status,    repo.upsert_training_status,    False),
            ("training_readiness", self._client.get_training_readiness, normalise.normalise_training_readiness, repo.upsert_training_readiness, True),
            ("max_metrics",        self._client.get_max_metrics,        normalise.normalise_max_metrics,        repo.upsert_max_metrics,        True),
            ("fitness_age",        self._client.get_fitnessage_data,    normalise.normalise_fitness_age,        repo.upsert_fitness_age,        False),
        ]
        for data_type, fetch_fn, norm_fn, upsert_fn, is_list in fetchers:
            try:
                raw = fetch_fn(date_str)
                if not raw:
                    logger.debug("Empty %s for %s", data_type, date_str)
                    continue
                payload_json = _canonical_json(raw)
                payload_hash = _sha256(payload_json)
                if self._dry_run:
                    logger.info("[dry-run] would store %s for %s", data_type, date_str)
                    stats.fetched += 1
                    continue
                raw_id, was_changed = repo.upsert_raw_payload_by_date(
                    self._conn,
                    source="garmin_connect",
                    data_type=data_type,
                    date=date_str,
                    payload_json=payload_json,
                    payload_hash=payload_hash,
                )
                stats.fetched += 1
                if was_changed:
                    if is_list:
                        row = norm_fn(raw, date_str, raw_id)
                    else:
                        row = norm_fn(raw, date_str, raw_id)
                    if row is not None:
                        upsert_fn(self._conn, row)
                    stats.stored += 1
                    stats.updated += 1
                else:
                    stats.skipped += 1
            except Exception as exc:
                from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
                if isinstance(exc, (LoginRateLimitedError, RateLimitExceeded)):
                    raise
                logger.warning("Perf-day fetch failed for %s on %s: %s — continuing.", data_type, date_str, exc)
                stats.skipped += 1

    def sync_performance(self, from_date: str, to_date: str) -> SyncResult:
        """Sync per-day performance metrics for [from_date, to_date], newest-first."""
        dates = list(reversed(self._date_range(from_date, to_date)))
        total = len(dates)
        logger.info("sync-performance: %d dates from %s back to %s", total, to_date, from_date)
        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "sync-performance")
        try:
            for i, date_str in enumerate(dates, 1):
                logger.info("Performance [%d/%d] %s", i, total, date_str)
                self._process_day(date_str, stats)
                if not self._dry_run:
                    repo.update_cursor(self._conn, _PERF_DAY_CURSOR, last_successful_date=date_str)
            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info("sync-performance done: fetched=%d stored=%d skipped=%d updated=%d",
                        stats.fetched, stats.stored, stats.skipped, stats.updated)
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise
        return stats.to_result()

    def backfill_performance(self, from_date: str, to_date: str) -> SyncResult:
        """Backfill per-day performance metrics, resuming from cursor if interrupted."""
        cursor = repo.get_cursor(self._conn, _PERF_DAY_CURSOR)
        resume_from = cursor["last_successful_date"] if cursor else None

        if resume_from:
            from datetime import date as _date, timedelta as _td
            resume_dt = _date.fromisoformat(resume_from) - _td(days=1)
            effective_to = resume_dt.isoformat()
            logger.info("Resuming backfill from cursor: %s (continuing back to %s)", resume_from, from_date)
            if effective_to < from_date:
                logger.info("Backfill already complete (cursor=%s, from=%s).", resume_from, from_date)
                stats = _Stats()
                return stats.to_result()
        else:
            effective_to = to_date

        dates = list(reversed(self._date_range(from_date, effective_to)))
        total = len(dates)
        logger.info("backfill-performance: %d dates from %s back to %s", total, effective_to, from_date)
        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "backfill-performance")
        try:
            for i, date_str in enumerate(dates, 1):
                logger.info("Performance [%d/%d] %s", i, total, date_str)
                self._process_day(date_str, stats)
                if not self._dry_run:
                    repo.update_cursor(self._conn, _PERF_DAY_CURSOR, last_successful_date=date_str)
            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info("backfill-performance done: fetched=%d stored=%d skipped=%d updated=%d",
                        stats.fetched, stats.stored, stats.skipped, stats.updated)
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise
        return stats.to_result()

    def reprocess_performance_derived(self) -> SyncResult:
        """Reprocess all performance tables from raw_payload. No Garmin calls."""
        stats = _Stats()
        for data_type, norm_fn, upsert_fn in [
            ("training_status",    normalise.normalise_training_status,    repo.upsert_training_status),
            ("training_readiness", normalise.normalise_training_readiness, repo.upsert_training_readiness),
            ("max_metrics",        normalise.normalise_max_metrics,        repo.upsert_max_metrics),
            ("fitness_age",        normalise.normalise_fitness_age,        repo.upsert_fitness_age),
        ]:
            for row in self._conn.execute(
                "SELECT id, date, payload_json FROM raw_payload WHERE data_type=?", (data_type,)
            ).fetchall():
                import json as _j
                raw = _j.loads(row["payload_json"])
                result = norm_fn(raw, row["date"], row["id"])
                if result is not None:
                    upsert_fn(self._conn, result)
                    stats.stored += 1
                stats.fetched += 1
        logger.info("reprocess_performance_derived done: fetched=%d stored=%d", stats.fetched, stats.stored)
        return stats.to_result()


# ---------------------------------------------------------------------------
# Activity sample sync (time-series: HR, pace, GPS, cadence, etc.)
# ---------------------------------------------------------------------------

_SAMPLES_DATA_TYPE = "activity_samples"


def _process_one_samples(
    conn: sqlite3.Connection,
    client: GarminClient,
    activity_id: str,
    stats: _Stats,
    dry_run: bool,
) -> None:
    """Fetch and store time-series samples for a single activity."""
    raw = client.get_activity_samples(activity_id)

    if not raw:
        logger.warning("Empty samples response for activity %s, skipping.", activity_id)
        stats.skipped += 1
        return

    payload_json = _canonical_json(raw)
    payload_hash = _sha256(payload_json)

    if dry_run:
        logger.info("[dry-run] would store samples for activity %s", activity_id)
        stats.fetched += 1
        return

    raw_id, was_changed = repo.upsert_raw_payload_by_garmin_id(
        conn,
        source="garmin_connect",
        data_type=_SAMPLES_DATA_TYPE,
        garmin_id=activity_id,
        payload_json=payload_json,
        payload_hash=payload_hash,
    )

    stats.fetched += 1

    if was_changed:
        samples = normalise.normalise_activity_samples(raw, activity_id, raw_id)
        repo.replace_activity_samples(conn, activity_id, samples)
        stats.stored += 1
        stats.updated += 1
        logger.debug("Stored %d samples for activity %s.", len(samples), activity_id)
    else:
        stats.skipped += 1
        logger.debug("Samples for activity %s unchanged (hash match), skipped.", activity_id)


class SampleSyncEngine:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: GarminClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._conn = conn
        self._client = client
        self._dry_run = dry_run

    def sync_recent_samples(
        self, limit: int = 20, refresh_existing: bool = False
    ) -> SyncResult:
        """
        Fetch time-series samples for up to `limit` activities.

        Picks the most recent activities that have no sample rows yet.
        --refresh-existing: re-fetch even if samples exist (hash change detection applies).
        """
        activity_ids = repo.get_activities_needing_samples(
            self._conn, limit=limit, refresh_existing=refresh_existing
        )
        logger.info(
            "sync-activity-samples: %d activities to process (limit=%d, refresh=%s)",
            len(activity_ids), limit, refresh_existing,
        )

        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "sync-activity-samples")

        try:
            for activity_id in activity_ids:
                try:
                    _process_one_samples(
                        self._conn, self._client, activity_id, stats, self._dry_run
                    )
                    if not self._dry_run:
                        repo.update_cursor(
                            self._conn, _SAMPLES_DATA_TYPE,
                            last_successful_activity_id=activity_id,
                        )
                except Exception as exc:
                    from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
                    if isinstance(exc, (LoginRateLimitedError, RateLimitExceeded)):
                        raise
                    logger.warning(
                        "Sample fetch failed for activity %s: %s — continuing.", activity_id, exc
                    )
                    stats.skipped += 1

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "sync-activity-samples done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched, stats.stored, stats.skipped, stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()

    def backfill_samples(self, refresh_existing: bool = False) -> SyncResult:
        """
        Fetch time-series samples for all activities without them, newest first.

        Resumable: activities already with sample rows are skipped.
        Individual fetch failures are logged and skipped.
        Auth and rate-limit errors stop the run; progress is preserved.
        """
        activity_ids = repo.get_activities_needing_samples(
            self._conn, limit=None, refresh_existing=refresh_existing
        )
        total = len(activity_ids)
        logger.info(
            "backfill-activity-samples: %d activities to process (refresh=%s)",
            total, refresh_existing,
        )

        stats = _Stats()
        run_id = repo.create_sync_run(self._conn, "backfill-activity-samples")

        try:
            for i, activity_id in enumerate(activity_ids, 1):
                if i % 50 == 0 or i == total:
                    logger.info("Progress: %d/%d activities processed.", i, total)

                try:
                    _process_one_samples(
                        self._conn, self._client, activity_id, stats, self._dry_run
                    )
                    if not self._dry_run:
                        repo.update_cursor(
                            self._conn, _SAMPLES_DATA_TYPE,
                            last_successful_activity_id=activity_id,
                        )
                except Exception as exc:
                    from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
                    if isinstance(exc, (LoginRateLimitedError, RateLimitExceeded)):
                        raise
                    logger.warning(
                        "Sample fetch failed for activity %s: %s — continuing.", activity_id, exc
                    )
                    stats.skipped += 1

            repo.finish_sync_run(self._conn, run_id, "completed")
            logger.info(
                "backfill-activity-samples done: fetched=%d stored=%d skipped=%d updated=%d",
                stats.fetched, stats.stored, stats.skipped, stats.updated,
            )
        except Exception as exc:
            repo.finish_sync_run(self._conn, run_id, "failed", error=str(exc))
            raise

        return stats.to_result()
