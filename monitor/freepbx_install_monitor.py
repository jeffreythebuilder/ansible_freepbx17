"""Persistent installation status and CLI. No daemon or third-party dependencies."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # Allows pure status tests on the Windows development host.
    fcntl = None


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean(value, limit=1500):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value))[:limit]


def process_identity(pid):
    """Boot + process start ticks avoid mistaking a reused PID for our runner."""
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()
        return boot, fields[19]
    except (OSError, ValueError, TypeError, IndexError):
        return '', ''


class ConsoleMirror:
    """Bounded, nonblocking fan-out: a dead serial receiver cannot stall setup."""

    def __init__(self, warning, devices=('/dev/tty0', '/dev/ttyS0'), capacity=262144):
        self.warning = warning
        self.devices = devices
        self.capacity = capacity
        self.outputs = {}
        self.pending = {device: bytearray() for device in devices}
        self.warned = set()

    def warn_once(self, device, reason):
        if device not in self.warned:
            self.warned.add(device)
            self.warning(f'Console mirror {device}: {reason}; full output remains in the installation logs.')

    def write(self, text):
        data = text.encode('utf-8', errors='replace')
        for device in self.devices:
            queue = self.pending[device]
            queue.extend(data)
            if len(queue) > self.capacity:
                del queue[:-self.capacity]
                self.warn_once(device, 'display is too slow; older console output was dropped')
        self.flush()

    def flush(self):
        for device in self.devices:
            descriptor = self.outputs.get(device)
            if descriptor is None:
                try:
                    descriptor = os.open(device, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
                    if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
                        os.close(descriptor)
                        raise OSError('not a character device')
                    self.outputs[device] = descriptor
                except OSError as error:
                    self.pending[device].clear()
                    self.warn_once(device, clean(error))
                    continue
            queue = self.pending[device]
            while queue:
                try:
                    written = os.write(descriptor, queue)
                    if not written:
                        break
                    del queue[:written]
                except BlockingIOError:
                    break
                except OSError as error:
                    self.warn_once(device, clean(error))
                    os.close(descriptor)
                    self.outputs.pop(device, None)
                    queue.clear()
                    break

    def close(self):
        # Keep shutdown bounded even if serial flow control is holding a port.
        deadline = time.monotonic() + 1
        while any(self.pending.values()) and time.monotonic() < deadline:
            self.flush()
            time.sleep(0.05)
        for descriptor in self.outputs.values():
            os.close(descriptor)


class Monitor:
    def __init__(self, state_dir=None, log_dir=None):
        self.directory = Path(state_dir or os.environ.get('FREEPBX_MONITOR_STATE_DIR', '/var/lib/freepbx-install'))
        self.log_dir = Path(log_dir or os.environ.get('FREEPBX_MONITOR_LOG_DIR', '/var/log/freepbx-install'))
        self.path = self.directory / 'status.json'
        self.log_path = self.log_dir / 'install.log'

    def prepare(self):
        for directory in (self.directory, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    @contextmanager
    def locked(self):
        self.prepare()
        with (self.directory / 'status.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self):
        try:
            with self.path.open() as source:
                data = json.load(source)
            if not isinstance(data, dict):
                raise ValueError('status must be an object')
            return data
        except FileNotFoundError:
            return {'state': 'not_started', 'stage': 'No monitored installation has started'}
        except (ValueError, OSError) as error:
            return {'state': 'unknown', 'stage': 'Unable to read installation status', 'last_error': clean(error)}

    def _write(self, data):
        data['updated_at'] = timestamp()
        fd, temporary = tempfile.mkstemp(prefix='.status-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return data

    def begin(self, pid, source, revision='', log_offset=0):
        with self.locked():
            previous = self.read()
            if self.is_alive(previous):
                raise RuntimeError('Another monitored installation is already running')
            boot, ticks = process_identity(pid)
            return self._write(dict(state='running', stage='Starting installation', task='',
                                    started_at=timestamp(), finished_at='', pid=int(pid),
                                    boot_id=boot, start_ticks=ticks, source=source,
                                    revision=clean(revision), last_error='', exit_code=None,
                                    playbook_result='', log_offset=int(log_offset)))

    def update(self, **changes):
        with self.locked():
            data = self.read()
            data.update(changes)
            return self._write(data)

    def finish(self, state, message='', exit_code=0):
        with self.locked():
            data = self.read()
            # Retain the more useful Ansible task error when Bash reports only
            # that ansible-playbook exited nonzero.
            error = data.get('last_error', '') if state == 'failed' else ''
            data.update(state=state, finished_at=timestamp(), exit_code=exit_code,
                        last_error=error or clean(message))
            return self._write(data)

    @staticmethod
    def is_alive(data):
        identity = process_identity(data.get('pid', 0))
        return bool(identity[0] and identity == (data.get('boot_id'), data.get('start_ticks')))

    def status(self):
        data = self.read()
        if data.get('state') == 'running' and not self.is_alive(data):
            # Derive this rather than rewriting the evidence from a previous boot.
            data = dict(data, state='interrupted', last_error=data.get('last_error') or
                        'Installation runner is no longer active; inspect logs before retrying.')
        return data

    def append_log(self, text):
        self.prepare()
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.log_path.chmod(0o600)
        with os.fdopen(fd, 'a') as output:
            output.write(f'{timestamp()} {text}\n')

    def upstream_log(self, started_at=None):
        try:
            earliest = datetime.fromisoformat(started_at).timestamp() if started_at else 0
            candidates = (path for path in Path('/var/log/pbx').glob('freepbx17-install-*.log')
                          if path.stat().st_mtime >= earliest)
            return max(candidates,
                       key=lambda path: path.stat().st_mtime, default=None)
        except (OSError, ValueError):
            return None

    def follow(self, emit=None, stop=None, from_start=False, tick=None):
        """Discover the upstream log even when --follow starts before installation.

        File identity/size checks also handle copytruncate and renamed logs.
        This reader never signals or otherwise controls the installation.
        """
        readers = {}
        emit = emit or (lambda text: print(text, end='', flush=True))
        try:
            while True:
                upstream = self.upstream_log(self.read().get('started_at'))
                paths = [self.log_path] + ([upstream] if upstream else [])
                for path in paths:
                    try:
                        metadata = path.stat()
                        reader = readers.get(path)
                        if reader is None or os.fstat(reader.fileno()).st_ino != metadata.st_ino:
                            if reader:
                                reader.close()
                            reader = path.open(errors='replace')
                            readers[path] = reader
                            emit(f'\n==> {path} <==\n')
                            # Start near the end on attach; subsequent reads are live.
                            offset = self.read().get('log_offset', 0) if path == self.log_path else 0
                            reader.seek(min(metadata.st_size, offset) if from_start else max(0, metadata.st_size - 16000))
                        if reader.tell() > metadata.st_size:
                            reader.seek(0)
                        chunk = reader.read(65536)
                        if chunk:
                            emit(chunk)
                    except FileNotFoundError:
                        continue
                for path in list(readers):
                    if path not in paths:
                        readers.pop(path).close()
                if tick:
                    tick()
                if stop and stop():
                    # Drain the remaining finite snapshot before closing viewers.
                    for reader in readers.values():
                        emit(reader.read(1048576))
                    return
                time.sleep(0.1 if from_start else 0.5)
        finally:
            for reader in readers.values():
                reader.close()

    def mirror_consoles(self):
        owner = self.read()
        mirror = ConsoleMirror(self.append_log)
        try:
            self.follow(emit=mirror.write, from_start=True, tick=mirror.flush,
                        stop=lambda: (self.directory / 'console-stop').exists() or not self.is_alive(owner))
        finally:
            mirror.close()


def display(monitor, data, notice=False):
    state = clean(data.get('state', 'unknown')).upper()
    print(f'FreePBX installation: {state}')
    print(f"Stage: {clean(data.get('stage', 'unknown'))}")
    if data.get('task'):
        print(f"Task: {clean(data['task'])}")
    if data.get('last_error'):
        print(f"Last error: {clean(data['last_error'])}")
    if not notice:
        for key in ('started_at', 'updated_at', 'finished_at', 'revision'):
            if data.get(key):
                print(f"{key}: {clean(data[key])}")
        print(f'Combined log: {monitor.log_path}')
        upstream = monitor.upstream_log(data.get('started_at'))
        if upstream:
            print(f'Latest upstream log: {upstream}')
        print('Live output: freepbx-install-status --follow')
        print('Systemd: systemctl status freepbx-install.service')
    else:
        print('Run freepbx-install-status for details or --follow for live output.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect FreePBX installation from any root/sudo SSH session')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--json', action='store_true', help='Print machine-readable status')
    modes.add_argument('--follow', action='store_true', help='Follow combined and current upstream logs; Ctrl-C only stops viewing')
    modes.add_argument('--notice', action='store_true', help='Print a short login notice only for running/failed/interrupted installs')
    modes.add_argument('--begin', type=int, metavar='PID', help=argparse.SUPPRESS)
    modes.add_argument('--stage', metavar='STAGE', help=argparse.SUPPRESS)
    modes.add_argument('--finish', choices=['completed', 'failed'], help=argparse.SUPPRESS)
    modes.add_argument('--service-result', help=argparse.SUPPRESS)
    modes.add_argument('--verify-playbook', action='store_true', help=argparse.SUPPRESS)
    modes.add_argument('--mirror-consoles', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--source', default='bootstrap', help=argparse.SUPPRESS)
    parser.add_argument('--revision', default='', help=argparse.SUPPRESS)
    parser.add_argument('--message', default='', help=argparse.SUPPRESS)
    parser.add_argument('--exit-code', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--log-offset', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if getattr(os, 'geteuid', lambda: -1)() != 0:
        if args.notice:
            return 0
        parser.exit(1, 'Run as root or use sudo freepbx-install-status.\n')
    monitor = Monitor()
    if args.begin is not None:
        monitor.begin(args.begin, args.source, args.revision, args.log_offset)
    elif args.stage:
        monitor.update(stage=clean(args.stage), task='')
        monitor.append_log(f'STAGE: {clean(args.stage)}')
    elif args.finish:
        monitor.finish(args.finish, args.message, args.exit_code)
    elif args.service_result:
        if args.service_result != 'success':
            monitor.finish('failed', f'Systemd reported {clean(args.service_result)}; inspect journalctl -u freepbx-install.service.', 1)
    elif args.verify_playbook:
        if monitor.read().get('playbook_result') != 'completed':
            raise RuntimeError('Ansible did not report monitored completion; check callback/configuration and logs')
    elif args.mirror_consoles:
        monitor.mirror_consoles()
    else:
        data = monitor.status()
        if args.json:
            print(json.dumps(data, indent=2))
        elif args.notice:
            if data.get('state') in ('running', 'failed', 'interrupted', 'unknown'):
                display(monitor, data, notice=True)
        elif args.follow:
            monitor.prepare()
            monitor.log_path.touch(mode=0o600, exist_ok=True)
            display(monitor, data)
            print('Following logs (Ctrl-C stops viewing, not installation).', flush=True)
            monitor.follow()
        else:
            display(monitor, data)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (OSError, RuntimeError) as error:
        sys.exit(f'FreePBX installation monitor: {error}')
