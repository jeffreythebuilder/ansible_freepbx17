# Debian Installer ISO integration

The first-boot bootstrap is not a standalone two-file deployment. It needs the
monitor helpers and `/etc/default/freepbx-install` with a reviewed `PLAYBOOK_REF`.
Downloading only bootstrap.sh and the service from moving `main` is incomplete.

For an unattended Debian 12 ISO, export a reviewed repository revision into
`/freepbx/` on the ISO and write that revision's full commit SHA plus a newline
to `/freepbx/REVISION`. Include `python3`, `git`, `curl`, `ca-certificates`,
`openssh-server`, `ansible`, and `logrotate` in the target's package selection.
Keep any existing packages needed by your image.

Replace only the existing late command with this single logical line:

```text
d-i preseed/late_command string mkdir -p /target/root/freepbx-setup && cp -a /cdrom/freepbx/. /target/root/freepbx-setup/ && in-target /bin/bash -c 'cd /root/freepbx-setup && bash scripts/setup-first-boot.sh "$(cat REVISION)"'
```

This copies a complete, version-consistent payload without relying on late-stage
network downloads. The setup script installs monitoring, bootstrap and service,
records the revision, and **enables but does not start** the first-boot service.
Failures propagate to Debian Installer; do not append `|| true`.

Do not place private password hashes or a deployment-specific destructive
preseed into the public repository. Preserve those in the local ISO build tree.
Update the ISO checksum manifest for modified/new files, and replay the source
image's BIOS/UEFI/hybrid boot configuration when rebuilding it. Default boot
entries must load the intended preseed; manual graphical entries need not do so.

Boot-test the rebuilt image only in a disposable VM with an empty virtual disk:
an unattended preseed may erase disks without confirmation. A boot-catalog
inspection is not a substitute for an actual VM installation test.

## Rebuild tool

`scripts/rebuild-iso.py` prepares an entirely new image from a mounted source
ISO, using a committed repository snapshot as its payload. Requires Python 3.12+
(for ISO verification, install `pycdlib==1.22.0`) and xorriso on the build host.
Neither xorriso nor pycdlib is needed on the installed PBX.

Example (adjust paths for your platform):

```sh
python scripts/rebuild-iso.py --input-iso original.iso --source-tree /mnt/original \
  --output-iso corrected.iso --work-dir /private/new-iso-stage --xorriso /usr/bin/xorriso
```

The source image is never overwritten. Only package selection and late-command
lines are rewritten; credentials, partitioning, boot-menu choices and all other
preseed settings remain unchanged. It regenerates the file checksum manifest,
replays BIOS/UEFI/hybrid boot metadata, verifies every manifest entry inside the
new image, and writes a SHA256 sidecar plus a verification report. The private
work directory contains your preseed: do not publish it or commit it to GitHub.
