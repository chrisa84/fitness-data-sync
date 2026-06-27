# Garmin Sync

A local, resumable mirror of your Garmin Connect data, with a built-in MCP server.

Sync your activities, health metrics, sleep, HRV, training load, VO2max, and more to a local SQLite database. Once synced, everything is queryable locally with no repeated Garmin API calls, no rate limits, and no waiting.

The read-only MCP server lets Claude and other AI assistants query your fitness data directly from the local database. Ask questions about your training, sleep trends, race predictions, or recovery without any live API access.

Everything stays local. The database is yours to query directly with SQL, export, or analyse however you want.

---

## Why raw JSON plus normalised tables?

Every Garmin API response is stored verbatim in the `raw_payload` table before any normalisation happens. The normalised tables (`activity`, `sleep`, `hrv`, `training_status`, etc.) are derived convenience views.

This means:
- If Garmin returns a field you later care about, you can reprocess your existing local data without hitting Garmin again.
- If a normalisation bug is fixed, you can re-derive the normalised tables from raw payloads (`reprocess-*-derived` commands).
- You have a complete audit trail of what Garmin actually returned.

Data flow:
```
Garmin Connect API
  → raw_payload table          (canonical, never deleted)
  → activity / health / ...    (derived, upserted from raw)
```

---

## Installation

Requires Python 3.12+.

```bash
# Clone the repository
git clone <repo-url>
cd Garmin-Sync

# Install
pip install -e .

# Install with optional Parquet export support
pip install -e ".[parquet]"
```

---

## Running in Docker

A `Dockerfile` is included for running the sync as a container (e.g. on a server
via Coolify). It pins Python 3.12 regardless of the host, and the container
**idles by default** so you can drive it on demand:

```bash
docker build -t garmin-sync .
docker run -d --name garmin-sync \
  -e GARMIN_DB_PATH=/data/garmin_sync.db \
  -e GARMIN_TOKEN_PATH=/tokens \
  -v /host/path/to/data:/data \
  -v garmin-tokens:/tokens \
  garmin-sync
```

Then drive it with `docker exec`:

- **One-time auth** (interactive — handles MFA):
  `docker exec -it -e GARMIN_EMAIL=... -e GARMIN_PASSWORD=... garmin-sync garmin-sync auth`
- **Init + backfill** (resumable): `docker exec garmin-sync garmin-sync init-db`, then
  `docker exec garmin-sync garmin-sync backfill-activities`, etc.
- **Recurring sync:** run `garmin-sync sync-all` on a schedule (host cron or a
  Coolify Scheduled Task).

Tokens persist on the `/tokens` volume, so auth survives restarts/redeploys. The
DB is opened **WAL**, so a separate reader (e.g. a visualiser) can share the same
DB file on the same host without blocking the sync.

---

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```
GARMIN_EMAIL=your.email@example.com
GARMIN_PASSWORD=your_password_here
GARMIN_TOKEN_PATH=~/.garmin_tokens
GARMIN_DB_PATH=garmin_sync.db
GARMIN_REQUEST_DELAY_SECONDS=1.0
GARMIN_MAX_RETRIES=3
GARMIN_BACKOFF_BASE_SECONDS=2.0
GARMIN_BACKFILL_PAGE_SIZE=100
LOG_LEVEL=INFO
```

All settings can also be passed as environment variables. Environment variables take precedence over `.env`.

**Never commit your `.env` file. It contains your Garmin password.**

---

## First-time setup

### 1. Initialise the database

```bash
garmin-sync init-db
```

Creates `garmin_sync.db` (or the path in `GARMIN_DB_PATH`) with the full schema.
Safe to rerun; uses `CREATE TABLE IF NOT EXISTS` and applies any pending migrations.

### 2. Authenticate

```bash
garmin-sync auth
```

Authenticates with Garmin Connect and stores tokens in `GARMIN_TOKEN_PATH` (default `~/.garmin_tokens/`).

On subsequent runs, stored tokens are reused and your password is not sent again unless tokens expire.

If Garmin returns HTTP 429 (rate limit) during login, the command stops immediately. Wait a few minutes before retrying.

### 3. Run historical backfills

Run these once to populate the full history:

```bash
# All activities (most-recent-first, resumable)
garmin-sync backfill-activities

# Activity detail (laps)
garmin-sync backfill-activity-details

# Health data (daily summary, sleep, HRV, stress, body battery, heart rate)
garmin-sync sync-health --from 2020-01-01 --to 2026-06-09

# Per-day performance metrics (VO2max, training load, readiness, fitness age) — resumable
garmin-sync backfill-performance --from 2020-01-01 --to 2026-06-09

# Range-based performance metrics (lactate threshold, race predictions, endurance score, hill score)
garmin-sync sync-performance-ranges --from 2020-01-01

# Populate derived fields (splits, training zones, running dynamics) from raw payloads
garmin-sync reprocess-all-derived
```

All backfill commands are **resumable**: if interrupted, rerun the same command and it continues from where it stopped. `reprocess-all-derived` is local-only (no Garmin calls) and safe to rerun any time.

---

## Daily incremental sync

```bash
garmin-sync sync-all
```

Syncs everything in one command: activity summaries, activity details, health data (last 7 days), performance ranges, and daily performance metrics.

Options:

| Option | Default | Description |
|--------|---------|-------------|
| `--limit N` | 20 | Number of recent activities and details to fetch |
| `--days N` | 7 | Days window for health and performance data |
| `--dry-run` | off | Call Garmin but do not write to the database |

Or run the individual steps manually:

```bash
garmin-sync sync-recent-activities --limit 20
garmin-sync sync-activity-details --limit 20
garmin-sync sync-recent-health --days 7
garmin-sync sync-performance-ranges
garmin-sync sync-performance
```

All sync commands are **idempotent**: rerunning does not create duplicates. If Garmin has corrected an activity or metric since the last sync, the `payload_hash` changes and the row is updated automatically.

---

## Dry-run mode

```bash
garmin-sync sync-recent-activities --limit 20 --dry-run
garmin-sync backfill-activities --dry-run
```

`--dry-run` **does** call Garmin (to show what would be fetched) but **does not write anything to SQLite**. Useful for verifying auth and checking what data is available.

---

## Check sync status

```bash
garmin-sync status
```

Shows record counts for all tables, sync cursor positions, and the last 5 sync runs.

```bash
garmin-sync status --json
```

Outputs JSON for scripting.

---

## MCP server

garmin-sync includes a read-only MCP server that exposes the local SQLite database as tools for Claude and other MCP clients.

The server opens SQLite in `mode=ro` (enforced at the driver level). It never calls Garmin Connect and never writes to the database.

### Install

```bash
pip install -e ".[mcp]"
```

### Run

```bash
python -m garmin_sync.mcp_server
# or
garmin-sync-mcp
```

The server communicates over stdio and is intended to be launched by an MCP client (e.g. Claude Desktop).

### Claude Desktop configuration

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin-sync": {
      "command": "python",
      "args": ["-m", "garmin_sync.mcp_server"],
      "cwd": "C:\\repos\\Garmin-Sync"
    }
  }
}
```

The server reads `GARMIN_DB_PATH` from `.env` in the `cwd`. No additional environment variables needed if `.env` is configured.

### Available tools (23)

| Tool | Description |
|------|-------------|
| `get_recent_activities` | Recent activities, newest first. Filter by type, date range, or count. |
| `get_activity` | One activity by Garmin ID. Returns null if not found. |
| `get_activity_splits` | Per-km/mile splits for an activity. |
| `get_weekly_running_volume` | Running distance, duration, elevation per week. |
| `get_monthly_running_volume` | Running distance, duration, elevation per month. |
| `get_daily_health_summary` | Steps, resting HR, sleep, HRV, stress, body battery, SpO2 per day. |
| `get_sleep_trend` | Sleep duration, stages, score, SpO2, respiration per day. |
| `get_hrv_trend` | HRV nightly avg, weekly avg, baseline, status per day. |
| `get_resting_hr_trend` | Resting and daily HR stats per day. |
| `get_stress_trend` | Stress average, peak, time per category per day. |
| `get_body_battery_trend` | Body battery charged/drained/end value per day. |
| `get_training_vs_sleep` | Training load vs sleep quality for recovery analysis. |
| `get_intensity_distribution` | Weekly HR zone time distribution for running. |
| `get_running_dynamics` | Cadence, GCT, oscillation, stride length, vertical ratio per run. |
| `get_training_status` | VO2max, training status phrase, acute/chronic load, ACWR per day. |
| `get_training_readiness` | Readiness score, recovery time, HRV/sleep/stress factors, morning score per day. |
| `get_vo2max_trend` | VO2max, precise VO2max, fitness age, achievable fitness age per day. |
| `get_lactate_threshold` | Lactate threshold HR and pace measurements. |
| `get_race_predictions` | Predicted 5K, 10K, half marathon, marathon times per day. |
| `get_endurance_score` | Endurance score and classification per day. |
| `get_hill_score` | Hill score with strength and endurance components per day. |
| `get_performance_summary` | Combined daily view: VO2max, training status, readiness, endurance, fitness age. |
| `get_database_status` | Row counts for all tables, sync cursor positions, recent sync runs. |

### Note on sync

Sync commands (`backfill-activities`, `sync-health`, etc.) are run separately via the CLI. The MCP server only reads data that has already been synced. Run sync commands on a schedule to keep data current.

---

## Reprocess derived tables

If you fix a normalisation bug, or want to re-derive tables from existing raw payloads without hitting Garmin:

```bash
garmin-sync reprocess-activity-derived
garmin-sync reprocess-health-derived
garmin-sync reprocess-sleep
garmin-sync reprocess-hrv
garmin-sync reprocess-performance-derived
garmin-sync reprocess-all-derived
```

---

## Query commands

Built-in tabular queries. Most support `--days N`, `--from YYYY-MM-DD`, `--to YYYY-MM-DD`, and `--json`.

```bash
# Activities
garmin-sync query-recent-activities [--limit 20] [--type running] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
garmin-sync query-activity ACTIVITY_ID
garmin-sync query-activity-splits ACTIVITY_ID
garmin-sync query-weekly-running-volume [--weeks 12]
garmin-sync query-monthly-running-volume [--months 12]

# Health
garmin-sync query-sleep-trend [--days 30]
garmin-sync query-hrv-trend [--days 30]
garmin-sync query-resting-hr-trend [--days 30]
garmin-sync query-stress-trend [--days 30]
garmin-sync query-body-battery-trend [--days 30]
garmin-sync query-training-vs-sleep [--days 90]

# Running dynamics and intensity
garmin-sync query-running-dynamics [--days 90]
garmin-sync query-intensity-distribution [--weeks 12]

# Performance
garmin-sync query-training-status [--days 90]
garmin-sync query-training-readiness [--days 90]
garmin-sync query-vo2max-trend [--days 180]
garmin-sync query-performance-summary [--days 90]

# Range-based performance metrics
garmin-sync query-lactate-threshold [--days 365]
garmin-sync query-race-predictions [--days 365]
garmin-sync query-endurance-score [--days 365]
garmin-sync query-hill-score [--days 365]
```

---

## Rate limiting

Garmin enforces rate limits. The app handles this conservatively:

- A configurable delay (`GARMIN_REQUEST_DELAY_SECONDS`, default 1 second) is added between every Garmin API call.
- On HTTP 429: exponential backoff with jitter, up to `GARMIN_MAX_RETRIES` attempts.
- On connection errors: same exponential backoff.
- If 429 occurs **during login**: the app stops immediately (does not retry login aggressively).
- Auth errors (401/403): stop immediately (not retried).
- Progress is always saved before stopping, so rerunning picks up where it left off.

If you get repeated 429s, increase `GARMIN_REQUEST_DELAY_SECONDS` in `.env`.

---

## Database tables

| Table | Key | Description |
|-------|-----|-------------|
| `raw_payload` | `(data_type, garmin_id)` or `(data_type, date)` | Canonical store of all Garmin API responses |
| `sync_cursor` | `data_type` | Resumable backfill progress |
| `sync_runs` | `id` | History of sync command executions |
| `activity` | `activity_id` | Normalised activity summaries |
| `activity_detail` | `activity_id` | Per-activity detail metadata (lap count, split count) |
| `activity_lap` | `(activity_id, lap_index)` | Per-lap metrics |
| `activity_split` | `(activity_id, split_index)` | Per-km/mile splits |
| `daily_summary` | `date` | Steps, calories, HR, stress, SpO2, body battery |
| `sleep` | `date` | Sleep stages, score, SpO2, respiration |
| `hrv` | `date` | HRV nightly avg, weekly avg, baseline, status |
| `stress` | `date` | Daily stress average and duration by category |
| `body_battery` | `date` | Body battery charged, drained, start/end values |
| `heart_rate` | `date` | Resting HR, max HR, min HR |
| `lactate_threshold` | `date` | Lactate threshold heart rate, speed, power |
| `race_predictions` | `date` | Predicted 5K/10K/half/full marathon times |
| `endurance_score` | `date` | Garmin endurance score and classification |
| `hill_score` | `date` | Hill score: overall, strength component, endurance component |
| `training_status` | `date` | VO2max, training status phrase, acute/chronic load, ACWR |
| `training_readiness` | `date` | Readiness score, recovery time, sleep/stress/HRV factors, morning score |
| `max_metrics` | `date` | Precise VO2max and fitness age descriptor per day |
| `fitness_age` | `date` | Fitness age, achievable fitness age, chronological age |

---

## Querying the database directly

```bash
sqlite3 garmin_sync.db
```

### Example queries

```sql
-- Recent running activities with pace
SELECT name, start_time,
       ROUND(distance_m/1000.0, 2) AS km,
       ROUND((duration_s/60.0) / (distance_m/1000.0), 2) AS pace_min_per_km,
       avg_hr
FROM activity
WHERE type = 'running'
ORDER BY start_time DESC LIMIT 20;

-- Weekly running volume
SELECT strftime('%Y-W%W', start_time) AS week,
       COUNT(*) AS runs,
       ROUND(SUM(distance_m)/1000.0, 1) AS total_km,
       ROUND(AVG(avg_hr)) AS avg_hr
FROM activity
WHERE type = 'running'
GROUP BY week ORDER BY week DESC;

-- Sleep trend
SELECT date, overall_sleep_score, avg_overnight_hrv,
       avg_respiration_rate, restless_moments_count
FROM sleep ORDER BY date DESC LIMIT 30;

-- Training readiness with recovery time
SELECT date, score, level, recovery_time_min,
       morning_readiness_score, feedback_short
FROM training_readiness ORDER BY date DESC LIMIT 14;

-- VO2max and fitness age trend
SELECT f.date, mm.vo2max, mm.vo2max_precise,
       f.fitness_age, f.achievable_fitness_age, f.chronological_age
FROM fitness_age f
LEFT JOIN max_metrics mm ON mm.date = f.date
ORDER BY f.date DESC LIMIT 30;

-- Raw payload for a specific activity
SELECT payload_json FROM raw_payload
WHERE data_type = 'activity_summary' AND garmin_id = '12345678901';
```

---

## Scheduled daily sync

### Linux/macOS (cron)

```bash
crontab -e
```

Add (adjust path as needed):
```
0 6 * * * cd /path/to/Garmin-Sync && garmin-sync sync-all >> /var/log/garmin-sync.log 2>&1
```

### Windows Task Scheduler

Create a `.bat` file:
```bat
@echo off
cd C:\repos\Garmin-Sync
garmin-sync sync-all
```

---

## Project structure

```
garmin_sync/
  cli.py           # Typer CLI — all commands
  config.py        # Configuration via pydantic-settings
  db.py            # SQLite connection, schema init, migrations
  models.py        # Internal TypedDicts (not ORM)
  repositories.py  # All database read/write operations
  normalise.py     # Raw Garmin dict → normalised row dict
  rate_limit.py    # Delay, retry, and backoff
  garmin_client.py # Adapter wrapping garminconnect.Garmin
  sync_engine.py   # Sync orchestration (activities, health, performance)
  mcp_server.py    # MCP server (FastMCP, stdio, read-only)
  export.py        # JSON and CSV export
  queries.py       # Shared query layer (CLI and MCP)

tests/
  conftest.py
  test_idempotency.py      # Upsert and hash-change tests
  test_resume.py           # Cursor tracking and resume tests
  test_normalise.py        # Activity normalisation edge cases
  test_rate_limit.py       # Backoff and retry behaviour
  test_health_sync.py      # Health sync and cursor tests
  test_performance.py      # Performance normalisation tests
  test_performance_sync.py # Performance sync, chunking, idempotency tests
  test_reprocess.py        # Reprocess derived tables tests

schema.sql         # SQLite schema (partial unique indexes)
pyproject.toml     # Project metadata and dependencies
.env.example       # Config template
```

---

## Running tests

No Garmin credentials needed. All tests use in-memory SQLite and mock Garmin exceptions.

```bash
pip install -e ".[dev]"
pytest
```
