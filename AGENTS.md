# AGENTS.md

Orientation for an AI agent working in this repo. Read this first, then dig into
`README.md` and `docs/` for depth. This file is a map and a list of gotchas, not a
full reference.

## What this is

A local, resumable, idempotent mirror of Garmin Connect data. It fetches raw JSON
from Garmin, stores it verbatim in SQLite, and normalises it into queryable tables.
A Typer CLI drives everything; an MCP server exposes read-only queries to Claude.

- Language: Python 3.12+
- Storage: a single SQLite file (`garmin_sync.db` by default), WAL mode
- No ORM — raw `sqlite3` with explicit SQL and `INSERT … ON CONFLICT DO UPDATE`

## Quick start

```bash
pip install -e ".[dev]"     # editable install + test deps
pytest                      # full suite, no credentials needed (in-memory SQLite, mocked Garmin)
python -m garmin_sync.cli --help
```

The console script `garmin-sync` is registered by the install, but invoking via
`python -m garmin_sync.cli <command>` always works and is the safest in a fresh shell.

Config lives in `.env` (see `.env.example`). Garmin credentials are only needed for
*sync* commands, never for *query*, *export*, *reprocess*, or tests.

## The one concept that bites people: two-pass sync → derive

Data lands in two stages, and they are **separate**:

1. **Sync** fetches raw payloads and writes the "obvious" columns
   (`normalise.normalise_activity_detail`, etc.).
2. **Derive / reprocess** turns those raw payloads into the *derived* columns —
   `training_load`, HR zones, stamina, and running dynamics including **L/R
   ground-contact balance** (`normalise.normalise_activity_derived`, called from
   `reprocess-activity-derived`).

`sync-all` now runs the derive pass automatically as its final step, so derived
columns stay fresh. But the derive logic only lives in the reprocess commands — if
you add a new derived field, wire it into the relevant `reprocess-*` command, not
into the sync engine. Reprocess commands are local-only (no Garmin calls), idempotent,
and safe to re-run any time because raw payloads are never deleted.

If a derived column looks stale or NULL, the fix is almost always
`python -m garmin_sync.cli reprocess-activity-derived` (or `reprocess-all-derived`),
not a re-sync.

## Code map

```
garmin_sync/
  cli.py           # Typer CLI — every command lives here
  config.py        # pydantic-settings config (.env)
  db.py            # connection, schema init, additive column migrations
  models.py        # internal TypedDicts (not an ORM)
  repositories.py  # ALL database read/write — keep SQL here
  normalise.py     # raw Garmin dict → normalised row dict (incl. *_derived fns)
  rate_limit.py    # delay, retry, backoff
  garmin_client.py # thin adapter over garminconnect.Garmin
  sync_engine.py   # sync orchestration (activities, health, performance, details)
  queries.py       # shared read layer used by BOTH cli.py and mcp_server.py
  mcp_server.py    # FastMCP stdio server, read-only
  export.py        # JSON / CSV export

schema.sql         # base schema; db.py adds derived columns via migration lists
tests/             # in-memory SQLite, mocked Garmin — no network, no creds
```

## Conventions and constraints

- **Idempotency is the contract.** Every write is an upsert; a SHA-256 of canonical
  JSON detects whether a payload actually changed. Re-running any command must be safe.
- **Raw payloads are never deleted** — they exist so derived tables can be rebuilt
  without re-fetching. Don't add logic that prunes `raw_payload`.
- **All SQL goes in `repositories.py` (writes) or `queries.py` (reads).** Don't scatter
  SQL through `cli.py`.
- **`queries.py` is shared by the CLI and the MCP server** — changing a query signature
  affects both. The MCP layer is strictly read-only; keep it that way.
- **Schema changes** are additive via the column lists in `db.py` (`_migrate_add_columns`),
  not destructive `ALTER`s. The live DB has years of data.
- **Rate limiting is real.** Sync commands stop immediately on auth or rate-limit errors.
  Don't add retry loops that hammer Garmin; `rate_limit.py` owns backoff.
- **Docs and commits stay plain and professional** — no jokes, no personality, regardless
  of how the surrounding chat reads.

## Where to look next

- `README.md` — full install, command catalogue, MCP setup, example SQL queries
- `docs/architecture.md` — table-by-table schema, phase status, key design decisions
- `docs/commands.md` — command reference
- `docs/data-availability.md` — which Garmin fields are populated and how often

## Working the database directly

Read-only inspection is fine and encouraged:

```bash
sqlite3 garmin_sync.db ".schema activity"
sqlite3 garmin_sync.db "SELECT ... FROM activity ORDER BY start_time DESC LIMIT 10;"
```

`.schema <table>` prints an existing table definition — it does not create anything.
Never hand-edit data the sync/reprocess pipeline owns; rebuild it through the
appropriate command instead.
