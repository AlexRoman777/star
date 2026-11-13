#!/bin/sh
# A local PostgreSQL that is an exact copy of the coordinator database.
#
#   scripts/star-db-local.sh restore private/database/star-20260916T030000Z.dump
#   scripts/star-db-local.sh psql            # interactive psql on the copy
#   scripts/star-db-local.sh psql -c 'SELECT count(*) FROM tasks'
#   scripts/star-db-local.sh url             # STAR_DATABASE_URL for local runs
#   scripts/star-db-local.sh stop | start | destroy
#
# Same image as the coordinator (pgvector, PostgreSQL 18, pg_stat_statements), a
# persistent volume so the copy survives restarts, bound to 127.0.0.1 only.
# The dump holds private family-photo metadata: keep it under an ignored
# directory and never publish the port.
set -eu

image='pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff'
container='star-db-local'
volume='star-db-local'
port="${STAR_DB_LOCAL_PORT:-55432}"
password='star-local-only'
database='star'

url() {
  echo "postgresql+asyncpg://star:${password}@127.0.0.1:${port}/${database}"
}

start() {
  if docker ps --format '{{.Names}}' | grep -qx "$container"; then
    return 0
  fi
  if docker ps --all --format '{{.Names}}' | grep -qx "$container"; then
    docker start "$container" >/dev/null
  else
    docker run --detach \
      --name "$container" \
      --cpus 4 --memory 6g --shm-size 2g \
      --publish "127.0.0.1:${port}:5432" \
      --volume "${volume}:/var/lib/postgresql" \
      --env "POSTGRES_USER=star" \
      --env "POSTGRES_DB=${database}" \
      --env "POSTGRES_PASSWORD=${password}" \
      "$image" postgres -c shared_preload_libraries=pg_stat_statements \
        -c shared_buffers=1GB -c work_mem=64MB -c maintenance_work_mem=512MB >/dev/null
  fi
  attempt=0
  until docker exec "$container" pg_isready --username star --dbname "$database" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || { echo "local PostgreSQL did not become ready" >&2; exit 1; }
    sleep 1
  done
}

restore() {
  dump="${1:?path to a star-*.dump file}"
  [ -f "$dump" ] || { echo "no such dump: $dump" >&2; exit 1; }
  start
  echo "Restoring $(basename "$dump") into $container (this replaces the previous copy)..."
  docker exec "$container" psql --username star --dbname postgres --quiet \
    --command "DROP DATABASE IF EXISTS ${database}_restore WITH (FORCE);" \
    --command "CREATE DATABASE ${database}_restore;" >/dev/null
  # A parallel restore needs a seekable file, so the dump is copied into the
  # container first (a few gigabytes; it is removed afterwards).
  docker cp "$dump" "${container}:/tmp/star.dump"
  docker exec "$container" pg_restore --username star --dbname "${database}_restore" \
    --no-owner --no-acl --jobs 4 --exit-on-error /tmp/star.dump \
    || { docker exec "$container" rm -f /tmp/star.dump; echo "restore failed; the previous copy is untouched" >&2; exit 1; }
  docker exec "$container" rm -f /tmp/star.dump
  docker exec "$container" psql --username star --dbname postgres --quiet \
    --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${database}' AND pid <> pg_backend_pid();" \
    --command "DROP DATABASE IF EXISTS ${database} WITH (FORCE);" \
    --command "ALTER DATABASE ${database}_restore RENAME TO ${database};" >/dev/null
  docker exec "$container" psql --username star --dbname "$database" --quiet \
    --command "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" \
    --command "ANALYZE;" >/dev/null
  echo "Restored. Revision: $(docker exec "$container" psql --username star --dbname "$database" --tuples-only --no-align --command 'SELECT version_num FROM alembic_version')"
  echo "STAR_DATABASE_URL=$(url)"
}

case "${1:-}" in
  restore) shift; restore "$@" ;;
  start) start; echo "running on 127.0.0.1:${port}" ;;
  stop) docker stop "$container" >/dev/null && echo "stopped" ;;
  destroy) docker rm --force "$container" >/dev/null 2>&1 || true; docker volume rm "$volume" >/dev/null 2>&1 || true; echo "removed container and volume" ;;
  psql)
    shift; start
    if [ -t 0 ]; then tty_flag="--tty"; else tty_flag=""; fi
    exec docker exec --interactive $tty_flag "$container" psql --username star --dbname "$database" "$@" ;;
  url) url ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
