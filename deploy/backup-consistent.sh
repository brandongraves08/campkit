#!/usr/bin/env bash
# Run the paired database/media backup with the app quiesced. Restore the app to
# its original running/stopped state even if backup creation fails.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
COMPOSE_FILE=${COMPOSE_FILE:-${REPO_ROOT}/compose.yaml}

compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

die() {
    printf 'backup-consistent.sh: %s\n' "$*" >&2
    exit 1
}

# Verification is read-only and does not need to interrupt the app.
if [ "${1:-}" = "--verify" ]; then
    exec "${SCRIPT_DIR}/backup.sh" "$@"
fi

running=$(compose ps --status running --services 2>/dev/null || true)
printf '%s\n' "${running}" | grep -qx "db" \
    || die "the 'db' service is not running; start it with: docker compose -f ${COMPOSE_FILE} up -d db"

restart_web=0
if printf '%s\n' "${running}" | grep -qx "web"; then
    # Set this first so EXIT also attempts recovery if stop partially fails.
    restart_web=1
fi

restore_web() {
    local status=$?
    trap - EXIT
    if [ "${restart_web}" -eq 1 ] && ! compose start web; then
        printf 'backup-consistent.sh: failed to restart web after backup\n' >&2
        status=1
    fi
    exit "${status}"
}
trap restore_web EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${restart_web}" -eq 1 ]; then
    compose stop web
fi

"${SCRIPT_DIR}/backup.sh" "$@"
