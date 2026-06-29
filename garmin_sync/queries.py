"""
Read-only query functions.

All functions:
  - take conn (sqlite3.Connection) as their first argument
  - return list[dict] or dict | None
  - never call Garmin Connect
  - never write to SQLite
  - are the intended integration boundary for future MCP tools

MCP tools should call these functions directly; they must not contain their
own SQL.
"""

import sqlite3
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _months_ago(months: int) -> str:
    import calendar as _cal
    today = date.today()
    year, month = today.year, today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, _cal.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


# ---------------------------------------------------------------------------
# Activity queries
# ---------------------------------------------------------------------------

def get_recent_activities(
    conn: sqlite3.Connection,
    limit: int | None = 20,
    activity_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """
    Return activities, newest first.

    :param limit: Max rows returned. None = no limit.
    :param activity_type: Exact match on activity.type (e.g. 'running').
    :param from_date: Include activities starting on or after YYYY-MM-DD.
    :param to_date: Include activities starting on or before YYYY-MM-DD (inclusive).
    """
    where: list[str] = []
    params: list = []

    if activity_type:
        where.append("a.type = ?")
        params.append(activity_type)
    if from_date:
        where.append("a.start_time >= ?")
        params.append(from_date)
    if to_date:
        where.append("a.start_time < date(?, '+1 day')")
        params.append(to_date)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""

    sql = f"""
        SELECT a.*, ad.has_splits, ad.has_laps, ad.sample_count
        FROM activity a
        LEFT JOIN activity_detail ad ON a.activity_id = ad.activity_id
        {where_clause}
        ORDER BY a.start_time DESC
        {limit_clause}
    """  # noqa: S608 — WHERE/LIMIT built from safe constants + int cast, params parameterised
    return _rows(conn, sql, tuple(params))


def get_activity(conn: sqlite3.Connection, activity_id: str) -> dict | None:
    """Return one activity with its detail metadata, or None if not found."""
    return _one(
        conn,
        """
        SELECT a.*, ad.has_splits, ad.has_laps, ad.sample_count
        FROM activity a
        LEFT JOIN activity_detail ad ON a.activity_id = ad.activity_id
        WHERE a.activity_id = ?
        """,
        (str(activity_id),),
    )


def get_activity_laps(conn: sqlite3.Connection, activity_id: str) -> list[dict]:
    """Return all laps for an activity ordered by lap_index."""
    return _rows(
        conn,
        "SELECT * FROM activity_lap WHERE activity_id = ? ORDER BY lap_index",
        (str(activity_id),),
    )


_RUN_FILTER = "%run%"


def get_weekly_running_volume(conn: sqlite3.Connection, weeks: int = 12) -> list[dict]:
    """
    Return running volume per week (Monday-start), newest first.

    Matches activity types containing 'run' (running, trail_running, virtual_run, etc.).
    :param weeks: Number of recent weeks to include.
    """
    from_date = _days_ago(weeks * 7)
    return _rows(
        conn,
        """
        SELECT
            date(start_time,
                 '-' || ((strftime('%w', start_time) + 6) % 7) || ' days'
            )                       AS week_start,
            COUNT(*)                AS run_count,
            SUM(distance_m)         AS total_distance_m,
            SUM(duration_s)         AS total_duration_s,
            SUM(elevation_gain_m)   AS total_elevation_m,
            AVG(avg_hr)             AS avg_hr
        FROM activity
        WHERE type LIKE ?
          AND start_time >= ?
        GROUP BY week_start
        ORDER BY week_start DESC
        LIMIT ?
        """,
        (_RUN_FILTER, from_date, weeks),
    )


def get_monthly_running_volume(conn: sqlite3.Connection, months: int = 12) -> list[dict]:
    """
    Return running volume per calendar month (YYYY-MM), newest first.

    :param months: Number of recent months to include.
    """
    from_date = _months_ago(months)
    return _rows(
        conn,
        """
        SELECT
            strftime('%Y-%m', start_time) AS month,
            COUNT(*)                      AS run_count,
            SUM(distance_m)               AS total_distance_m,
            SUM(duration_s)               AS total_duration_s,
            SUM(elevation_gain_m)         AS total_elevation_m,
            AVG(avg_hr)                   AS avg_hr
        FROM activity
        WHERE type LIKE ?
          AND start_time >= ?
        GROUP BY month
        ORDER BY month DESC
        LIMIT ?
        """,
        (_RUN_FILTER, from_date, months),
    )


# ---------------------------------------------------------------------------
# Health / wellness queries
# ---------------------------------------------------------------------------

def get_sleep_trend(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return sleep records for the last N days, oldest first."""
    return _rows(
        conn,
        """
        SELECT date, total_sleep_seconds, deep_sleep_seconds, light_sleep_seconds,
               rem_sleep_seconds, awake_seconds, sleep_score, avg_spo2, avg_respiration
        FROM sleep
        WHERE date >= ?
        ORDER BY date ASC
        """,
        (_days_ago(days),),
    )


def get_hrv_trend(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return HRV records for the last N days, oldest first."""
    return _rows(
        conn,
        """
        SELECT date, weekly_avg, last_night_avg, last_night_5min_high,
               baseline_low, baseline_high, status
        FROM hrv
        WHERE date >= ?
        ORDER BY date ASC
        """,
        (_days_ago(days),),
    )


def get_resting_hr_trend(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return daily resting HR for the last N days, oldest first."""
    return _rows(
        conn,
        """
        SELECT h.date, h.resting_hr, h.max_hr, h.min_hr,
               ds.resting_hr AS summary_resting_hr
        FROM heart_rate h
        LEFT JOIN daily_summary ds ON h.date = ds.date
        WHERE h.date >= ?
        ORDER BY h.date ASC
        """,
        (_days_ago(days),),
    )


def get_stress_trend(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return stress records for the last N days, oldest first."""
    return _rows(
        conn,
        """
        SELECT date, avg_stress_level, max_stress_level,
               stress_duration_seconds, rest_stress_duration_seconds,
               low_stress_duration_seconds, medium_stress_duration_seconds,
               high_stress_duration_seconds
        FROM stress
        WHERE date >= ?
        ORDER BY date ASC
        """,
        (_days_ago(days),),
    )


def get_body_battery_trend(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return body battery records for the last N days, oldest first."""
    return _rows(
        conn,
        """
        SELECT date, charged, drained, starting_value, ending_value
        FROM body_battery
        WHERE date >= ?
        ORDER BY date ASC
        """,
        (_days_ago(days),),
    )


def get_training_vs_sleep(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """
    Return per-date training load alongside sleep quality for the last N days.

    Left-joins sleep records with running activity aggregates on calendar date.
    Only dates that have a sleep record are returned. Dates with no runs show
    run_count=0 and None load values.
    """
    return _rows(
        conn,
        """
        SELECT
            s.date,
            s.total_sleep_seconds,
            s.sleep_score,
            s.deep_sleep_seconds,
            s.rem_sleep_seconds,
            COALESCE(r.run_count, 0) AS run_count,
            r.total_distance_m,
            r.total_duration_s,
            r.avg_hr AS run_avg_hr
        FROM sleep s
        LEFT JOIN (
            SELECT
                date(start_time)    AS run_date,
                COUNT(*)            AS run_count,
                SUM(distance_m)     AS total_distance_m,
                SUM(duration_s)     AS total_duration_s,
                AVG(avg_hr)         AS avg_hr
            FROM activity
            WHERE type LIKE '%run%'
            GROUP BY run_date
        ) r ON s.date = r.run_date
        WHERE s.date >= ?
        ORDER BY s.date ASC
        """,
        (_days_ago(days),),
    )


def get_activity_splits(conn: sqlite3.Connection, activity_id: str) -> list[dict]:
    """Return all splits for an activity ordered by split_index."""
    return _rows(
        conn,
        "SELECT * FROM activity_split WHERE activity_id = ? ORDER BY split_index",
        (str(activity_id),),
    )


def get_intensity_distribution(conn: sqlite3.Connection, weeks: int = 12) -> list[dict]:
    """
    Return HR zone time distribution per week for running activities, newest first.

    Only includes weeks where zone data exists.
    :param weeks: Number of recent weeks to include.
    """
    from_date = _days_ago(weeks * 7)
    return _rows(
        conn,
        """
        SELECT
            date(start_time, '-' || ((strftime('%w', start_time) + 6) % 7) || ' days') AS week_start,
            COUNT(*) AS run_count,
            SUM(hr_zone_1_s) AS zone_1_s,
            SUM(hr_zone_2_s) AS zone_2_s,
            SUM(hr_zone_3_s) AS zone_3_s,
            SUM(hr_zone_4_s) AS zone_4_s,
            SUM(hr_zone_5_s) AS zone_5_s,
            SUM(COALESCE(hr_zone_1_s,0)+COALESCE(hr_zone_2_s,0)+COALESCE(hr_zone_3_s,0)+COALESCE(hr_zone_4_s,0)+COALESCE(hr_zone_5_s,0)) AS total_zone_s
        FROM activity
        WHERE type LIKE '%run%'
          AND start_time >= ?
          AND hr_zone_1_s IS NOT NULL
        GROUP BY week_start
        ORDER BY week_start DESC
        LIMIT ?
        """,
        (from_date, weeks),
    )


def get_running_dynamics(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """
    Return running dynamics (GCT, vertical oscillation, stride length, etc.) for recent runs.

    Only includes runs where ground_contact_ms is populated.
    :param days: Number of recent days to include.
    """
    from_date = _days_ago(days)
    return _rows(
        conn,
        """
        SELECT
            date(start_time) AS date,
            start_time,
            name,
            distance_m,
            avg_cadence,
            ground_contact_ms,
            ground_contact_balance_left,
            vertical_oscillation_cm,
            vertical_ratio_pct,
            stride_length_cm,
            avg_hr
        FROM activity
        WHERE type LIKE '%run%'
          AND start_time >= ?
          AND ground_contact_ms IS NOT NULL
        ORDER BY start_time DESC
        """,
        (from_date,),
    )


def get_daily_health(
    conn: sqlite3.Connection,
    from_date: str,
    to_date: str,
) -> list[dict]:
    """
    Return one row per calendar date in [from_date, to_date] joining all six
    health tables (daily_summary, sleep, hrv, stress, body_battery, heart_rate).

    Generates a complete date series; missing data for a table on a given date
    produces None fields, not missing rows. This is the recommended function for
    any MCP tool that needs a unified daily health view.
    """
    return _rows(
        conn,
        """
        WITH RECURSIVE dates(date) AS (
            SELECT ?
            UNION ALL
            SELECT date(date, '+1 day') FROM dates WHERE date < ?
        )
        SELECT
            d.date,
            ds.total_steps,
            ds.step_goal,
            ds.active_calories,
            ds.resting_calories,
            ds.total_calories,
            ds.resting_hr            AS summary_resting_hr,
            ds.avg_stress_level      AS summary_avg_stress,
            ds.moderate_intensity_minutes,
            ds.vigorous_intensity_minutes,
            s.total_sleep_seconds,
            s.deep_sleep_seconds,
            s.rem_sleep_seconds,
            s.sleep_score,
            s.avg_spo2,
            h.last_night_avg         AS hrv_last_night,
            h.weekly_avg             AS hrv_weekly_avg,
            h.status                 AS hrv_status,
            st.avg_stress_level,
            st.high_stress_duration_seconds,
            bb.charged               AS battery_charged,
            bb.drained               AS battery_drained,
            bb.ending_value          AS battery_end,
            hr.resting_hr,
            hr.max_hr,
            hr.min_hr
        FROM dates d
        LEFT JOIN daily_summary  ds ON d.date = ds.date
        LEFT JOIN sleep          s  ON d.date = s.date
        LEFT JOIN hrv            h  ON d.date = h.date
        LEFT JOIN stress         st ON d.date = st.date
        LEFT JOIN body_battery   bb ON d.date = bb.date
        LEFT JOIN heart_rate     hr ON d.date = hr.date
        ORDER BY d.date ASC
        """,
        (from_date, to_date),
    )


# ---------------------------------------------------------------------------
# Performance / training metrics (Phase 5b)
# ---------------------------------------------------------------------------


def get_lactate_threshold_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    since = from_date or _days_ago(days or 365)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        "SELECT date, threshold_hr, threshold_speed_value, threshold_power_w, series "
        "FROM lactate_threshold WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until))


def get_race_predictions_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    since = from_date or _days_ago(days or 365)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        "SELECT date, race_5k_s, race_10k_s, race_half_s, race_full_s "
        "FROM race_predictions WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until))


def get_endurance_score_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    since = from_date or _days_ago(days or 365)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        "SELECT date, score, classification FROM endurance_score "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until))


def get_hill_score_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    since = from_date or _days_ago(days or 365)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        "SELECT date, overall_score, strength_score, hill_endurance_score, classification "
        "FROM hill_score WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until))


def get_training_status_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    since = from_date or _days_ago(days or 90)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        "SELECT date, vo2max, vo2max_precise, training_status_code, training_status_phrase, "
        "load_aerobic_low, load_aerobic_high, load_anaerobic, load_feedback, "
        "acute_load, chronic_load, acwr "
        "FROM training_status WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until))


def get_training_readiness_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    since = from_date or _days_ago(days or 90)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        "SELECT date, score, level, recovery_time_min, acwr_factor_pct, "
        "acute_load, hrv_factor_pct, sleep_factor_pct, stress_factor_pct, feedback_short, "
        "morning_readiness_score, morning_readiness_level, morning_recovery_time_min "
        "FROM training_readiness WHERE date >= ? AND date <= ? ORDER BY date",
        (since, until))


def get_vo2max_trend(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """VO2max and fitness age from max_metrics (preferred) or training_status."""
    since = from_date or _days_ago(days or 180)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        """
        SELECT
            d.date,
            COALESCE(mm.vo2max, ts.vo2max)                 AS vo2max,
            COALESCE(mm.vo2max_precise, ts.vo2max_precise) AS vo2max_precise,
            fa.fitness_age,
            fa.achievable_fitness_age
        FROM (
            SELECT date FROM max_metrics      WHERE date >= ? AND date <= ? AND vo2max IS NOT NULL
            UNION
            SELECT date FROM training_status  WHERE date >= ? AND date <= ? AND vo2max IS NOT NULL
        ) d
        LEFT JOIN max_metrics     mm ON mm.date = d.date
        LEFT JOIN training_status ts ON ts.date = d.date
        LEFT JOIN fitness_age     fa ON fa.date = d.date
        WHERE COALESCE(mm.vo2max, ts.vo2max) IS NOT NULL
        ORDER BY d.date
        """,
        (since, until, since, until))


def get_performance_summary(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Combined per-date view: training status, readiness, endurance score, VO2max."""
    since = from_date or _days_ago(days or 90)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        """
        SELECT
            d.date,
            COALESCE(mm.vo2max, ts.vo2max)  AS vo2max,
            ts.training_status_phrase,
            ts.acute_load,
            ts.acwr,
            tr.score                        AS readiness_score,
            tr.level                        AS readiness_level,
            tr.morning_readiness_score,
            tr.recovery_time_min,
            tr.feedback_short,
            es.score                        AS endurance_score,
            fa.fitness_age
        FROM (
            SELECT date FROM training_status    WHERE date >= ? AND date <= ?
            UNION
            SELECT date FROM training_readiness WHERE date >= ? AND date <= ?
            UNION
            SELECT date FROM endurance_score    WHERE date >= ? AND date <= ?
            UNION
            SELECT date FROM max_metrics        WHERE date >= ? AND date <= ?
            UNION
            SELECT date FROM fitness_age        WHERE date >= ? AND date <= ?
        ) d
        LEFT JOIN training_status    ts ON ts.date = d.date
        LEFT JOIN training_readiness tr ON tr.date = d.date
        LEFT JOIN endurance_score    es ON es.date = d.date
        LEFT JOIN max_metrics        mm ON mm.date = d.date
        LEFT JOIN fitness_age        fa ON fa.date = d.date
        ORDER BY d.date
        """,
        (since, until, since, until, since, until, since, until, since, until))


def get_daily_health_summary(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Compact per-day health summary joining all six health tables."""
    since = from_date or _days_ago(days or 30)
    until = to_date or date.today().isoformat()
    return _rows(conn,
        """
        WITH RECURSIVE dates(date) AS (
            SELECT ?
            UNION ALL
            SELECT date(date, '+1 day') FROM dates WHERE date < ?
        )
        SELECT
            d.date,
            ds.total_steps,
            hr.resting_hr,
            s.sleep_score,
            s.total_sleep_seconds,
            s.avg_spo2,
            h.last_night_avg    AS hrv_last_night,
            h.status            AS hrv_status,
            st.avg_stress_level,
            bb.ending_value     AS body_battery_end,
            bb.charged          AS body_battery_charged
        FROM dates d
        LEFT JOIN daily_summary  ds ON d.date = ds.date
        LEFT JOIN heart_rate     hr ON d.date = hr.date
        LEFT JOIN sleep          s  ON d.date = s.date
        LEFT JOIN hrv            h  ON d.date = h.date
        LEFT JOIN stress         st ON d.date = st.date
        LEFT JOIN body_battery   bb ON d.date = bb.date
        ORDER BY d.date ASC
        """,
        (since, until))


def get_activity_samples(
    conn: sqlite3.Connection,
    activity_id: str,
    fields: list[str] | None = None,
) -> list[dict]:
    """
    Return per-sample time-series rows for an activity, ordered by sample_index.

    fields: optional list of column names to return (e.g. ["timestamp_utc", "heart_rate", "speed_mps"]).
    Defaults to all columns except raw_payload_id.
    """
    allowed = {
        "sample_index", "timestamp_utc", "distance_m", "heart_rate",
        "speed_mps", "cadence", "power_w", "altitude_m", "lat", "lon",
        "respiration_rate", "ground_contact_ms", "vertical_oscillation_cm",
        "vertical_ratio_pct", "stride_length_cm",
    }
    if fields:
        cols = ", ".join(c for c in fields if c in allowed) or "*"
    else:
        cols = ", ".join(sorted(allowed))
    return _rows(
        conn,
        f"SELECT {cols} FROM activity_sample WHERE activity_id = ? ORDER BY sample_index ASC",  # noqa: S608
        (activity_id,),
    )


def get_database_status(conn: sqlite3.Connection) -> dict:
    """Row counts for all tables, sync cursors, and recent sync runs."""
    from garmin_sync import repositories as _repo
    return _repo.get_sync_status(conn)
