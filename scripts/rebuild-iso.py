"""Rebuild a separate ISO, preserving its preseed credentials/disk choices.

Requires xorriso and pycdlib on the build machine, not on the installed PBX.
Never boots the image, writes a disk device, or overwrites an existing ISO.
"""

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile


LATE_COMMAND = (
    "d-i preseed/late_command string mkdir -p /target/root/freepbx-setup && "
    "cp -a /cdrom/freepbx/. /target/root/freepbx-setup/ && "
    "in-target /bin/bash -c 'cd /root/freepbx-setup && "
    'bash scripts/setup-first-boot.sh "$(cat REVISION)"\''
)
NETWORK_PREFIXES = ('d-i netcfg/link_wait_timeout string ', 'd-i netcfg/dhcp_timeout string ')


def update_preseed(text):
    result = []
    late = packages = network = 0
    for line in text.splitlines(keepends=True):
        ending = '\r\n' if line.endswith('\r\n') else '\n'
        if line.startswith(NETWORK_PREFIXES):
            continue
        if line.startswith('d-i netcfg/choose_interface select '):
            result.append('d-i netcfg/link_wait_timeout string 30' + ending)
            result.append('d-i netcfg/dhcp_timeout string 120' + ending)
            result.append(line)
            network += 1
        elif line.startswith('d-i preseed/late_command string '):
            result.append(LATE_COMMAND + ending)
            late += 1
        elif line.startswith('d-i pkgsel/include string '):
            selection = line.strip().split()[3:]
            for package in ['python3', 'git', 'curl', 'ca-certificates', 'openssh-server', 'ansible', 'logrotate', 'iproute2']:
                if package not in selection:
                    selection.append(package)
            result.append('d-i pkgsel/include string ' + ' '.join(selection) + ending)
            packages += 1
        else:
            result.append(line)
    if late != 1 or packages != 1 or network != 1:
        raise ValueError('Expected exactly one late command, package-selection and network-interface line')
    return ''.join(result)


def protected_lines(text):
    return [line for line in text.splitlines() if not line.startswith(
        ('d-i preseed/late_command string ', 'd-i pkgsel/include string ') + NETWORK_PREFIXES)]


def xorriso_path(path):
    path = Path(path).resolve().as_posix()
    # A standalone MSYS2 runtime uses /cygdrive drive mounts. Although -indev
    # also accepts C:/..., -map treats that spelling as a relative path.
    if os.name == 'nt':
        return '/cygdrive/' + path[0].lower() + path[2:]
    return path


def run(command):
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout + result.stderr


def digest_file(path, algorithm):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, algorithm).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-iso', type=Path, required=True)
    parser.add_argument('--source-tree', type=Path, required=True, help='Mounted original ISO')
    parser.add_argument('--output-iso', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True, help='New private staging directory')
    parser.add_argument('--xorriso', type=Path, required=True)
    args = parser.parse_args()
    import pycdlib

    if args.output_iso.exists() or args.input_iso.resolve() == args.output_iso.resolve():
        parser.error('Output must be a new ISO filename; the original is never overwritten')
    args.work_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    repository = Path(__file__).resolve().parents[1]
    revision = run(['git', '-C', str(repository), 'rev-parse', 'HEAD']).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise RuntimeError('Unable to resolve reviewed revision')
    original_hash = digest_file(args.input_iso, 'sha256')
    original_preseed = (args.source_tree / 'preseed.cfg').read_bytes().decode('utf-8')
    preseed = update_preseed(original_preseed)
    if protected_lines(original_preseed) != protected_lines(preseed):
        raise RuntimeError('Protected preseed settings changed')
    preseed_file = args.work_dir / 'preseed.cfg'
    preseed_file.write_bytes(preseed.encode('utf-8'))
    archive = args.work_dir / 'payload.tar'
    run(['git', '-C', str(repository), 'archive', '--format=tar', '--output=' + str(archive.resolve()), revision])
    payload = args.work_dir / 'freepbx'
    payload.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(payload, filter='data')
    (payload / 'REVISION').write_bytes((revision + '\n').encode())
    if not (payload / 'scripts/setup-first-boot.sh').exists():
        raise RuntimeError('Commit does not contain the first-boot setup script')

    # Regenerate existing manifest entries from the actual mounted source, since
    # custom ISO boot-menu edits may already have invalidated the old manifest.
    entries = {}
    for row in (args.source_tree / 'md5sum.txt').read_text().splitlines():
        name = row.split(None, 1)[1].lstrip('*').removeprefix('./')
        if name == 'md5sum.txt':
            continue
        source = preseed_file if name == 'preseed.cfg' else args.source_tree.joinpath(*name.split('/'))
        entries[name] = digest_file(source, 'md5')
    entries['preseed.cfg'] = digest_file(preseed_file, 'md5')
    for path in payload.rglob('*'):
        if path.is_file():
            entries['freepbx/' + path.relative_to(payload).as_posix()] = digest_file(path, 'md5')
    manifest = args.work_dir / 'md5sum.txt'
    manifest.write_bytes(''.join(f'{entries[name]}  ./{name}\n' for name in sorted(entries)).encode())
    tool = str(args.xorriso.resolve())
    source_report = run([tool, '-indev', xorriso_path(args.input_iso), '-report_el_torito', 'plain', '-report_system_area', 'plain'])
    (args.work_dir / 'source-boot-report.txt').write_text(source_report)
    build_output = run([tool, '-abort_on', 'FAILURE', '-indev', xorriso_path(args.input_iso),
                        '-outdev', xorriso_path(args.output_iso), '-boot_image', 'any', 'replay',
                        '-uid', '0', '-gid', '0',
                        '-map', xorriso_path(preseed_file), '/preseed.cfg',
                        '-map', xorriso_path(payload), '/freepbx',
                        '-map', xorriso_path(manifest), '/md5sum.txt', '-commit', '-end'])
    (args.work_dir / 'build.log').write_text(build_output)
    output_report = run([tool, '-indev', xorriso_path(args.output_iso), '-report_el_torito', 'plain', '-report_system_area', 'plain'])
    (args.work_dir / 'output-boot-report.txt').write_text(output_report)
    platforms = lambda text: set(re.findall(r'El Torito boot img\s*:\s*\d+\s+(\S+)', text))
    if not platforms(source_report) or platforms(source_report) != platforms(output_report):
        raise RuntimeError('BIOS/UEFI boot platform metadata changed')

    iso = pycdlib.PyCdlib()
    iso.open(str(args.output_iso))
    try:
        for name, expected in entries.items():
            digest = hashlib.md5()
            with iso.open_file_from_iso(rr_path='/' + name) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError('ISO integrity check failed for ' + name)
        stream = io.BytesIO()
        iso.get_file_from_iso_fp(stream, rr_path='/preseed.cfg')
        if protected_lines(stream.getvalue().decode()) != protected_lines(original_preseed):
            raise RuntimeError('Output ISO changed credentials or partitioning settings')
    finally:
        iso.close()
    if digest_file(args.input_iso, 'sha256') != original_hash:
        raise RuntimeError('Original ISO changed during build')
    final_hash = digest_file(args.output_iso, 'sha256')
    args.output_iso.with_suffix(args.output_iso.suffix + '.sha256').write_text(final_hash + '  ' + args.output_iso.name + '\n')
    report = (f'Revision: {revision}\nOriginal SHA256: {original_hash}\nOutput SHA256: {final_hash}\n'
              f'Verified manifest entries: {len(entries)}\n'
              'Credentials and partitioning settings: preserved\n'
              'Network waits: link detection 30s; DHCP 120s; first-boot address/default route 120s\n'
              'BIOS/UEFI boot platform metadata: preserved\n'
              'No boot or full installation test performed. Test only in a disposable VM.\n')
    (args.work_dir / 'verification.txt').write_text(report)
    print(report)
    print('New ISO:', args.output_iso)


if __name__ == '__main__':
    main()
