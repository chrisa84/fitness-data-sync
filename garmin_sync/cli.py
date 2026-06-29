"""
CLI entry point: garmin-sync <command>

Commands:
  init-db                  Create/migrate SQLite database.
  auth                     Authenticate with Garmin Connect and store tokens.
  sync-all                 Incremental sync of all data types in one command.
  sync-recent-activities   Fetch most recent N activities (incremental sync).
  backfill-activities      Fetch all activities page by page (historical backfill).
  status                   Show sync state, counts, and recent runs.
"""

import json
import json as _json
import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from garmin_sync.config import load_config
from garmin_sync.db import get_connection, init_db
from garmin_sync.garmin_client import GarminClient
from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded
from garmin_sync import queries, repositories as repo
from garmin_sync import export as exporter
from garmin_sync import normalise
from garmin_sync.sync_engine import ActivitySyncEngine, DetailSyncEngine, HealthSyncEngine, PerformanceSyncEngine, PerformanceDaySyncEngine, SampleSyncEngine

app = typer.Typer(
    name="garmin-sync",
    help="Local, resumable, idempotent Garmin Connect data mirror.",
    no_args_is_help=True,
)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _get_config_and_conn(
    env_file: Optional[str],
    db_path: Optional[str],
    log_level: Optional[str],
):
    config = load_config(env_file)
    if log_level:
        config.log_level = log_level.upper()
    _setup_logging(config.log_level)

    effective_db = Path(db_path) if db_path else config.garmin_db_path
    conn = get_connection(effective_db)
    return config, conn


# ---------------------------------------------------------------------------
# init-db
# ---------------------------------------------------------------------------


@app.command("init-db")
def cmd_init_db(
    env_file: Annotated[Optional[str], typer.Option("--config", help=".env file path")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db", help="Database path override")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Create or migrate the SQLite database."""
    config, conn = _get_config_and_conn(env_file, db_path, log_level)
    with conn:
        init_db(conn)
    effective_db = Path(db_path) if db_path else config.garmin_db_path
    typer.echo(f"Database initialised: {effective_db.resolve()}")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@app.command("auth")
def cmd_auth(
    env_file: Annotated[Optional[str], typer.Option("--config", help=".env file path")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Authenticate with Garmin Connect and store tokens for reuse."""
    config, _ = _get_config_and_conn(env_file, db_path, log_level)

    if not config.garmin_email or not config.garmin_password.get_secret_value():
        typer.echo(
            "Error: GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env or environment.",
            err=True,
        )
        raise typer.Exit(1)

    client = GarminClient(config)
    try:
        client.authenticate()
        typer.echo(f"Authenticated. Tokens stored in: {config.garmin_token_path.expanduser().resolve()}")
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Authentication failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-recent-activities
# ---------------------------------------------------------------------------


@app.command("sync-recent-activities")
def cmd_sync_recent(
    limit: Annotated[int, typer.Option("--limit", help="Number of recent activities to fetch")] = 20,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Fetch from Garmin but do not write to DB")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch the most recent N activities from Garmin and upsert into the database.

    Always starts at offset 0 (most recent). Idempotent: rerunning with the same
    limit is safe and will update any activities whose data has changed.

    Use this for daily incremental sync. For a full historical backfill, use
    backfill-activities instead.

    --dry-run: Garmin is called but nothing is written to SQLite.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = ActivitySyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.sync_recent_activities(limit=limit)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited during sync: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Sync failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# backfill-activities
# ---------------------------------------------------------------------------


@app.command("backfill-activities")
def cmd_backfill(
    page_size: Annotated[int, typer.Option("--page-size")] = 0,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch all activities page by page (historical backfill).

    Resumable: if interrupted, reruns continue from the last completed page.
    Rerunning is safe — duplicate activities are not created.

    IMPORTANT: Garmin pagination is most-recent-first (offset 0 = newest).
    KNOWN LIMITATION: Offset pagination drifts if new activities are added
    during a long backfill. Run backfill once; use sync-recent-activities for
    ongoing incremental sync thereafter.

    --dry-run: Garmin is called but nothing is written to SQLite.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    effective_page_size = page_size or config.garmin_backfill_page_size

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = ActivitySyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.backfill_activities(page_size=effective_page_size)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited (progress saved). Run again to resume: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Backfill failed (progress saved). Run again to resume: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-activity-details
# ---------------------------------------------------------------------------


@app.command("sync-activity-details")
def cmd_sync_details(
    limit: Annotated[int, typer.Option("--limit", help="Max activities to fetch detail for")] = 20,
    refresh_existing: Annotated[bool, typer.Option("--refresh-existing", help="Re-fetch even if detail exists")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch detail payloads (including laps) for recent activities.

    Picks the most recent activities that don't yet have a detail row.
    Use --refresh-existing to re-fetch activities that already have detail.
    Use --dry-run to call Garmin without writing to the database.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = DetailSyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.sync_recent_details(limit=limit, refresh_existing=refresh_existing)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited during sync (progress saved): {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Detail sync failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# backfill-activity-details
# ---------------------------------------------------------------------------


@app.command("backfill-activity-details")
def cmd_backfill_details(
    refresh_existing: Annotated[bool, typer.Option("--refresh-existing")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch detail payloads for all activities, newest first.

    Resumable: already-fetched activities are skipped automatically.
    Individual failures are logged and skipped; auth/rate-limit errors stop cleanly.
    Use --refresh-existing to re-process activities that already have detail.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = DetailSyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.backfill_details(refresh_existing=refresh_existing)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited (progress saved). Run again to resume: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Backfill failed (progress saved). Run again to resume: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-activity-samples / backfill-activity-samples
# ---------------------------------------------------------------------------


@app.command("sync-activity-samples")
def cmd_sync_samples(
    limit: Annotated[int, typer.Option("--limit", help="Max activities to fetch samples for")] = 20,
    refresh_existing: Annotated[bool, typer.Option("--refresh-existing", help="Re-fetch even if samples exist")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch per-sample time-series data (HR, pace, GPS, cadence, power) for recent activities.

    Picks the most recent activities that don't yet have sample rows.
    Use --refresh-existing to re-fetch activities that already have samples.
    Use --dry-run to call Garmin without writing to the database.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = SampleSyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.sync_recent_samples(limit=limit, refresh_existing=refresh_existing)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited during sync (progress saved): {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Sample sync failed: {exc}", err=True)
        raise typer.Exit(1)


@app.command("backfill-activity-samples")
def cmd_backfill_samples(
    refresh_existing: Annotated[bool, typer.Option("--refresh-existing")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch time-series samples for all activities without them, newest first.

    Resumable: activities already with sample rows are skipped automatically.
    Individual failures are logged and skipped; auth/rate-limit errors stop cleanly.
    Use --refresh-existing to re-fetch activities that already have samples.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = SampleSyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.backfill_samples(refresh_existing=refresh_existing)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited (progress saved). Run again to resume: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Backfill failed (progress saved). Run again to resume: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-recent-health
# ---------------------------------------------------------------------------


@app.command("sync-recent-health")
def cmd_sync_recent_health(
    days: Annotated[int, typer.Option("--days", help="Number of recent days to sync")] = 7,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch health data (steps, sleep, HRV, stress, body battery, HR) for the last N days.

    Always re-syncs even if data already exists — Garmin corrects health data
    retroactively so re-fetching recent days is the correct strategy.
    Use --dry-run to call Garmin without writing to the database.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    engine = HealthSyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.sync_recent_health(days=days)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited during sync: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Health sync failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-health
# ---------------------------------------------------------------------------


@app.command("sync-health")
def cmd_sync_health(
    from_date: Annotated[str, typer.Option("--from", help="Start date YYYY-MM-DD")],
    to_date: Annotated[str, typer.Option("--to", help="End date YYYY-MM-DD")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Fetch health data for every date in a specified range.

    Both --from and --to are inclusive (YYYY-MM-DD). Re-syncs all dates in range
    regardless of what is already stored (Garmin corrects health data retroactively).
    Use --dry-run to call Garmin without writing to the database.
    """
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    # Resume from cursor if a previous run was interrupted (sync goes newest-first).
    # last_successful_date is the oldest date completed so far; resume_to is one day earlier.
    # Only resume if last_done is past the halfway point — guards against a stale
    # forward-direction cursor (which sits near from_date, not to_date).
    cursor = repo.get_cursor(conn, "health")
    if cursor and cursor.get("last_successful_date"):
        from datetime import date as _date, timedelta as _td
        last_done_d = _date.fromisoformat(cursor["last_successful_date"])
        from_d = _date.fromisoformat(from_date)
        to_d = _date.fromisoformat(to_date)
        midpoint = from_d + (to_d - from_d) / 2
        if last_done_d > midpoint:
            resume_to = (last_done_d - _td(days=1)).isoformat()
            typer.echo(f"Resuming: last completed {cursor['last_successful_date']}, continuing back to {from_date}.")
            to_date = resume_to

    client = GarminClient(config)
    engine = HealthSyncEngine(config, conn, client, dry_run=dry_run)

    try:
        result = engine.sync_health(from_date=from_date, to_date=to_date)
        typer.echo(
            f"Done. fetched={result['fetched']} stored={result['stored']} "
            f"skipped={result['skipped']} updated={result['updated']}"
        )
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited during sync: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Health sync failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-performance-ranges
# ---------------------------------------------------------------------------


def _today() -> str:
    from datetime import date as _date
    return _date.today().isoformat()


@app.command("sync-performance-ranges")
def cmd_sync_performance_ranges(
    from_date: Annotated[str, typer.Option("--from", help="Start date YYYY-MM-DD")] = "2020-04-01",
    to_date: Annotated[str, typer.Option("--to", help="End date YYYY-MM-DD")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Sync range-based performance metrics: lactate threshold, race predictions, endurance score, hill score."""
    config, conn = _get_config_and_conn(env_file, db_path, log_level)
    client = GarminClient(config)
    effective_to = to_date or _today()
    engine = PerformanceSyncEngine(config, conn, client, dry_run=dry_run)
    result = engine.sync_performance_ranges(from_date, effective_to)
    typer.echo(f"Performance ranges synced: fetched={result['fetched']} stored={result['stored']}")


# ---------------------------------------------------------------------------
# sync-performance / backfill-performance (Phase 5b.2)
# ---------------------------------------------------------------------------


@app.command("sync-performance")
def cmd_sync_performance(
    from_date: Annotated[str, typer.Option("--from", help="Start date YYYY-MM-DD")] = "",
    to_date: Annotated[str, typer.Option("--to", help="End date YYYY-MM-DD")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Sync per-day performance metrics (training_status, training_readiness, max_metrics)."""
    config, conn = _get_config_and_conn(env_file, db_path, log_level)
    client = GarminClient(config)
    from datetime import date as _date, timedelta as _td
    effective_to = to_date or _today()
    effective_from = from_date or (_date.today() - _td(days=7)).isoformat()
    engine = PerformanceDaySyncEngine(config, conn, client, dry_run=dry_run)
    try:
        result = engine.sync_performance(effective_from, effective_to)
        typer.echo(f"sync-performance done: fetched={result['fetched']} stored={result['stored']} skipped={result['skipped']}")
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"sync-performance failed: {exc}", err=True)
        raise typer.Exit(1)


@app.command("backfill-performance")
def cmd_backfill_performance(
    from_date: Annotated[str, typer.Option("--from", help="Start date YYYY-MM-DD")] = "2020-04-01",
    to_date: Annotated[str, typer.Option("--to", help="End date YYYY-MM-DD")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Backfill per-day performance metrics from historical dates, resumable from cursor."""
    config, conn = _get_config_and_conn(env_file, db_path, log_level)
    client = GarminClient(config)
    effective_to = to_date or _today()
    engine = PerformanceDaySyncEngine(config, conn, client, dry_run=dry_run)
    try:
        result = engine.backfill_performance(from_date, effective_to)
        typer.echo(f"backfill-performance done: fetched={result['fetched']} stored={result['stored']} skipped={result['skipped']}")
    except LoginRateLimitedError as exc:
        typer.echo(f"Rate limited during login: {exc}", err=True)
        raise typer.Exit(1)
    except RateLimitExceeded as exc:
        typer.echo(f"Rate limited: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"backfill-performance failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# sync-all
# ---------------------------------------------------------------------------


@app.command("sync-all")
def cmd_sync_all(
    limit: Annotated[int, typer.Option("--limit", help="Number of recent activities and details to fetch")] = 20,
    samples_limit: Annotated[int, typer.Option("--samples-limit", help="Max activities to fetch time-series samples for")] = 5,
    days: Annotated[int, typer.Option("--days", help="Days window for health and performance sync")] = 7,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Incremental sync of all data types in one command.

    Runs in order: activity summaries, activity details, activity time-series
    samples (HR/pace/GPS/cadence), health data, performance ranges, performance
    daily metrics, then a local derive pass that populates training_load, HR
    zones, and running dynamics from the freshly-synced raw payloads.

    Equivalent to running sync-recent-activities, sync-activity-details,
    sync-activity-samples, sync-recent-health, sync-performance-ranges,
    sync-performance, and reprocess-activity-derived in sequence with matching
    defaults.

    Stops immediately on rate-limit or auth errors.
    Use --dry-run to call Garmin without writing to the database (the derive
    pass is skipped under --dry-run since there are no new payloads to derive).
    """
    from datetime import date as _date, timedelta as _td

    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    if dry_run:
        typer.echo("[dry-run] Garmin will be called. No database writes will happen.")

    client = GarminClient(config)
    from_date = (_date.today() - _td(days=days)).isoformat()
    to_date = _today()

    steps: list[tuple[str, object]] = [
        ("activities", lambda: ActivitySyncEngine(config, conn, client, dry_run=dry_run).sync_recent_activities(limit=limit)),
        ("activity-details", lambda: DetailSyncEngine(config, conn, client, dry_run=dry_run).sync_recent_details(limit=limit)),
        ("activity-samples", lambda: SampleSyncEngine(config, conn, client, dry_run=dry_run).sync_recent_samples(limit=samples_limit)),
        ("health", lambda: HealthSyncEngine(config, conn, client, dry_run=dry_run).sync_recent_health(days=days)),
        ("performance-ranges", lambda: PerformanceSyncEngine(config, conn, client, dry_run=dry_run).sync_performance_ranges(from_date, to_date)),
        ("performance", lambda: PerformanceDaySyncEngine(config, conn, client, dry_run=dry_run).sync_performance(from_date, to_date)),
    ]

    for step_name, step_fn in steps:
        typer.echo(f"\n[{step_name}]")
        try:
            result = step_fn()
            parts = [f"fetched={result['fetched']}", f"stored={result['stored']}"]
            if "skipped" in result:
                parts.append(f"skipped={result['skipped']}")
            if "updated" in result:
                parts.append(f"updated={result['updated']}")
            typer.echo("Done. " + " ".join(parts))
        except LoginRateLimitedError as exc:
            typer.echo(f"Rate limited during login: {exc}", err=True)
            raise typer.Exit(1)
        except RateLimitExceeded as exc:
            typer.echo(f"Rate limited: {exc}", err=True)
            raise typer.Exit(1)
        except Exception as exc:
            typer.echo(f"{step_name} failed: {exc}", err=True)
            raise typer.Exit(1)

    # Local-only derive pass: turns the raw detail payloads we just stored into
    # the derived activity columns (training_load, HR zones, running dynamics
    # incl. L/R ground-contact balance). No Garmin calls. Skipped on dry-run
    # because no new payloads were written to derive from.
    typer.echo("\n[reprocess-derived]")
    if dry_run:
        typer.echo("[dry-run] skipping derive pass.")
    else:
        try:
            cmd_reprocess_activity_derived(env_file=env_file, db_path=db_path, log_level=log_level)
        except Exception as exc:
            typer.echo(f"reprocess-derived failed: {exc}", err=True)
            raise typer.Exit(1)

    typer.echo("\nAll done.")


# ---------------------------------------------------------------------------
# Query/export formatting helpers (no Garmin calls)
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_km(meters: float | None) -> str:
    return "-" if meters is None else f"{meters / 1000:.1f}km"


def _fmt_val(v: object, fmt: str = "") -> str:
    if v is None:
        return "-"
    return format(v, fmt) if fmt else str(v)


def _fmt_pct(part: float | None, total: float | None) -> str:
    if not part or not total:
        return "-"
    return f"{int(100 * part / total)}%"


def _fmt_hhmm(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}m"


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _get_conn_only(
    env_file: Optional[str],
    db_path: Optional[str],
    log_level: Optional[str],
):
    """Lightweight setup for read-only query commands — no GarminClient needed."""
    config = load_config(env_file)
    if log_level:
        config.log_level = log_level.upper()
    _setup_logging(config.log_level)
    effective_db = Path(db_path) if db_path else config.garmin_db_path
    conn = get_connection(effective_db)
    return conn


# ---------------------------------------------------------------------------
# query-recent-activities
# ---------------------------------------------------------------------------


@app.command("query-recent-activities")
def cmd_query_recent_activities(
    limit: Annotated[int, typer.Option("--limit")] = 20,
    activity_type: Annotated[Optional[str], typer.Option("--type", help="e.g. running, cycling")] = None,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """List recent activities. Filter by type and date range."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_recent_activities(
        conn, limit=limit, activity_type=activity_type,
        from_date=from_date, to_date=to_date,
    )
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No activities found.")
        return
    typer.echo(f"{'DATE':<20} {'TYPE':<20} {'DIST':>8} {'TIME':>9} {'HR':>4} {'LOAD':>5}  NAME")
    typer.echo("-" * 82)
    for r in rows:
        typer.echo(
            f"{str(r['start_time'])[:16]:<20} {str(r['type'] or ''):<20} "
            f"{_fmt_km(r['distance_m']):>8} {_fmt_duration(r['duration_s']):>9} "
            f"{_fmt_val(r['avg_hr']):>4} {_fmt_val(r.get('training_load'), '.0f'):>5}  {r['name'] or ''}"
        )


# ---------------------------------------------------------------------------
# query-activity
# ---------------------------------------------------------------------------


@app.command("query-activity")
def cmd_query_activity(
    activity_id: Annotated[str, typer.Argument(help="Garmin activity ID")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show details for a single activity including laps."""
    conn = _get_conn_only(env_file, db_path, log_level)
    activity = queries.get_activity(conn, activity_id)
    if activity is None:
        typer.echo(f"Activity {activity_id} not found.", err=True)
        raise typer.Exit(1)
    laps = queries.get_activity_laps(conn, activity_id)
    if json_output:
        _print_json({**activity, "laps": laps})
        return
    typer.echo(f"Activity:  {activity['activity_id']}")
    typer.echo(f"Name:      {activity['name']}")
    typer.echo(f"Type:      {activity['type']}")
    typer.echo(f"Date:      {activity['start_time']}")
    typer.echo(f"Distance:  {_fmt_km(activity['distance_m'])}")
    typer.echo(f"Duration:  {_fmt_duration(activity['duration_s'])}")
    typer.echo(f"Avg HR:    {_fmt_val(activity['avg_hr'])} bpm")
    typer.echo(f"Max HR:    {_fmt_val(activity['max_hr'])} bpm")
    typer.echo(f"Cadence:   {_fmt_val(activity['avg_cadence'])} spm")
    typer.echo(f"Elev gain: {_fmt_val(activity['elevation_gain_m'])} m")
    typer.echo(f"Calories:  {_fmt_val(activity['calories'])} kcal")
    typer.echo(f"VO2max:    {_fmt_val(activity['vo2max'])}")
    typer.echo(f"Aerobic TE:{_fmt_val(activity['aerobic_te'])}")
    if activity.get('training_load') is not None:
        typer.echo(f"Training load: {activity['training_load']:.1f}")
    if activity.get('body_battery_delta') is not None:
        typer.echo(f"Body battery:  {activity['body_battery_delta']:+d}")
    if activity.get('hr_zone_1_s') is not None:
        z1 = activity.get('hr_zone_1_s') or 0
        z2 = activity.get('hr_zone_2_s') or 0
        z3 = activity.get('hr_zone_3_s') or 0
        z4 = activity.get('hr_zone_4_s') or 0
        z5 = activity.get('hr_zone_5_s') or 0
        typer.echo(f"HR zones:      Z1={_fmt_hhmm(z1)} Z2={_fmt_hhmm(z2)} Z3={_fmt_hhmm(z3)} Z4={_fmt_hhmm(z4)} Z5={_fmt_hhmm(z5)}")
    if activity.get('ground_contact_ms') is not None:
        typer.echo(f"GCT:           {activity['ground_contact_ms']:.0f}ms  Balance L:{_fmt_val(activity.get('ground_contact_balance_left'), '.1f')}%")
        typer.echo(f"Vert osc:      {_fmt_val(activity.get('vertical_oscillation_cm'), '.1f')}cm  Ratio:{_fmt_val(activity.get('vertical_ratio_pct'), '.1f')}%")
        typer.echo(f"Stride:        {_fmt_val(activity.get('stride_length_cm'), '.0f')}cm")
    if activity.get('stamina_start') is not None:
        typer.echo(f"Stamina:       start={activity['stamina_start']:.0f}%  end={activity.get('stamina_end', 0):.0f}%  min={activity.get('stamina_min', 0):.0f}%")
    if laps:
        typer.echo(f"\nLaps ({len(laps)}):")
        typer.echo(f"  {'LAP':>3} {'DIST':>8} {'TIME':>9} {'HR':>4}  {'CADENCE':>7}")
        typer.echo("  " + "-" * 40)
        for lap in laps:
            typer.echo(
                f"  {_fmt_val(lap['lap_index']):>3} {_fmt_km(lap['distance_m']):>8} "
                f"{_fmt_duration(lap['duration_s']):>9} {_fmt_val(lap['avg_hr']):>4}  "
                f"{_fmt_val(lap['avg_cadence']):>7}"
            )


# ---------------------------------------------------------------------------
# query-activity-splits
# ---------------------------------------------------------------------------


@app.command("query-activity-splits")
def cmd_query_activity_splits(
    activity_id: Annotated[str, typer.Argument(help="Garmin activity ID")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show splits for a single activity."""
    conn = _get_conn_only(env_file, db_path, log_level)
    splits = queries.get_activity_splits(conn, activity_id)
    if json_output:
        _print_json(splits)
        return
    if not splits:
        typer.echo("No splits found.")
        return
    typer.echo(f"{'SPLIT':>5}  {'TYPE':<20} {'DIST':>8} {'TIME':>9} {'HR':>4}  {'SPEED':>7}")
    typer.echo("-" * 62)
    for s in splits:
        typer.echo(
            f"{_fmt_val(s['split_index']):>5}  {str(s.get('split_type') or ''):<20} "
            f"{_fmt_km(s['distance_m']):>8} {_fmt_duration(s['duration_s']):>9} "
            f"{_fmt_val(s['avg_hr']):>4}  {_fmt_val(s.get('avg_speed_mps'), '.2f'):>7}"
        )


# ---------------------------------------------------------------------------
# query-weekly-running-volume
# ---------------------------------------------------------------------------


@app.command("query-weekly-running-volume")
def cmd_query_weekly_running_volume(
    weeks: Annotated[int, typer.Option("--weeks")] = 12,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show weekly running volume (distance, time, elevation) for recent weeks."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_weekly_running_volume(conn, weeks=weeks)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No running data found.")
        return
    typer.echo(f"{'WEEK':>10} {'RUNS':>5} {'DISTANCE':>10} {'TIME':>10} {'ELEV':>7} {'AVG HR':>7}")
    typer.echo("-" * 57)
    for r in rows:
        typer.echo(
            f"{r['week_start']:>10} {_fmt_val(r['run_count']):>5} "
            f"{_fmt_km(r['total_distance_m']):>10} {_fmt_duration(r['total_duration_s']):>10} "
            f"{_fmt_val(r['total_elevation_m'], '.0f'):>7} {_fmt_val(r['avg_hr'], '.0f'):>7}"
        )


# ---------------------------------------------------------------------------
# query-monthly-running-volume
# ---------------------------------------------------------------------------


@app.command("query-monthly-running-volume")
def cmd_query_monthly_running_volume(
    months: Annotated[int, typer.Option("--months")] = 12,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show monthly running volume for recent months."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_monthly_running_volume(conn, months=months)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No running data found.")
        return
    typer.echo(f"{'MONTH':>8} {'RUNS':>5} {'DISTANCE':>10} {'TIME':>10} {'ELEV':>7} {'AVG HR':>7}")
    typer.echo("-" * 55)
    for r in rows:
        typer.echo(
            f"{r['month']:>8} {_fmt_val(r['run_count']):>5} "
            f"{_fmt_km(r['total_distance_m']):>10} {_fmt_duration(r['total_duration_s']):>10} "
            f"{_fmt_val(r['total_elevation_m'], '.0f'):>7} {_fmt_val(r['avg_hr'], '.0f'):>7}"
        )


# ---------------------------------------------------------------------------
# query-intensity-distribution
# ---------------------------------------------------------------------------


@app.command("query-intensity-distribution")
def cmd_query_intensity_distribution(
    weeks: Annotated[int, typer.Option("--weeks")] = 12,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show HR zone distribution per week for running activities."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_intensity_distribution(conn, weeks=weeks)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No intensity data found.")
        return
    typer.echo(f"{'WEEK':>10} {'RUNS':>5} {'Z1':>7} {'Z2':>7} {'Z3':>7} {'Z4':>7} {'Z5':>7} {'TOTAL':>8}")
    typer.echo("-" * 65)
    for r in rows:
        typer.echo(
            f"{r['week_start']:>10} {_fmt_val(r['run_count']):>5} "
            f"{_fmt_hhmm(r['zone_1_s']):>7} {_fmt_hhmm(r['zone_2_s']):>7} "
            f"{_fmt_hhmm(r['zone_3_s']):>7} {_fmt_hhmm(r['zone_4_s']):>7} "
            f"{_fmt_hhmm(r['zone_5_s']):>7} {_fmt_hhmm(r['total_zone_s']):>8}"
        )


# ---------------------------------------------------------------------------
# query-running-dynamics
# ---------------------------------------------------------------------------


@app.command("query-running-dynamics")
def cmd_query_running_dynamics(
    days: Annotated[int, typer.Option("--days")] = 90,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show running dynamics (GCT, balance, vertical oscillation, stride) for recent runs."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_running_dynamics(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No running dynamics data found.")
        return
    typer.echo(f"{'DATE':<12} {'DIST':>6} {'GCT':>5} {'BAL':>5} {'VO':>5} {'VR':>5} {'STRIDE':>7} {'CAD':>5} {'HR':>4}")
    typer.echo("-" * 63)
    for r in rows:
        typer.echo(
            f"{str(r['date']):<12} {_fmt_km(r['distance_m']):>6} "
            f"{_fmt_val(r['ground_contact_ms'], '.0f'):>5} "
            f"{_fmt_val(r['ground_contact_balance_left'], '.1f'):>5} "
            f"{_fmt_val(r['vertical_oscillation_cm'], '.1f'):>5} "
            f"{_fmt_val(r['vertical_ratio_pct'], '.1f'):>5} "
            f"{_fmt_val(r['stride_length_cm'], '.0f'):>7} "
            f"{_fmt_val(r['avg_cadence'], '.0f'):>5} "
            f"{_fmt_val(r['avg_hr']):>4}"
        )


# ---------------------------------------------------------------------------
# query-sleep-trend
# ---------------------------------------------------------------------------


@app.command("query-sleep-trend")
def cmd_query_sleep_trend(
    days: Annotated[int, typer.Option("--days")] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show sleep duration, stages and score for recent days."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_sleep_trend(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No sleep data found.")
        return
    typer.echo(f"{'DATE':<12} {'TOTAL':>7} {'DEEP':>7} {'REM':>7} {'AWAKE':>6} {'SCORE':>6} {'SPO2':>5}")
    typer.echo("-" * 58)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_hhmm(r['total_sleep_seconds']):>7} "
            f"{_fmt_hhmm(r['deep_sleep_seconds']):>7} {_fmt_hhmm(r['rem_sleep_seconds']):>7} "
            f"{_fmt_hhmm(r['awake_seconds']):>6} {_fmt_val(r['sleep_score']):>6} "
            f"{_fmt_val(r['avg_spo2'], '.1f'):>5}"
        )


# ---------------------------------------------------------------------------
# query-hrv-trend
# ---------------------------------------------------------------------------


@app.command("query-hrv-trend")
def cmd_query_hrv_trend(
    days: Annotated[int, typer.Option("--days")] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show HRV (last night, weekly avg, status) for recent days."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_hrv_trend(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No HRV data found.")
        return
    typer.echo(f"{'DATE':<12} {'LAST NIGHT':>11} {'WEEKLY AVG':>11} {'STATUS'}")
    typer.echo("-" * 50)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['last_night_avg']):>11} "
            f"{_fmt_val(r['weekly_avg']):>11}  {r['status'] or '-'}"
        )


# ---------------------------------------------------------------------------
# query-resting-hr-trend
# ---------------------------------------------------------------------------


@app.command("query-resting-hr-trend")
def cmd_query_resting_hr_trend(
    days: Annotated[int, typer.Option("--days")] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show resting heart rate trend for recent days."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_resting_hr_trend(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No heart rate data found.")
        return
    typer.echo(f"{'DATE':<12} {'RESTING':>8} {'MAX':>6} {'MIN':>6}")
    typer.echo("-" * 36)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['resting_hr']):>8} "
            f"{_fmt_val(r['max_hr']):>6} {_fmt_val(r['min_hr']):>6}"
        )


# ---------------------------------------------------------------------------
# query-stress-trend
# ---------------------------------------------------------------------------


@app.command("query-stress-trend")
def cmd_query_stress_trend(
    days: Annotated[int, typer.Option("--days")] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show daily stress levels and duration breakdown for recent days."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_stress_trend(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No stress data found.")
        return
    typer.echo(f"{'DATE':<12} {'AVG':>5} {'MAX':>5} {'HIGH%':>6}")
    typer.echo("-" * 32)
    for r in rows:
        total = r.get("stress_duration_seconds") or 0
        high = r.get("high_stress_duration_seconds") or 0
        high_pct = f"{int(100 * high / total)}%" if total else "-"
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['avg_stress_level']):>5} "
            f"{_fmt_val(r['max_stress_level']):>5} {high_pct:>6}"
        )


# ---------------------------------------------------------------------------
# query-body-battery-trend
# ---------------------------------------------------------------------------


@app.command("query-body-battery-trend")
def cmd_query_body_battery_trend(
    days: Annotated[int, typer.Option("--days")] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show body battery (charged, drained, end value) for recent days."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_body_battery_trend(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No body battery data found.")
        return
    typer.echo(f"{'DATE':<12} {'END':>5} {'CHARGED':>8} {'DRAINED':>8} {'START':>6}")
    typer.echo("-" * 44)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['ending_value']):>5} "
            f"{_fmt_val(r['charged']):>8} {_fmt_val(r['drained']):>8} "
            f"{_fmt_val(r['starting_value']):>6}"
        )


# ---------------------------------------------------------------------------
# query-training-vs-sleep
# ---------------------------------------------------------------------------


@app.command("query-training-vs-sleep")
def cmd_query_training_vs_sleep(
    days: Annotated[int, typer.Option("--days")] = 90,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show running load vs sleep quality for recent days (days with sleep data)."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_training_vs_sleep(conn, days=days)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data found.")
        return
    typer.echo(f"{'DATE':<12} {'SLEEP':>7} {'SCORE':>6} {'RUNS':>5} {'DIST':>8} {'HR':>5}")
    typer.echo("-" * 50)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_hhmm(r['total_sleep_seconds']):>7} "
            f"{_fmt_val(r['sleep_score']):>6} {_fmt_val(r['run_count']):>5} "
            f"{_fmt_km(r['total_distance_m']):>8} {_fmt_val(r['run_avg_hr']):>5}"
        )


# ---------------------------------------------------------------------------
# Performance / training metric query commands (Phase 5b)
# ---------------------------------------------------------------------------


def _fmt_time(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@app.command("query-lactate-threshold")
def cmd_query_lactate_threshold(
    days: Annotated[int, typer.Option("--days")] = 365,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show lactate threshold measurements (HR, speed, power) over time."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_lactate_threshold_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'SERIES':<10} {'HR':>4} {'SPEED':>7} {'POWER':>6}")
    typer.echo("-" * 44)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {str(r['series'] or ''):<10} "
            f"{_fmt_val(r['threshold_hr']):>4} "
            f"{_fmt_val(r['threshold_speed_value'], '.3f'):>7} "
            f"{_fmt_val(r['threshold_power_w'], '.0f'):>6}"
        )


@app.command("query-race-predictions")
def cmd_query_race_predictions(
    days: Annotated[int, typer.Option("--days")] = 365,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show race time predictions (5K, 10K, half, full) over time."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_race_predictions_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'5K':>7} {'10K':>8} {'HALF':>9} {'FULL':>9}")
    typer.echo("-" * 50)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} "
            f"{_fmt_time(r['race_5k_s']):>7} "
            f"{_fmt_time(r['race_10k_s']):>8} "
            f"{_fmt_time(r['race_half_s']):>9} "
            f"{_fmt_time(r['race_full_s']):>9}"
        )


@app.command("query-endurance-score")
def cmd_query_endurance_score(
    days: Annotated[int, typer.Option("--days")] = 365,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show endurance score and classification over time."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_endurance_score_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'SCORE':>6} {'CLASS':>6}")
    typer.echo("-" * 28)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['score']):>6} {_fmt_val(r['classification']):>6}"
        )


@app.command("query-hill-score")
def cmd_query_hill_score(
    days: Annotated[int, typer.Option("--days")] = 365,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show hill score (overall, strength, endurance, classification) over time."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_hill_score_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'OVERALL':>8} {'STRENGTH':>9} {'ENDURANCE':>10} {'CLASS':>6}")
    typer.echo("-" * 50)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['overall_score']):>8} "
            f"{_fmt_val(r['strength_score']):>9} "
            f"{_fmt_val(r['hill_endurance_score']):>10} "
            f"{_fmt_val(r['classification']):>6}"
        )


@app.command("query-training-status")
def cmd_query_training_status(
    days: Annotated[int, typer.Option("--days")] = 90,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show training status (VO2max, status phrase, acute/chronic load, ACWR) over time."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_training_status_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'VO2MAX':>7} {'STATUS':<20} {'ACUTE':>6} {'CHRONIC':>8} {'ACWR':>6}")
    typer.echo("-" * 65)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['vo2max'], '.1f'):>7} "
            f"{str(r['training_status_phrase'] or ''):<20} "
            f"{_fmt_val(r['acute_load']):>6} {_fmt_val(r['chronic_load']):>8} "
            f"{_fmt_val(r['acwr'], '.2f'):>6}"
        )


@app.command("query-training-readiness")
def cmd_query_training_readiness(
    days: Annotated[int, typer.Option("--days")] = 90,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show daily training readiness (score, level, recovery time, feedback) over time."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_training_readiness_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'SCORE':>6} {'LEVEL':<8} {'REC_MIN':>8} {'MORN':>6} {'FEEDBACK'}")
    typer.echo("-" * 60)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['score']):>6} "
            f"{str(r['level'] or ''):<8} "
            f"{_fmt_val(r['recovery_time_min']):>8} "
            f"{_fmt_val(r['morning_readiness_score']):>6}  "
            f"{r['feedback_short'] or '-'}"
        )


@app.command("query-vo2max-trend")
def cmd_query_vo2max_trend(
    days: Annotated[int, typer.Option("--days")] = 180,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show VO2max and fitness age trend (from max_metrics, falling back to training_status)."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_vo2max_trend(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'VO2MAX':>7} {'PRECISE':>8} {'FIT_AGE':>8} {'ACHIEVABLE':>10}")
    typer.echo("-" * 53)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['vo2max'], '.1f'):>7} "
            f"{_fmt_val(r['vo2max_precise'], '.1f'):>8} "
            f"{_fmt_val(r.get('fitness_age'), '.1f'):>8} "
            f"{_fmt_val(r.get('achievable_fitness_age'), '.1f'):>10}"
        )


@app.command("query-performance-summary")
def cmd_query_performance_summary(
    days: Annotated[int, typer.Option("--days")] = 90,
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Show combined performance summary: VO2max, training status, readiness, endurance score per date."""
    conn = _get_conn_only(env_file, db_path, log_level)
    rows = queries.get_performance_summary(conn, days=days, from_date=from_date, to_date=to_date)
    if json_output:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No data for requested range.")
        return
    typer.echo(f"{'DATE':<12} {'VO2MAX':>7} {'STATUS':<20} {'ACUTE':>6} {'ACWR':>6} {'READY':>6} {'MORN':>6} {'ENDURANCE':>10}")
    typer.echo("-" * 85)
    for r in rows:
        typer.echo(
            f"{r['date']:<12} {_fmt_val(r['vo2max'], '.1f'):>7} "
            f"{str(r['training_status_phrase'] or ''):<20} "
            f"{_fmt_val(r['acute_load']):>6} {_fmt_val(r['acwr'], '.2f'):>6} "
            f"{_fmt_val(r['readiness_score']):>6} {_fmt_val(r.get('morning_readiness_score')):>6} "
            f"{_fmt_val(r['endurance_score']):>10}"
        )


# ---------------------------------------------------------------------------
# reprocess-activity-derived
# ---------------------------------------------------------------------------


@app.command("reprocess-activity-derived")
def cmd_reprocess_activity_derived(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Derive training_load, HR zones, running dynamics etc. from existing raw_payload. No Garmin calls."""
    conn = _get_conn_only(env_file, db_path, log_level)

    summaries = conn.execute(
        "SELECT garmin_id, payload_json, id FROM raw_payload WHERE data_type='activity_summary'"
    ).fetchall()
    detail_rows = conn.execute(
        "SELECT garmin_id, payload_json, id FROM raw_payload WHERE data_type='activity_detail'"
    ).fetchall()
    detail_map = {r["garmin_id"]: r for r in detail_rows}

    processed = updated = splits_written = skipped = 0
    for s in summaries:
        activity_id = s["garmin_id"]
        summary_raw = _json.loads(s["payload_json"])
        detail_row = detail_map.get(activity_id)
        detail_raw = _json.loads(detail_row["payload_json"]) if detail_row else None
        raw_id = detail_row["id"] if detail_row else s["id"]

        derived = normalise.normalise_activity_derived(summary_raw, detail_raw)
        if repo.update_activity_derived(conn, activity_id, derived):
            updated += 1
        else:
            skipped += 1

        split_rows = normalise.normalise_activity_splits(detail_raw, summary_raw, activity_id, raw_id)
        if split_rows:
            repo.replace_activity_splits(conn, activity_id, split_rows)
            splits_written += len(split_rows)
        processed += 1

    typer.echo(f"Activities processed: {processed}  updated: {updated}  skipped: {skipped}")
    typer.echo(f"Splits written: {splits_written}")


# ---------------------------------------------------------------------------
# reprocess-health-derived
# ---------------------------------------------------------------------------


@app.command("reprocess-health-derived")
def cmd_reprocess_health_derived(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Derive SpO2, body battery detail etc. from existing daily_summary raw_payload. No Garmin calls."""
    conn = _get_conn_only(env_file, db_path, log_level)

    rows = conn.execute(
        "SELECT date, payload_json FROM raw_payload WHERE data_type='daily_summary'"
    ).fetchall()

    processed = updated = skipped = 0
    for r in rows:
        raw = _json.loads(r["payload_json"])
        derived = normalise.normalise_daily_summary_derived(raw)
        if repo.update_daily_summary_derived(conn, r["date"], derived):
            updated += 1
        else:
            skipped += 1
        processed += 1

    typer.echo(f"Daily summaries processed: {processed}  updated: {updated}  skipped: {skipped}")


# ---------------------------------------------------------------------------
# reprocess-sleep
# ---------------------------------------------------------------------------


@app.command("reprocess-sleep")
def cmd_reprocess_sleep(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Re-normalise sleep records from raw_payload. Fixes sleep_score and avg_spo2. No Garmin calls."""
    conn = _get_conn_only(env_file, db_path, log_level)

    rows = conn.execute(
        "SELECT rp.id, rp.date, rp.payload_json FROM raw_payload rp WHERE rp.data_type='sleep'"
    ).fetchall()

    processed = updated = 0
    for r in rows:
        raw = _json.loads(r["payload_json"])
        row = normalise.normalise_sleep(raw, r["date"], r["id"])
        if row:
            repo.upsert_sleep(conn, row)
            updated += 1
        processed += 1

    typer.echo(f"Sleep records processed: {processed}  updated: {updated}")


# ---------------------------------------------------------------------------
# reprocess-hrv
# ---------------------------------------------------------------------------


@app.command("reprocess-hrv")
def cmd_reprocess_hrv(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Re-normalise HRV records from raw_payload. Fixes last_night_avg. No Garmin calls."""
    conn = _get_conn_only(env_file, db_path, log_level)

    rows = conn.execute(
        "SELECT rp.id, rp.date, rp.payload_json FROM raw_payload rp WHERE rp.data_type='hrv'"
    ).fetchall()

    processed = updated = 0
    for r in rows:
        raw = _json.loads(r["payload_json"])
        row = normalise.normalise_hrv(raw, r["date"], r["id"])
        if row:
            repo.upsert_hrv(conn, row)
            updated += 1
        processed += 1

    typer.echo(f"HRV records processed: {processed}  updated: {updated}")


# ---------------------------------------------------------------------------
# reprocess-performance-derived
# ---------------------------------------------------------------------------


@app.command("reprocess-performance-derived")
def cmd_reprocess_performance_derived(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Re-normalise all performance tables from existing raw_payload. No Garmin calls."""
    conn = _get_conn_only(env_file, db_path, log_level)
    lt_count = rp_count = es_count = hs_count = ts_count = tr_count = mm_count = fa_count = 0

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='lactate_threshold'").fetchall():
        raw = _json.loads(row["payload_json"])
        for r in normalise.normalise_lactate_threshold(raw, row["id"]):
            repo.upsert_lactate_threshold(conn, r)
            lt_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='race_predictions'").fetchall():
        entries = _json.loads(row["payload_json"])
        if isinstance(entries, list):
            for entry in entries:
                r = normalise.normalise_race_prediction(entry, row["id"])
                if r:
                    repo.upsert_race_prediction(conn, r)
                    rp_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='endurance_score'").fetchall():
        raw = _json.loads(row["payload_json"])
        for r in normalise.normalise_endurance_score(raw, row["id"]):
            repo.upsert_endurance_score(conn, r)
            es_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='hill_score'").fetchall():
        raw = _json.loads(row["payload_json"])
        for entry in (raw.get("hillScoreDTOList") or []):
            r = normalise.normalise_hill_score_entry(entry, row["id"])
            if r:
                repo.upsert_hill_score(conn, r)
                hs_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='training_status'").fetchall():
        raw = _json.loads(row["payload_json"])
        r = normalise.normalise_training_status(raw, row["date"], row["id"])
        if r:
            repo.upsert_training_status(conn, r)
            ts_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='training_readiness'").fetchall():
        raw_list = _json.loads(row["payload_json"])
        if isinstance(raw_list, list):
            r = normalise.normalise_training_readiness(raw_list, row["date"], row["id"])
            if r:
                repo.upsert_training_readiness(conn, r)
                tr_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='max_metrics'").fetchall():
        raw_list = _json.loads(row["payload_json"])
        if isinstance(raw_list, list):
            r = normalise.normalise_max_metrics(raw_list, row["date"], row["id"])
            if r:
                repo.upsert_max_metrics(conn, r)
                mm_count += 1

    for row in conn.execute("SELECT id, date, payload_json FROM raw_payload WHERE data_type='fitness_age'").fetchall():
        raw = _json.loads(row["payload_json"])
        r = normalise.normalise_fitness_age(raw, row["date"], row["id"])
        if r:
            repo.upsert_fitness_age(conn, r)
            fa_count += 1

    typer.echo(
        f"Reprocessed: lactate={lt_count} race_predictions={rp_count} endurance={es_count} hill={hs_count} "
        f"training_status={ts_count} training_readiness={tr_count} max_metrics={mm_count} fitness_age={fa_count}"
    )


# ---------------------------------------------------------------------------
# reprocess-all-derived
# ---------------------------------------------------------------------------


@app.command("reprocess-all-derived")
def cmd_reprocess_all_derived(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """Run all reprocess commands. No Garmin calls."""
    cmd_reprocess_activity_derived(env_file=env_file, db_path=db_path, log_level=log_level)
    cmd_reprocess_health_derived(env_file=env_file, db_path=db_path, log_level=log_level)
    cmd_reprocess_sleep(env_file=env_file, db_path=db_path, log_level=log_level)
    cmd_reprocess_hrv(env_file=env_file, db_path=db_path, log_level=log_level)
    cmd_reprocess_performance_derived(env_file=env_file, db_path=db_path, log_level=log_level)


# ---------------------------------------------------------------------------
# export-json
# ---------------------------------------------------------------------------


@app.command("export-json")
def cmd_export_json(
    output_dir: Annotated[str, typer.Option("--output-dir")] = "exports",
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Export normalised tables to JSON files in --output-dir.

    Writes activities.json and daily_health.json. Applies optional date filters.
    Raw payloads are not exported.
    """
    conn = _get_conn_only(env_file, db_path, log_level)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        n_act = exporter.export_activities_json(conn, out / "activities.json", from_date, to_date)
        typer.echo(f"Wrote {n_act:>6} rows  ->  {(out / 'activities.json').resolve()}")

        n_hlth = exporter.export_health_json(conn, out / "daily_health.json", from_date, to_date)
        typer.echo(f"Wrote {n_hlth:>6} rows  ->  {(out / 'daily_health.json').resolve()}")
    except Exception as exc:
        typer.echo(f"Export failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# export-csv
# ---------------------------------------------------------------------------


@app.command("export-csv")
def cmd_export_csv(
    output_dir: Annotated[str, typer.Option("--output-dir")] = "exports",
    from_date: Annotated[Optional[str], typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="YYYY-MM-DD")] = None,
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
) -> None:
    """
    Export normalised tables to CSV files in --output-dir.

    Writes activities.csv and daily_health.csv. Applies optional date filters.
    Raw payloads are not exported.
    """
    conn = _get_conn_only(env_file, db_path, log_level)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        n_act = exporter.export_activities_csv(conn, out / "activities.csv", from_date, to_date)
        typer.echo(f"Wrote {n_act:>6} rows  ->  {(out / 'activities.csv').resolve()}")

        n_hlth = exporter.export_health_csv(conn, out / "daily_health.csv", from_date, to_date)
        typer.echo(f"Wrote {n_hlth:>6} rows  ->  {(out / 'daily_health.csv').resolve()}")
    except Exception as exc:
        typer.echo(f"Export failed: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command("status")
def cmd_status(
    env_file: Annotated[Optional[str], typer.Option("--config")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db")] = None,
    log_level: Annotated[Optional[str], typer.Option("--log-level")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show sync state: activity counts, cursor positions, recent runs."""
    config, conn = _get_config_and_conn(env_file, db_path, log_level)

    effective_db = Path(db_path) if db_path else config.garmin_db_path

    try:
        status = repo.get_sync_status(conn)
    except Exception as exc:
        typer.echo(
            f"Could not read status (is the database initialised? Run init-db first): {exc}",
            err=True,
        )
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(status, indent=2))
        return

    typer.echo(f"Database:              {effective_db.resolve()}")
    typer.echo(f"Activities:            {status['activity_count']}")
    typer.echo(f"Activity details:      {status['activity_detail_count']}")
    typer.echo(f"Activity laps:         {status['activity_lap_count']}")
    typer.echo(f"Activity splits:       {status['activity_split_count']}")
    typer.echo(f"Daily summaries:       {status['daily_summary_count']}")
    typer.echo(f"Sleep records:         {status['sleep_count']}")
    typer.echo(f"HRV records:           {status['hrv_count']}")
    typer.echo(f"Stress records:        {status['stress_count']}")
    typer.echo(f"Body battery records:  {status['body_battery_count']}")
    typer.echo(f"Heart rate records:    {status['heart_rate_count']}")
    typer.echo(f"Lactate threshold:     {status['lactate_threshold_count']}")
    typer.echo(f"Race predictions:      {status['race_predictions_count']}")
    typer.echo(f"Endurance score:       {status['endurance_score_count']}")
    typer.echo(f"Hill score:            {status['hill_score_count']}")
    typer.echo(f"Training status:       {status['training_status_count']}")
    typer.echo(f"Training readiness:    {status['training_readiness_count']}")
    typer.echo(f"Max metrics:           {status['max_metrics_count']}")
    typer.echo(f"Fitness age:           {status['fitness_age_count']}")
    typer.echo(f"Raw payloads:          {status['raw_payload_count']}")

    if status["cursors"]:
        typer.echo("\nSync cursors:")
        for c in status["cursors"]:
            parts = [f"  {c['data_type']:30s}"]
            if c["last_offset"] is not None:
                parts.append(f"offset={c['last_offset']}")
            if c["last_successful_activity_id"] is not None:
                parts.append(f"last_activity={c['last_successful_activity_id']}")
            if c["last_successful_date"] is not None:
                parts.append(f"last_date={c['last_successful_date']}")
            parts.append(f"updated={c['updated_at']}")
            typer.echo("  ".join(parts))
    else:
        typer.echo("\nNo sync cursors yet.")

    if status["recent_runs"]:
        typer.echo("\nRecent sync runs (newest first):")
        for r in status["recent_runs"]:
            error_str = f"  error={r['error']}" if r["error"] else ""
            typer.echo(
                f"  [{r['id']}] {r['command']:30s}  {r['status']:12s}  "
                f"started={r['started_at']}{error_str}"
            )
    else:
        typer.echo("\nNo sync runs yet.")


if __name__ == "__main__":
    app()
