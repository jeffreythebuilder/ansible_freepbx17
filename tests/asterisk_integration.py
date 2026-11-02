"""Exercise the supplied dialplan in an isolated Asterisk (CI, never a live PBX)."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    asterisk = shutil.which('asterisk') or '/usr/sbin/asterisk'
    package_files = subprocess.check_output(['dpkg', '-L', 'asterisk-modules'], text=True).splitlines()
    module_dir = next(Path(path).parent for path in package_files if path.endswith('/pbx_config.so'))
    with tempfile.TemporaryDirectory(prefix='freepbx-dialplan-test-') as directory:
        base = Path(directory)
        for name in ['run', 'log', 'spool', 'lib', 'cache', 'keys']:
            (base / name).mkdir()
        config = base / 'asterisk.conf'
        config.write_text(f'''[directories]
astetcdir => {base}
astmoddir => {module_dir}
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
load=res_pjproject.so
load=res_sorcery_config.so
load=res_sorcery_memory.so
load=res_sorcery_astdb.so
load=res_pjsip.so
load=res_pjsip_pubsub.so
load=res_pjsip_session.so
load=chan_pjsip.so
load=pbx_config.so
load=app_exec.so
load=app_verbose.so
load=func_strings.so
load=func_cut.so
load=func_logic.so
load=func_channel.so
load=func_callerid.so
load=res_clioriginate.so
''')
        # No transports/endpoints: URI parsing works without any SIP listener.
        (base / 'pjsip.conf').write_text('[global]\ntype=global\n')
        (base / 'logger.conf').write_text('[logfiles]\nconsole => error,warning,notice,verbose\n')
        dialplan = (ROOT / 'files/from-pstn-sg.conf').read_text() + '\n[test-cases]\n'
        cases = []

        def add_case(name, header='', original='8888', expected='8888', caller='91234567',
                     caller_name='Office, Reception', expected_caller=None, expected_name=None,
                     entry='parse'):
            cases.append(name)
            header = header.replace(';', r'\;')
            expected_caller = caller if expected_caller is None else expected_caller
            expected_name = caller_name if expected_name is None else expected_name
            # Inject a To header at the production parse label on a Local channel.
            # This exercises the exact supplied parser/normalizer, not a copy.
            return f'''exten => {name},1,Set(TEST_ID={name})
 same => n,Set(EXPECT_DID={expected})
 same => n,Set(EXPECT_CALLER={expected_caller})
 same => n,Set(EXPECT_NAME={expected_name})
 same => n,Set(CALLERID(num)={caller})
 same => n,Set(CALLERID(name)={caller_name})
 same => n,Set(SG_DID={original})
 same => n,Set(SG_TO={header})
 same => n,Goto(from-pstn-sg,{original},{entry})
'''

        for name, header, expected in [
            ('bare', 'sip:+6565123456@provider.example', '65123456'),
            ('brackets', '<sip:6565123456@provider.example>', '65123456'),
            ('display', '"Office: Main" <sip:+6565123456@provider.example>;tag=123', '65123456'),
            ('comma', '"Office, Main" <sip:65123456@provider.example>', '65123456'),
            ('sips', 'sips:+6565123456@provider.example:5061;user=phone', '65123456'),
            ('ipv6', '<sip:65123456@[2001:db8::1]:5060>', '65123456'),
            ('missing', '', '8888'),
            ('malformed', 'not-a-uri', '8888'),
            ('nonnumeric', 'sip:sales@provider.example', '8888'),
        ]:
            dialplan += add_case('parse_' + name, header=header, expected=expected)
        for name, value, expected in [
            ('plus', '+6565123456', '65123456'),
            ('country', '6565123456', '65123456'),
            ('local', '65123456', '65123456'),
            ('short', '65123', '65123'),
            ('foreign', '+442012345678', '+442012345678'),
        ]:
            dialplan += add_case('fallback_' + name, original=value, expected=expected)
            dialplan += add_case('caller_' + name, caller=value, caller_name=value,
                                 expected_caller=expected, expected_name=expected)
        dialplan += add_case('caller_display', caller_name='+65 Office')
        dialplan += add_case('unsupported_channel', entry='1')

        dialplan += '''
[from-pstn]
exten => _.,1,GotoIf($["${EXTEN}"="${EXPECT_DID}"]?caller:fail)
 same => n(caller),GotoIf($["${CALLERID(num)}"="${EXPECT_CALLER}"]?name:fail)
 same => n(name),GotoIf($["${CALLERID(name)}"="${EXPECT_NAME}"]?pass:fail)
 same => n(pass),Verbose(0,TEST_PASS_${TEST_ID})
 same => n,Hangup()
 same => n(fail),Verbose(0,TEST_FAIL_${TEST_ID}_DID_${EXTEN}_CALLER_${CALLERID(num)}_NAME_${CALLERID(name)})
 same => n,Hangup()
exten => h,1,NoOp()
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
                                          text=True, capture_output=True, check=True, timeout=20)

                cli('core waitfullybooted')
                if 'PJSIP_PARSE_URI(' not in cli('core show function PJSIP_PARSE_URI').stdout:
                    raise RuntimeError('Required production PJSIP_PARSE_URI function is unavailable')
                if "[ Context 'test-cases'" not in cli('dialplan show test-cases').stdout:
                    raise RuntimeError('Test dialplan did not load')
                for name in cases:
                    cli(f'channel originate Local/{name}@test-cases application Wait 1')
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    logs = output_path.read_text()
                    if 'TEST_FAIL_' in logs:
                        raise AssertionError('Dialplan returned an unexpected DID/caller value')
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
