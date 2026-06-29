# Command Reference

## Setup

```bash
garmin-sync init-db          # Create/migrate database (safe to rerun)
garmin-sync auth             # Authenticate with Garmin Connect
```

---

## Sync Commands

### Activities
```bash
garmin-sync sync-recent-activities [--limit 20]      # Incremental sync (run daily)
garmin-sync backfill-activities [--page-size 100]     # Full historical backfill (run once)
garmin-sync sync-activity-details [--limit 50]       # Fetch lap/split detail for recent activities
garmin-sync backfill-activity-details                # Fetch detail for all activities
garmin-sync sync-activity-samples [--limit 5]        # Fetch time-series samples for recent activities
garmin-sync backfill-activity-samples                # Fetch time-series samples for all activities (run once)
```

Time-series samples include HR, pace, GPS, cadence, power, altitude, and running dynamics at ~1Hz (up to 2000 samples per activity). The backfill is resumable — already-synced activities are skipped automatically.

### Daily Health
```bash
garmin-sync sync-health --from 2020-04-01 --to 2026-06-08   # Full backfill
garmin-sync sync-recent-health [--days 7]                    # Recent days (run daily)
```

### Intraday Health Time-Series
```bash
garmin-sync sync-intraday [--days 7]                         # Recent days (run daily, wired into sync-all)
garmin-sync backfill-intraday [--from-date 2020-01-01]       # Full historical backfill (resumable, run once)
```

Per-minute HR, per-~4min stress, per-15min steps, and per-sample respiration rate.
Full backfill is ~9000 API calls (~2260 days × 4 endpoints) and adds ~270MB to the database.
The backfill is resumable — if interrupted, rerun the same command to continue from where it stopped.

### Performance Metrics
```bash
# Range-based metrics (lactate threshold, race predictions, endurance score, hill score)
# Low API cost (~10 calls total for full history)
garmin-sync sync-performance-ranges [--from 2020-04-01] [--to 2026-06-08]

# Per-day performance metrics (training status, training readiness, max metrics, fitness age)
# Defaults to last 7 days if --from/--to are omitted
garmin-sync sync-performance [--from YYYY-MM-DD] [--to YYYY-MM-DD]

# Full historical backfill of per-day performance metrics (resumable)
garmin-sync backfill-performance [--from 2020-04-01] [--to YYYY-MM-DD]
```

All sync commands accept `--dry-run` (fetches Garmin, no DB writes) and `--log-level DEBUG`.

---

## Query Commands

All query commands are **read-only** and make **no Garmin API calls**.

### Activities
```bash
garmin-sync query-recent-activities [--limit 20] [--type running] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-activity <activity-id>
garmin-sync query-activity-splits <activity-id>
garmin-sync query-weekly-running-volume [--weeks 12]
garmin-sync query-monthly-running-volume [--months 12]
garmin-sync query-intensity-distribution [--weeks 12]
garmin-sync query-running-dynamics [--days 90]
```

### Health
```bash
garmin-sync query-sleep-trend [--days 30]
garmin-sync query-hrv-trend [--days 30]
garmin-sync query-resting-hr-trend [--days 30]
garmin-sync query-stress-trend [--days 30]
garmin-sync query-body-battery-trend [--days 30]
garmin-sync query-training-vs-sleep [--days 90]
```

### Performance Metrics
All performance query commands accept `--days N`, `--from YYYY-MM-DD`, `--to YYYY-MM-DD`, and `--json`.
```bash
garmin-sync query-lactate-threshold [--days 365] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-race-predictions [--days 365] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-endurance-score [--days 365] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-hill-score [--days 365] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-training-status [--days 90] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-training-readiness [--days 90] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-vo2max-trend [--days 180] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-performance-summary [--days 90] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
```

---

## Export Commands

Both commands write `activities.json`/`activities.csv` and `daily_health.json`/`daily_health.csv` to the output directory. Raw payloads are not exported.

```bash
garmin-sync export-json [--output-dir exports] [--from 2025-01-01] [--to 2025-12-31]
garmin-sync export-csv  [--output-dir exports] [--from 2025-01-01] [--to 2025-12-31]
```

---

## Maintenance Commands

```bash
garmin-sync status [--json]             # Show counts, cursors, recent runs

garmin-sync reprocess-activity-derived  # Rebuild derived activity fields from raw
garmin-sync reprocess-health-derived    # Rebuild derived daily_summary fields from raw
garmin-sync reprocess-sleep             # Fix sleep_score and avg_spo2 from raw
garmin-sync reprocess-hrv              # Fix HRV last_night_avg from raw
garmin-sync reprocess-performance-derived  # Rebuild performance tables from raw
garmin-sync reprocess-all-derived      # Run all of the above
```

---

## Global Options

```bash
--config PATH      Alternative .env file
--db PATH          Override database path
--log-level TEXT   Override log level (DEBUG, INFO, WARNING)
```
