# Containerised Garmin sync for a scheduled deploy (e.g. Coolify).
# Pins Python 3.12 so dependencies install cleanly regardless of the host.
#
# The container idles (sleep infinity) so it stays up for:
#   - one-time interactive auth:  docker exec -it <c> garmin-sync auth
#   - the initial backfills:       docker exec <c> garmin-sync backfill-activities ...
# Recurring syncs are driven by a scheduler (Coolify Scheduled Task / cron) running
#   garmin-sync sync-all
#
# Needs a writable DB path (GARMIN_DB_PATH) and token dir (GARMIN_TOKEN_PATH),
# both expected on mounted volumes. The DB is opened WAL, so it can be shared
# read-only with the visualiser on the same host.
FROM python:3.12-slim

WORKDIR /app

# Editable install keeps the source layout intact so schema.sql resolves via
# Path(__file__).parent.parent (db.py) at runtime.
COPY pyproject.toml schema.sql ./
COPY garmin_sync ./garmin_sync
RUN pip install --no-cache-dir -e .

CMD ["sleep", "infinity"]
