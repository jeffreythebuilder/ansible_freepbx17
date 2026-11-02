"""Unit tests and harmless real-Ansible callback integration tests."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('monitor_under_test', ROOT / 'monitor/freepbx_install_monitor.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.monitor = MODULE.Monitor(self.base / 'state', self.base / 'log')

    def begin(self):
        return self.monitor.begin(os.getpid(), 'test', 'a' * 40)

    def cli(self, *arguments):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'FREEPBX_MONITOR_STATE_DIR': str(self.monitor.directory),
                                          'FREEPBX_MONITOR_LOG_DIR': str(self.monitor.log_dir)}), \
                mock.patch.object(MODULE.os, 'geteuid', return_value=0, create=True), redirect_stdout(output):
            result = MODULE.main(list(arguments))
        return result, output.getvalue()

    def test_not_started_does_not_create_state_just_for_viewing(self):
        self.assertEqual(self.monitor.status()['state'], 'not_started')
        self.assertFalse(self.monitor.directory.exists())

    def test_begin_stage_and_completion_persist(self):
        self.begin()
        self.monitor.update(stage='Installing FreePBX', task='Run pinned FreePBX installer')
        data = self.monitor.finish('completed')
        self.assertEqual(data['state'], 'completed')
        self.assertTrue(data['finished_at'])
        self.assertEqual(self.monitor.read()['task'], 'Run pinned FreePBX installer')
        self.assertEqual(json.loads(self.monitor.path.read_text()), data)

    def test_generic_bootstrap_failure_keeps_detailed_ansible_error(self):
        self.begin()
        self.monitor.finish('failed', 'Package repository unreachable', 42)
        self.monitor.finish('failed', 'ansible-playbook exited nonzero', 2)
        self.assertEqual(self.monitor.read()['last_error'], 'Package repository unreachable')

    def test_stale_running_record_is_interrupted_not_completed(self):
        self.begin()
        self.monitor.update(boot_id='previous-boot')
        self.assertEqual(self.monitor.status()['state'], 'interrupted')
        self.assertEqual(self.monitor.read()['state'], 'running')

    def test_live_runner_cannot_be_replaced(self):
        with mock.patch.object(MODULE, 'process_identity', return_value=('boot', 'start')):
            self.begin()
            self.assertEqual(self.monitor.status()['state'], 'running')
            with self.assertRaises(RuntimeError):
                self.begin()

    def test_missing_reused_pid_or_new_boot_is_not_live(self):
        record = dict(pid=123, boot_id='boot', start_ticks='original')
        for identity in [('', ''), ('new-boot', 'original'), ('boot', 'reused-pid')]:
            with self.subTest(identity=identity), mock.patch.object(MODULE, 'process_identity', return_value=identity):
                self.assertFalse(self.monitor.is_alive(record))

    def test_corrupt_status_is_visible_not_successful(self):
        self.monitor.prepare()
        self.monitor.path.write_text('not JSON')
        self.assertEqual(self.monitor.status()['state'], 'unknown')
        self.assertTrue(self.monitor.status()['last_error'])

    def test_login_notice_only_for_attention_states(self):
        self.assertEqual(self.cli('--notice')[1], '')
        self.begin()
        self.monitor.finish('failed', 'Explicit failure')
        self.assertIn('FAILED', self.cli('--notice')[1])
        self.monitor.finish('completed')
        self.assertEqual(self.cli('--notice')[1], '')

    def test_nonroot_notice_is_silent_and_does_not_request_sudo(self):
        output = io.StringIO()
        with mock.patch.object(MODULE.os, 'geteuid', return_value=1000, create=True), redirect_stdout(output):
            self.assertEqual(MODULE.main(['--notice']), 0)
        self.assertEqual(output.getvalue(), '')

    def test_json_output_and_terminal_control_sanitization(self):
        self.begin()
        _, output = self.cli('--json')
        self.assertEqual(json.loads(output)['revision'], 'a' * 40)
        self.assertEqual(MODULE.clean('error\n\x1b[31m'), 'error  [31m')

    def test_failed_service_hook_covers_failures_outside_bootstrap(self):
        self.begin()
        self.cli('--service-result', 'signal')
        self.assertEqual(self.monitor.read()['state'], 'failed')
        self.assertIn('Systemd reported signal', self.monitor.read()['last_error'])

    def test_verify_playbook_does_not_trust_exit_zero_alone(self):
        self.begin()
        with self.assertRaises(RuntimeError):
            self.cli('--verify-playbook')
        self.monitor.update(playbook_result='completed')
        self.assertEqual(self.cli('--verify-playbook')[0], 0)

    def test_disk_logs_are_private(self):
        self.begin()
        self.monitor.append_log('Persist this output')
        self.assertIn('Persist this output', self.monitor.log_path.read_text())
        if os.name == 'posix':
            self.assertEqual(self.monitor.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.monitor.log_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.monitor.directory.stat().st_mode & 0o777, 0o700)

    def test_follow_discovers_upstream_log_created_after_attach(self):
        self.monitor.append_log('bootstrap output')
        upstream = self.base / 'new-upstream.log'
        output = io.StringIO()
        calls = 0

        def tick(seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                upstream.write_text('upstream installation progress\n')
            else:
                raise KeyboardInterrupt

        with mock.patch.object(self.monitor, 'upstream_log', side_effect=[None, upstream]), \
                mock.patch.object(MODULE.time, 'sleep', side_effect=tick), redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                self.monitor.follow()
        self.assertIn('bootstrap output', output.getvalue())
        self.assertIn('upstream installation progress', output.getvalue())
        self.assertEqual(self.monitor.read()['state'], 'not_started')

    def test_follow_handles_copytruncate_rotation(self):
        self.monitor.append_log('old output ' * 100)
        output = io.StringIO()
        calls = 0

        def tick(seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.monitor.log_path.write_text('new output after rotation\n')
            else:
                raise KeyboardInterrupt

        with mock.patch.object(self.monitor, 'upstream_log', return_value=None), \
                mock.patch.object(MODULE.time, 'sleep', side_effect=tick), redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                self.monitor.follow()
        self.assertIn('new output after rotation', output.getvalue())


@unittest.skipUnless(shutil.which('ansible-playbook'), 'requires Ansible on the Linux CI runner')
class AnsibleCallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.monitor = MODULE.Monitor(self.base / 'state', self.base / 'log')
        self.env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / 'ansible.cfg'),
                        FREEPBX_MONITOR_STATE_DIR=str(self.monitor.directory),
                        FREEPBX_MONITOR_LOG_DIR=str(self.monitor.log_dir))
        self.env.pop('FREEPBX_MONITOR_CAPTURED', None)
        self.env.pop('FREEPBX_MONITOR_ACTIVE', None)

    def run_play(self, extra=None, check=False):
        command = ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(ROOT / 'tests/monitor-playbook.yml')]
        if extra:
            command += ['--extra-vars', json.dumps(extra)]
        if check:
            command += ['--check']
        return subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=45)

    def test_real_success_logs_tasks_and_hides_private_results(self):
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.monitor.read()
        self.assertEqual(data['state'], 'completed')
        self.assertEqual(data['playbook_result'], 'completed')
        log = self.monitor.log_path.read_text()
        self.assertIn('Finish monitored smoke test', log)
        self.assertNotIn('MONITOR_TEST_SECRET_MUST_NOT_APPEAR', log)
        self.assertIn('Output hidden by no_log', log)

    def test_real_failure_records_task_and_error(self):
        result = self.run_play({'fail_monitor_smoke': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.monitor.read()['state'], 'failed')
        self.assertIn('Exercise monitored failure', self.monitor.read()['task'])
        self.assertIn('deliberate monitor smoke failure', self.monitor.read()['last_error'])

    def test_private_failure_does_not_leak_last_error(self):
        result = self.run_play({'fail_monitor_smoke': True, 'hide_monitor_failure': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.monitor.read()['task'], '[no_log task]')
        self.assertNotIn('deliberate monitor smoke failure', self.monitor.read()['last_error'])
        self.assertNotIn('deliberate monitor smoke failure', self.monitor.log_path.read_text())

    def test_dry_run_does_not_replace_real_install_status(self):
        self.monitor.update(state='failed', last_error='existing failure')
        before = self.monitor.path.read_text()
        result = self.run_play(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.monitor.path.read_text(), before)


if __name__ == '__main__':
    unittest.main()
