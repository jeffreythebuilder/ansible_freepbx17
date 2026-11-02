"""Test preseed rewriting without credentials, booting an ISO or touching disks."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('iso_builder_test', ROOT / 'scripts/rebuild-iso.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IsoPreseedTests(unittest.TestCase):
    def source(self):
        return ('# Local private settings\n'
                'd-i passwd/root-password-crypted password FAKE_TEST_HASH\n'
                'd-i passwd/user-password-crypted password FAKE_USER_TEST_HASH\n'
                'd-i partman-auto/disk string /dev/sda\n'
                'd-i partman-auto/choose_recipe select atomic\n'
                'd-i time/zone string Asia/Singapore\n'
                'd-i netcfg/choose_interface select auto\n'
                'd-i pkgsel/include string openssh-server sudo curl git python3 ansible\n'
                'd-i preseed/late_command string wget old-bootstrap || true\n'
                'd-i finish-install/reboot_in_progress note\n')

    def test_only_package_network_timeout_and_late_command_lines_change(self):
        source = self.source()
        result = MODULE.update_preseed(source)
        self.assertEqual(MODULE.protected_lines(source), MODULE.protected_lines(result))
        self.assertIn('FAKE_TEST_HASH', result)
        self.assertIn('partman-auto/disk string /dev/sda', result)
        self.assertIn('ca-certificates', result)
        self.assertIn('logrotate', result)
        self.assertIn('iproute2', result)
        self.assertIn('cp -a /cdrom/freepbx/.', result)
        self.assertIn('bash scripts/setup-first-boot.sh "$(cat REVISION)"', result)
        self.assertNotIn('|| true', result)
        self.assertNotIn('wget', result)
        self.assertIn('d-i netcfg/link_wait_timeout string 30\n', result)
        self.assertIn('d-i netcfg/dhcp_timeout string 120\n', result)

    def test_package_selection_is_not_duplicated_on_rebuild(self):
        first = MODULE.update_preseed(self.source())
        self.assertEqual(MODULE.update_preseed(first), first)
        packages = next(line for line in first.splitlines() if line.startswith('d-i pkgsel/include')).split()[3:]
        self.assertEqual(len(packages), len(set(packages)))
        self.assertEqual(first.count('netcfg/link_wait_timeout'), 1)
        self.assertEqual(first.count('netcfg/dhcp_timeout'), 1)

    def test_old_network_timeouts_are_replaced_without_duplicates(self):
        source = 'd-i netcfg/link_wait_timeout string 3\nd-i netcfg/dhcp_timeout string 5\n' + self.source()
        result = MODULE.update_preseed(source)
        self.assertEqual(result.count('netcfg/link_wait_timeout'), 1)
        self.assertEqual(result.count('netcfg/dhcp_timeout'), 1)
        self.assertIn('netcfg/dhcp_timeout string 120', result)

    def test_missing_or_ambiguous_setup_hook_is_rejected(self):
        for text in ['', self.source() + 'd-i preseed/late_command string other\n']:
            with self.assertRaises(ValueError):
                MODULE.update_preseed(text)

    def test_crlf_private_settings_are_preserved(self):
        source = self.source().replace('\n', '\r\n')
        result = MODULE.update_preseed(source)
        self.assertEqual(MODULE.protected_lines(result), MODULE.protected_lines(source))
        self.assertEqual(result.count('\n'), result.count('\r\n'))

    @unittest.skipUnless(shutil.which('bash'), 'requires Bash syntax checker')
    def test_late_command_shell_syntax_without_executing_it(self):
        command = MODULE.LATE_COMMAND.split(' string ', 1)[1]
        result = subprocess.run(['bash', '-n', '-c', command], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
