"""Shared test fixtures. All DB tests use in-memory SQLite — no Garmin credentials needed."""

import sqlite3
from pathlib import Path

import pytest

from garmin_sync.db import init_db

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


@pytest.fixture()
def db_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with the full schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


@pytest.fixture()
def sample_activity() -> dict:
    """Realistic Garmin activity summary dict. Mirrors observed API responses."""
    return {
        "activityId": 12345678901,
        "activityName": "Morning Run",
        "activityType": {"typeId": 1, "typeKey": "running"},
        "startTimeGMT": "2024-03-15 06:30:00",
        "startTimeLocal": "2024-03-15 07:30:00",
        "distance": 10050.0,
        "duration": 3180.0,
        "movingDuration": 3120.0,
        "elapsedDuration": 3180.0,
        "averageHR": 152,
        "maxHR": 174,
        "averageRunningCadenceInStepsPerMinute": 168.0,
        "maxRunningCadenceInStepsPerMinute": 182.0,
        "avgPower": None,
        "maxPower": None,
        "elevationGain": 45.0,
        "elevationLoss": 43.0,
        "averageSpeed": 3.16,
        "maxSpeed": 4.2,
        "calories": 612,
        "aerobicTrainingEffect": 3.2,
        "anaerobicTrainingEffect": 0.5,
        "vO2MaxValue": 52.0,
    }


@pytest.fixture()
def sample_activity_sparse() -> dict:
    """Garmin activity dict with most optional fields missing."""
    return {
        "activityId": 99999999999,
        "activityName": "Unknown Workout",
    }


@pytest.fixture()
def sample_activity_no_id() -> dict:
    """Garmin activity dict with no activityId — normalisation should return None."""
    return {
        "activityName": "Broken Activity",
        "duration": 1000.0,
    }
