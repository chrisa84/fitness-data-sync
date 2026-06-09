"""
MCP server for the Garmin Sync local data mirror.

Read-only guarantees:
  - SQLite is opened in URI mode=ro — writes are rejected at the driver level.
  - Never calls Garmin Connect.
  - Never writes to SQLite.
  - All tools delegate to garmin_sync.queries functions.

Run:
  python -m garmin_sync.mcp_server
  garmin-sync-mcp
"""

import sqlite3
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from garmin_sync import queries

mcp = FastMCP("garmin-sync")

# ---------------------------------------------------------------------------
# DB connection — lazy, read-only
# ---------------------------------------------------------------------------

_conn: sqlite3.Connection | None = None


def _open_ro_conn() -> sqlite3.Connection:
    """Open a read-only SQLite connection using URI mode=ro."""
    from garmin_sync.config import Config
    config = Config()
    db_path = config.garmin_db_path.expanduser().resolve()
    # Path.as_uri() produces file:///C:/... on Windows — safe for SQLite URI.
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_conn() -> sqlite3.Connection:
    """Return the cached read-only connection, opening it on first call."""
    global _conn
    if _conn is None:
        _conn = _open_ro_conn()
    return _conn


# ---------------------------------------------------------------------------
# Input validators — raise ValueError for bad input
# ---------------------------------------------------------------------------

_MAX_DAYS   = 3650
_MAX_WEEKS  = 520
_MAX_MONTHS = 120
_MAX_LIMIT  = 500


def _vdate(value: str, name: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")


def _vrange(from_date: str | None, to_date: str | None) -> None:
    if from_date is not None:
        _vdate(from_date, "from_date")
    if to_date is not None:
        _vdate(to_date, "to_date")
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError(
            f"from_date {from_date!r} must be <= to_date {to_date!r}"
        )


def _vdays(n: int, name: str = "days") -> None:
    if n < 1 or n > _MAX_DAYS:
        raise ValueError(f"{name} must be 1–{_MAX_DAYS}, got {n}")


def _vweeks(n: int) -> None:
    if n < 1 or n > _MAX_WEEKS:
        raise ValueError(f"weeks must be 1–{_MAX_WEEKS}, got {n}")


def _vmonths(n: int) -> None:
    if n < 1 or n > _MAX_MONTHS:
        raise ValueError(f"months must be 1–{_MAX_MONTHS}, got {n}")


def _vlimit(n: int) -> None:
    if n < 1 or n > _MAX_LIMIT:
        raise ValueError(f"limit must be 1–{_MAX_LIMIT}, got {n}")


def _vactivity_id(value: str) -> None:
    if not value or not value.strip():
        raise ValueError("activity_id must be non-empty")


# ---------------------------------------------------------------------------
# Activity tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_recent_activities(
    limit: int = 20,
    activity_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Recent activities, newest first. Filter by type (e.g. 'running'), date range, or count."""
    _vlimit(limit)
    _vrange(from_date, to_date)
    return queries.get_recent_activities(
        get_conn(), limit=limit, activity_type=activity_type,
        from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_activity(activity_id: str) -> dict[str, Any] | None:
    """One activity by Garmin ID including detail metadata. Returns null if not found."""
    _vactivity_id(activity_id)
    return queries.get_activity(get_conn(), activity_id)


@mcp.tool()
def get_activity_splits(activity_id: str) -> list[dict[str, Any]]:
    """Per-km/mile splits for an activity. Empty list if no splits stored."""
    _vactivity_id(activity_id)
    return queries.get_activity_splits(get_conn(), activity_id)


@mcp.tool()
def get_weekly_running_volume(weeks: int = 12) -> list[dict[str, Any]]:
    """Running volume per week (Monday-start): distance, duration, elevation, avg HR."""
    _vweeks(weeks)
    return queries.get_weekly_running_volume(get_conn(), weeks=weeks)


@mcp.tool()
def get_monthly_running_volume(months: int = 12) -> list[dict[str, Any]]:
    """Running volume per calendar month: distance, duration, elevation, avg HR."""
    _vmonths(months)
    return queries.get_monthly_running_volume(get_conn(), months=months)


# ---------------------------------------------------------------------------
# Health / wellness tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_daily_health_summary(
    days: int = 30,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Compact daily health summary: steps, resting HR, sleep, HRV, stress, body battery, SpO2."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_daily_health_summary(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_sleep_trend(days: int = 30) -> list[dict[str, Any]]:
    """Sleep data for the last N days: total duration, stages, score, SpO2, respiration."""
    _vdays(days)
    return queries.get_sleep_trend(get_conn(), days=days)


@mcp.tool()
def get_hrv_trend(days: int = 30) -> list[dict[str, Any]]:
    """HRV for the last N days: nightly avg, weekly avg, baseline range, status."""
    _vdays(days)
    return queries.get_hrv_trend(get_conn(), days=days)


@mcp.tool()
def get_resting_hr_trend(days: int = 30) -> list[dict[str, Any]]:
    """Resting heart rate and daily HR stats for the last N days."""
    _vdays(days)
    return queries.get_resting_hr_trend(get_conn(), days=days)


@mcp.tool()
def get_stress_trend(days: int = 30) -> list[dict[str, Any]]:
    """Stress levels for the last N days: average, peak, time in each category."""
    _vdays(days)
    return queries.get_stress_trend(get_conn(), days=days)


@mcp.tool()
def get_body_battery_trend(days: int = 30) -> list[dict[str, Any]]:
    """Body battery charged/drained/end value for the last N days."""
    _vdays(days)
    return queries.get_body_battery_trend(get_conn(), days=days)


@mcp.tool()
def get_training_vs_sleep(days: int = 90) -> list[dict[str, Any]]:
    """Per-day training load alongside sleep quality. Useful for recovery analysis."""
    _vdays(days)
    return queries.get_training_vs_sleep(get_conn(), days=days)


@mcp.tool()
def get_intensity_distribution(weeks: int = 12) -> list[dict[str, Any]]:
    """Weekly HR zone time distribution for running. Shows training polarisation."""
    _vweeks(weeks)
    return queries.get_intensity_distribution(get_conn(), weeks=weeks)


@mcp.tool()
def get_running_dynamics(days: int = 90) -> list[dict[str, Any]]:
    """Running form metrics: cadence, GCT, vertical oscillation, stride length, ratio."""
    _vdays(days)
    return queries.get_running_dynamics(get_conn(), days=days)


# ---------------------------------------------------------------------------
# Performance / training tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_training_status(
    days: int = 90,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """VO2max, training status phrase, acute/chronic load, ACWR per day."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_training_status_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_training_readiness(
    days: int = 90,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Readiness score, recovery time, HRV/sleep/stress factors, morning score per day."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_training_readiness_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_vo2max_trend(
    days: int = 180,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """VO2max, precise VO2max, fitness age, and achievable fitness age per day."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_vo2max_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_lactate_threshold(
    days: int = 365,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Lactate threshold heart rate and pace measurements."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_lactate_threshold_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_race_predictions(
    days: int = 365,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Predicted race times for 5K, 10K, half marathon, full marathon per day."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_race_predictions_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_endurance_score(
    days: int = 365,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Endurance score and classification per day."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_endurance_score_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_hill_score(
    days: int = 365,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Hill score: overall, strength component, and endurance component per day."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_hill_score_trend(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


@mcp.tool()
def get_performance_summary(
    days: int = 90,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Combined daily view: VO2max, training status, readiness, endurance score, fitness age."""
    _vdays(days)
    _vrange(from_date, to_date)
    return queries.get_performance_summary(
        get_conn(), days=days, from_date=from_date, to_date=to_date,
    )


# ---------------------------------------------------------------------------
# Status tool
# ---------------------------------------------------------------------------

@mcp.tool()
def get_database_status() -> dict[str, Any]:
    """Row counts for all tables, sync cursor positions, recent sync run history."""
    return queries.get_database_status(get_conn())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
