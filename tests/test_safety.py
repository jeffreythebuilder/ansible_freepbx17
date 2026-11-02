"""Non-destructive regression tests. Bootstrap commands are mocked."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_yaml_documents_parse(self):
        for path in ROOT.rglob('*.yml'):
            with self.subTest(path=path):
                yaml.safe_load(path.read_text())

    def test_safe_defaults(self):
        defaults = yaml.safe_load((ROOT / 'defaults.yml').read_text())
        self.assertIs(defaults['enable_serial_console'], False)
        self.assertEqual(defaults['ntp_allowed_networks'], [])
        self.assertIs(defaults['resume_partial_install'], False)
        self.assertRegex(defaults['freepbx_installer_commit'], r'^[0-9a-f]{40}$')
        self.assertRegex(defaults['freepbx_installer_sha256'], r'^[0-9a-f]{64}$')

    def test_failures_are_not_suppressed(self):
        for path in [ROOT / 'site.yml', *ROOT.glob('tasks/*.yml')]:
            self.assertNotIn('ignore_errors:', path.read_text())

    def test_service_keeps_bootstrap_and_only_reboots_after_success(self):
        service = (ROOT / 'freepbx-install.service').read_text()
        self.assertIn('ConditionPathExists=!/var/lib/freepbx-install/complete', service)
        self.assertIn('ExecStartPost=/usr/bin/systemctl --no-block reboot', service)
        self.assertNotIn('/bin/rm', service)
        self.assertNotIn('reboot', (ROOT / 'bootstrap.sh').read_text().split('echo "Installation completed.')[0].replace('No reboot requested', ''))

    def test_ssh_and_dns_are_not_enabled(self):
        content = (ROOT / 'bootstrap.sh').read_text() + (ROOT / 'site.yml').read_text()
        self.assertNotIn('PermitRootLogin yes', content)
        self.assertNotIn('name: dnsmasq', content)
        self.assertNotIn('rm -rf', content)

    def test_upstream_identity_and_completion_guards(self):
        tasks = (ROOT / 'tasks/install.yml').read_text()
        self.assertIn('checksum: "sha256:', tasks)
        self.assertIn('--skipversion', tasks)
        self.assertNotIn('creates: /etc/freepbx.conf', tasks)
        self.assertLess(tasks.index('upstream-started\n'), tasks.index('name: Run pinned FreePBX installer'))
        self.assertLess(tasks.index('name: Verify upstream installation'), tasks.index('name: Record verified upstream'))

    def test_final_health_checks_are_not_skipped_on_first_install(self):
        play = yaml.safe_load((ROOT / 'site.yml').read_text())[0]
        final = play['tasks'][-1]
        self.assertTrue(final['vars']['check_custom_dialplan'])
        self.assertEqual(final['vars']['required_freepbx_modules'], ['framework', 'core', 'certman'])


@unittest.skipUnless(os.name == 'posix' and shutil.which('flock'), 'requires Linux Bash/flock')
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / 'state'
        self.log = self.base / 'commands.log'
        mockbin = self.base / 'bin'
        mockbin.mkdir()
        mock = '''#!/bin/bash
set -eu
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$CALL_LOG"
if [[ $name == "${FAIL_COMMAND:-}" ]]; then exit 42; fi
if [[ $name == dpkg ]]; then echo amd64; fi
if [[ $name == git && $* == *rev-parse* ]]; then echo "$PLAYBOOK_REF"; fi
'''
        for name in ['apt-get', 'curl', 'git', 'ansible-playbook', 'dpkg']:
            target = mockbin / name
            target.write_text(mock)
            target.chmod(0o755)
        os_release = self.base / 'os-release'
        os_release.write_text('ID=debian\nVERSION_ID=12\n')
        # Redirect only root/platform guards and persistent state into a sandbox.
        script = (ROOT / 'bootstrap.sh').read_text()
        script = script.replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
        script = script.replace('source /etc/os-release', f'source "{os_release}"')
        script = script.replace('state_dir=/var/lib/freepbx-install', f'state_dir="{self.state}"')
        self.script = self.base / 'bootstrap.sh'
        self.script.write_text(script)
        self.env = dict(os.environ, PATH=f'{mockbin}:{os.environ["PATH"]}',
                        CALL_LOG=str(self.log), PLAYBOOK_REF='a' * 40)

    def run_bootstrap(self, **overrides):
        return subprocess.run(['bash', str(self.script)], env=dict(self.env, **overrides),
                              text=True, capture_output=True, timeout=15)

    def commands(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_success_marks_complete_and_does_not_reboot_itself(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'complete').read_text(), 'a' * 40 + '\n')
        self.assertIn('ansible-playbook ', self.commands())
        self.assertNotIn('systemctl', self.commands())
        self.assertTrue(self.script.exists())

    def test_each_external_failure_prevents_completion(self):
        for command in ['apt-get', 'curl', 'git', 'ansible-playbook']:
            with self.subTest(command=command):
                result = self.run_bootstrap(FAIL_COMMAND=command)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.state / 'complete').exists())
                self.assertIn('No reboot requested', result.stderr)
                self.assertTrue(self.script.exists())

    def test_invalid_or_moving_ref_stops_before_package_changes(self):
        for ref in ['', 'main', 'a' * 39, 'b' * 41]:
            with self.subTest(ref=ref):
                result = self.run_bootstrap(PLAYBOOK_REF=ref)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('apt-get', self.commands())

    def test_completion_skips_installation(self):
        self.state.mkdir()
        (self.state / 'complete').write_text('a' * 40 + '\n')
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('apt-get', self.commands())

    def test_extra_vars_file_is_passed_as_one_argument(self):
        config = self.base / 'local vars.yml'
        config.write_text('enable_serial_console: false\n')
        result = self.run_bootstrap(PLAYBOOK_VARS_FILE=str(config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'--extra-vars @{config}', self.commands())

    def test_missing_extra_vars_file_fails(self):
        result = self.run_bootstrap(PLAYBOOK_VARS_FILE=str(self.base / 'missing.yml'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / 'complete').exists())
        self.assertNotIn('ansible-playbook ', self.commands())


if __name__ == '__main__':
    unittest.main()
