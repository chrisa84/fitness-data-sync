# Changelog

All notable changes to garmin-sync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[Semantic Versioning](https://semver.org/).

Check your installed version with `garmin-sync --version`.

Each release lists **Upgrade steps**: what to run after updating from the
previous version. If you skip versions, run the steps for every version you
skipped, in order. Schema changes always apply automatically the first time
any command opens the database, so `init-db` is never required for an upgrade
(only for a brand-new database).

## [0.5.2] - 2026-10-19

### Fixed
- `altitude_m` in `activity_sample` was always NULL: Garmin sends elevation as
  `directElevation`, which was missing from the sample normalisation map.
- `reprocess-activity-samples` loaded every raw sample payload into memory at
  once (gigabytes on a long history). It now loads one payload at a time.

### Added
- `activity_sample.temperature_c`, from `directAirTemperature` (the watch's own
  sensor, so it reads warm when worn). Added automatically on first open.

### Upgrade steps
- `garmin-sync reprocess-activity-samples` — fills `altitude_m` and
  `temperature_c` from stored raw payloads. Local only, no Garmin calls.

## [0.5.1] - 2026-06-30

### Fixed
- Three metric keys were missing from the activity sample normalisation map
  (`directRunCadence`, `sumDistance`, `directGroundContactBalanceLeft`),
  leaving `cadence`, `distance_m`, and `ground_contact_balance_left` NULL in
  `activity_sample` for running activities synced with 0.4.0 or 0.5.0.

### Upgrade steps
- `garmin-sync reprocess-activity-samples` — rebuilds sample rows from stored
  raw payloads. Local only, no Garmin calls, no re-sync needed.

## [0.5.0] - 2026-06-30

### Added
- Intraday health time-series sync (Phase 7): `sync-intraday` command and
  intraday health tables. Always re-syncs the requested window because Garmin
  corrects intraday data retroactively.

### Upgrade steps
- `garmin-sync sync-intraday` to fetch intraday data (requires Garmin
  credentials).

## [0.4.0] - 2026-06-29

### Added
- Activity time-series sample sync (Phase 6b): `sync-activity-samples`
  command and the `activity_sample` table.
- `ground_contact_balance_left` column on `activity_sample`.
- `reprocess-activity-samples` command to rebuild sample rows from stored raw
  payloads without Garmin calls.

### Upgrade steps
- `garmin-sync sync-activity-samples` to fetch per-activity time-series
  samples (requires Garmin credentials).

## [0.3.0] - 2026-06-27

### Added
- Dockerfile for containerised scheduled sync.
- Docker / Coolify deployment documentation.

### Fixed
- Backfill documentation: added the `sync-performance-ranges` and
  `reprocess-all-derived` steps.

### Upgrade steps
- None.

## [0.2.0] - 2026-06-14

### Changed
- `sync-all` now runs the derived-data reprocess pass automatically as its
  final step, so derived columns stay fresh after every sync.

### Upgrade steps
- None. Optionally run `garmin-sync reprocess-all-derived` once if derived
  columns look stale.

## [0.1.0] - 2026-06-10

Initial release: activity, health, and performance sync; historical backfill;
derived-data reprocess commands; query and export commands; `sync-all`;
read-only MCP server.
