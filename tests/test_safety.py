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
        self.assertNotIn('enable_serial_console', defaults)
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
        self.assertIn('StandardOutput=journal\n', service)
        self.assertNotIn('StandardOutput=journal+console', service)
        self.assertIn('--mirror-consoles', (ROOT / 'bootstrap.sh').read_text())
        self.assertNotIn('reboot', (ROOT / 'bootstrap.sh').read_text().split('echo "Installation completed.')[0].replace('No reboot requested', ''))

    def test_intended_appliance_features_are_preserved(self):
        play = yaml.safe_load((ROOT / 'site.yml').read_text())[0]
        ssh = (ROOT / 'tasks/ssh.yml').read_text()
        self.assertIn('PermitRootLogin yes', ssh)
        self.assertIn('PasswordAuthentication yes', ssh)
        self.assertIn('validate: /usr/sbin/sshd -t -f %s', ssh)
        serial = next(t for t in play['tasks'] if t.get('ansible.builtin.import_tasks') == 'tasks/serial.yml')
        self.assertNotIn('when', serial)
        content = (ROOT / 'site.yml').read_text()
        self.assertIn('name: dnsmasq', content)
        self.assertIn('          allow\n', content)
        self.assertIn('downloadinstall certman --force', content)
        serial_config = (ROOT / 'tasks/serial.yml').read_text()
        self.assertIn('stty -F /dev/ttyS0 speed 115200', serial_config)
        self.assertIn('setserial /dev/ttyS0 baud_base 115200', serial_config)
        self.assertNotIn('rm -rf', (ROOT / 'bootstrap.sh').read_text())

    def test_supplied_dialplan_and_hangup_extension_are_installed(self):
        dialplan = (ROOT / 'files/from-pstn-sg.conf').read_text()
        self.assertIn('Set(SG_DID=${EXTEN})', dialplan)
        self.assertIn('PJSIP_PARSE_URI(${SG_URI},user)', dialplan)
        self.assertIn('exten => h,1,NoOp()', dialplan)
        self.assertNotIn('CUT(CUT(', dialplan)

    def test_management_access_is_applied_before_freepbx_installation(self):
        tasks = yaml.safe_load((ROOT / 'site.yml').read_text())[0]['tasks']
        installer_index = next(i for i, task in enumerate(tasks)
                               if task.get('ansible.builtin.import_tasks') == 'tasks/install.yml')
        ssh_index = next(i for i, task in enumerate(tasks)
                         if task.get('ansible.builtin.import_tasks') == 'tasks/ssh.yml')
        serial_index = next(i for i, task in enumerate(tasks)
                            if task.get('ansible.builtin.import_tasks') == 'tasks/serial.yml')
        flush_index = next(i for i, task in enumerate(tasks)
                           if task.get('ansible.builtin.meta') == 'flush_handlers')
        self.assertLess(ssh_index, installer_index)
        self.assertLess(serial_index, flush_index)
        self.assertLess(flush_index, installer_index)
        ssh_tasks = yaml.safe_load((ROOT / 'tasks/ssh.yml').read_text())
        package_index = next(i for i, task in enumerate(ssh_tasks) if 'ansible.builtin.apt' in task)
        validate_index = next(i for i, task in enumerate(ssh_tasks) if 'ansible.builtin.blockinfile' in task)
        self.assertLess(package_index, validate_index)
        self.assertEqual(ssh_tasks[package_index]['ansible.builtin.apt']['name'], 'openssh-server')
        apply = ssh_tasks[-1]['ansible.builtin.service']
        self.assertEqual(apply['name'], 'ssh')
        self.assertEqual(apply['state'], 'reloaded')
        self.assertTrue(apply['enabled'])

    def test_upstream_identity_and_completion_guards(self):
        tasks = (ROOT / 'tasks/install.yml').read_text()
        self.assertIn('checksum: "sha256:', tasks)
        self.assertIn('--skipversion', tasks)
        self.assertNotIn('creates: /etc/freepbx.conf', tasks)
        self.assertLess(tasks.index('upstream-started\n'), tasks.index('name: Run pinned FreePBX installer'))
        self.assertLess(tasks.index('name: Verify upstream installation'), tasks.index('name: Record verified upstream'))

    def test_network_failures_are_not_hidden_by_stale_apt_indexes(self):
        self.assertIn('APT::Update::Error-Mode=any update', (ROOT / 'bootstrap.sh').read_text())
        tasks = yaml.safe_load((ROOT / 'tasks/install.yml').read_text())
        block = next(task['block'] for task in tasks if 'block' in task)
        profiles = next(task['loop'] for task in block if task['name'] == 'Configure short network retries for this installer only')
        apt = next(profile['content'] for profile in profiles if profile['name'] == 'apt-network.conf')
        wget = next(profile['content'] for profile in profiles if profile['name'] == 'wget-network.conf')
        self.assertIn('APT::Update::Error-Mode "any";', apt)
        self.assertIn('Acquire::Retries "3";', apt)
        self.assertIn('read_timeout = 30', wget)
        self.assertIn('tries = 3', wget)
        # Validate real config syntax where the Linux test tools are available.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.conf'
            if shutil.which('apt-config'):
                path.write_text(apt)
                result = subprocess.run(['apt-config', '-c', str(path), 'dump'], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            if shutil.which('wget'):
                path.write_text(wget)
                result = subprocess.run(['wget', '--config=' + str(path), '--version'], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_final_health_checks_are_not_skipped_on_first_install(self):
        play = yaml.safe_load((ROOT / 'site.yml').read_text())[0]
        final = play['tasks'][-1]
        self.assertTrue(final['vars']['check_custom_dialplan'])
        self.assertEqual(final['vars']['required_freepbx_modules'], ['framework', 'core', 'certman'])
        health = yaml.safe_load((ROOT / 'tasks/health.yml').read_text())
        web = next(task['ansible.builtin.uri'] for task in health if 'ansible.builtin.uri' in task)
        self.assertEqual(web['url'], 'http://127.0.0.1/admin/')
        self.assertNotIn(500, web['status_code'])
        self.assertFalse(web['use_proxy'])


@unittest.skipUnless(os.name == 'posix' and shutil.which('flock'), 'requires Linux Bash/flock')
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / 'state'
        self.install_logs = self.base / 'logs'
        self.log = self.base / 'commands.log'
        mockbin = self.base / 'bin'
        mockbin.mkdir()
        mock = '''#!/bin/bash
set -eu
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$CALL_LOG"
echo "$name simulated output"
if [[ $name == "${FAIL_COMMAND:-}" ]]; then exit 42; fi
if [[ $name == dpkg ]]; then echo amd64; fi
if [[ $name == git && $* == *rev-parse* ]]; then echo "$PLAYBOOK_REF"; fi
if [[ $name == ansible-playbook && ${SKIP_MONITOR_COMPLETION:-} != 1 ]]; then
    "$REAL_PYTHON" -c 'import json,os,pathlib; p=pathlib.Path(os.environ["FREEPBX_MONITOR_STATE_DIR"])/"status.json"; d=json.loads(p.read_text()); d["playbook_result"]="completed"; p.write_text(json.dumps(d))'
fi
'''
        # stdout of dpkg/rev-parse is parsed by bootstrap, so do not add banners.
        mock = mock.replace('echo "$name simulated output"', 'if [[ $name != dpkg && $name != git ]]; then echo "$name simulated output"; fi')
        for name in ['apt-get', 'curl', 'git', 'ansible-playbook', 'dpkg']:
            target = mockbin / name
            target.write_text(mock)
            target.chmod(0o755)
        os_release = self.base / 'os-release'
        os_release.write_text('ID=debian\nVERSION_ID=12\n')
        # Exercise the real monitor, faking root only inside the test CLI process.
        import sys
        cli_runner = self.base / 'monitor_cli.py'
        cli_runner.write_text('import os, runpy, sys\nos.geteuid = lambda: 0\nsys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name="__main__")\n')
        python_wrapper = mockbin / 'python3'
        python_wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{cli_runner}" "$@"\n')
        python_wrapper.chmod(0o755)
        # Redirect only root/platform guards and persistent state into a sandbox.
        script = (ROOT / 'bootstrap.sh').read_text()
        script = script.replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
        script = script.replace('source /etc/os-release', f'source "{os_release}"')
        script = script.replace('state_dir=/var/lib/freepbx-install', f'state_dir="{self.state}"')
        script = script.replace('log_dir=/var/log/freepbx-install', f'log_dir="{self.install_logs}"')
        script = script.replace('monitor=/usr/local/lib/freepbx-install/freepbx_install_monitor.py',
                                f'monitor="{ROOT / "monitor/freepbx_install_monitor.py"}"')
        self.script = self.base / 'bootstrap.sh'
        self.script.write_text(script)
        self.env = dict(os.environ, PATH=f'{mockbin}:{os.environ["PATH"]}',
                        CALL_LOG=str(self.log), PLAYBOOK_REF='a' * 40, REAL_PYTHON=sys.executable)

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
        import json
        self.assertEqual(json.loads((self.state / 'status.json').read_text())['state'], 'completed')
        self.assertIn('ansible-playbook simulated output', (self.install_logs / 'install.log').read_text())

    def test_each_external_failure_prevents_completion(self):
        for command in ['apt-get', 'curl', 'git', 'ansible-playbook']:
            with self.subTest(command=command):
                result = self.run_bootstrap(FAIL_COMMAND=command)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.state / 'complete').exists())
                self.assertIn('No reboot requested', result.stdout + result.stderr)
                self.assertTrue(self.script.exists())
                import json
                status = json.loads((self.state / 'status.json').read_text())
                self.assertEqual(status['state'], 'failed')
                self.assertTrue(status['last_error'])
                self.assertIn(f'{command} simulated output' if command != 'git' else 'Fetching',
                              (self.install_logs / 'install.log').read_text())

    def test_missing_callback_completion_cannot_mark_success(self):
        result = self.run_bootstrap(SKIP_MONITOR_COMPLETION='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / 'complete').exists())
        self.assertIn('did not report monitored completion', result.stdout)

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
        config.write_text('resume_partial_install: false\n')
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
