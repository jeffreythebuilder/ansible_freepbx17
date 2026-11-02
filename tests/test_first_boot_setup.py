"""Exercise ISO/first-boot setup in a sandbox; never enable host services."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix' and shutil.which('install'), 'requires Linux Bash/install')
class FirstBootSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.payload = self.base / 'payload'
        self.target = self.base / 'target'
        self.bin = self.base / 'bin'
        for path in [self.payload / 'scripts', self.target / 'root', self.target / 'etc/default',
                     self.target / 'etc/systemd/system', self.bin]:
            path.mkdir(parents=True, exist_ok=True)
        self.log = self.base / 'calls'
        self.environment_file = self.target / 'etc/default/freepbx-install'
        (self.payload / 'bootstrap.sh').write_text('#!/bin/bash\necho bootstrap\n')
        (self.payload / 'freepbx-install.service').write_text('[Service]\nType=oneshot\n')
        (self.payload / 'scripts/install-monitor.sh').write_text(
            '#!/bin/sh\nprintf "monitor\\n" >> "$CALL_LOG"\ntest "${FAIL_MONITOR:-0}" = 0\n')
        source = (ROOT / 'scripts/setup-first-boot.sh').read_text()
        source = source.replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
        source = source.replace('if [[ -d /run/systemd/system ]]; then', 'if true; then')
        source = source.replace('/root/bootstrap.sh', str(self.target / 'root/bootstrap.sh'))
        source = source.replace('/etc/systemd/system/freepbx-install.service', str(self.target / 'etc/systemd/system/freepbx-install.service'))
        source = source.replace('/etc/default/freepbx-install', str(self.environment_file))
        self.script = self.payload / 'scripts/setup-first-boot.sh'
        self.script.write_text(source)
        systemctl = self.bin / 'systemctl'
        systemctl.write_text('#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$CALL_LOG"\n')
        systemctl.chmod(0o755)
        python = self.bin / 'python3'
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', CALL_LOG=str(self.log))

    def run_setup(self, revision='a' * 40, **environment):
        return subprocess.run(['bash', str(self.script), revision], text=True, capture_output=True,
                              env=dict(self.env, **environment), timeout=15)

    def test_complete_payload_is_configured_before_service_is_enabled(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls, ['monitor', 'systemctl daemon-reload', 'systemctl --root=/ enable freepbx-install.service'])
        self.assertEqual(self.environment_file.read_text(), 'PLAYBOOK_REF=' + 'a' * 40 + '\n')
        self.assertEqual(self.environment_file.stat().st_mode & 0o777, 0o600)
        bootstrap = self.target / 'root/bootstrap.sh'
        self.assertEqual(bootstrap.read_bytes(), (self.payload / 'bootstrap.sh').read_bytes())
        self.assertEqual(bootstrap.stat().st_mode & 0o777, 0o700)

    def test_existing_extra_vars_are_preserved_and_revision_is_updated_once(self):
        self.environment_file.write_text('# custom\nPLAYBOOK_VARS_FILE=/root/my-vars.yml\nPLAYBOOK_REF=' + 'b' * 40 + '\n')
        for _ in range(2):
            result = self.run_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
        text = self.environment_file.read_text()
        self.assertIn('PLAYBOOK_VARS_FILE=/root/my-vars.yml', text)
        self.assertIn('# custom', text)
        self.assertEqual(text.count('PLAYBOOK_REF='), 1)
        self.assertIn('PLAYBOOK_REF=' + 'a' * 40, text)

    def test_monitor_failure_is_not_hidden_and_service_is_not_enabled(self):
        result = self.run_setup(FAIL_MONITOR='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text(), 'monitor\n')
        self.assertFalse(self.environment_file.exists())
        self.assertFalse((self.target / 'root/bootstrap.sh').exists())

    def test_missing_or_moving_revision_is_rejected_before_setup(self):
        for revision in ['', 'main', 'a' * 39]:
            with self.subTest(revision=revision):
                result = self.run_setup(revision)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
