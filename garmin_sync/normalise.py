"""
Normalisation functions: raw Garmin dicts → typed dicts for DB insertion.

All functions are pure (no DB access, no side effects).
They are defensive: missing or null Garmin fields produce None, not crashes.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ts_ms: object) -> str | None:
    """Convert a millisecond epoch timestamp to ISO-8601 UTC string."""
    if ts_ms is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Health / wellness normalisation
# ---------------------------------------------------------------------------


def normalise_daily_summary(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_user_summary() response to the daily_summary table row."""
    if not raw:
        return None
    now = datetime.now(timezone.utc).isoformat()
    return {
        "date": date_str,
        "total_steps": _safe_int(raw.get("totalSteps")),
        "step_goal": _safe_int(raw.get("dailyStepGoal")),
        "total_distance_m": _safe_float(raw.get("totalDistanceMeters")),
        "active_calories": _safe_int(raw.get("activeKilocalories")),
        "resting_calories": _safe_int(raw.get("bmrKilocalories")),
        "total_calories": _safe_int(raw.get("totalKilocalories")),
        "avg_hr": _safe_int(raw.get("averageHeartRateInBeatsPerMinute")),
        "max_hr": _safe_int(raw.get("maxHeartRateInBeatsPerMinute")),
        "resting_hr": _safe_int(raw.get("restingHeartRateInBeatsPerMinute")),
        "avg_stress_level": _safe_int(raw.get("averageStressLevel")),
        "max_stress_level": _safe_int(raw.get("maxStressLevel")),
        "moderate_intensity_minutes": _safe_int(raw.get("moderateIntensityMinutes")),
        "vigorous_intensity_minutes": _safe_int(raw.get("vigorousIntensityMinutes")),
        "intensity_minutes_goal": _safe_int(raw.get("intensityMinutesGoal")),
        "floors_ascended": _safe_float(raw.get("floorsAscended")),
        "floors_descended": _safe_float(raw.get("floorsDescended")),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


def normalise_sleep(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_sleep_data() response to the sleep table row."""
    if not raw:
        return None
    dto = raw.get("dailySleepDTO") or raw
    now = datetime.now(timezone.utc).isoformat()

    # sleepScores.overall.value is the canonical score in newer payloads;
    # fall back to overallSleepScore (older format, sometimes an int directly)
    sleep_scores = dto.get("sleepScores") or {}
    overall = sleep_scores.get("overall") if isinstance(sleep_scores, dict) else None
    if isinstance(overall, dict):
        sleep_score = _safe_int(overall.get("value"))
    else:
        sleep_score_raw = dto.get("overallSleepScore")
        if isinstance(sleep_score_raw, dict):
            sleep_score = _safe_int(sleep_score_raw.get("value"))
        else:
            sleep_score = _safe_int(sleep_score_raw)

    return {
        "date": date_str,
        "sleep_start": _ms_to_iso(dto.get("sleepStartTimestampGMT")),
        "sleep_end": _ms_to_iso(dto.get("sleepEndTimestampGMT")),
        "total_sleep_seconds": _safe_int(dto.get("sleepTimeSeconds")),
        "deep_sleep_seconds": _safe_int(dto.get("deepSleepSeconds")),
        "light_sleep_seconds": _safe_int(dto.get("lightSleepSeconds")),
        "rem_sleep_seconds": _safe_int(dto.get("remSleepSeconds")),
        "awake_seconds": _safe_int(dto.get("awakeSleepSeconds")),
        "sleep_score": sleep_score,
        "avg_spo2": _safe_float(dto.get("averageSpO2Value")),
        "avg_respiration": _safe_float(dto.get("averageRespirationValue")),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


def normalise_hrv(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_hrv_data() response to the hrv table row."""
    if not raw:
        return None
    summary = raw.get("hrvSummary") or raw
    baseline = summary.get("baseline") or {}
    now = datetime.now(timezone.utc).isoformat()
    return {
        "date": date_str,
        "weekly_avg": _safe_int(summary.get("weeklyAvg")),
        "last_night_avg": _safe_int(summary.get("lastNightAvg")),
        "last_night_5min_high": _safe_int(summary.get("lastNight5MinHigh")),
        "baseline_low": _safe_int(baseline.get("balancedLow") or baseline.get("lowUpper")),
        "baseline_high": _safe_int(baseline.get("balancedUpper")),
        "status": summary.get("status"),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


def normalise_stress(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_stress_data() response to the stress table row."""
    if not raw:
        return None
    now = datetime.now(timezone.utc).isoformat()
    return {
        "date": date_str,
        "avg_stress_level": _safe_int(raw.get("avgStressLevel") or raw.get("averageStressLevel")),
        "max_stress_level": _safe_int(raw.get("maxStressLevel")),
        "stress_duration_seconds": _safe_int(raw.get("stressDuration")),
        "rest_stress_duration_seconds": _safe_int(raw.get("restStressDuration")),
        "low_stress_duration_seconds": _safe_int(raw.get("lowStressDuration")),
        "medium_stress_duration_seconds": _safe_int(raw.get("mediumStressDuration")),
        "high_stress_duration_seconds": _safe_int(raw.get("highStressDuration")),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


def normalise_body_battery(raw: list | dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_body_battery() response to the body_battery table row."""
    if not raw:
        return None
    if isinstance(raw, list):
        entry = next(
            (item for item in raw if isinstance(item, dict) and
             (item.get("date") or item.get("calendarDate")) == date_str),
            raw[0] if raw else None,
        )
    else:
        entry = raw

    if not entry or not isinstance(entry, dict):
        return None

    now = datetime.now(timezone.utc).isoformat()
    return {
        "date": date_str,
        "charged": _safe_int(entry.get("charged")),
        "drained": _safe_int(entry.get("drained")),
        "starting_value": _safe_int(entry.get("startingValue") or entry.get("startValue")),
        "ending_value": _safe_int(entry.get("endingValue") or entry.get("endValue")),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


def normalise_heart_rate(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_heart_rates() response to the heart_rate table row."""
    if not raw:
        return None
    now = datetime.now(timezone.utc).isoformat()
    return {
        "date": date_str,
        "resting_hr": _safe_int(raw.get("restingHeartRate")),
        "max_hr": _safe_int(raw.get("maxHeartRate")),
        "min_hr": _safe_int(raw.get("minHeartRate")),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Intraday health time-series normalisation (Phase 7)
# ---------------------------------------------------------------------------


def normalise_intraday_heart_rate(raw: dict, date_str: str, raw_payload_id: int) -> list[dict]:
    """Extract per-minute HR rows from get_heart_rates() response.

    heartRateValues is a list of [timestamp_ms, bpm] pairs. Null bpm values are
    kept (sleep/no-wear gaps) so the time axis stays continuous.
    """
    values = raw.get("heartRateValues") or []
    rows = []
    for entry in values:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        ts_ms, bpm = entry[0], entry[1]
        ts = _ms_to_iso(ts_ms)
        if ts is None:
            continue
        rows.append({
            "date": date_str,
            "timestamp_utc": ts,
            "heart_rate": _safe_int(bpm),
            "raw_payload_id": raw_payload_id,
        })
    return rows


def normalise_intraday_stress(raw: dict, date_str: str, raw_payload_id: int) -> list[dict]:
    """Extract per-sample stress rows from get_all_day_stress() response.

    stressValuesArray is a list of [timestamp_ms, stress_level] pairs.
    stress_level of -1 or -2 means no reading (rest/unmeasured) — kept as NULL.
    """
    values = raw.get("stressValuesArray") or []
    rows = []
    for entry in values:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        ts_ms, level = entry[0], entry[1]
        ts = _ms_to_iso(ts_ms)
        if ts is None:
            continue
        stress = _safe_int(level)
        if stress is not None and stress < 0:
            stress = None
        rows.append({
            "date": date_str,
            "timestamp_utc": ts,
            "stress_level": stress,
            "raw_payload_id": raw_payload_id,
        })
    return rows


def normalise_intraday_steps(raw: list, date_str: str, raw_payload_id: int) -> list[dict]:
    """Extract per-block step rows from get_steps_data() response.

    Each entry is a dict with startGMT, endGMT, steps, primaryActivityLevel.
    startGMT is a local-time string like '2024-03-15 06:30:00' — stored as-is
    since we don't have timezone offset in the payload.
    """
    if not isinstance(raw, list):
        return []
    rows = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        start_gmt = entry.get("startGMT") or entry.get("startGmt")
        if not start_gmt:
            continue
        rows.append({
            "date": date_str,
            "timestamp_utc": str(start_gmt),
            "steps": _safe_int(entry.get("steps")),
            "activity_level": entry.get("primaryActivityLevel"),
            "raw_payload_id": raw_payload_id,
        })
    return rows


def normalise_intraday_respiration(raw: dict, date_str: str, raw_payload_id: int) -> list[dict]:
    """Extract per-sample respiration rows from get_respiration_data() response.

    respirationValues is a list of [timestamp_ms, breaths_per_min] pairs.
    """
    values = raw.get("respirationValues") or []
    rows = []
    for entry in values:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        ts_ms, bpm = entry[0], entry[1]
        ts = _ms_to_iso(ts_ms)
        if ts is None:
            continue
        rows.append({
            "date": date_str,
            "timestamp_utc": ts,
            "breaths_per_min": _safe_float(bpm),
            "raw_payload_id": raw_payload_id,
        })
    return rows


def normalise_activity(raw: dict) -> dict | None:
    """
    Map a Garmin activity summary dict to the activity table row dict.

    Returns None if activity_id cannot be determined (unrecoverable).
    All other missing fields produce None.

    Field names are taken from observed garminconnect responses.
    They may change if Garmin alters their API — adjust here if needed.
    """
    activity_id_raw = raw.get("activityId")
    if activity_id_raw is None:
        logger.warning("normalise_activity: activityId missing, skipping normalisation")
        return None

    activity_id = str(activity_id_raw)

    # Activity type may be a nested dict or a plain string.
    activity_type_raw = raw.get("activityType")
    if isinstance(activity_type_raw, dict):
        activity_type = activity_type_raw.get("typeKey") or activity_type_raw.get("typeId")
        if activity_type is not None:
            activity_type = str(activity_type)
    elif activity_type_raw is not None:
        activity_type = str(activity_type_raw)
    else:
        activity_type = None

    # Cadence: running vs cycling use different field names.
    avg_cadence = _safe_float(
        raw.get("averageRunningCadenceInStepsPerMinute")
        or raw.get("averageBikingCadenceInRevPerMinute")
        or raw.get("averageCadence")
    )
    max_cadence = _safe_float(
        raw.get("maxRunningCadenceInStepsPerMinute")
        or raw.get("maxBikingCadenceInRevPerMinute")
        or raw.get("maxCadence")
    )

    now = datetime.now(timezone.utc).isoformat()

    return {
        "activity_id": activity_id,
        "name": raw.get("activityName"),
        "type": activity_type,
        "start_time": raw.get("startTimeGMT"),
        "start_time_local": raw.get("startTimeLocal"),
        "distance_m": _safe_float(raw.get("distance")),
        "duration_s": _safe_float(raw.get("duration")),
        "moving_duration_s": _safe_float(raw.get("movingDuration")),
        "elapsed_duration_s": _safe_float(raw.get("elapsedDuration")),
        "avg_hr": _safe_int(raw.get("averageHR")),
        "max_hr": _safe_int(raw.get("maxHR")),
        "avg_cadence": avg_cadence,
        "max_cadence": max_cadence,
        "avg_power": _safe_float(raw.get("avgPower")),
        "max_power": _safe_float(raw.get("maxPower")),
        "elevation_gain_m": _safe_float(raw.get("elevationGain")),
        "elevation_loss_m": _safe_float(raw.get("elevationLoss")),
        "avg_speed_mps": _safe_float(raw.get("averageSpeed")),
        "max_speed_mps": _safe_float(raw.get("maxSpeed")),
        "calories": _safe_int(raw.get("calories")),
        "training_effect": _safe_float(raw.get("trainingEffectLabel")),
        "aerobic_te": _safe_float(raw.get("aerobicTrainingEffect")),
        "anaerobic_te": _safe_float(raw.get("anaerobicTrainingEffect")),
        "vo2max": _safe_float(raw.get("vO2MaxValue")),
        # raw_payload_id must be set by the caller before inserting.
        "raw_payload_id": None,
        "updated_at": now,
    }


def normalise_activity_detail(raw: dict) -> dict | None:
    """
    Map a Garmin get_activity() response to the activity_detail table row.

    get_activity() returns activity summary plus a 'laps' list.
    Returns None if activity_id is missing.
    """
    activity_id_raw = raw.get("activityId")
    if activity_id_raw is None:
        logger.warning("normalise_activity_detail: activityId missing")
        return None

    laps = raw.get("laps")
    has_laps = 1 if laps else 0
    has_splits = 1 if raw.get("splits") else 0

    # Time-series samples may appear under activityDetail.measurements.
    ad = raw.get("activityDetail") or {}
    measurements = ad.get("measurements")
    sample_count = len(measurements) if isinstance(measurements, list) else None

    now = datetime.now(timezone.utc).isoformat()
    return {
        "activity_id": str(activity_id_raw),
        "has_splits": has_splits,
        "has_laps": has_laps,
        "sample_count": sample_count,
        "raw_payload_id": None,  # set by caller
        "updated_at": now,
    }


def normalise_activity_laps(
    raw: dict, activity_id: str, raw_payload_id: int
) -> list[dict]:
    """
    Extract laps from a Garmin get_activity() response.

    Returns an empty list if the response has no laps or laps is not a list.
    Defensive: missing fields produce None, bad types are skipped.

    Garmin lap field names vary slightly by activity type; we try common
    alternatives for cadence and elevation so running, cycling and other
    types all work.
    """
    laps_raw = raw.get("laps")
    if not isinstance(laps_raw, list) or not laps_raw:
        return []

    now = datetime.now(timezone.utc).isoformat()
    result = []

    for i, lap in enumerate(laps_raw):
        if not isinstance(lap, dict):
            logger.warning("Lap %d for activity %s is not a dict, skipping.", i, activity_id)
            continue

        avg_cadence = _safe_float(
            lap.get("averageRunCadence")
            or lap.get("averageCadence")
            or lap.get("averageRunningCadenceInStepsPerMinute")
            or lap.get("averageBikingCadenceInRevPerMinute")
        )
        elevation_gain = _safe_float(
            lap.get("gainElevation") or lap.get("elevationGain")
        )
        elevation_loss = _safe_float(
            lap.get("lossElevation") or lap.get("elevationLoss")
        )

        result.append({
            "activity_id": activity_id,
            "lap_index": lap.get("lapIndex", i),
            "start_time": lap.get("startTimeGMT"),
            "distance_m": _safe_float(lap.get("distance")),
            "duration_s": _safe_float(lap.get("duration")),
            "moving_duration_s": _safe_float(lap.get("movingDuration")),
            "avg_hr": _safe_int(lap.get("averageHR")),
            "max_hr": _safe_int(lap.get("maxHR")),
            "avg_cadence": avg_cadence,
            "avg_power": _safe_float(lap.get("avgPower")),
            "elevation_gain_m": elevation_gain,
            "elevation_loss_m": elevation_loss,
            "raw_payload_id": raw_payload_id,
            "updated_at": now,
        })

    return result


def normalise_activity_derived(
    summary_raw: dict, detail_raw: dict | None = None
) -> dict:
    """
    Extract derived fields from activity summary and optional detail payloads.

    Prefers detail_raw["summaryDTO"] values where available, falls back to
    summary_raw. Returns a dict with all derived keys; missing fields are None.
    """
    detail_summary = detail_raw.get("summaryDTO", {}) if detail_raw else {}

    def _ds(key: str):
        """Get from detail summaryDTO first, fall back to summary_raw."""
        v = detail_summary.get(key)
        if v is not None:
            return v
        return summary_raw.get(key)

    # HR zones come only from summary_raw
    def _zone_s(n: int) -> int | None:
        v = summary_raw.get(f"hrTimeInZone_{n}")
        if v is None:
            return None
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    pr_raw = summary_raw.get("pr")
    is_pr = 1 if pr_raw else 0

    return {
        "training_load": _safe_float(_ds("activityTrainingLoad")),
        "activity_steps": _safe_int(_ds("steps")),
        "body_battery_delta": _safe_int(_ds("differenceBodyBattery")),
        "avg_respiration_rate": _safe_float(_ds("avgRespirationRate")),
        "hr_zone_1_s": _zone_s(1),
        "hr_zone_2_s": _zone_s(2),
        "hr_zone_3_s": _zone_s(3),
        "hr_zone_4_s": _zone_s(4),
        "hr_zone_5_s": _zone_s(5),
        "norm_power": _safe_float(
            detail_summary.get("normalizedPower") or summary_raw.get("normPower")
        ),
        "fastest_km_s": _safe_float(summary_raw.get("fastestSplit_1000")),
        "fastest_mile_s": _safe_float(summary_raw.get("fastestSplit_1609")),
        "fastest_5k_s": _safe_float(summary_raw.get("fastestSplit_5000")),
        "temp_avg_c": _safe_float(_ds("averageTemperature")),
        "temp_min_c": _safe_float(_ds("minTemperature")),
        "temp_max_c": _safe_float(_ds("maxTemperature")),
        "water_estimated_ml": _safe_float(summary_raw.get("waterEstimated")),
        "is_pr": is_pr,
        "stamina_start": _safe_float(detail_summary.get("beginPotentialStamina")),
        "stamina_end": _safe_float(detail_summary.get("endPotentialStamina")),
        "stamina_min": _safe_float(detail_summary.get("minAvailableStamina")),
        "total_work_j": _safe_float(detail_summary.get("totalWork")),
        "ground_contact_ms": _safe_float(
            detail_summary.get("groundContactTime") or summary_raw.get("avgGroundContactTime")
        ),
        "ground_contact_balance_left": _safe_float(
            detail_summary.get("groundContactBalanceLeft") or summary_raw.get("avgGroundContactBalance")
        ),
        "vertical_oscillation_cm": _safe_float(
            detail_summary.get("verticalOscillation") or summary_raw.get("avgVerticalOscillation")
        ),
        "vertical_ratio_pct": _safe_float(
            detail_summary.get("verticalRatio") or summary_raw.get("avgVerticalRatio")
        ),
        "stride_length_cm": _safe_float(
            detail_summary.get("strideLength") or summary_raw.get("avgStrideLength")
        ),
    }


def normalise_activity_splits(
    detail_raw: dict | None,
    summary_raw: dict,
    activity_id: str,
    raw_payload_id: int,
) -> list[dict]:
    """
    Extract splitSummaries from activity detail or summary payloads.

    Prefers detail_raw["splitSummaries"] (richer, has HR).
    Falls back to summary_raw["splitSummaries"].
    Returns empty list if no splitSummaries found.
    """
    splits_raw = None
    if detail_raw is not None:
        splits_raw = detail_raw.get("splitSummaries")
    if not splits_raw:
        splits_raw = summary_raw.get("splitSummaries")
    if not splits_raw:
        return []

    now = datetime.now(timezone.utc).isoformat()
    result = []

    for i, split in enumerate(splits_raw):
        if not isinstance(split, dict):
            continue

        avg_hr_raw = split.get("averageHR")
        avg_hr = int(avg_hr_raw) if avg_hr_raw is not None else None

        max_hr_raw = split.get("maxHR")
        max_hr = int(max_hr_raw) if max_hr_raw is not None else None

        calories_raw = split.get("calories")
        calories = int(calories_raw) if calories_raw is not None else None

        avg_cadence = _safe_float(
            split.get("averageRunCadence") or split.get("avgStepFrequency")
        )

        result.append({
            "activity_id": activity_id,
            "split_index": i,
            "split_type": split.get("splitType"),
            "distance_m": _safe_float(split.get("distance")),
            "duration_s": _safe_float(split.get("duration")),
            "moving_duration_s": _safe_float(split.get("movingDuration")),
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "avg_speed_mps": _safe_float(split.get("averageSpeed")),
            "avg_cadence": avg_cadence,
            "avg_power": _safe_float(split.get("averagePower")),
            "max_power": _safe_float(split.get("maxPower")),
            "norm_power": _safe_float(split.get("normalizedPower")),
            "calories": calories,
            "elevation_gain_m": _safe_float(split.get("elevationGain")),
            "elevation_loss_m": _safe_float(split.get("elevationLoss")),
            "ground_contact_ms": _safe_float(split.get("groundContactTime")),
            "vertical_oscillation_cm": _safe_float(split.get("verticalOscillation")),
            "raw_payload_id": raw_payload_id,
            "updated_at": now,
        })

    return result


_SAMPLE_METRIC_MAP: dict[str, str] = {
    "directTimestamp": "ts_ms",
    "directLatitude": "lat",
    "directLongitude": "lon",
    "directAltitude": "altitude_m",
    "directHeartRate": "heart_rate",
    "directSpeed": "speed_mps",
    "directCadence": "cadence",
    "directPower": "power_w",
    "directRespirationRate": "respiration_rate",
    "directDistance": "distance_m",
    "directGroundContactTime": "ground_contact_ms",
    "directGroundContactBalance": "ground_contact_balance_left",
    "directVerticalOscillation": "vertical_oscillation_cm",
    "directVerticalRatio": "vertical_ratio_pct",
    "directStrideLength": "stride_length_cm",
    # alternate key names observed on some devices/activity types
    "latitude": "lat",
    "longitude": "lon",
    "altitude": "altitude_m",
    "heartRate": "heart_rate",
    "speed": "speed_mps",
    "cadence": "cadence",
    "power": "power_w",
    "directRunCadence": "cadence",
    "sumDistance": "distance_m",
    "directGroundContactBalanceLeft": "ground_contact_balance_left",
}

_INT_SAMPLE_FIELDS = {"heart_rate", "cadence"}


def normalise_activity_samples(
    raw: dict, activity_id: str, raw_payload_id: int
) -> list[dict]:
    """
    Extract per-sample time-series from a get_activity_details() response.

    Uses metricDescriptors to map array indices to field names, then converts
    each activityDetailMetrics entry to a flat row dict. GPS coordinates are
    taken from the metrics array if present (directLatitude/Longitude), falling
    back to geoPolylineDTO by index when the polyline has the same length.

    Returns an empty list if there are no chart samples.
    """
    descriptors = raw.get("metricDescriptors") or []
    chart_data = raw.get("activityDetailMetrics") or []
    polyline = (raw.get("geoPolylineDTO") or {}).get("polyline") or []

    if not chart_data:
        return []

    idx_to_field: dict[int, str] = {}
    for desc in descriptors:
        if not isinstance(desc, dict):
            continue
        key = desc.get("key") or ""
        idx = desc.get("metricsIndex")
        if idx is None:
            continue
        mapped = _SAMPLE_METRIC_MAP.get(key)
        if mapped:
            idx_to_field[int(idx)] = mapped

    use_polyline_gps = (
        not any(f in idx_to_field.values() for f in ("lat", "lon"))
        and len(polyline) == len(chart_data)
    )

    result = []
    for i, entry in enumerate(chart_data):
        if not isinstance(entry, dict):
            continue

        metrics = entry.get("metrics") or []
        ts_ms: object = None

        row: dict = {
            "activity_id": activity_id,
            "sample_index": i,
            "timestamp_utc": entry.get("startGMT"),
            "distance_m": None,
            "heart_rate": None,
            "speed_mps": None,
            "cadence": None,
            "power_w": None,
            "altitude_m": None,
            "lat": None,
            "lon": None,
            "respiration_rate": None,
            "ground_contact_ms": None,
            "ground_contact_balance_left": None,
            "vertical_oscillation_cm": None,
            "vertical_ratio_pct": None,
            "stride_length_cm": None,
            "raw_payload_id": raw_payload_id,
        }

        for idx, field in idx_to_field.items():
            if idx >= len(metrics):
                continue
            val = metrics[idx]
            if field == "ts_ms":
                ts_ms = val
            elif field in _INT_SAMPLE_FIELDS:
                row[field] = _safe_int(val)
            else:
                row[field] = _safe_float(val)

        if ts_ms is not None:
            row["timestamp_utc"] = _ms_to_iso(ts_ms)

        if use_polyline_gps and isinstance(polyline[i], dict):
            row["lat"] = _safe_float(polyline[i].get("lat"))
            row["lon"] = _safe_float(polyline[i].get("lon"))
            if row["altitude_m"] is None:
                row["altitude_m"] = _safe_float(polyline[i].get("altitude"))

        result.append(row)

    return result


def normalise_daily_summary_derived(raw: dict) -> dict:
    """
    Extract derived fields from daily_summary raw payload.

    Returns a dict with all derived keys; missing fields are None.
    """
    return {
        "average_spo2": _safe_float(raw.get("averageSpo2")),
        "latest_spo2": _safe_float(raw.get("latestSpo2")),
        "lowest_spo2": _safe_float(raw.get("lowestSpo2")),
        "body_battery_highest": _safe_int(raw.get("bodyBatteryHighestValue")),
        "body_battery_lowest": _safe_int(raw.get("bodyBatteryLowestValue")),
        "body_battery_at_wake": _safe_int(raw.get("bodyBatteryAtWakeTime")),
        "sedentary_seconds": _safe_int(raw.get("sedentarySeconds")),
        "resting_hr_7d_avg": _safe_float(raw.get("lastSevenDaysAvgRestingHeartRate")),
    }


# ---------------------------------------------------------------------------
# Performance / training metrics (Phase 5b)
# ---------------------------------------------------------------------------


def normalise_lactate_threshold(raw: dict, raw_payload_id: int) -> list[dict]:
    """Merge speed/heart_rate/power arrays by updatedDate. Returns list (may be empty)."""
    if not raw:
        return []
    now = datetime.now(timezone.utc).isoformat()
    entries: dict[str, dict] = {}
    for item in raw.get("speed") or []:
        d = item.get("updatedDate") or item.get("from")
        if d:
            entries.setdefault(d, {})["speed"] = _safe_float(item.get("value"))
            entries[d]["series"] = item.get("series")
    for item in raw.get("heart_rate") or []:
        d = item.get("updatedDate") or item.get("from")
        if d:
            entries.setdefault(d, {})["hr"] = _safe_int(item.get("value"))
    for item in raw.get("power") or []:
        d = item.get("updatedDate") or item.get("from")
        if d:
            entries.setdefault(d, {})["power"] = _safe_float(item.get("value"))
    return [
        {
            "date": d,
            "threshold_hr": data.get("hr"),
            "threshold_speed_value": data.get("speed"),
            "threshold_power_w": data.get("power"),
            "series": data.get("series"),
            "raw_payload_id": raw_payload_id,
            "updated_at": now,
        }
        for d, data in entries.items()
    ]


def normalise_race_prediction(entry: dict, raw_payload_id: int) -> dict | None:
    """One entry from the race_predictions list."""
    date_str = entry.get("calendarDate")
    if not date_str:
        return None
    return {
        "date": date_str,
        "race_5k_s":   _safe_int(entry.get("time5K")),
        "race_10k_s":  _safe_int(entry.get("time10K")),
        "race_half_s": _safe_int(entry.get("timeHalfMarathon")),
        "race_full_s": _safe_int(entry.get("timeMarathon")),
        "raw_payload_id": raw_payload_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def normalise_endurance_score(raw: dict, raw_payload_id: int) -> list[dict]:
    """Returns list: one row from enduranceScoreDTO plus one per groupMap week entry."""
    if not raw:
        return []
    now = datetime.now(timezone.utc).isoformat()
    results = []
    dto = raw.get("enduranceScoreDTO") or {}
    if dto.get("calendarDate"):
        results.append({
            "date": dto["calendarDate"],
            "score": _safe_int(dto.get("overallScore")),
            "classification": _safe_int(dto.get("classification")),
            "raw_payload_id": raw_payload_id,
            "updated_at": now,
        })
    for week_date, week_data in (raw.get("groupMap") or {}).items():
        if isinstance(week_data, dict):
            results.append({
                "date": week_date,
                "score": _safe_int(week_data.get("groupAverage")),
                "classification": None,
                "raw_payload_id": raw_payload_id,
                "updated_at": now,
            })
    return results


def normalise_hill_score_entry(entry: dict, raw_payload_id: int) -> dict | None:
    """One entry from hillScoreDTOList."""
    date_str = entry.get("calendarDate")
    if not date_str:
        return None
    return {
        "date": date_str,
        "overall_score":        _safe_int(entry.get("overallScore")),
        "strength_score":       _safe_int(entry.get("strengthScore")),
        "hill_endurance_score": _safe_int(entry.get("enduranceScore")),
        "classification":       _safe_int(entry.get("hillScoreClassificationId")),
        "raw_payload_id": raw_payload_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def normalise_training_status(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_training_status() response to training_status row."""
    if not raw:
        return None
    now = datetime.now(timezone.utc).isoformat()
    vo2_generic = (raw.get("mostRecentVO2Max") or {}).get("generic") or {}
    ts_map = ((raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")) or {}
    ts_data = next((v for v in ts_map.values() if v.get("primaryTrainingDevice")), next(iter(ts_map.values()), {})) if ts_map else {}
    lb_map = ((raw.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap")) or {}
    lb_data = next((v for v in lb_map.values() if v.get("primaryTrainingDevice")), next(iter(lb_map.values()), {})) if lb_map else {}
    atl = ts_data.get("acuteTrainingLoadDTO") or {}
    return {
        "date": date_str,
        "vo2max":                _safe_float(vo2_generic.get("vo2MaxValue")),
        "vo2max_precise":        _safe_float(vo2_generic.get("vo2MaxPreciseValue")),
        "training_status_code":  _safe_int(ts_data.get("trainingStatus")),
        "training_status_phrase": ts_data.get("trainingStatusFeedbackPhrase"),
        "load_aerobic_low":      _safe_float(lb_data.get("monthlyLoadAerobicLow")),
        "load_aerobic_high":     _safe_float(lb_data.get("monthlyLoadAerobicHigh")),
        "load_anaerobic":        _safe_float(lb_data.get("monthlyLoadAnaerobic")),
        "load_feedback":         lb_data.get("trainingBalanceFeedbackPhrase"),
        "acute_load":            _safe_int(atl.get("dailyTrainingLoadAcute")),
        "chronic_load":          _safe_int(atl.get("dailyTrainingLoadChronic")),
        "acwr":                  _safe_float(atl.get("dailyAcuteChronicWorkloadRatio")),
        "raw_payload_id": raw_payload_id,
        "updated_at": now,
    }


def normalise_training_readiness(raw_list: list, date_str: str, raw_payload_id: int) -> dict | None:
    """
    Map get_training_readiness() list to training_readiness row.

    Primary entry: latest non-morning entry (AFTER_POST_EXERCISE_RESET or similar),
    falling back to first entry.
    Morning entry: AFTER_WAKEUP_RESET (stored in morning_* columns).
    Recovery time values from Garmin are in minutes.
    """
    if not raw_list:
        return None
    morning = next((e for e in raw_list if e.get("inputContext") == "AFTER_WAKEUP_RESET"), None)
    non_morning = [e for e in raw_list if e.get("inputContext") != "AFTER_WAKEUP_RESET"]
    entry = non_morning[0] if non_morning else raw_list[0]
    return {
        "date": date_str,
        "score":              _safe_int(entry.get("score")),
        "level":              entry.get("level"),
        "recovery_time_min":  _safe_int(entry.get("recoveryTime")),
        "acwr_factor_pct":    _safe_int(entry.get("acwrFactorPercent")),
        "acute_load":         _safe_int(entry.get("acuteLoad")),
        "hrv_factor_pct":     _safe_int(entry.get("hrvFactorPercent")),
        "sleep_factor_pct":   _safe_int(entry.get("sleepHistoryFactorPercent")),
        "stress_factor_pct":  _safe_int(entry.get("stressHistoryFactorPercent")),
        "feedback_short":     entry.get("feedbackShort"),
        "morning_readiness_score":    _safe_int(morning.get("score")) if morning else None,
        "morning_readiness_level":    morning.get("level") if morning else None,
        "morning_recovery_time_min":  _safe_int(morning.get("recoveryTime")) if morning else None,
        "raw_payload_id": raw_payload_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def normalise_max_metrics(raw_list: list, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_max_metrics() list to max_metrics row. Returns None if no VO2max data."""
    if not raw_list:
        return None
    entry = next(
        (e for e in raw_list if isinstance(e.get("generic"), dict)
         and e["generic"].get("calendarDate") == date_str),
        next((e for e in raw_list if isinstance(e.get("generic"), dict)), None),
    )
    if not entry:
        return None
    generic = entry.get("generic") or {}
    if generic.get("vo2MaxValue") is None and generic.get("vo2MaxPreciseValue") is None:
        return None
    return {
        "date": date_str,
        "vo2max":         _safe_float(generic.get("vo2MaxValue")),
        "vo2max_precise": _safe_float(generic.get("vo2MaxPreciseValue")),
        "fitness_age":    _safe_float(generic.get("fitnessAge")),
        "fitness_age_desc": generic.get("fitnessAgeDescription"),
        "raw_payload_id": raw_payload_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def normalise_fitness_age(raw: dict, date_str: str, raw_payload_id: int) -> dict | None:
    """Map get_fitnessage_data() response to fitness_age row."""
    if not raw:
        return None
    return {
        "date": date_str,
        "fitness_age":            _safe_float(raw.get("fitnessAge")),
        "achievable_fitness_age": _safe_float(raw.get("achievableFitnessAge")),
        "chronological_age":      _safe_int(raw.get("chronologicalAge")),
        "raw_payload_id": raw_payload_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
