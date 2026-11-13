#!/bin/sh
# Restore a NAS PostgreSQL dump into a local container for realistic testing.
#
# The NAS backup service already writes verified custom-format dumps to
#   ./star-data/starml/database/star-<UTC stamp>.dump  (+ .sha256)
# Copy the newest pair to private/database/ on this machine, then run:
#   ./scripts/restore-nas-database-dump.sh private/database/star-<stamp>.dump
#
# The restored database holds real family-photo metadata: filenames, captions,
# people names, ratings and EXIF. It stays on this machine, in a git-ignored
# directory, in a container bound to loopback only. Never commit a dump and
# never point a worker or a write-back at this copy.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

dump=${1:-}
container_name=${STAR_LOCAL_DB_CONTAINER:-star-postgres-live}
database_name='star'
database_password=${STAR_LOCAL_DB_PASSWORD:-star-local-only}
postgres_image='pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff'

if [ -z "$dump" ]; then
  echo "usage: $0 private/database/star-<stamp>.dump" >&2
  echo "copy the newest dump and its .sha256 from the NAS path above first" >&2
  exit 2
fi
[ -f "$dump" ] || { echo "dump not found: $dump" >&2; exit 2; }
case $(cd "$(dirname -- "$dump")" && pwd) in
  "$ROOT"/private/*|"$ROOT"/private-benchmarks/*) ;;
  *) echo "keep private dumps under private/ or private-benchmarks/" >&2; exit 2 ;;
esac

if [ -f "$dump.sha256" ]; then
  ( cd "$(dirname -- "$dump")" && shasum -a 256 -c "$(basename -- "$dump").sha256" ) \
    || { echo "dump checksum does not match its .sha256" >&2; exit 1; }
  echo "Checksum verified."
else
  echo "No .sha256 beside the dump; copy it too so the transfer is verified." >&2
fi
chmod 0600 "$dump" 2>/dev/null || true

if [ -n "$(docker ps --quiet --filter "name=^${container_name}$")" ]; then
  echo "Reusing the running container: $container_name"
else
  docker rm --force "$container_name" >/dev/null 2>&1 || true
  # Loopback only: this copy is never reachable from the LAN.
  docker run --detach \
    --name "$container_name" \
    --cpus 2 --memory 2560m \
    --publish 127.0.0.1::5432 \
    --env "POSTGRES_DB=$database_name" \
    --env "POSTGRES_USER=star" \
    --env "POSTGRES_PASSWORD=$database_password" \
    "$postgres_image" postgres -c shared_preload_libraries=pg_stat_statements >/dev/null
fi

attempt=0
until docker exec "$container_name" pg_isready --username star --dbname "$database_name" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || { echo "local PostgreSQL did not become ready" >&2; exit 1; }
  sleep 1
done

echo "Restoring $dump …"
docker exec --interactive "$container_name" \
  pg_restore --username star --dbname "$database_name" --clean --if-exists \
    --no-owner --no-acl --exit-on-error < "$dump"

published_port=$(docker port "$container_name" 5432/tcp | awk -F: 'NR == 1 {print $NF}')
url="postgresql+asyncpg://star:${database_password}@127.0.0.1:${published_port}/${database_name}"
echo
echo "Restored. Local copy of the live database:"
echo "  STAR_DATABASE_URL=$url"
echo "  STAR_POSTGRES_TEST_URL=$url   # backend/tests/postgres runs against it"
echo
echo "Read-only exploration:"
echo "  docker exec -it $container_name psql -U star -d star"
echo "Stop it with: docker rm --force $container_name"
