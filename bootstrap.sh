#!/bin/bash
set -Eeuo pipefail
umask 077

if [[ $EUID -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

state_dir=/var/lib/freepbx-install
log_dir=/var/log/freepbx-install
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

install -d -m 0700 "$log_dir"
log_file="$log_dir/install.log"
touch "$log_file"
chmod 0600 "$log_file"
log_offset=$(wc -c < "$log_file")
# Preserve journal output while also retaining a root-only disk log.
# tee must not inherit the bootstrap lock, or it would retain it after exit.
exec 3>&1 4>&2
exec > >(tee -a "$log_file" 9>&-) 2>&1
tee_pid=$!
monitor=/usr/local/lib/freepbx-install/freepbx_install_monitor.py
if [[ ! -f $monitor ]]; then
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    monitor="$script_dir/monitor/freepbx_install_monitor.py"
fi
monitor_ready=false
console_pid=''
last_error='Bootstrap exited before completing installation.'

finish_bootstrap() {
    local status=$?
    trap - EXIT
    if [[ $status -ne 0 ]]; then
        echo "$last_error No reboot requested; bootstrap and logs retained." >&2
        if [[ $monitor_ready == true ]]; then
            python3 "$monitor" --finish failed --message "$last_error" --exit-code "$status" || true
        fi
    fi
    exec 1>&3 2>&4
    exec 3>&- 4>&-
    wait "$tee_pid" || {
        echo "Persistent log capture failed. Check $log_file and free disk space." >&2
        if [[ $status -eq 0 ]]; then
            status=74
            rm -f "$state_dir/complete"
            if [[ $monitor_ready == true ]]; then
                python3 "$monitor" --finish failed --message 'Persistent log capture failed.' --exit-code "$status" || true
            fi
        fi
    }
    if [[ -n $console_pid ]]; then
        # The viewer drains the now-flushed log and closes both console outputs.
        touch "$state_dir/console-stop"
        wait "$console_pid" || echo "Console mirroring stopped; persistent logs are retained." >&2
    fi
    exit "$status"
}
record_error() {
    last_error="Bootstrap failed at line $2 (exit $1)."
}
trap finish_bootstrap EXIT
trap 'record_error "$?" "$LINENO"' ERR
trap 'last_error="Installation interrupted by SIGTERM."; exit 143' TERM
trap 'last_error="Installation interrupted by SIGINT."; exit 130' INT

echo "=== FreePBX installation started $(date -u +%FT%TZ) ==="
if [[ ! -f $monitor ]] || ! command -v python3 >/dev/null; then
    last_error='Monitor/Python missing. Run scripts/install-monitor.sh from the reviewed checkout before deploying bootstrap.sh.'
    exit 1
fi
export FREEPBX_MONITOR_STATE_DIR="$state_dir" FREEPBX_MONITOR_LOG_DIR="$log_dir"
python3 "$monitor" --begin "$$" --source bootstrap --revision "${PLAYBOOK_REF:-}" --log-offset "$log_offset"
monitor_ready=true
rm -f "$state_dir/console-stop"
# Separate, nonblocking viewer includes the detailed upstream installer log.
# It must not retain the bootstrap lock or write its own output into the tee pipe.
python3 "$monitor" --mirror-consoles 9>&- 1>/dev/null 2>&4 &
console_pid=$!
stage() { python3 "$monitor" --stage "$1"; }

stage 'Validating platform and deployment settings'
# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_ID:-} != 12 || $(dpkg --print-architecture) != amd64 ]]; then
    last_error='This installer requires Debian 12 amd64.'
    exit 1
fi
if [[ ! ${PLAYBOOK_REF:-} =~ ^[0-9a-f]{40}$ ]]; then
    last_error='Set PLAYBOOK_REF to the full, reviewed playbook commit SHA.'
    exit 1
fi

stage 'Waiting for a network address and default route (up to 120 seconds)'
python3 "$(dirname -- "$monitor")/wait_for_network.py" --timeout 120

export DEBIAN_FRONTEND=noninteractive
stage 'Installing bootstrap dependencies'
echo "Installing dependencies (APT retries transient network failures)..."
apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any update
apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y ansible git curl ca-certificates

echo "Checking HTTPS access to GitHub..."
stage 'Checking GitHub HTTPS access'
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 10 --max-time 30 --retry 5 --retry-delay 5 --retry-max-time 180 \
    --output /dev/null https://github.com/

# Keep each checkout for diagnosis; never delete an existing /root/playbook.
checkout=$(mktemp -d "$state_dir/playbook.XXXXXXXX")
stage 'Fetching reviewed playbook revision'
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
stage 'Running Ansible playbook'
export ANSIBLE_CONFIG="$checkout/ansible.cfg" FREEPBX_MONITOR_CAPTURED=1
ansible-playbook -i localhost, -c local "$checkout/site.yml" "${extra_vars[@]}"
python3 "$monitor" --verify-playbook

# All tasks, handlers, reloads and health checks must succeed first.
stage 'Verified installation completed'
python3 "$monitor" --finish completed
printf '%s\n' "$PLAYBOOK_REF" > "$state_dir/complete"
echo "Installation completed. The systemd service will now request a reboot."
