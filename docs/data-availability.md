# Garmin Data Availability

Inspection date: 2026-06-08. Based on 3,764 activity payloads and 241 daily health payloads in the local database.

---

## 1. Already in raw_payload but NOT normalised

Zero new API calls needed for any of these.

### activity_summary (3,764 payloads)

| Field | Coverage | Notes |
|---|---|---|
| `activityTrainingLoad` | 85% | Training load score per activity |
| `steps` | 68% | Step count for the activity |
| `differenceBodyBattery` | 32% | Body battery change during activity |
| `splitSummaries` | 25% | Per-km/mile splits with avg HR, speed, cadence, power, elevation |
| `avgRespirationRate` | 20% | Average respiration per activity |
| `hrTimeInZone_1..5` | 17% | Time in each HR zone in seconds |
| `avgGroundContactTime` | 10% | Running dynamics: ground contact time (ms) |
| `avgVerticalOscillation` | 10% | Running dynamics: bounce (cm) |
| `avgVerticalRatio` | 10% | Running dynamics: vertical ratio (%) |
| `avgStrideLength` | 10% | Stride length (cm) |
| `normPower` / `normalizedPower` | 10% | Normalized power (watts) |
| `fastestSplit_1000/1609/5000` | 8% | Fastest 1km / 1mi / 5km split in seconds |
| `maxTemperature`, `minTemperature` | ~80% | Activity temperature range |
| `waterEstimated` | ~80% | Estimated sweat loss (ml) |
| `pr` | 0.2% | Whether activity set a personal record |

### activity_detail → summaryDTO (3,764 payloads)

Richer version of activity_summary fields, plus:

| Field | Coverage | Notes |
|---|---|---|
| `beginPotentialStamina` | 48% | Garmin stamina model: start of activity |
| `endPotentialStamina` | 48% | Garmin stamina model: end of activity |
| `minAvailableStamina` | 48% | Garmin stamina model: lowest point |
| `totalWork` | ~50% | Total mechanical work in joules |
| `groundContactBalanceLeft` | ~10% | Left/right ground contact balance (%) |
| `averageTemperature` | ~80% | Activity average temperature |

### activity_detail → splitSummaries (51% of payloads)

Per-split records (by km or mile) with richer fields than laps:
`averageHR`, `maxHR`, `averagePower`, `maxPower`, `averageRunCadence`, `averageSpeed`,
`groundContactTime`, `verticalOscillation`, `normalizedPower`, `splitType`,
`calories`, `elevationGain`, `elevationLoss`, `movingDuration`

### daily_summary (241 payloads): unnormalised useful fields

| Field | Notes |
|---|---|
| `averageSpo2`, `latestSpo2`, `lowestSpo2` | Daytime SpO2: different from the sleep SpO2 already stored |
| `bodyBatteryHighestValue`, `bodyBatteryLowestValue`, `bodyBatteryAtWakeTime` | More body battery detail than charged/drained/start/end |
| `totalDistanceMeters` | Total daily distance across all activities |
| `sedentarySeconds` | Time sedentary in seconds |
| `lastSevenDaysAvgRestingHeartRate` | Rolling 7-day resting HR average |

---

## 2. New API calls required

| Method | Signature | Cadence | Notes |
|---|---|---|---|
| `get_training_status(cdate)` | per-day | Daily | Training load, VO2max, recovery time, status label |
| `get_max_metrics(cdate)` | per-day | Daily | VO2max value trend, fitness age |
| `get_morning_training_readiness(cdate)` | per-day | Daily | Readiness score + contributing factors (HRV, sleep, load) |
| `get_training_readiness(cdate)` | per-day | Daily | Similar to morning readiness, slightly different model |
| `get_lactate_threshold(start, end, aggregation)` | date range | One-time + periodic | LT heart rate, speed, power trend |
| `get_race_predictions(startdate, enddate)` | date range | One-time + periodic | Predicted 5k / 10k / HM / marathon times over time |
| `get_endurance_score(startdate, enddate)` | date range | One-time + periodic | Garmin endurance score trend |
| `get_hill_score(startdate, enddate)` | date range | One-time + periodic | Hill climbing score trend |
| `get_activity_splits(activity_id)` | per-activity | One-time backfill | Per-km/mi splits: richer than laps, 3,764 calls |
| `get_gear()` / `get_gear_stats(uuid)` | one-time + periodic | Infrequent | Shoe mileage, gear linked to activities |
| `get_personal_record()` | one-time | Infrequent | All-time PRs by sport |
| `get_body_composition(startdate, enddate)` | date range | Only if Index scale | Weight, body fat %, BMI, muscle mass |

---

## 3. Priority ranking

### High: implement next

1. **`activityTrainingLoad` normalisation**: already in raw, 85% coverage, zero new API calls. Most useful missing field for training analysis.
2. **`get_training_status`**: per-day training load, recovery time, VO2max trend, training status label. Performance backbone for running analysis.
3. **HR time-in-zones**: already in raw, no API calls. Essential for intensity distribution.
4. **Running dynamics**: already in raw (ground contact, vertical oscillation, stride length, vertical ratio). Normalise from existing data.

### High: implement soon

5. **`get_lactate_threshold`**: range query, one backfill call. Directly useful for threshold tracking.
6. **`get_morning_training_readiness`**: per-day, pairs well with HRV and sleep data already stored.
7. **`get_race_predictions`**: range query, shows fitness trend over time.
8. **Stamina fields**: already in activity_detail summaryDTO, zero new API calls.

### Medium

9. **Activity splits** (`get_activity_splits`): per-activity backfill, 3,764 API calls. Rich data but expensive to fetch.
10. **`get_endurance_score` / `get_hill_score`**: range queries, quick to add.
11. **Daily SpO2 fields**: already in daily_summary raw, just need normalisation.

### Low

12. Gear / shoe mileage: useful but not training-critical.
13. Personal records: one-time snapshot, not time-series.
14. Body composition: only useful with a Garmin Index scale.

---

## 4. Suggested schemas

Not implemented. Minimal tables for high-priority categories.

### `training_status` (per day, from `get_training_status`)
```sql
CREATE TABLE training_status (
    date                    TEXT PRIMARY KEY,
    training_load_acute     REAL,
    training_load_7d        REAL,
    training_load_28d       REAL,
    recovery_time_hours     INTEGER,
    vo2max                  REAL,
    training_status_label   TEXT,
    heat_acclimation_pct    REAL,
    altitude_acclimation_pct REAL,
    raw_payload_id          INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at              TEXT NOT NULL
);
```

### `training_readiness` (per day, from `get_morning_training_readiness`)
```sql
CREATE TABLE training_readiness (
    date                TEXT PRIMARY KEY,
    score               INTEGER,
    level               TEXT,
    hrv_factor          REAL,
    sleep_factor        REAL,
    recovery_factor     REAL,
    acute_load_factor   REAL,
    combined_readiness  REAL,
    raw_payload_id      INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at          TEXT NOT NULL
);
```

### `lactate_threshold` (per measurement date, from `get_lactate_threshold`)
```sql
CREATE TABLE lactate_threshold (
    date            TEXT PRIMARY KEY,
    lt_heart_rate   INTEGER,
    lt_speed_mps    REAL,
    ftp_watts       INTEGER,
    raw_payload_id  INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at      TEXT NOT NULL
);
```

### `race_predictions` (per date snapshot, from `get_race_predictions`)
```sql
CREATE TABLE race_predictions (
    date                TEXT PRIMARY KEY,
    time_5k_seconds     INTEGER,
    time_10k_seconds    INTEGER,
    time_hm_seconds     INTEGER,
    time_marathon_seconds INTEGER,
    raw_payload_id      INTEGER NOT NULL REFERENCES raw_payload(id),
    updated_at          TEXT NOT NULL
);
```

### Extended `activity` columns (normalise from existing raw: zero new API calls)
```
training_load, steps, body_battery_delta,
hr_zone_1_s, hr_zone_2_s, hr_zone_3_s, hr_zone_4_s, hr_zone_5_s,
avg_respiration, norm_power,
ground_contact_ms, vertical_oscillation_cm, vertical_ratio_pct, stride_length_cm,
fastest_km_s, fastest_mile_s,
temp_avg_c, temp_min_c, temp_max_c,
stamina_start, stamina_end, stamina_min
```
