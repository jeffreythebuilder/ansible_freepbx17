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

    def test_console_viewer_starts_at_current_attempt_not_old_log_history(self):
        self.monitor.prepare()
        self.monitor.log_path.write_text('OLD_ATTEMPT\nCURRENT_ATTEMPT\n')
        self.monitor.update(log_offset=len('OLD_ATTEMPT\n'))
        output = []
        with mock.patch.object(self.monitor, 'upstream_log', return_value=None):
            self.monitor.follow(emit=output.append, stop=lambda: True, from_start=True)
        self.assertIn('CURRENT_ATTEMPT', ''.join(output))
        self.assertNotIn('OLD_ATTEMPT', ''.join(output))

    def test_new_attempt_clears_previous_console_stop_marker(self):
        self.monitor.prepare()
        marker = self.monitor.directory / 'console-stop'
        marker.touch()
        self.begin()
        self.assertFalse(marker.exists())

    def test_ssh_login_streams_automatically_and_returns_when_finished(self):
        self.begin()

        def follow(**kwargs):
            self.assertTrue(kwargs['current_attempt'])
            self.assertFalse(kwargs['stop']())
            self.monitor.finish('completed')
            self.assertTrue(kwargs['stop']())

        with mock.patch.object(MODULE.Monitor, 'is_alive', return_value=True), \
                mock.patch.object(MODULE.Monitor, 'follow', side_effect=follow) as viewer:
            result, output = self.cli('--login')
        self.assertEqual(result, 0)
        viewer.assert_called_once()
        self.assertIn('Showing installation progress automatically', output)
        self.assertIn('COMPLETED', output)
        self.assertIn('Returning to the shell', output)

    def test_ssh_viewer_ctrl_c_does_not_change_installation_state(self):
        self.begin()
        before = self.monitor.path.read_text()
        with mock.patch.object(MODULE.Monitor, 'is_alive', return_value=True), \
                mock.patch.object(MODULE.Monitor, 'follow', side_effect=KeyboardInterrupt):
            result, output = self.cli('--login')
        self.assertEqual(result, 0)
        self.assertIn('installation has not been stopped', output)
        self.assertEqual(self.monitor.path.read_text(), before)

    def test_ssh_login_after_failure_shows_recent_output_without_waiting(self):
        self.begin()
        self.monitor.finish('failed', 'test failure')

        def follow(**kwargs):
            self.assertTrue(kwargs['stop']())

        with mock.patch.object(MODULE.Monitor, 'follow', side_effect=follow):
            _, output = self.cli('--login')
        self.assertIn('FAILED', output)
        self.assertIn('Recent installation output', output)

    def test_ssh_login_is_quiet_without_an_active_installation(self):
        self.assertEqual(self.cli('--login')[1], '')
        self.begin()
        self.monitor.finish('completed')
        self.assertEqual(self.cli('--login')[1], '')


@unittest.skipUnless(os.name == 'posix', 'requires Linux nonblocking TTY support')
class ConsoleMirrorTests(unittest.TestCase):
    def test_physical_progress_uses_tty1_and_requests_that_visible_vt(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = MODULE.Monitor(Path(directory) / 'state', Path(directory) / 'log')
            import stat
            with mock.patch.object(MODULE.os, 'open', return_value=17), \
                    mock.patch.object(MODULE.os, 'fstat', return_value=mock.Mock(st_mode=stat.S_IFCHR)), \
                    mock.patch.object(MODULE.os, 'close') as close, \
                    mock.patch.object(MODULE.fcntl, 'ioctl') as ioctl:
                monitor.activate_progress_console()
            ioctl.assert_called_once_with(17, 0x5606, 1)
            close.assert_called_once_with(17)
            self.assertEqual(MODULE.ConsoleMirror(lambda message: None).devices, ('/dev/tty1', '/dev/ttyS0'))

    def test_unavailable_physical_console_is_logged_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = MODULE.Monitor(Path(directory) / 'state', Path(directory) / 'log')
            with mock.patch.object(MODULE.os, 'open', side_effect=OSError('no virtual console')), \
                    mock.patch.object(monitor, 'append_log') as warning:
                monitor.activate_progress_console()
            self.assertIn('Could not select physical progress console tty1', warning.call_args.args[0])

    def test_real_bytes_reach_both_independent_ttys(self):
        import pty
        import select
        pairs = [pty.openpty(), pty.openpty()]
        for master, slave in pairs:
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
        warnings = []
        mirror = MODULE.ConsoleMirror(warnings.append, tuple(os.ttyname(slave) for _, slave in pairs))
        try:
            mirror.write('INSTALL_STAGE_VISIBLE_ON_BOTH\n')
            for master, _ in pairs:
                self.assertTrue(select.select([master], [], [], 1)[0])
                self.assertIn(b'INSTALL_STAGE_VISIBLE_ON_BOTH', os.read(master, 4096))
            self.assertEqual(warnings, [])
        finally:
            mirror.close()

    def test_blocked_port_does_not_block_other_display_and_memory_is_bounded(self):
        import stat
        warnings = []
        healthy = bytearray()

        def write(descriptor, data):
            if descriptor == 11:
                raise BlockingIOError
            healthy.extend(data)
            return len(data)

        mirror = MODULE.ConsoleMirror(warnings.append, ('blocked', 'healthy'), capacity=64)
        with mock.patch.object(MODULE.os, 'open', side_effect=[11, 12]), \
                mock.patch.object(MODULE.os, 'fstat', return_value=mock.Mock(st_mode=stat.S_IFCHR)), \
                mock.patch.object(MODULE.os, 'write', side_effect=write), \
                mock.patch.object(MODULE.os, 'close'):
            mirror.write('x' * 40)
            mirror.write('y' * 40)
            self.assertEqual(len(mirror.pending['blocked']), 64)
            self.assertEqual(healthy, b'x' * 40 + b'y' * 40)
            self.assertEqual(len(warnings), 1)
            with mock.patch.object(MODULE.time, 'monotonic', side_effect=[0, 2]):
                mirror.close()

    def test_missing_or_non_tty_destination_is_best_effort_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'not-a-device'
            path.write_text('must remain untouched')
            warnings = []
            mirror = MODULE.ConsoleMirror(warnings.append, (str(path), str(path.parent / 'missing')))
            mirror.write('installer output')
            mirror.write('more output')
            mirror.close()
            self.assertEqual(path.read_text(), 'must remain untouched')
            self.assertEqual(len(warnings), 2)


@unittest.skipUnless(os.name == 'posix', 'requires Linux Bash login-shell semantics')
class LoginHookTests(unittest.TestCase):
    def run_hook(self, interactive=True, ssh=True, root=True, viewer_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            helper = base / 'status'
            helper.write_text('#!/bin/sh\nprintf "AUTO:%s\\n" "$*"\nexit "${VIEW_EXIT:-0}"\n')
            helper.chmod(0o755)
            profile = base / 'profile.sh'
            profile.write_text((ROOT / 'monitor/freepbx-install-notice.sh').read_text().replace(
                '/usr/local/sbin/freepbx-install-status', str(helper)))
            env = dict(os.environ, PROFILE=str(profile), VIEW_EXIT=str(viewer_exit))
            env.pop('SSH_CONNECTION', None)
            env.pop('SSH_TTY', None)
            if ssh:
                env['SSH_CONNECTION'] = 'test connection'
            command = f"id() {{ printf '{0 if root else 1000}\\n'; }}; . \"$PROFILE\"; printf 'SHELL_READY\\n'"
            return subprocess.run(['bash', '--noprofile', '--norc', '-ic' if interactive else '-c', command],
                                  env=env, text=True, capture_output=True, timeout=10)

    def test_root_interactive_ssh_login_starts_progress_without_user_command(self):
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('AUTO:--login', result.stdout)
        self.assertIn('SHELL_READY', result.stdout)

    def test_exiting_viewer_does_not_abort_login_shell(self):
        result = self.run_hook(viewer_exit=130)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SHELL_READY', result.stdout)

    def test_noninteractive_nonssh_and_nonroot_sessions_are_not_attached(self):
        for arguments in [dict(interactive=False), dict(ssh=False), dict(root=False)]:
            with self.subTest(arguments=arguments):
                result = self.run_hook(**arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('AUTO:', result.stdout)


@unittest.skipUnless(importlib.util.find_spec('ansible'), 'requires Ansible callback framework')
class CallbackViewerTests(unittest.TestCase):
    def test_direct_playbook_starts_and_stops_console_viewer_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(FREEPBX_MONITOR_STATE_DIR=str(Path(directory) / 'state'),
                       FREEPBX_MONITOR_LOG_DIR=str(Path(directory) / 'log'))
            with mock.patch.dict(os.environ, env):
                os.environ.pop('FREEPBX_MONITOR_CAPTURED', None)
                spec = importlib.util.spec_from_file_location('callback_viewer_test', ROOT / 'callback_plugins/freepbx_monitor.py')
                plugin = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(plugin)
                callback = plugin.CallbackModule()
                child = mock.Mock()
                child.wait.return_value = 0
                with mock.patch.object(plugin.context, 'CLIARGS', {'check': False}), \
                        mock.patch.object(plugin.os, 'geteuid', return_value=0), \
                        mock.patch.object(plugin.subprocess, 'Popen', return_value=child) as launch:
                    callback.v2_playbook_on_start(None)
                    self.assertIn('--mirror-consoles', launch.call_args.args[0])
                    self.assertTrue(launch.call_args.kwargs['close_fds'])
                    callback.v2_playbook_on_stats(mock.Mock(processed={}))
                child.wait.assert_called_once_with(timeout=3)
                self.assertTrue((callback.monitor.directory / 'console-stop').exists())
                self.assertEqual(callback.monitor.read()['state'], 'completed')

    def test_bootstrap_does_not_get_a_duplicate_console_viewer(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(FREEPBX_MONITOR_STATE_DIR=str(Path(directory) / 'state'),
                       FREEPBX_MONITOR_LOG_DIR=str(Path(directory) / 'log'), FREEPBX_MONITOR_CAPTURED='1')
            with mock.patch.dict(os.environ, env):
                spec = importlib.util.spec_from_file_location('callback_bootstrap_test', ROOT / 'callback_plugins/freepbx_monitor.py')
                plugin = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(plugin)
                callback = plugin.CallbackModule()
                callback.monitor.begin(os.getpid(), 'bootstrap')
                with mock.patch.object(plugin.context, 'CLIARGS', {'check': False}), \
                        mock.patch.object(plugin.subprocess, 'Popen') as launch:
                    callback.v2_playbook_on_start(None)
                    callback.v2_playbook_on_stats(mock.Mock(processed={}))
                launch.assert_not_called()
                self.assertEqual(callback.monitor.read()['state'], 'running')


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
