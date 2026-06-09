import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"

_ACTIVITY_DERIVED_COLS = [
    ("training_load", "REAL"), ("activity_steps", "INTEGER"),
    ("body_battery_delta", "INTEGER"), ("avg_respiration_rate", "REAL"),
    ("hr_zone_1_s", "INTEGER"), ("hr_zone_2_s", "INTEGER"),
    ("hr_zone_3_s", "INTEGER"), ("hr_zone_4_s", "INTEGER"),
    ("hr_zone_5_s", "INTEGER"), ("norm_power", "REAL"),
    ("fastest_km_s", "REAL"), ("fastest_mile_s", "REAL"),
    ("fastest_5k_s", "REAL"), ("temp_avg_c", "REAL"),
    ("temp_min_c", "REAL"), ("temp_max_c", "REAL"),
    ("water_estimated_ml", "REAL"), ("is_pr", "INTEGER"),
    ("stamina_start", "REAL"), ("stamina_end", "REAL"),
    ("stamina_min", "REAL"), ("total_work_j", "REAL"),
    ("ground_contact_ms", "REAL"), ("ground_contact_balance_left", "REAL"),
    ("vertical_oscillation_cm", "REAL"), ("vertical_ratio_pct", "REAL"),
    ("stride_length_cm", "REAL"),
]

_DAILY_SUMMARY_DERIVED_COLS = [
    ("average_spo2", "REAL"), ("latest_spo2", "REAL"), ("lowest_spo2", "REAL"),
    ("body_battery_highest", "INTEGER"), ("body_battery_lowest", "INTEGER"),
    ("body_battery_at_wake", "INTEGER"), ("sedentary_seconds", "INTEGER"),
    ("resting_hr_7d_avg", "REAL"),
]

_TRAINING_READINESS_NEW_COLS = [
    ("sleep_factor_pct", "INTEGER"),
    ("stress_factor_pct", "INTEGER"),
    ("morning_readiness_score", "INTEGER"),
    ("morning_readiness_level", "TEXT"),
    ("morning_recovery_time_min", "INTEGER"),
]


def _migrate_add_columns(conn: sqlite3.Connection, table: str, cols: list) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, type_ in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")
    conn.commit()


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    # Execute each statement separately; sqlite3 executescript auto-commits.
    conn.executescript(schema_sql)
    conn.commit()
    _migrate_add_columns(conn, "activity", _ACTIVITY_DERIVED_COLS)
    _migrate_add_columns(conn, "daily_summary", _DAILY_SUMMARY_DERIVED_COLS)
    # Rename recovery_time_h → recovery_time_min if old column exists.
    tr_cols = {row[1] for row in conn.execute("PRAGMA table_info(training_readiness)").fetchall()}
    if "recovery_time_h" in tr_cols and "recovery_time_min" not in tr_cols:
        conn.execute("ALTER TABLE training_readiness RENAME COLUMN recovery_time_h TO recovery_time_min")
        conn.commit()
    _migrate_add_columns(conn, "training_readiness", _TRAINING_READINESS_NEW_COLS)
    logger.info("Database schema initialised.")
