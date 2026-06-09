# Architecture & Phase Status

## Overview

Local, resumable, idempotent mirror of Garmin Connect data into SQLite.
The sync app is the only Garmin API client. The MCP server queries SQLite only and never calls Garmin.

```
CLI (Typer)
  └─ SyncEngine classes
       ├─ GarminClient          ← single point of Garmin API access (rate-limited)
       ├─ Repository layer      ← all DB reads/writes (plain functions, conn as first arg)
       └─ Normaliser            ← pure functions: raw dict → normalised dict

MCP Server (FastMCP, stdio)
  └─ queries.py                 ← shared read-only query layer
       └─ SQLite (mode=ro)      ← same database, opened read-only
```

**Data flow (canonical):**
```
Garmin Connect API
  → GarminClient.*            [raw dict/list]
  → repo.upsert_raw_payload() → raw_payload table   (canonical, never deleted)
  → normalise.*()
  → repo.upsert_*()           → normalised tables   (derived, re-creatable)
```

The `raw_payload` table is canonical. Normalised tables are derived.
If normalisation fails, the raw payload is kept and the error is logged. No data loss.
All normalised tables can be rebuilt from `raw_payload` using `reprocess-*` commands.

---

## Database Tables

### Core
| Table | Key | Description |
|-------|-----|-------------|
| `raw_payload` | (data_type, garmin_id) or (data_type, date) | Canonical store of every Garmin API response |
| `sync_runs` | id | Audit log of every sync command run |
| `sync_cursor` | data_type | Resume state for paginated/date-range syncs |

### Activities (Phase 1 & 2)
| Table | Key | Description |
|-------|-----|-------------|
| `activity` | activity_id | Activity summaries with ~50 normalised fields |
| `activity_detail` | activity_id | Per-activity detail metadata (lap/split counts) |
| `activity_lap` | (activity_id, lap_index) | Per-lap metrics |
| `activity_split` | (activity_id, split_index) | Per-split metrics |

### Daily Health (Phase 3)
| Table | Key | Description |
|-------|-----|-------------|
| `daily_summary` | date | Steps, calories, HR, stress, SpO2, body battery |
| `sleep` | date | Sleep stages, score, SpO2, respiration |
| `hrv` | date | HRV weekly avg, last night avg, baseline, status |
| `stress` | date | Stress levels and duration by category |
| `body_battery` | date | Body battery charged/drained |
| `heart_rate` | date | Resting, min, max HR |

### Performance Metrics (Phase 5b)
| Table | Key | Description |
|-------|-----|-------------|
| `lactate_threshold` | date | LT heart rate, speed, power (sparse, from test dates) |
| `race_predictions` | date | Predicted 5K / 10K / half / full marathon times |
| `endurance_score` | date | Garmin endurance score and classification |
| `hill_score` | date | Hill score (overall, strength, endurance component) |
| `training_status` | date | VO2max, training status code/phrase, load balance, ACWR |
| `training_readiness` | date | Morning readiness score/level, recovery time, ACWR, HRV/sleep/stress factors |
| `max_metrics` | date | VO2max precise value and fitness age descriptor |
| `fitness_age` | date | Fitness age, achievable fitness age, chronological age |

---

## Phases

### Phase 1: Activity Sync ✅ Complete
- `sync-recent-activities`, `backfill-activities`
- 3,764 activities stored

### Phase 2: Activity Detail ✅ Complete
- `sync-activity-details`, `backfill-activity-details`
- Laps, splits per activity

### Phase 3: Daily Health ✅ Complete
- `sync-health`, `sync-recent-health`
- 2,260 days of data from 2020-04-01 to 2026-06-08

### Phase 4: Query & Export ✅ Complete
- `query-*` commands for activities, health, HRV, sleep, stress, running dynamics
- `export-json`, `export-csv`

### Phase 5: Derived Field Normalisation ✅ Complete
- 27 additional activity fields from existing raw payloads (training load, HR zones, running dynamics, stamina)
- 8 additional daily_summary fields (SpO2, body battery detail, sedentary time)
- Activity splits table
- `reprocess-*` commands to rebuild normalised tables from raw

### Phase 5b.1: Range Performance Metrics ✅ Complete
- `sync-performance-ranges` (4 range-based endpoints, ~10 API calls total)
- `lactate_threshold`, `race_predictions`, `endurance_score`, `hill_score` tables
- Full query layer for all 5b tables

### Phase 5b.2: Per-Day Performance Metrics ✅ Complete
- `sync-performance`, `backfill-performance`
- `training_status` (VO2max, training load balance, ACWR)
- `training_readiness` (morning readiness score, recovery time, HRV/sleep/stress factors)
- `max_metrics` (precise VO2max per day)
- `fitness_age` (fitness age, achievable fitness age)

### Phase 6: MCP Server ✅ Complete
- Read-only server implemented in `garmin_sync/mcp_server.py`
- 23 tools covering activities, health, and all performance metrics
- Delegates entirely to `queries.py`; no SQL in the server itself
- See MCP Layer section below for details

### Phase 7: Daily Time-Series Health Data - Next phase
- Per-day granular health metrics (steps time series, intraday HR, etc.)
- Not yet implemented

---

## MCP Layer

### What it is

`garmin_sync/mcp_server.py` is a Model Context Protocol server that exposes the local SQLite database as tools for Claude and other MCP clients. It is implemented with `FastMCP` from the `mcp` package.

### Read-only guarantee

SQLite is opened in URI `mode=ro`. This is enforced at the driver level - the `sqlite3.connect()` call will raise an error on any write attempt. The server never calls Garmin Connect and never modifies the database.

### Transport

The server uses stdio transport (the MCP default). It is designed to be launched by an MCP client process. Launch it with:

```
python -m garmin_sync.mcp_server
# or
garmin-sync-mcp
```

### Relationship to queries.py

Every MCP tool delegates to a function in `garmin_sync/queries.py`. The server contains no SQL. `queries.py` is the single query interface shared by both the CLI and MCP layers. Adding a new query function to `queries.py` makes it available to both surfaces.

Input validation (date format, range checks, limit bounds) is done in the server before calling `queries.py`.

### Connection lifecycle

The connection is opened lazily on the first tool call and cached for the lifetime of the process (`_conn` module-level singleton). The `check_same_thread=False` flag is set because FastMCP may call tools from different threads.

### Tool inventory

| Tool | Parameters | Returns |
|------|-----------|---------|
| `get_recent_activities` | `limit`, `activity_type`, `from_date`, `to_date` | list - activity summaries, newest first |
| `get_activity` | `activity_id` | dict or null - single activity with detail metadata |
| `get_activity_splits` | `activity_id` | list - per-km/mile splits |
| `get_weekly_running_volume` | `weeks` | list - weekly distance/duration/elevation/HR |
| `get_monthly_running_volume` | `months` | list - monthly distance/duration/elevation/HR |
| `get_daily_health_summary` | `days`, `from_date`, `to_date` | list - compact daily health across all health tables |
| `get_sleep_trend` | `days` | list - sleep stages, score, SpO2, respiration |
| `get_hrv_trend` | `days` | list - nightly HRV, weekly avg, baseline, status |
| `get_resting_hr_trend` | `days` | list - resting, min, max HR |
| `get_stress_trend` | `days` | list - avg/peak stress, time per category |
| `get_body_battery_trend` | `days` | list - charged, drained, end value |
| `get_training_vs_sleep` | `days` | list - training load alongside sleep quality |
| `get_intensity_distribution` | `weeks` | list - weekly HR zone time for running |
| `get_running_dynamics` | `days` | list - GCT, vertical oscillation, stride, cadence |
| `get_training_status` | `days`, `from_date`, `to_date` | list - VO2max, status phrase, acute/chronic load, ACWR |
| `get_training_readiness` | `days`, `from_date`, `to_date` | list - readiness score, recovery time, contributing factors |
| `get_vo2max_trend` | `days`, `from_date`, `to_date` | list - VO2max, precise VO2max, fitness age |
| `get_lactate_threshold` | `days`, `from_date`, `to_date` | list - LT heart rate and pace |
| `get_race_predictions` | `days`, `from_date`, `to_date` | list - predicted 5K/10K/half/full times |
| `get_endurance_score` | `days`, `from_date`, `to_date` | list - endurance score and classification |
| `get_hill_score` | `days`, `from_date`, `to_date` | list - overall, strength, endurance component |
| `get_performance_summary` | `days`, `from_date`, `to_date` | list - combined VO2max, status, readiness, endurance score |
| `get_database_status` | none | dict - row counts, cursor positions, recent sync runs |

Parameter limits enforced by the server: `days` max 3650, `weeks` max 520, `months` max 120, `limit` max 500.

---

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| DB driver | raw sqlite3 | Zero deps, explicit SQL, partial indexes |
| Upsert | INSERT … ON CONFLICT DO UPDATE | Idempotent by design |
| Hash | SHA-256 of sorted JSON | Detects Garmin data corrections |
| Raw payload retention | Never deleted | Enables reprocessing without re-fetching |
| Range endpoint storage | One raw_payload per request (date=end_date) | Range APIs return bulk data; per-request storage is pragmatic |
| Per-day endpoint storage | One raw_payload per date | Consistent with cursor-based resume |
| Dry-run | Fetches Garmin, no SQLite writes | Shows what would happen |
| Cursor granularity | Per page (activity backfill) / per date (health/perf backfill) | Mid-crash re-fetch is safe (upserts are idempotent) |

---

## Reprocess Commands

All reprocess commands read from `raw_payload` and rebuild normalised tables.
No Garmin API calls. Safe to re-run at any time.

| Command | What it rebuilds |
|---------|-----------------|
| `reprocess-activity-derived` | 27 derived activity fields + splits |
| `reprocess-health-derived` | 8 derived daily_summary fields |
| `reprocess-sleep` | sleep_score, avg_spo2 (field name fix) |
| `reprocess-hrv` | last_night_avg (field name fix) |
| `reprocess-performance-derived` | lactate_threshold, race_predictions, endurance_score, hill_score, training_status, training_readiness, max_metrics, fitness_age |
| `reprocess-all-derived` | All of the above |
