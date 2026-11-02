#!/bin/bash
# Run from a complete reviewed checkout or an ISO payload of that revision.
# Only enable the first-boot service; never start it inside Debian Installer.
set -euo pipefail
umask 077
if [[ $EUID -ne 0 ]]; then
    echo "Run first-boot setup as root." >&2
    exit 1
fi
revision=${1:-}
if [[ ! $revision =~ ^[0-9a-f]{40}$ ]]; then
    echo "Pass the reviewed, full 40-character playbook commit SHA." >&2
    exit 1
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v python3 >/dev/null
test -f "$root/bootstrap.sh"
test -f "$root/freepbx-install.service"

# Install every helper before enabling a service which depends on them.
bash "$root/scripts/install-monitor.sh"
install -m 0700 "$root/bootstrap.sh" /root/bootstrap.sh
install -m 0644 "$root/freepbx-install.service" /etc/systemd/system/freepbx-install.service
python3 - "$revision" /etc/default/freepbx-install <<'PY'
import os
from pathlib import Path
import re
import sys
import tempfile

revision, filename = sys.argv[1:]
path = Path(filename)
text = path.read_text() if path.exists() else ''
lines = [line for line in text.splitlines() if not re.match(r'^\s*PLAYBOOK_REF\s*=', line)]
lines.append('PLAYBOOK_REF=' + revision)
fd, temporary = tempfile.mkstemp(prefix='.freepbx-install-', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as output:
        output.write('\n'.join(lines) + '\n')
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
if [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
fi
systemctl --root=/ enable freepbx-install.service
echo "First-boot installation prepared for $revision. It will start at the next boot."
