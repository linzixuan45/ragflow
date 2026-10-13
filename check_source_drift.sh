#!/usr/bin/env bash
# Report source files the running container has not actually picked up.
#
# docker/docker-compose.knowledge-dev.yml overlays the checked-out source onto
# the service image with *single-file* bind mounts. Two consequences, and both
# have already cost a debugging session:
#
#   * replacing a file on the host (rather than editing it in place) leaves the
#     container pinned to the old inode until it is restarted, so the container
#     keeps serving bytes that no longer match the checkout;
#   * even when the container can see the new bytes, a Python module that has
#     already been imported keeps running the old code.
#
# So every edit to a mounted file owes a restart. Run this to find out whether
# one is still outstanding.

set -Eeuo pipefail

CONTAINER="${1:-ragflow-knowledge-dev-ragflow-gpu-1}"

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "Error: no such container: ${CONTAINER}" >&2
    exit 2
fi

STATUS="$(docker inspect "${CONTAINER}" --format '{{.State.Status}}')"
if [ "${STATUS}" != "running" ]; then
    echo "Error: ${CONTAINER} is ${STATUS}, not running." >&2
    exit 2
fi

STARTED_AT="$(docker inspect "${CONTAINER}" --format '{{.State.StartedAt}}')"
STARTED_EPOCH="$(date -d "${STARTED_AT}" +%s)"

SOURCES=()
DESTINATIONS=()
while IFS='|' read -r source destination; do
    [ -n "${source}" ] || continue
    # Directory mounts track their contents live; only single files pin an inode.
    [ -f "${source}" ] || continue
    SOURCES+=("${source}")
    DESTINATIONS+=("${destination}")
done < <(docker inspect "${CONTAINER}" --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}{{end}}')

if [ "${#SOURCES[@]}" -eq 0 ]; then
    echo "No single-file bind mounts on ${CONTAINER}; nothing to check."
    exit 0
fi

declare -A CONTAINER_HASH=()
while read -r hash path; do
    [ -n "${path}" ] && CONTAINER_HASH["${path}"]="${hash}"
done < <(docker exec "${CONTAINER}" md5sum "${DESTINATIONS[@]}" 2>/dev/null || true)

DRIFT=()
STALE=()
UNREADABLE=()
for index in "${!SOURCES[@]}"; do
    source="${SOURCES[$index]}"
    destination="${DESTINATIONS[$index]}"
    host_hash="$(md5sum "${source}" | awk '{print $1}')"
    container_hash="${CONTAINER_HASH[${destination}]:-}"

    if [ -z "${container_hash}" ]; then
        UNREADABLE+=("${destination}")
    elif [ "${host_hash}" != "${container_hash}" ]; then
        # Container is pinned to stale bytes -- typically a replaced inode.
        DRIFT+=("${destination}")
    elif [ "$(stat -c %Y "${source}")" -gt "${STARTED_EPOCH}" ]; then
        # Bytes match, but the edit landed after the process started, so the
        # already-imported module predates it.
        STALE+=("${destination}")
    fi
done

echo "Checked ${#SOURCES[@]} single-file bind mounts on ${CONTAINER} (started ${STARTED_AT})."

if [ "${#DRIFT[@]}" -eq 0 ] && [ "${#STALE[@]}" -eq 0 ] && [ "${#UNREADABLE[@]}" -eq 0 ]; then
    echo "OK: every mounted source matches the container and predates its process."
    exit 0
fi

if [ "${#DRIFT[@]}" -gt 0 ]; then
    echo
    echo "DRIFT - container holds different bytes than the checkout:"
    printf '  %s\n' "${DRIFT[@]}"
fi

if [ "${#STALE[@]}" -gt 0 ]; then
    echo
    echo "STALE - edited after this process started, so the running code predates the edit:"
    printf '  %s\n' "${STALE[@]}"
fi

if [ "${#UNREADABLE[@]}" -gt 0 ]; then
    echo
    echo "UNREADABLE - mounted but could not be hashed inside the container:"
    printf '  %s\n' "${UNREADABLE[@]}"
fi

echo
echo "Restart to pick these up:  docker restart ${CONTAINER}   (or ./re_start.sh)"
exit 1
