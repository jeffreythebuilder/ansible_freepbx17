# ansible_freepbx17

Ansible playbook for installing FreePBX 17 on Debian 12, including the
Provisioner module and the local Singapore trunk customizations used by this
deployment.

## What it installs

- FreePBX 17 prerequisites and the upstream FreePBX installer.
- `dnsmasq`, `chrony`, serial-console settings, and `certman`.
- [Provisioner](https://github.com/jeffreythebuilder/provisioner), checked out
  from the configured repository and installed with FreePBX Module Admin.
- The `from-pstn-sg` inbound dialplan context.
- The `macro-dialout-trunk-predial-hook` for trunk `4`, which preserves the
  originating extension caller ID for forwarded and blind-transferred calls.

## Run

```sh
ansible-playbook -i localhost, -c local site.yml
```

The playbook is intended for a fresh FreePBX 17 Debian 12 host and requires
root privileges. `bootstrap.sh` installs Ansible and runs the playbook during
initial provisioning.

## Pin the Provisioner build

The default follows the Provisioner `main` branch. For repeatable production
installs, override `provisioner_version` with a reviewed commit SHA:

```sh
ansible-playbook -i localhost, -c local site.yml \
  -e provisioner_version=64a7685
```

You can also override `provisioner_repo`, `provisioner_source`, and
`provisioner_module_dir` in inventory or extra variables.

## Trunk hook assumptions

The managed hook applies only when `${DIAL_TRUNK}` is `4` and uses the
FreePBX device, call-forward, PJSIP endpoint, and outbound caller-ID data.
It expects the trunk to support the `Referred-By` SIP header and the standard
`func-set-sipheader` subroutine. Review the generated dialplan and test normal
outbound calls, unconditional forwarding, blind transfers, invalid caller IDs,
and non-trunk-4 calls after deployment.

The hook is stored in `/etc/asterisk/extensions_custom.conf` using an Ansible
managed block. Do not edit that block manually; change `site.yml` and rerun
the playbook.
