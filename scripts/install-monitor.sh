#!/bin/bash
set -euo pipefail
umask 077
if [[ $EUID -ne 0 ]]; then
    echo "Run as root." >&2
    exit 1
fi
command -v python3 >/dev/null || { echo "Install python3 before setting up the monitor." >&2; exit 1; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install -d -m 0700 /var/lib/freepbx-install /var/log/freepbx-install
install -d -m 0755 /usr/local/lib/freepbx-install /usr/local/sbin /etc/profile.d /etc/logrotate.d
install -m 0644 "$root/monitor/freepbx_install_monitor.py" /usr/local/lib/freepbx-install/freepbx_install_monitor.py
install -m 0644 "$root/monitor/wait_for_network.py" /usr/local/lib/freepbx-install/wait_for_network.py
install -m 0755 "$root/monitor/freepbx-install-status" /usr/local/sbin/freepbx-install-status
install -m 0644 "$root/monitor/freepbx-install-notice.sh" /etc/profile.d/freepbx-install.sh
install -m 0644 "$root/monitor/freepbx-install.logrotate" /etc/logrotate.d/freepbx-install
echo "Installation monitor installed. Run freepbx-install-status from any root/sudo SSH session."
