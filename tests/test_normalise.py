"""
Tests for activity normalisation.

Verifies:
  - Full payload maps all fields correctly.
  - Sparse payload returns None for missing optional fields without crashing.
  - Nested activityType.typeKey is extracted.
  - Missing activityId returns None (unrecoverable).
"""

import pytest

from garmin_sync.normalise import normalise_activity


class TestNormaliseActivity:
    def test_full_payload_maps_all_fields(self, sample_activity):
        result = normalise_activity(sample_activity)
        assert result is not None
        assert result["activity_id"] == "12345678901"
        assert result["name"] == "Morning Run"
        assert result["type"] == "running"
        assert result["start_time"] == "2024-03-15 06:30:00"
        assert result["start_time_local"] == "2024-03-15 07:30:00"
        assert result["distance_m"] == pytest.approx(10050.0)
        assert result["duration_s"] == pytest.approx(3180.0)
        assert result["moving_duration_s"] == pytest.approx(3120.0)
        assert result["avg_hr"] == 152
        assert result["max_hr"] == 174
        assert result["avg_cadence"] == pytest.approx(168.0)
        assert result["max_cadence"] == pytest.approx(182.0)
        assert result["elevation_gain_m"] == pytest.approx(45.0)
        assert result["calories"] == 612
        assert result["aerobic_te"] == pytest.approx(3.2)
        assert result["anaerobic_te"] == pytest.approx(0.5)
        assert result["vo2max"] == pytest.approx(52.0)
        assert result["raw_payload_id"] is None  # Set by caller before insert.
        assert result["updated_at"] is not None

    def test_sparse_payload_returns_none_for_missing_fields(self, sample_activity_sparse):
        result = normalise_activity(sample_activity_sparse)
        assert result is not None
        assert result["activity_id"] == "99999999999"
        assert result["name"] == "Unknown Workout"
        assert result["type"] is None
        assert result["distance_m"] is None
        assert result["avg_hr"] is None
        assert result["vo2max"] is None

    def test_no_activity_id_returns_none(self, sample_activity_no_id):
        result = normalise_activity(sample_activity_no_id)
        assert result is None

    def test_empty_dict_returns_none(self):
        result = normalise_activity({})
        assert result is None

    def test_nested_activity_type_dict(self):
        raw = {"activityId": 555, "activityType": {"typeKey": "cycling", "typeId": 2}}
        result = normalise_activity(raw)
        assert result is not None
        assert result["type"] == "cycling"

    def test_string_activity_type(self):
        raw = {"activityId": 666, "activityType": "swimming"}
        result = normalise_activity(raw)
        assert result is not None
        assert result["type"] == "swimming"

    def test_none_activity_type(self):
        raw = {"activityId": 777}
        result = normalise_activity(raw)
        assert result is not None
        assert result["type"] is None

    def test_avg_power_none_when_missing(self, sample_activity):
        # sample_activity has avgPower=None
        result = normalise_activity(sample_activity)
        assert result is not None
        assert result["avg_power"] is None

    def test_cycling_cadence_field(self):
        raw = {
            "activityId": 888,
            "activityType": {"typeKey": "cycling"},
            "averageBikingCadenceInRevPerMinute": 85.5,
            "maxBikingCadenceInRevPerMinute": 110.0,
        }
        result = normalise_activity(raw)
        assert result is not None
        assert result["avg_cadence"] == pytest.approx(85.5)
        assert result["max_cadence"] == pytest.approx(110.0)

    def test_non_numeric_hr_returns_none(self):
        raw = {"activityId": 999, "averageHR": "not-a-number"}
        result = normalise_activity(raw)
        assert result is not None
        assert result["avg_hr"] is None

    def test_activity_id_as_integer_becomes_string(self):
        raw = {"activityId": 42}
        result = normalise_activity(raw)
        assert result is not None
        assert result["activity_id"] == "42"
        assert isinstance(result["activity_id"], str)
