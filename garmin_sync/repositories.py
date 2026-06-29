"""Database operations. All functions take a sqlite3.Connection as first argument."""

import logging
import sqlite3
from datetime import datetime, timezone

from garmin_sync.models import ActivityRow, CursorState, SyncRunRow

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# raw_payload
# ---------------------------------------------------------------------------


def upsert_raw_payload_by_garmin_id(
    conn: sqlite3.Connection,
    *,
    source: str,
    data_type: str,
    garmin_id: str,
    payload_json: str,
    payload_hash: str,
) -> tuple[int, bool]:
    """
    Insert or update a raw payload keyed by (data_type, garmin_id).

    Returns (row_id, was_changed) where was_changed is True if the row was
    newly inserted or the payload_hash differed from the stored value.

    was_changed detection uses total_changes because SQLite sets lastrowid
    even when the DO UPDATE WHERE clause prevents any actual write.
    """
    fetched_at = _now_utc()
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO raw_payload (source, data_type, garmin_id, fetched_at, payload_json, payload_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(data_type, garmin_id) WHERE garmin_id IS NOT NULL
        DO UPDATE SET
            payload_json = excluded.payload_json,
            payload_hash = excluded.payload_hash,
            fetched_at   = excluded.fetched_at
        WHERE excluded.payload_hash != raw_payload.payload_hash
        """,
        (source, data_type, garmin_id, fetched_at, payload_json, payload_hash),
    )
    conn.commit()
    was_changed = conn.total_changes > before

    row = conn.execute(
        "SELECT id FROM raw_payload WHERE data_type=? AND garmin_id=?",
        (data_type, garmin_id),
    ).fetchone()
    return row["id"], was_changed


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


def upsert_activity(conn: sqlite3.Connection, row: ActivityRow) -> None:
    conn.execute(
        """
        INSERT INTO activity (
            activity_id, name, type, start_time, start_time_local,
            distance_m, duration_s, moving_duration_s, elapsed_duration_s,
            avg_hr, max_hr, avg_cadence, max_cadence, avg_power, max_power,
            elevation_gain_m, elevation_loss_m, avg_speed_mps, max_speed_mps,
            calories, training_effect, aerobic_te, anaerobic_te, vo2max,
            raw_payload_id, updated_at
        ) VALUES (
            :activity_id, :name, :type, :start_time, :start_time_local,
            :distance_m, :duration_s, :moving_duration_s, :elapsed_duration_s,
            :avg_hr, :max_hr, :avg_cadence, :max_cadence, :avg_power, :max_power,
            :elevation_gain_m, :elevation_loss_m, :avg_speed_mps, :max_speed_mps,
            :calories, :training_effect, :aerobic_te, :anaerobic_te, :vo2max,
            :raw_payload_id, :updated_at
        )
        ON CONFLICT(activity_id) DO UPDATE SET
            name               = excluded.name,
            type               = excluded.type,
            start_time         = excluded.start_time,
            start_time_local   = excluded.start_time_local,
            distance_m         = excluded.distance_m,
            duration_s         = excluded.duration_s,
            moving_duration_s  = excluded.moving_duration_s,
            elapsed_duration_s = excluded.elapsed_duration_s,
            avg_hr             = excluded.avg_hr,
            max_hr             = excluded.max_hr,
            avg_cadence        = excluded.avg_cadence,
            max_cadence        = excluded.max_cadence,
            avg_power          = excluded.avg_power,
            max_power          = excluded.max_power,
            elevation_gain_m   = excluded.elevation_gain_m,
            elevation_loss_m   = excluded.elevation_loss_m,
            avg_speed_mps      = excluded.avg_speed_mps,
            max_speed_mps      = excluded.max_speed_mps,
            calories           = excluded.calories,
            training_effect    = excluded.training_effect,
            aerobic_te         = excluded.aerobic_te,
            anaerobic_te       = excluded.anaerobic_te,
            vo2max             = excluded.vo2max,
            raw_payload_id     = excluded.raw_payload_id,
            updated_at         = excluded.updated_at
        """,
        dict(row),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# sync_cursor
# ---------------------------------------------------------------------------


def get_cursor(conn: sqlite3.Connection, data_type: str) -> CursorState | None:
    row = conn.execute(
        "SELECT * FROM sync_cursor WHERE data_type=?", (data_type,)
    ).fetchone()
    if row is None:
        return None
    return CursorState(**dict(row))


def update_cursor(
    conn: sqlite3.Connection,
    data_type: str,
    *,
    last_offset: int | None = None,
    last_successful_activity_id: str | None = None,
    last_successful_date: str | None = None,
) -> None:
    existing = get_cursor(conn, data_type)
    now = _now_utc()

    if existing is None:
        conn.execute(
            """
            INSERT INTO sync_cursor
                (data_type, last_successful_date, last_successful_activity_id, last_offset, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (data_type, last_successful_date, last_successful_activity_id, last_offset, now),
        )
    else:
        fields: dict = {}
        if last_offset is not None:
            fields["last_offset"] = last_offset
        if last_successful_activity_id is not None:
            fields["last_successful_activity_id"] = last_successful_activity_id
        if last_successful_date is not None:
            fields["last_successful_date"] = last_successful_date
        fields["updated_at"] = now

        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [data_type]
        conn.execute(
            f"UPDATE sync_cursor SET {set_clause} WHERE data_type=?",  # noqa: S608
            values,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# sync_runs
# ---------------------------------------------------------------------------


def create_sync_run(conn: sqlite3.Connection, command: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (started_at, status, command) VALUES (?, 'running', ?)",
        (_now_utc(), command),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at=?, status=?, error=? WHERE id=?",
        (_now_utc(), status, error, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def upsert_raw_payload_by_date(
    conn: sqlite3.Connection,
    *,
    source: str,
    data_type: str,
    date: str,
    payload_json: str,
    payload_hash: str,
) -> tuple[int, bool]:
    """
    Insert or update a raw payload keyed by (data_type, date).

    Returns (row_id, was_changed). Mirrors upsert_raw_payload_by_garmin_id
    but uses the date partial unique index instead.
    """
    fetched_at = _now_utc()
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO raw_payload (source, data_type, date, fetched_at, payload_json, payload_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(data_type, date) WHERE date IS NOT NULL AND garmin_id IS NULL
        DO UPDATE SET
            payload_json = excluded.payload_json,
            payload_hash = excluded.payload_hash,
            fetched_at   = excluded.fetched_at
        WHERE excluded.payload_hash != raw_payload.payload_hash
        """,
        (source, data_type, date, fetched_at, payload_json, payload_hash),
    )
    conn.commit()
    was_changed = conn.total_changes > before

    row = conn.execute(
        "SELECT id FROM raw_payload WHERE data_type=? AND date=? AND garmin_id IS NULL",
        (data_type, date),
    ).fetchone()
    return row["id"], was_changed


# ---------------------------------------------------------------------------
# Health tables
# ---------------------------------------------------------------------------

def _upsert_health_row(conn: sqlite3.Connection, table: str, columns: list[str], row: dict) -> None:
    cols = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "date")
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"  # noqa: S608
        f" ON CONFLICT(date) DO UPDATE SET {updates}",
        row,
    )
    conn.commit()


def upsert_daily_summary(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_health_row(conn, "daily_summary", [
        "date", "total_steps", "step_goal", "total_distance_m",
        "active_calories", "resting_calories", "total_calories",
        "avg_hr", "max_hr", "resting_hr",
        "avg_stress_level", "max_stress_level",
        "moderate_intensity_minutes", "vigorous_intensity_minutes", "intensity_minutes_goal",
        "floors_ascended", "floors_descended",
        "raw_payload_id", "updated_at",
    ], row)


def upsert_sleep(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_health_row(conn, "sleep", [
        "date", "sleep_start", "sleep_end",
        "total_sleep_seconds", "deep_sleep_seconds", "light_sleep_seconds",
        "rem_sleep_seconds", "awake_seconds", "sleep_score",
        "avg_spo2", "avg_respiration",
        "raw_payload_id", "updated_at",
    ], row)


def upsert_hrv(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_health_row(conn, "hrv", [
        "date", "weekly_avg", "last_night_avg", "last_night_5min_high",
        "baseline_low", "baseline_high", "status",
        "raw_payload_id", "updated_at",
    ], row)


def upsert_stress(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_health_row(conn, "stress", [
        "date", "avg_stress_level", "max_stress_level",
        "stress_duration_seconds", "rest_stress_duration_seconds",
        "low_stress_duration_seconds", "medium_stress_duration_seconds",
        "high_stress_duration_seconds",
        "raw_payload_id", "updated_at",
    ], row)


def upsert_body_battery(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_health_row(conn, "body_battery", [
        "date", "charged", "drained", "starting_value", "ending_value",
        "raw_payload_id", "updated_at",
    ], row)


def upsert_heart_rate(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_health_row(conn, "heart_rate", [
        "date", "resting_hr", "max_hr", "min_hr",
        "raw_payload_id", "updated_at",
    ], row)


def get_sync_status(conn: sqlite3.Connection) -> dict:
    activity_count = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0]

    cursors = conn.execute("SELECT * FROM sync_cursor").fetchall()
    cursor_info = [dict(r) for r in cursors]

    recent_runs = conn.execute(
        "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    run_info = [dict(r) for r in recent_runs]

    detail_count = conn.execute("SELECT COUNT(*) FROM activity_detail").fetchone()[0]
    lap_count = conn.execute("SELECT COUNT(*) FROM activity_lap").fetchone()[0]
    split_count = conn.execute("SELECT COUNT(*) FROM activity_split").fetchone()[0]
    sample_count = conn.execute("SELECT COUNT(*) FROM activity_sample").fetchone()[0]
    sample_activity_count = conn.execute("SELECT COUNT(DISTINCT activity_id) FROM activity_sample").fetchone()[0]
    daily_summary_count = conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0]
    sleep_count = conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0]
    hrv_count = conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0]
    stress_count = conn.execute("SELECT COUNT(*) FROM stress").fetchone()[0]
    body_battery_count = conn.execute("SELECT COUNT(*) FROM body_battery").fetchone()[0]
    heart_rate_count = conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0]

    return {
        "activity_count": activity_count,
        "activity_detail_count": detail_count,
        "activity_lap_count": lap_count,
        "activity_split_count": split_count,
        "activity_sample_count": sample_count,
        "activity_sample_activity_count": sample_activity_count,
        "daily_summary_count": daily_summary_count,
        "sleep_count": sleep_count,
        "hrv_count": hrv_count,
        "stress_count": stress_count,
        "body_battery_count": body_battery_count,
        "heart_rate_count": heart_rate_count,
        "lactate_threshold_count":  conn.execute("SELECT COUNT(*) FROM lactate_threshold").fetchone()[0],
        "race_predictions_count":   conn.execute("SELECT COUNT(*) FROM race_predictions").fetchone()[0],
        "endurance_score_count":    conn.execute("SELECT COUNT(*) FROM endurance_score").fetchone()[0],
        "hill_score_count":         conn.execute("SELECT COUNT(*) FROM hill_score").fetchone()[0],
        "training_status_count":    conn.execute("SELECT COUNT(*) FROM training_status").fetchone()[0],
        "training_readiness_count": conn.execute("SELECT COUNT(*) FROM training_readiness").fetchone()[0],
        "max_metrics_count":        conn.execute("SELECT COUNT(*) FROM max_metrics").fetchone()[0],
        "fitness_age_count":        conn.execute("SELECT COUNT(*) FROM fitness_age").fetchone()[0],
        "intraday_hr_count": conn.execute("SELECT COUNT(*) FROM intraday_heart_rate").fetchone()[0],
        "intraday_hr_date_count": conn.execute("SELECT COUNT(DISTINCT date) FROM intraday_heart_rate").fetchone()[0],
        "intraday_stress_count": conn.execute("SELECT COUNT(*) FROM intraday_stress").fetchone()[0],
        "intraday_steps_count": conn.execute("SELECT COUNT(*) FROM intraday_steps").fetchone()[0],
        "intraday_respiration_count": conn.execute("SELECT COUNT(*) FROM intraday_respiration").fetchone()[0],
        "raw_payload_count": raw_count,
        "cursors": cursor_info,
        "recent_runs": run_info,
    }


# ---------------------------------------------------------------------------
# activity_detail
# ---------------------------------------------------------------------------


def get_activities_needing_detail(
    conn: sqlite3.Connection,
    limit: int | None = None,
    refresh_existing: bool = False,
) -> list[str]:
    """
    Return activity_ids that need detail sync, ordered newest first.

    refresh_existing=False: only activities without an activity_detail row.
    refresh_existing=True: all activities (re-fetch even if detail exists).
    limit=None: return all matching.
    """
    if refresh_existing:
        sql = "SELECT activity_id FROM activity ORDER BY start_time DESC"
        params: tuple = ()
    else:
        sql = """
            SELECT a.activity_id FROM activity a
            LEFT JOIN activity_detail ad ON a.activity_id = ad.activity_id
            WHERE ad.activity_id IS NULL
            ORDER BY a.start_time DESC
        """
        params = ()

    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def upsert_activity_detail(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO activity_detail
            (activity_id, raw_payload_id, has_splits, has_laps, sample_count, updated_at)
        VALUES
            (:activity_id, :raw_payload_id, :has_splits, :has_laps, :sample_count, :updated_at)
        ON CONFLICT(activity_id) DO UPDATE SET
            raw_payload_id = excluded.raw_payload_id,
            has_splits     = excluded.has_splits,
            has_laps       = excluded.has_laps,
            sample_count   = excluded.sample_count,
            updated_at     = excluded.updated_at
        """,
        row,
    )
    conn.commit()


def replace_activity_laps(
    conn: sqlite3.Connection, activity_id: str, laps: list[dict]
) -> None:
    """Delete all existing laps for this activity and insert the new set."""
    conn.execute("DELETE FROM activity_lap WHERE activity_id=?", (activity_id,))
    if laps:
        conn.executemany(
            """
            INSERT INTO activity_lap (
                activity_id, lap_index, start_time, distance_m, duration_s,
                moving_duration_s, avg_hr, max_hr, avg_cadence, avg_power,
                elevation_gain_m, elevation_loss_m, raw_payload_id, updated_at
            ) VALUES (
                :activity_id, :lap_index, :start_time, :distance_m, :duration_s,
                :moving_duration_s, :avg_hr, :max_hr, :avg_cadence, :avg_power,
                :elevation_gain_m, :elevation_loss_m, :raw_payload_id, :updated_at
            )
            """,
            laps,
        )
    conn.commit()


def update_activity_derived(
    conn: sqlite3.Connection, activity_id: str, fields: dict
) -> bool:
    """Update derived fields on an existing activity row. Returns True if a row was updated."""
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    result = conn.execute(
        f"UPDATE activity SET {set_clause} WHERE activity_id = ?",  # noqa: S608
        (*fields.values(), activity_id),
    )
    conn.commit()
    return result.rowcount > 0


def get_activities_needing_samples(
    conn: sqlite3.Connection,
    limit: int | None = None,
    refresh_existing: bool = False,
) -> list[str]:
    """
    Return activity_ids that need sample sync, ordered newest first.

    refresh_existing=False: only activities with no rows in activity_sample.
    refresh_existing=True: all activities.
    """
    if refresh_existing:
        sql = "SELECT activity_id FROM activity ORDER BY start_time DESC"
        params: tuple = ()
    else:
        sql = """
            SELECT a.activity_id FROM activity a
            WHERE NOT EXISTS (
                SELECT 1 FROM activity_sample s WHERE s.activity_id = a.activity_id
            )
            ORDER BY a.start_time DESC
        """
        params = ()

    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def replace_activity_samples(
    conn: sqlite3.Connection, activity_id: str, samples: list[dict]
) -> None:
    """Delete all existing samples for this activity and insert the new set."""
    conn.execute("DELETE FROM activity_sample WHERE activity_id=?", (activity_id,))
    if samples:
        conn.executemany(
            """
            INSERT INTO activity_sample (
                activity_id, sample_index, timestamp_utc,
                distance_m, heart_rate, speed_mps, cadence, power_w,
                altitude_m, lat, lon, respiration_rate,
                ground_contact_ms, ground_contact_balance_left,
                vertical_oscillation_cm, vertical_ratio_pct, stride_length_cm,
                raw_payload_id
            ) VALUES (
                :activity_id, :sample_index, :timestamp_utc,
                :distance_m, :heart_rate, :speed_mps, :cadence, :power_w,
                :altitude_m, :lat, :lon, :respiration_rate,
                :ground_contact_ms, :ground_contact_balance_left,
                :vertical_oscillation_cm, :vertical_ratio_pct, :stride_length_cm,
                :raw_payload_id
            )
            """,
            samples,
        )
    conn.commit()


def replace_activity_splits(
    conn: sqlite3.Connection, activity_id: str, splits: list[dict]
) -> None:
    """Delete all existing splits for this activity and insert the new set."""
    conn.execute("DELETE FROM activity_split WHERE activity_id = ?", (activity_id,))
    if splits:
        conn.executemany(
            """INSERT INTO activity_split (
                activity_id, split_index, split_type,
                distance_m, duration_s, moving_duration_s,
                avg_hr, max_hr, avg_speed_mps, avg_cadence,
                avg_power, max_power, norm_power, calories,
                elevation_gain_m, elevation_loss_m,
                ground_contact_ms, vertical_oscillation_cm,
                raw_payload_id, updated_at
            ) VALUES (
                :activity_id, :split_index, :split_type,
                :distance_m, :duration_s, :moving_duration_s,
                :avg_hr, :max_hr, :avg_speed_mps, :avg_cadence,
                :avg_power, :max_power, :norm_power, :calories,
                :elevation_gain_m, :elevation_loss_m,
                :ground_contact_ms, :vertical_oscillation_cm,
                :raw_payload_id, :updated_at
            )""",
            splits,
        )
    conn.commit()


def update_daily_summary_derived(
    conn: sqlite3.Connection, date: str, fields: dict
) -> bool:
    """Update derived fields on an existing daily_summary row. Returns True if a row was updated."""
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    result = conn.execute(
        f"UPDATE daily_summary SET {set_clause} WHERE date = ?",  # noqa: S608
        (*fields.values(), date),
    )
    conn.commit()
    return result.rowcount > 0


# ---------------------------------------------------------------------------
# Intraday health time-series tables (Phase 7)
# ---------------------------------------------------------------------------


def replace_intraday_heart_rate(conn: sqlite3.Connection, date_str: str, rows: list[dict]) -> None:
    """Replace all intraday HR rows for a date with a fresh set."""
    conn.execute("DELETE FROM intraday_heart_rate WHERE date=?", (date_str,))
    if rows:
        conn.executemany(
            "INSERT INTO intraday_heart_rate (date, timestamp_utc, heart_rate, raw_payload_id)"
            " VALUES (:date, :timestamp_utc, :heart_rate, :raw_payload_id)",
            rows,
        )
    conn.commit()


def replace_intraday_stress(conn: sqlite3.Connection, date_str: str, rows: list[dict]) -> None:
    """Replace all intraday stress rows for a date with a fresh set."""
    conn.execute("DELETE FROM intraday_stress WHERE date=?", (date_str,))
    if rows:
        conn.executemany(
            "INSERT INTO intraday_stress (date, timestamp_utc, stress_level, raw_payload_id)"
            " VALUES (:date, :timestamp_utc, :stress_level, :raw_payload_id)",
            rows,
        )
    conn.commit()


def replace_intraday_steps(conn: sqlite3.Connection, date_str: str, rows: list[dict]) -> None:
    """Replace all intraday steps rows for a date with a fresh set."""
    conn.execute("DELETE FROM intraday_steps WHERE date=?", (date_str,))
    if rows:
        conn.executemany(
            "INSERT INTO intraday_steps (date, timestamp_utc, steps, activity_level, raw_payload_id)"
            " VALUES (:date, :timestamp_utc, :steps, :activity_level, :raw_payload_id)",
            rows,
        )
    conn.commit()


def replace_intraday_respiration(conn: sqlite3.Connection, date_str: str, rows: list[dict]) -> None:
    """Replace all intraday respiration rows for a date with a fresh set."""
    conn.execute("DELETE FROM intraday_respiration WHERE date=?", (date_str,))
    if rows:
        conn.executemany(
            "INSERT INTO intraday_respiration (date, timestamp_utc, breaths_per_min, raw_payload_id)"
            " VALUES (:date, :timestamp_utc, :breaths_per_min, :raw_payload_id)",
            rows,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Performance tables (Phase 5b)
# ---------------------------------------------------------------------------

def _upsert_perf_row(conn: sqlite3.Connection, table: str, pk: str, columns: list[str], row: dict) -> None:
    cols = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != pk)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"  # noqa: S608
        f" ON CONFLICT({pk}) DO UPDATE SET {updates}",
        row,
    )
    conn.commit()


def upsert_lactate_threshold(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "lactate_threshold", "date",
        ["date", "threshold_hr", "threshold_speed_value", "threshold_power_w", "series", "raw_payload_id", "updated_at"], row)


def upsert_race_prediction(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "race_predictions", "date",
        ["date", "race_5k_s", "race_10k_s", "race_half_s", "race_full_s", "raw_payload_id", "updated_at"], row)


def upsert_endurance_score(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "endurance_score", "date",
        ["date", "score", "classification", "raw_payload_id", "updated_at"], row)


def upsert_hill_score(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "hill_score", "date",
        ["date", "overall_score", "strength_score", "hill_endurance_score", "classification", "raw_payload_id", "updated_at"], row)


def upsert_training_status(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "training_status", "date",
        ["date", "vo2max", "vo2max_precise", "training_status_code", "training_status_phrase",
         "load_aerobic_low", "load_aerobic_high", "load_anaerobic", "load_feedback",
         "acute_load", "chronic_load", "acwr", "raw_payload_id", "updated_at"], row)


def upsert_training_readiness(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "training_readiness", "date",
        ["date", "score", "level", "recovery_time_min", "acwr_factor_pct", "acute_load",
         "hrv_factor_pct", "sleep_factor_pct", "stress_factor_pct", "feedback_short",
         "morning_readiness_score", "morning_readiness_level", "morning_recovery_time_min",
         "raw_payload_id", "updated_at"], row)


def upsert_max_metrics(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "max_metrics", "date",
        ["date", "vo2max", "vo2max_precise", "fitness_age", "fitness_age_desc",
         "raw_payload_id", "updated_at"], row)


def upsert_fitness_age(conn: sqlite3.Connection, row: dict) -> None:
    _upsert_perf_row(conn, "fitness_age", "date",
        ["date", "fitness_age", "achievable_fitness_age", "chronological_age", "raw_payload_id", "updated_at"], row)
