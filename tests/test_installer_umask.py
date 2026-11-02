"""Regression for the observed unreadable FreePBX APT public-keyring failure."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class InstallerConfigurationTests(unittest.TestCase):
    def test_pinned_vendor_is_executed_through_scoped_umask_wrapper(self):
        tasks = yaml.safe_load((ROOT / 'tasks/install.yml').read_text())
        block = next(task['block'] for task in tasks if 'block' in task)
        installer = next(task for task in block if task['name'] == 'Run pinned FreePBX installer')
        argv = installer['ansible.builtin.command']['argv']
        self.assertEqual(argv, ['bash', '{{ playbook_dir }}/scripts/run-upstream-installer.sh',
                                '/var/lib/freepbx-install/sng_freepbx_debian_install.sh', '--skipversion'])
        private = next(task['ansible.builtin.file'] for task in block
                       if task.get('ansible.builtin.file', {}).get('path') == '/var/log/pbx')
        self.assertEqual(private['mode'], '0700')
        play = yaml.safe_load((ROOT / 'site.yml').read_text())[0]
        self.assertTrue(any(task.get('ansible.builtin.import_tasks') == 'tasks/apt-keyring.yml'
                            for task in play['pre_tasks']))


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'requires Linux file modes and Bash')
class InstallerUmaskTests(unittest.TestCase):
    def test_vendor_public_files_use_normal_permissions_but_parent_stays_private(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            vendor = base / 'vendor.sh'
            original = '#!/bin/bash\nset -eu\nprintf "public key" > "$1/public-key.gpg"\nmkdir "$1/system-dir"\numask > "$1/vendor-mask"\n'
            vendor.write_text(original)
            result = subprocess.run(['bash', '-c',
                                     'umask 077\nbash "$1" "$2" "$3"\ntouch "$3/private-parent-file"\n',
                                     'test', str(ROOT / 'scripts/run-upstream-installer.sh'), str(vendor), str(base)],
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((base / 'public-key.gpg').stat().st_mode & 0o777, 0o644)
            self.assertEqual((base / 'system-dir').stat().st_mode & 0o777, 0o755)
            self.assertEqual((base / 'private-parent-file').stat().st_mode & 0o777, 0o600)
            self.assertEqual(int((base / 'vendor-mask').read_text().strip(), 8), 0o22)
            self.assertEqual(vendor.read_text(), original)

    def test_wrapper_preserves_vendor_failure_code(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = Path(directory) / 'failed.sh'
            vendor.write_text('exit 42\n')
            result = subprocess.run(['bash', str(ROOT / 'scripts/run-upstream-installer.sh'), str(vendor)],
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 42)


if __name__ == '__main__':
    unittest.main()
