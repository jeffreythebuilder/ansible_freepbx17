# FreePBX 17 on Debian 12

Ansible wrapper around Sangoma's FreePBX installer, with an optional first-boot
systemd service and a Singapore inbound-trunk dialplan.

**Use a fresh Debian 12 amd64 machine or full VM with systemd. Take a snapshot
first. This is not an upgrade/migration tool and must not be run on a live PBX.**
Set a resolvable hostname in `/etc/hostname` and `/etc/hosts`. Ensure working APT
repositories, DNS, internet access and adequate disk space before starting.

## Safety defaults

- Failures stop the bootstrap without rebooting; source checkouts and installer
  logs are retained. `/root/playbook` is never deleted.
- Require a reviewed playbook commit SHA. The upstream script is also pinned to
  a commit and verified by SHA256 before execution.
- Existing SSH authentication settings are left untouched. Root password login
  is **not** enabled. Establish and test SSH keys or a sudo account yourself.
- Serial-console/GRUB changes are disabled by default. When enabled, a GRUB
  drop-in appends console options without replacing existing kernel parameters.
- No `dnsmasq` installation. DNS/DHCP provisioning is outside this playbook.
- Chrony is installed by upstream, but NTP serving is disabled (`port 0`) unless
  client networks are explicitly provided. NTP synchronization still works.
- Completion requires successful commands, enabled framework/core/certman
  modules, active core services, an Asterisk response and the custom context
  loaded after `fwconsole reload`.

These defaults do **not** harden an existing installation: previously enabled
root SSH, dnsmasq and old GRUB edits are not automatically undone. Audit them
before adopting this version. This playbook does not configure a firewall or
complete FreePBX's web setup. Keep the machine on a trusted management network,
restrict administrative access, and configure provider/SIP/RTP firewall rules
before exposure. Do not expose an unfinished FreePBX web setup to the internet.

## Run directly

As root, clone the repository, check out a **reviewed full commit SHA**, then:

```sh
apt-get update
apt-get install -y ansible git ca-certificates
git clone https://github.com/jeffreythebuilder/ansible_freepbx17.git
cd ansible_freepbx17
git checkout --detach YOUR_REVIEWED_40_CHARACTER_COMMIT_SHA
ansible-playbook -i localhost, -c local site.yml
```

This does not reboot. Reboot manually after a successful run and verify services,
inbound/outbound calls and audio. Re-running skips only an upstream installation
that has a verified completion record; configuration and health checks run again.
Run only one installation at a time; do not run a direct playbook alongside the
first-boot service.

Optional overrides, for example `/root/freepbx-vars.yml` (root-owned, mode 0600):

```yaml
enable_serial_console: true  # Requires a working /dev/ttyS0 and Debian GRUB.
ntp_allowed_networks:
  - 192.168.10.0/24          # Replace with your actual LAN; do not use 0.0.0.0/0.
```

```sh
ansible-playbook -i localhost, -c local site.yml --extra-vars @/root/freepbx-vars.yml
```

Only advertise NTP to networks you trust and allow UDP/123 from those networks
in your firewall. Invalid CIDRs fail before installation. To return to client-only
NTP, set `ntp_allowed_networks: []` and rerun. Disabling the serial option merely
stops managing it; remove `/etc/default/grub.d/99-freepbx-serial.cfg`, run
`update-grub` and disable `serial-getty@ttyS0.service` to undo an earlier opt-in.

## First-boot service

From the reviewed checkout, as root:

```sh
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

## Failure and recovery

Inspect:

```sh
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

Only `+65` plus 8 digits or `65` plus 8 digits is reduced to an 8-digit DID.
An already-local DID beginning with `65` is unchanged. Caller number/name has
`+65` removed only when the entire value is an 11-character Singapore number;
ordinary display names are preserved. Match your inbound routes to the resulting
8-digit DIDs. Modern FreePBX uses PJSIP; the SIP branch is retained only for older
channel drivers and cannot fall through into PJSIP handling.

## Dependency updates and testing

Update `freepbx_installer_commit` and `freepbx_installer_sha256` in `defaults.yml`
together after reviewing upstream. `--skipversion` bypasses upstream's comparison
to moving `master`, **not** checksum verification. OS packages and FreePBX modules
still come from live upstream repositories; this is not a fully reproducible
package snapshot. Upstream also controls repository trust and system-wide
configuration changes; pinning the wrapper does not audit all its dependencies.

CI checks Bash syntax/ShellCheck, Ansible syntax, regression tests and dialplan
behavior in an isolated Asterisk instance. These do not substitute for a complete
Debian 12 installation test and real provider call/audio testing in a disposable
VM before production use.
