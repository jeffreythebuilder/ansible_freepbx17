"""Run actual dialplan helpers in an isolated Asterisk (CI, not on a live PBX)."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    asterisk = shutil.which('asterisk') or '/usr/sbin/asterisk'
    with tempfile.TemporaryDirectory(prefix='freepbx-dialplan-test-') as directory:
        base = Path(directory)
        for name in ['run', 'log', 'spool', 'lib', 'cache', 'keys']:
            (base / name).mkdir()
        config = base / 'asterisk.conf'
        config.write_text(f'''[directories]
astetcdir => {base}
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => {base}/lib
astdbdir => {base}/lib
astkeydir => {base}/keys
astdatadir => /usr/share/asterisk
astagidir => {base}/lib
astspooldir => {base}/spool
astrundir => {base}/run
astlogdir => {base}/log
astcachedir => {base}/cache
[options]
verbose = 5
''')
        (base / 'modules.conf').write_text('''[modules]
autoload=no
load=pbx_config.so
load=app_stack.so
load=app_exec.so
load=app_verbose.so
load=func_strings.so
load=func_cut.so
load=func_logic.so
load=func_channel.so
load=func_callerid.so
load=res_clioriginate.so
''')
        (base / 'logger.conf').write_text('[logfiles]\nconsole => error,warning,notice,verbose\n')
        dialplan = (ROOT / 'files/from-pstn-sg.conf').read_text()
        cases = []

        def add_case(name, variable, value, context, expected):
            cases.append(name)
            value = value.replace(';', r'\;')
            return f'''exten => {name},1,Set({variable}={value})
 same => n,Gosub({context},s,1)
 same => n,GotoIf($["${{GOSUB_RETVAL}}"="{expected}"]?pass:fail)
 same => n(pass),Verbose(0,TEST_PASS_{name})
 same => n,Hangup()
 same => n(fail),Verbose(0,TEST_FAIL_{name}_got_${{GOSUB_RETVAL}})
 same => n,Hangup()
'''

        dialplan += '\n[test-cases]\n'
        headers = [
            ('bare', 'sip:+6565123456@provider.example', '+6565123456'),
            ('brackets', '<sip:6565123456@provider.example>', '6565123456'),
            ('display', '"Office: Main" <sip:+6565123456@provider.example>;tag=123', '+6565123456'),
            ('comma', '"Office, Main" <sip:65123456@provider.example>', '65123456'),
            ('sips', 'sips:+6565123456@provider.example:5061;user=phone', '+6565123456'),
            ('ipv6', '<sip:65123456@[2001:db8::1]:5060>', '65123456'),
            ('missing', '', ''),
            ('malformed', 'not-a-uri', ''),
            ('nonnumeric', 'sip:sales@provider.example', ''),
            ('nohost', 'sip:65123456', ''),
            ('emptyhost', 'sip:65123456@', ''),
        ]
        for name, header, expected in headers:
            dialplan += add_case('parse_' + name, 'SG_TO', header, 'sg-pstn-parse-to', expected)

        # ARG1 is used by the numeric normalizer, but pass it via a wrapper
        # so the caller-ID path also exercises comma-containing variable values.
        normalizations = [
            ('plus', '+6565123456', '65123456'),
            ('country', '6565123456', '65123456'),
            ('local', '65123456', '65123456'),
            ('short', '65123', '65123'),
            ('foreign', '+442012345678', '+442012345678'),
        ]
        for name, value, expected in normalizations:
            dialplan += add_case('norm_' + name, 'TEST_NUMBER', value, 'test-normalize', expected)
        for name, value, expected in [
            ('plus', '+6565123456', '65123456'),
            ('local', '65123456', '65123456'),
            ('country', '6565123456', '6565123456'),
            ('display', 'Office, Reception', 'Office, Reception'),
            ('prefix', '+65 Office', '+65 Office'),
            ('empty', '', ''),
        ]:
            dialplan += add_case('caller_' + name, 'SG_CALLER', value, 'sg-pstn-normalize-caller', expected)

        dialplan += '''
[test-normalize]
exten => s,1,Gosub(sg-pstn-normalize,s,1(${TEST_NUMBER}))
 same => n,Return(${GOSUB_RETVAL})
[from-pstn]
exten => 8888,1,Verbose(0,TEST_PASS_fallback)
 same => n,Hangup()
'''
        (base / 'extensions.conf').write_text(dialplan)
        output_path = base / 'console.log'
        with output_path.open('w') as output:
            process = subprocess.Popen([asterisk, '-f', '-n', '-C', str(config)],
                                       stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 20
                while not (base / 'run/asterisk.ctl').exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Asterisk did not start')
                    time.sleep(0.1)

                def cli(command):
                    return subprocess.run([asterisk, '-C', str(config), '-rx', command],
                                          text=True, capture_output=True, check=True, timeout=10)

                # Wait until the dialplan module is ready, not merely the socket.
                while "[ Context 'test-cases'" not in cli('dialplan show test-cases').stdout:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Dialplan did not load')
                    time.sleep(0.1)
                for name in cases:
                    cli(f'channel originate Local/{name}@test-cases application Wait 1')
                cli('channel originate Local/8888@from-pstn-sg application Wait 1')
                cases.append('fallback')
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    logs = output_path.read_text()
                    if 'TEST_FAIL_' in logs:
                        raise AssertionError('Dialplan returned an unexpected value')
                    if all('TEST_PASS_' + name in logs for name in cases):
                        print(f'Passed {len(cases)} actual Asterisk dialplan cases')
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError('Timed out waiting for dialplan results')
            except Exception:
                print(output_path.read_text())
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == '__main__':
    main()
