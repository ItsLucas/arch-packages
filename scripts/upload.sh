#!/usr/bin/env bash
# Stream data to the server's forced command; never run artifact contents.
set -euo pipefail
archive=${1:?tar archive}
: "${AUR_UPLOAD_KEY:?missing upload key}" "${AUR_UPLOAD_HOST:?missing upload host}"
: "${AUR_UPLOAD_PORT:?missing upload port}" "${AUR_UPLOAD_USER:?missing upload user}"
: "${AUR_SSH_KNOWN_HOSTS:?missing pinned host key}"
[[ "$AUR_UPLOAD_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || exit 2
[[ "$AUR_UPLOAD_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 2
[[ "$AUR_UPLOAD_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || exit 2
(( AUR_UPLOAD_PORT <= 65535 )) || exit 2
[[ -f "$archive" && ! -L "$archive" ]] || exit 2
umask 077
work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:?TMPDIR required}}/aur-upload.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
printf '%s\n' "$AUR_UPLOAD_KEY" > "$work/key"
printf '%s\n' "$AUR_SSH_KNOWN_HOSTS" > "$work/known_hosts"
if [[ "$AUR_UPLOAD_PORT" == 22 ]]; then
  host_pattern=$AUR_UPLOAD_HOST
else
  host_pattern="[$AUR_UPLOAD_HOST]:$AUR_UPLOAD_PORT"
fi
ssh-keygen -F "$host_pattern" -f "$work/known_hosts" >/dev/null
ssh -F /dev/null -T -p "$AUR_UPLOAD_PORT" -i "$work/key" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$work/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o ConnectTimeout=30 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  "$AUR_UPLOAD_USER@$AUR_UPLOAD_HOST" < "$archive"
