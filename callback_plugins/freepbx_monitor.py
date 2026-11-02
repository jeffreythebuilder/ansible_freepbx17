"""Track the actual Ansible task, including preflight and ignored/no_log failures."""

import importlib.util
import json
import os
from pathlib import Path

from ansible import context
from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'freepbx_monitor'
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        source = Path(__file__).resolve().parents[1] / 'monitor/freepbx_install_monitor.py'
        spec = importlib.util.spec_from_file_location('freepbx_install_monitor', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.monitor = module.Monitor()
        self.clean = module.clean
        self.managed = os.environ.get('FREEPBX_MONITOR_CAPTURED') == '1'
        self.active = False
        self.failed = False

    def _log(self, message):
        if not self.managed:
            self.monitor.append_log(message)

    def v2_playbook_on_start(self, playbook):
        if context.CLIARGS.get('check'):
            return
        if self.managed:
            self.active = True
        else:
            self.monitor.begin(os.getpid(), 'ansible-playbook')
            self.active = True
        self.monitor.update(stage='Running Ansible playbook', task='')
        self._log('PLAYBOOK START')
        os.environ['FREEPBX_MONITOR_ACTIVE'] = '1'

    def v2_playbook_on_task_start(self, task, is_conditional):
        if self.active:
            name = '[no_log task]' if task.no_log else self.clean(task.get_name())
            self.monitor.update(task=name)
            self._log(f'TASK: {name}')

    def v2_playbook_on_handler_task_start(self, task):
        self.v2_playbook_on_task_start(task, False)

    def _result(self, result, failed=False, unreachable=False, ignored=False):
        if not self.active:
            return
        private = result._task.no_log or result._result.get('_ansible_no_log')
        data = {'message': 'Output hidden by no_log'} if private else result._result
        self._log(json.dumps(data, default=str))
        if (failed or unreachable) and not ignored:
            self.failed = True
            self.monitor.update(task='[no_log task]' if private else self.clean(result._task.get_name()))
            message = 'Output hidden by no_log; inspect the task configuration.' if private else self.clean(
                result._result.get('msg') or result._result.get('stderr') or 'Ansible task failed; see combined log.')
            self.monitor.finish('failed', message, result._result.get('rc', 1))

    def v2_runner_on_ok(self, result):
        self._result(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._result(result, failed=True, ignored=ignore_errors)

    def v2_runner_on_unreachable(self, result):
        self._result(result, unreachable=True)

    def v2_playbook_on_stats(self, stats):
        if not self.active:
            return
        failed = self.failed or any(stats.summarize(host).get('failures', 0) or
                                    stats.summarize(host).get('unreachable', 0) for host in stats.processed)
        self._log('PLAYBOOK FAILED' if failed else 'PLAYBOOK COMPLETED')
        if failed:
            self.monitor.update(playbook_result='failed')
            self.monitor.finish('failed', 'Ansible playbook failed; see combined log.', 1)
        else:
            self.monitor.update(playbook_result='completed')
            if not self.managed:
                self.monitor.update(stage='Ansible playbook completed')
                self.monitor.finish('completed')
