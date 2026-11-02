# FreePBX 17 on Debian 12

Ansible wrapper around Sangoma's FreePBX installer, with an optional first-boot
systemd service and a Singapore inbound-trunk dialplan.

**Use a fresh Debian 12 amd64 machine or full VM with systemd. Take a snapshot
first. This is not an upgrade/migration tool and must not be run on a live PBX.**
The appliance requires a usable `/dev/ttyS0` and Debian GRUB; serial-console
configuration is mandatory and is checked before FreePBX installation starts.
Set a resolvable hostname in `/etc/hostname` and `/etc/hosts`. Ensure working APT
repositories, DNS, internet access and adequate disk space before starting.

## Preserved appliance features and reliability fixes

- Failures stop the bootstrap without rebooting; source checkouts and installer
  logs are retained. `/root/playbook` is never deleted.
- Require a reviewed playbook commit SHA. The upstream script is also pinned to
  a commit and verified by SHA256 before execution.
- Root password SSH login is deliberately enabled (`PermitRootLogin yes` and
  `PasswordAuthentication yes`). The configuration is syntax-checked before
  applying and effective root settings are checked before reloading Debian's
  `ssh` service. Set a strong root password yourself; this playbook does not set,
  unlock or reset passwords or bypass PAM/account/access restrictions. Test from
  your actual management client; address-specific `Match` rules may differ.
  The SSH server is installed and its configuration applied **before FreePBX
  starts**, including on retries where an earlier run wrote but did not apply it.
- Serial-console setup is mandatory: GRUB, serial-getty, `stty` at 115200 and
  `setserial baud_base 115200` are retained. A GRUB drop-in appends console options
  without replacing existing kernel parameters. The UART baud base is a hardware
  parameter, not the current line speed; unsupported hardware fails explicitly.
  Runtime serial speed/login-console setup and the GRUB update also complete
  **before FreePBX starts**, so an installer failure does not defer those steps.
- `dnsmasq` is installed as before. Its site-specific DNS/DHCP configuration is
  left to the administrator; installing it does not configure DHCP provisioning.
- Chrony serves NTP with unrestricted `allow`, as intended for this appliance.
  Restrict network exposure externally if the machine can reach untrusted peers.
- Certman is force-installed with `fwconsole ma downloadinstall certman --force`,
  preserving the original behavior. Required failures are no longer ignored.
- Completion requires successful commands, enabled framework/core/certman
  modules, active core services, an Asterisk response, a non-error web-application
  response and the custom context loaded after `fwconsole reload`.
- Default monitoring records the current stage/task, terminal success/failure,
  timestamps and last error. Root SSH login notices flag running, failed or
  interrupted installs; persistent logs remain available after disconnect/reboot.
- First-boot/bootstrap output automatically streams to **both the physical
  display (`/dev/tty0`) and serial console (`/dev/ttyS0`)**, including FreePBX's
  detailed upstream log once it appears. Journal and disk logs are retained too.

This preserves your appliance's intended services rather than imposing a new
security policy. Old GRUB edits are not automatically undone. Audit them before
adopting this version. This playbook does not configure a firewall or
complete FreePBX's web setup. Keep the machine on a trusted management network,
restrict administrative access, and configure provider/SIP/RTP firewall rules
before exposure. Do not expose an unfinished FreePBX web setup to the internet.

## Run directly

As root, clone the repository, check out a **reviewed full commit SHA**, then:

```sh
apt-get update
apt-get install -y ansible git ca-certificates python3
git clone https://github.com/jeffreythebuilder/ansible_freepbx17.git
cd ansible_freepbx17
git checkout --detach YOUR_REVIEWED_40_CHARACTER_COMMIT_SHA
bash scripts/install-monitor.sh
ansible-playbook -i localhost, -c local site.yml
```

This does not reboot. Reboot manually after a successful run and verify services,
inbound/outbound calls and audio. Re-running skips only an upstream installation
that has a verified completion record; configuration and health checks run again.
The supplied `ansible.cfg` enables the monitoring callback. Run from this checkout
(or explicitly set `ANSIBLE_CONFIG` to its config file); the playbook checks that
monitoring is active before making installation changes. Run only one installation
at a time; do not run a direct playbook alongside the first-boot service.

Overrides such as the explicit recovery option below can be placed in a
root-owned, mode 0600 `/root/freepbx-vars.yml` and passed using
`--extra-vars @/root/freepbx-vars.yml`. Root password SSH, serial-console setup,
dnsmasq installation, unrestricted NTP and forced certman installation are
intentional features, not opt-in features.

## First-boot service

From the reviewed checkout, as root:

```sh
apt-get install -y python3
bash scripts/install-monitor.sh
install -m 0700 bootstrap.sh /root/bootstrap.sh
install -m 0644 freepbx-install.service /etc/systemd/system/freepbx-install.service
printf 'PLAYBOOK_REF=%s\n' "$(git rev-parse HEAD)" > /etc/default/freepbx-install
chmod 0600 /etc/default/freepbx-install
# Optionally add: PLAYBOOK_VARS_FILE=/root/freepbx-vars.yml
systemctl daemon-reload
systemctl enable freepbx-install.service
```

Start it on next boot, or run `systemctl start freepbx-install.service` now.
**A successful service run automatically reboots the machine.** Failure never
requests a reboot. The bootstrap is kept for diagnosis, and
`/var/lib/freepbx-install/complete` prevents another installation on later boots.
The commit SHA is stored in that file for auditing. The bootstrap's lock prevents
two concurrent bootstrap runs. Running the bootstrap directly requires exporting
`PLAYBOOK_REF`; only the systemd service loads `/etc/default/freepbx-install`.

Install the monitor files **before** enabling the first-boot service so even an
early APT/Git/network failure has a status command and SSH notice available.
Bootstrap records a missing/invalid deployment revision as a monitored failure;
systemd also records abnormal termination or a failed reboot request.

## Check from any SSH session

As root (or prefix the commands with `sudo`):

```sh
freepbx-install-status          # State, current stage/task, last error and log paths
freepbx-install-status --follow # Live bootstrap/Ansible AND upstream installer logs
freepbx-install-status --json   # Machine-readable status
```

`--follow` discovers the upstream log automatically when FreePBX starts, even if
you attach during APT/package installation. Ctrl-C or an SSH disconnect stops
only the viewer, not the installation. Multiple SSH sessions can view it at once.
**No screen session is needed:** systemd owns the first-boot process independently
of SSH. A directly launched `ansible-playbook` still belongs to its shell; use the
systemd service when installation must survive disconnects.

The state is `RUNNING`, `FAILED`, `COMPLETED`, `INTERRUPTED` (runner died or the
machine rebooted mid-install), `NOT_STARTED`, or `UNKNOWN` if state cannot be read.
Interruption detection checks both the boot ID and process start time, not just a
PID that could have been reused. A root interactive SSH login displays a brief
notice only for running/failed/interrupted/unknown states. This uses
`/etc/profile.d/freepbx-install.sh` for standard login shells; it stays silent for
non-interactive SSH commands, SCP/SFTP, non-root users and completed installs.
Non-root administrators can still inspect details using `sudo`.

### Physical display and serial console

The bootstrap mirrors the current attempt to `tty0` (the active Linux virtual
console on the VGA/HDMI display) and `ttyS0` concurrently. Nothing needs to be
typed on either console. GRUB/serial-getty settings remain unchanged. The
systemd service writes to the journal rather than also writing `/dev/console`,
which avoids duplicating output on whichever device is the primary console.
This requires a Linux text console; a graphical desktop need not show `tty0`.

Console mirroring is a separate nonblocking viewer, so an unplugged, unavailable
or flow-controlled display cannot stall installation. If a console cannot keep
up, its bounded buffer may drop older display output; a warning is recorded and
the full output remains in the persistent logs. Historical attempts are not
replayed on the consoles at the start of a new attempt. These automatic console
streams apply to bootstrap/service runs; a manual `ansible-playbook` still prints
to its invoking terminal and records its task/results in the combined log.

### Network outages and installation correctness

The selected policy is **fail after short retries**, not wait indefinitely.
Bootstrap uses bounded APT/HTTPS retries; repository-update errors are fatal
instead of being accepted with stale package lists. The upstream installer gets
private APT/wget timeout/retry configuration, without changing the machine's
global package-manager configuration or modifying the checksum-verified script.
These settings apply to APT/wget, not every network client used by FreePBX/npm.
There is no overall stall watchdog for arbitrary upstream commands.

If the **server** loses connectivity, a command may recover within its retries or
fail. Failure stops deployment without requesting a reboot and records the
stage/task/error. If only your **SSH session** drops, the systemd-run installation
continues; reconnect to inspect it. A partial upstream install is never silently
retried or marked complete just because a configuration file exists. Follow the
recovery instructions below. Monitoring and checks detect known failures; only
an actual fresh Debian 12 installation and call tests can validate the complete
deployment in your environment.

Persistent files:

- `/var/log/freepbx-install/install.log`: combined bootstrap/Ansible output. For
  direct playbook runs, the callback records task/results into the same file.
- `/var/log/pbx/freepbx17-install-*.log`: upstream FreePBX's detailed progress.
- `/var/lib/freepbx-install/status.json`: current/last attempt and last error.

Logs/status are root-only because installer output can contain sensitive data.
The callback respects Ansible `no_log`; it records only a censored error for
private tasks. The combined log has a logrotate policy (10 MB threshold, five
rotations). Upstream log retention remains controlled by upstream. Each new
attempt resets status while preserving existing log history. This monitors
**installation**, not ongoing PBX uptime, and does not send email/webhook alerts.

## Failure and recovery

Inspect:

```sh
freepbx-install-status
freepbx-install-status --follow
journalctl -u freepbx-install.service -b --no-pager
ls -l /var/log/pbx/
ls -l /var/lib/freepbx-install/
```

The upstream installer writes logs under `/var/log/pbx`. Bootstrap checkouts are
retained under `/var/lib/freepbx-install/playbook.*`; remove individual old
checkouts only after investigating, not the installation state directory.

- Failure before upstream starts: correct networking/configuration and restart
  the service (`systemctl reset-failed freepbx-install.service`, then start it).
- Failure after `upstream-started` but before `upstream-complete`: restore your
  fresh snapshot if possible. Neither `/etc/freepbx.conf` nor a zero exit status
  alone proves installation succeeded. Automatic retries deliberately stop.
- Only after investigation and a backup, set `resume_partial_install: true` in
  your extra-vars file to explicitly rerun upstream. This is not guaranteed to
  repair arbitrary partial state; remove the override after recovery.
- Failure after `upstream-complete`: fix the reported configuration/module/health
  issue and rerun. Upstream is skipped, but remaining steps are retried.

Existing installations made by older versions have no completion record and
are deliberately refused. Do not create a marker just to bypass that safeguard.

## Inbound dialplan

Set a **trusted provider trunk's** context to `from-pstn-sg`. This parses numeric
DIDs from SIP/SIPS `To` headers, including angle-bracketed display-name forms.
Missing/invalid headers fall back to `from-pstn,${EXTEN},1`. It does not configure
the trunk, authenticate providers or create FreePBX inbound routes.

The supplied dialplan is used unchanged, including the explicit `h` extension.
It requires Asterisk's `PJSIP_PARSE_URI` function, even on the legacy SIP path;
the final health check verifies availability. Only `+65` plus 8 digits or `65`
plus 8 digits is reduced to an 8-digit DID. An already-local DID beginning with
`65` is unchanged. Both country-prefix forms are also removed from complete
Singapore caller numbers and numeric caller-ID names; ordinary display names
are preserved. Fallback DIDs are normalized too. Match inbound routes to resulting
8-digit DIDs. Modern FreePBX uses PJSIP; the SIP branch is retained only for older
channel drivers and cannot fall through into PJSIP handling.

## Dependency updates and testing

Update `freepbx_installer_commit` and `freepbx_installer_sha256` in `defaults.yml`
together after reviewing upstream. `--skipversion` bypasses upstream's comparison
to moving `master`, **not** checksum verification. OS packages and FreePBX modules
still come from live upstream repositories; this is not a fully reproducible
package snapshot. Upstream also controls repository trust and system-wide
configuration changes; pinning the wrapper does not audit all its dependencies.

CI checks Bash syntax/ShellCheck, Ansible syntax, monitor/bootstrap regression
tests, actual Ansible callback behavior (including failed/private tasks) and
dialplan behavior in an isolated Asterisk instance. Ubuntu 22.04 is only the
GitHub-hosted test runner, chosen for its packaged Asterisk; deployment targets
remain Debian 12. These checks do not substitute for a complete
Debian 12 installation test and real provider call/audio testing in a disposable
VM before production use.
