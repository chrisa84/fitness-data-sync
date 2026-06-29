PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    command      TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS sync_cursor (
    data_type                   TEXT PRIMARY KEY,
    last_successful_date        TEXT,
    last_successful_activity_id TEXT,
    last_offset                 INTEGER,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_payload (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL DEFAULT 'garmin_connect',
    data_type    TEXT NOT NULL,
    garmin_id    TEXT,
    date         TEXT,
    fetched_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

-- One row per (data_type, garmin_id) for entity-keyed payloads.
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_payload_garmin_id
    ON raw_payload(data_type, garmin_id)
    WHERE garmin_id IS NOT NULL;

-- One row per (data_type, date) for date-keyed payloads (no garmin_id).
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_payload_date
    ON raw_payload(data_type, date)
    WHERE date IS NOT NULL AND garmin_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_raw_payload_data_type ON raw_payload(data_type);
CREATE INDEX IF NOT EXISTS idx_raw_payload_date      ON raw_payload(date);
CREATE INDEX IF NOT EXISTS idx_raw_payload_garmin_id ON raw_payload(garmin_id);

CREATE TABLE IF NOT EXISTS activity_detail (
    activity_id    TEXT PRIMARY KEY,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    has_splits     INTEGER,
    has_laps       INTEGER,
    sample_count   INTEGER,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_lap (
    activity_id       TEXT NOT NULL,
    lap_index         INTEGER NOT NULL,
    start_time        TEXT,
    distance_m        REAL,
    duration_s        REAL,
    moving_duration_s REAL,
    avg_hr            INTEGER,
    max_hr            INTEGER,
    avg_cadence       REAL,
    avg_power         REAL,
    elevation_gain_m  REAL,
    elevation_loss_m  REAL,
    raw_payload_id    INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS activity_split (
    activity_id             TEXT NOT NULL,
    split_index             INTEGER NOT NULL,
    split_type              TEXT,
    distance_m              REAL,
    duration_s              REAL,
    moving_duration_s       REAL,
    avg_hr                  INTEGER,
    max_hr                  INTEGER,
    avg_speed_mps           REAL,
    avg_cadence             REAL,
    avg_power               REAL,
    max_power               REAL,
    norm_power              REAL,
    calories                INTEGER,
    elevation_gain_m        REAL,
    elevation_loss_m        REAL,
    ground_contact_ms       REAL,
    vertical_oscillation_cm REAL,
    raw_payload_id          INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (activity_id, split_index)
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date                       TEXT PRIMARY KEY,
    total_steps                INTEGER,
    step_goal                  INTEGER,
    total_distance_m           REAL,
    active_calories            INTEGER,
    resting_calories           INTEGER,
    total_calories             INTEGER,
    avg_hr                     INTEGER,
    max_hr                     INTEGER,
    resting_hr                 INTEGER,
    avg_stress_level           INTEGER,
    max_stress_level           INTEGER,
    moderate_intensity_minutes INTEGER,
    vigorous_intensity_minutes INTEGER,
    intensity_minutes_goal     INTEGER,
    floors_ascended            REAL,
    floors_descended           REAL,
    average_spo2               REAL,
    latest_spo2                REAL,
    lowest_spo2                REAL,
    body_battery_highest       INTEGER,
    body_battery_lowest        INTEGER,
    body_battery_at_wake       INTEGER,
    sedentary_seconds          INTEGER,
    resting_hr_7d_avg          REAL,
    raw_payload_id             INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep (
    date                TEXT PRIMARY KEY,
    sleep_start         TEXT,
    sleep_end           TEXT,
    total_sleep_seconds INTEGER,
    deep_sleep_seconds  INTEGER,
    light_sleep_seconds INTEGER,
    rem_sleep_seconds   INTEGER,
    awake_seconds       INTEGER,
    sleep_score         INTEGER,
    avg_spo2            REAL,
    avg_respiration     REAL,
    raw_payload_id      INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hrv (
    date                 TEXT PRIMARY KEY,
    weekly_avg           INTEGER,
    last_night_avg       INTEGER,
    last_night_5min_high INTEGER,
    baseline_low         INTEGER,
    baseline_high        INTEGER,
    status               TEXT,
    raw_payload_id       INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stress (
    date                           TEXT PRIMARY KEY,
    avg_stress_level               INTEGER,
    max_stress_level               INTEGER,
    stress_duration_seconds        INTEGER,
    rest_stress_duration_seconds   INTEGER,
    low_stress_duration_seconds    INTEGER,
    medium_stress_duration_seconds INTEGER,
    high_stress_duration_seconds   INTEGER,
    raw_payload_id                 INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at                     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS body_battery (
    date           TEXT PRIMARY KEY,
    charged        INTEGER,
    drained        INTEGER,
    starting_value INTEGER,
    ending_value   INTEGER,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heart_rate (
    date           TEXT PRIMARY KEY,
    resting_hr     INTEGER,
    max_hr         INTEGER,
    min_hr         INTEGER,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    activity_id                    TEXT PRIMARY KEY,
    name                           TEXT,
    type                           TEXT,
    start_time                     TEXT,
    start_time_local               TEXT,
    distance_m                     REAL,
    duration_s                     REAL,
    moving_duration_s              REAL,
    elapsed_duration_s             REAL,
    avg_hr                         INTEGER,
    max_hr                         INTEGER,
    avg_cadence                    REAL,
    max_cadence                    REAL,
    avg_power                      REAL,
    max_power                      REAL,
    elevation_gain_m               REAL,
    elevation_loss_m               REAL,
    avg_speed_mps                  REAL,
    max_speed_mps                  REAL,
    calories                       INTEGER,
    training_effect                REAL,
    aerobic_te                     REAL,
    anaerobic_te                   REAL,
    vo2max                         REAL,
    training_load                  REAL,
    activity_steps                 INTEGER,
    body_battery_delta             INTEGER,
    avg_respiration_rate           REAL,
    hr_zone_1_s                    INTEGER,
    hr_zone_2_s                    INTEGER,
    hr_zone_3_s                    INTEGER,
    hr_zone_4_s                    INTEGER,
    hr_zone_5_s                    INTEGER,
    norm_power                     REAL,
    fastest_km_s                   REAL,
    fastest_mile_s                 REAL,
    fastest_5k_s                   REAL,
    temp_avg_c                     REAL,
    temp_min_c                     REAL,
    temp_max_c                     REAL,
    water_estimated_ml             REAL,
    is_pr                          INTEGER,
    stamina_start                  REAL,
    stamina_end                    REAL,
    stamina_min                    REAL,
    total_work_j                   REAL,
    ground_contact_ms              REAL,
    ground_contact_balance_left    REAL,
    vertical_oscillation_cm        REAL,
    vertical_ratio_pct             REAL,
    stride_length_cm               REAL,
    raw_payload_id                 INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at                     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_sample (
    activity_id             TEXT NOT NULL,
    sample_index            INTEGER NOT NULL,
    timestamp_utc           TEXT,
    distance_m              REAL,
    heart_rate              INTEGER,
    speed_mps               REAL,
    cadence                 INTEGER,
    power_w                 REAL,
    altitude_m              REAL,
    lat                     REAL,
    lon                     REAL,
    respiration_rate        REAL,
    ground_contact_ms           REAL,
    ground_contact_balance_left REAL,
    vertical_oscillation_cm     REAL,
    vertical_ratio_pct          REAL,
    stride_length_cm            REAL,
    raw_payload_id              INTEGER NOT NULL REFERENCES raw_payload(id),
    PRIMARY KEY (activity_id, sample_index)
);

CREATE INDEX IF NOT EXISTS idx_activity_sample_activity_id ON activity_sample(activity_id);

-- ---------------------------------------------------------------------------
-- Intraday health time-series (Phase 7)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS intraday_heart_rate (
    date           TEXT NOT NULL,
    timestamp_utc  TEXT NOT NULL,
    heart_rate     INTEGER,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    PRIMARY KEY (date, timestamp_utc)
);
CREATE INDEX IF NOT EXISTS idx_intraday_hr_date ON intraday_heart_rate(date);

CREATE TABLE IF NOT EXISTS intraday_stress (
    date           TEXT NOT NULL,
    timestamp_utc  TEXT NOT NULL,
    stress_level   INTEGER,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    PRIMARY KEY (date, timestamp_utc)
);
CREATE INDEX IF NOT EXISTS idx_intraday_stress_date ON intraday_stress(date);

CREATE TABLE IF NOT EXISTS intraday_steps (
    date           TEXT NOT NULL,
    timestamp_utc  TEXT NOT NULL,
    steps          INTEGER,
    activity_level TEXT,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    PRIMARY KEY (date, timestamp_utc)
);
CREATE INDEX IF NOT EXISTS idx_intraday_steps_date ON intraday_steps(date);

CREATE TABLE IF NOT EXISTS intraday_respiration (
    date            TEXT NOT NULL,
    timestamp_utc   TEXT NOT NULL,
    breaths_per_min REAL,
    raw_payload_id  INTEGER NOT NULL REFERENCES raw_payload(id),
    PRIMARY KEY (date, timestamp_utc)
);
CREATE INDEX IF NOT EXISTS idx_intraday_respiration_date ON intraday_respiration(date);

-- ---------------------------------------------------------------------------
-- Performance / training metrics (Phase 5b)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lactate_threshold (
    date                  TEXT PRIMARY KEY,
    threshold_hr          INTEGER,
    threshold_speed_value REAL,
    threshold_power_w     REAL,
    series                TEXT,
    raw_payload_id        INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS race_predictions (
    date           TEXT PRIMARY KEY,
    race_5k_s      INTEGER,
    race_10k_s     INTEGER,
    race_half_s    INTEGER,
    race_full_s    INTEGER,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS endurance_score (
    date           TEXT PRIMARY KEY,
    score          INTEGER,
    classification INTEGER,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hill_score (
    date                 TEXT PRIMARY KEY,
    overall_score        INTEGER,
    strength_score       INTEGER,
    hill_endurance_score INTEGER,
    classification       INTEGER,
    raw_payload_id       INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at           TEXT NOT NULL
);

-- 5b.2 tables

CREATE TABLE IF NOT EXISTS training_status (
    date                   TEXT PRIMARY KEY,
    vo2max                 REAL,
    vo2max_precise         REAL,
    training_status_code   INTEGER,
    training_status_phrase TEXT,
    load_aerobic_low       REAL,
    load_aerobic_high      REAL,
    load_anaerobic         REAL,
    load_feedback          TEXT,
    acute_load             INTEGER,
    chronic_load           INTEGER,
    acwr                   REAL,
    raw_payload_id         INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_readiness (
    date                      TEXT PRIMARY KEY,
    score                     INTEGER,
    level                     TEXT,
    recovery_time_min         INTEGER,
    acwr_factor_pct           INTEGER,
    acute_load                INTEGER,
    hrv_factor_pct            INTEGER,
    sleep_factor_pct          INTEGER,
    stress_factor_pct         INTEGER,
    feedback_short            TEXT,
    morning_readiness_score   INTEGER,
    morning_readiness_level   TEXT,
    morning_recovery_time_min INTEGER,
    raw_payload_id            INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS max_metrics (
    date             TEXT PRIMARY KEY,
    vo2max           REAL,
    vo2max_precise   REAL,
    fitness_age      REAL,
    fitness_age_desc TEXT,
    raw_payload_id   INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fitness_age (
    date                   TEXT PRIMARY KEY,
    fitness_age            REAL,
    achievable_fitness_age REAL,
    chronological_age      INTEGER,
    raw_payload_id         INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at             TEXT NOT NULL
);
