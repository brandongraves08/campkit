#!/usr/bin/env bash
#
# CampKit backup: one PostgreSQL custom-format dump plus one archive of the
# durable media volume, both checksummed and pruned by age.
#
# Usage:
#   deploy/backup.sh                 # take a backup of the running stack
#   deploy/backup.sh --verify        # re-check the newest backup's checksums
#   deploy/backup.sh --help
#
# Environment:
#   COMPOSE_FILE                     compose file to use (default: ../compose.yaml)
#   CAMPKIT_BACKUP_DIR               destination (default: ../backups)
#   CAMPKIT_BACKUP_RETENTION_DAYS    prune files older than this (default: 14)
#
# The database password is never read here: pg_dump runs inside the db
# container, where compose already provided POSTGRES_USER/POSTGRES_DB. For a
# consistent pair, stop the web service first (see deploy/README.md).

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
COMPOSE_FILE=${COMPOSE_FILE:-${REPO_ROOT}/compose.yaml}
BACKUP_DIR=${CAMPKIT_BACKUP_DIR:-${REPO_ROOT}/backups}
RETENTION_DAYS=${CAMPKIT_BACKUP_RETENTION_DAYS:-14}

die() {
    printf 'backup.sh: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
CampKit backup: one PostgreSQL custom-format dump plus one archive of the
durable media volume, both checksummed and pruned by age.

Usage:
  deploy/backup.sh                 # take a backup of the running stack
  deploy/backup.sh --verify        # re-check the newest backup's checksums
  deploy/backup.sh --help

Environment:
  COMPOSE_FILE                     compose file to use (default: <repo>/compose.yaml)
  CAMPKIT_BACKUP_DIR               destination (default: <repo>/backups)
  CAMPKIT_BACKUP_RETENTION_DAYS    prune files older than this (default: 14)

The database password is never read here: pg_dump runs inside the db
container, where compose already provided POSTGRES_USER/POSTGRES_DB. For a
consistent pair, stop the web service first (see deploy/README.md).
USAGE
}

compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

require_tools() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum is not available"
    command -v gzip >/dev/null 2>&1 || die "gzip is not available"
    [ -f "${COMPOSE_FILE}" ] || die "compose file not found: ${COMPOSE_FILE}"
}

require_stack_running() {
    local running
    running=$(compose ps --status running --services 2>/dev/null || true)
    printf '%s\n' "${running}" | grep -qx "db" \
        || die "the 'db' service is not running; start it with: docker compose -f ${COMPOSE_FILE} up -d db"
}

take_backup() {
    local stamp db_dump media_archive checksums manifest
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    db_dump="${BACKUP_DIR}/campkit-db-${stamp}.dump"
    media_archive="${BACKUP_DIR}/campkit-media-${stamp}.tar.gz"
    checksums="${BACKUP_DIR}/campkit-${stamp}.sha256"
    manifest="${BACKUP_DIR}/campkit-${stamp}.manifest"

    mkdir -p "${BACKUP_DIR}"

    printf 'Writing PostgreSQL dump to %s\n' "${db_dump}" >&2
    compose exec -T db sh -c \
        'exec pg_dump --format=custom --no-owner --no-privileges --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' \
        >"${db_dump}"
    [ -s "${db_dump}" ] || die "pg_dump produced an empty file"
    # A custom-format dump always starts with the PGDMP signature.
    [ "$(head -c 5 "${db_dump}")" = "PGDMP" ] || die "the database dump is not a PostgreSQL custom-format archive"

    printf 'Writing media archive to %s\n' "${media_archive}" >&2
    # tar runs inside the container so files keep their on-volume ownership.
    compose exec -T web sh -c \
        'mkdir -p /data/private && exec tar --create --gzip --file - --directory /data private' \
        >"${media_archive}"
    gzip -t "${media_archive}" || die "the media archive is not a valid gzip stream"

    (
        cd "${BACKUP_DIR}"
        sha256sum "$(basename "${db_dump}")" "$(basename "${media_archive}")" >"$(basename "${checksums}")"
    )

    {
        printf 'campkit_backup=%s\n' "${stamp}"
        printf 'created_utc=%s\n' "${stamp}"
        printf 'compose_file=%s\n' "${COMPOSE_FILE}"
        printf 'database_dump=%s\n' "$(basename "${db_dump}")"
        printf 'media_archive=%s\n' "$(basename "${media_archive}")"
        printf 'checksums=%s\n' "$(basename "${checksums}")"
        printf 'note=Restore with the commands in deploy/README.md; the media archive is relative to /data.\n'
    } >"${manifest}"

    printf '\nBackup complete:\n'
    printf '  %s (%s bytes)\n' "${db_dump}" "$(wc -c <"${db_dump}" | tr -d ' ')"
    printf '  %s (%s bytes)\n' "${media_archive}" "$(wc -c <"${media_archive}" | tr -d ' ')"

    prune_backups
}

prune_backups() {
    [ "${RETENTION_DAYS}" -gt 0 ] 2>/dev/null || return 0
    printf '\nPruning campkit-* files older than %s days in %s\n' "${RETENTION_DAYS}" "${BACKUP_DIR}" >&2
    find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'campkit-*' \
        -mtime "+${RETENTION_DAYS}" -print -delete
}

verify_backup() {
    local newest
    newest=$(ls -1t "${BACKUP_DIR}"/campkit-*.sha256 2>/dev/null | head -n 1 || true)
    [ -n "${newest}" ] || die "no checksum file found in ${BACKUP_DIR}"

    printf 'Verifying %s\n' "${newest}"
    ( cd "${BACKUP_DIR}" && sha256sum -c "$(basename "${newest}")" )

    local dump
    dump="${BACKUP_DIR}/$(sed -n 's/^database_dump=//p' "${newest%.sha256}.manifest")"
    [ -f "${dump}" ] || die "manifest points at a missing dump: ${dump}"
    [ "$(head -c 5 "${dump}")" = "PGDMP" ] || die "the database dump is not a PostgreSQL custom-format archive"
    printf 'Dump signature ok: %s\n' "${dump}"
}

main() {
    case "${1:-}" in
        --help | -h)
            usage
            exit 0
            ;;
        --verify)
            require_tools
            verify_backup
            exit 0
            ;;
        "") ;;
        *)
            usage >&2
            die "unknown option: $1"
            ;;
    esac
    require_tools
    require_stack_running
    take_backup
}

main "$@"
