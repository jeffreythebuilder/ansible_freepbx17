#!/bin/bash
set -Eeuo pipefail
umask 077

trap 'status=$?; echo "Installation failed at line $LINENO (exit $status). No reboot requested; bootstrap and logs retained." >&2; exit "$status"' ERR

if [[ $EUID -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_ID:-} != 12 || $(dpkg --print-architecture) != amd64 ]]; then
    echo "This installer requires Debian 12 amd64." >&2
    exit 1
fi

# Require an explicitly reviewed revision, not a moving branch.
if [[ ! ${PLAYBOOK_REF:-} =~ ^[0-9a-f]{40}$ ]]; then
    echo "Set PLAYBOOK_REF to the full, reviewed playbook commit SHA." >&2
    exit 1
fi

state_dir=/var/lib/freepbx-install
install -d -m 0700 "$state_dir"
exec 9>"$state_dir/bootstrap.lock"
if ! flock -n 9; then
    echo "Another bootstrap is already running." >&2
    exit 1
fi
if [[ -e $state_dir/complete ]]; then
    echo "Installation already completed; nothing to do."
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive
echo "Installing dependencies (APT retries transient network failures)..."
apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y ansible git curl ca-certificates

echo "Checking HTTPS access to GitHub..."
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 10 --max-time 30 --retry 5 --retry-delay 5 --retry-max-time 180 \
    --output /dev/null https://github.com/

# Keep each checkout for diagnosis; never delete an existing /root/playbook.
checkout=$(mktemp -d "$state_dir/playbook.XXXXXXXX")
echo "Fetching $PLAYBOOK_REF into $checkout..."
git -C "$checkout" init --quiet
git -C "$checkout" remote add origin https://github.com/jeffreythebuilder/ansible_freepbx17.git
timeout 180 git -C "$checkout" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 \
    fetch --depth 1 origin "$PLAYBOOK_REF"
git -C "$checkout" checkout --quiet --detach FETCH_HEAD
test "$(git -C "$checkout" rev-parse HEAD)" = "$PLAYBOOK_REF"

extra_vars=()
if [[ -n ${PLAYBOOK_VARS_FILE:-} ]]; then
    test -f "$PLAYBOOK_VARS_FILE"
    extra_vars=(--extra-vars "@$PLAYBOOK_VARS_FILE")
fi

echo "Running playbook..."
ansible-playbook -i localhost, -c local "$checkout/site.yml" "${extra_vars[@]}"

# All tasks, handlers, reloads and health checks must succeed first.
printf '%s\n' "$PLAYBOOK_REF" > "$state_dir/complete"
echo "Installation completed. The systemd service will now request a reboot."
