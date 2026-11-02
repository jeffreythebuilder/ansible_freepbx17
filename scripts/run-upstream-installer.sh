#!/bin/bash
# Bootstrap intentionally uses 077 for private state/logs. Do not let that
# restrictive mask leak into a vendor system installer: its public APT keyring
# and ordinary system directories must be readable by sandboxed services.
set -euo pipefail
umask 022
exec bash "$@"
