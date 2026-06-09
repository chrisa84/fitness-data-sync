"""Internal TypedDicts for passing data between layers. Not ORM models."""

from typing import TypedDict


class RawPayloadRow(TypedDict):
    id: int
    source: str
    data_type: str
    garmin_id: str | None
    date: str | None
    fetched_at: str
    payload_json: str
    payload_hash: str


class ActivityRow(TypedDict):
    activity_id: str
    name: str | None
    type: str | None
    start_time: str | None
    start_time_local: str | None
    distance_m: float | None
    duration_s: float | None
    moving_duration_s: float | None
    elapsed_duration_s: float | None
    avg_hr: int | None
    max_hr: int | None
    avg_cadence: float | None
    max_cadence: float | None
    avg_power: float | None
    max_power: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    avg_speed_mps: float | None
    max_speed_mps: float | None
    calories: int | None
    training_effect: float | None
    aerobic_te: float | None
    anaerobic_te: float | None
    vo2max: float | None
    raw_payload_id: int
    updated_at: str


class CursorState(TypedDict):
    data_type: str
    last_successful_date: str | None
    last_successful_activity_id: str | None
    last_offset: int | None
    updated_at: str


class SyncRunRow(TypedDict):
    id: int
    started_at: str
    finished_at: str | None
    status: str
    command: str | None
    error: str | None


class SyncResult(TypedDict):
    fetched: int
    stored: int
    skipped: int
    updated: int
