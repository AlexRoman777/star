#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

postgres_image='pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff'
container_name="star-postgres-test-$$"
database_name='star_test'
database_password='star-test-only'

cleanup() {
  docker stop "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker run --rm --detach \
  --name "$container_name" \
  --cpus 2 --memory 1792m \
  --publish 127.0.0.1::5432 \
  --env "POSTGRES_DB=$database_name" \
  --env "POSTGRES_PASSWORD=$database_password" \
  "$postgres_image" postgres -c shared_preload_libraries=pg_stat_statements >/dev/null

attempt=0
until docker exec "$container_name" pg_isready \
  --username postgres --dbname "$database_name" >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Disposable PostgreSQL did not become ready within 60 seconds." >&2
    docker logs "$container_name" >&2 || true
    exit 1
  fi
  sleep 1
done

published_port=$(docker port "$container_name" 5432/tcp | awk -F: 'NR == 1 {print $NF}')
if [ -z "$published_port" ]; then
  echo "Disposable PostgreSQL did not publish a host port." >&2
  exit 1
fi

database_url="postgresql+asyncpg://postgres:${database_password}@127.0.0.1:${published_port}/${database_name}"
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/star-uv} \
STAR_DATABASE_URL="$database_url" \
  uv run alembic upgrade head

# Reproduce the production upgrade path as well as the fresh-install path.
# v180 shipped at 20260903_0105; the squashed history must recognize and
# bridge that exact revision without requiring an intermediate deployment.
legacy_database_name='star_v180_upgrade_test'
docker exec "$container_name" createdb --username postgres "$legacy_database_name"
docker exec --interactive "$container_name" \
  psql --username postgres --dbname "$legacy_database_name" --set ON_ERROR_STOP=1 \
  < backend/alembic/versions/baseline_v180_schema.sql >/dev/null
docker exec "$container_name" \
  psql --username postgres --dbname "$legacy_database_name" --set ON_ERROR_STOP=1 \
  --command "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY); INSERT INTO alembic_version VALUES ('20260903_0105');" \
  >/dev/null
legacy_database_url="postgresql+asyncpg://postgres:${database_password}@127.0.0.1:${published_port}/${legacy_database_name}"
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/star-uv} \
STAR_DATABASE_URL="$legacy_database_url" \
  uv run alembic upgrade head
legacy_revision=$(docker exec "$container_name" \
  psql --username postgres --dbname "$legacy_database_name" --tuples-only --no-align \
  --command "SELECT version_num FROM alembic_version")
legacy_roster=$(docker exec "$container_name" \
  psql --username postgres --dbname "$legacy_database_name" --tuples-only --no-align \
  --command "SELECT to_regclass('public.family_roster_members')")
legacy_primary_index=$(docker exec "$container_name" \
  psql --username postgres --dbname "$legacy_database_name" --tuples-only --no-align \
  --command "SELECT indexdef FROM pg_indexes WHERE indexname='uq_tasks_active_primary_generation'")
case "$legacy_primary_index" in
  *asset.primary_review*) legacy_index_ok=true ;;
  *) legacy_index_ok=false ;;
esac
if [ "$legacy_revision" != '20260920_0017' ] \
  || [ "$legacy_roster" != 'family_roster_members' ] \
  || [ "$legacy_index_ok" != true ]; then
  echo "Released v180 schema did not migrate cleanly to the current head." >&2
  exit 1
fi

UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/star-uv} \
STAR_POSTGRES_TEST_URL="$database_url" \
  uv run pytest -n 0 backend/tests/postgres -rP
