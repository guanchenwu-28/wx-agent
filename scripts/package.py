#!/usr/bin/env python3
"""Build source and skill ZIPs from tracked workspace files, without nested bundles."""
import hashlib
from pathlib import Path
import re
import stat
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def archive(destination, files, prefix, relative_to):
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for source in sorted(files):
            entry = zipfile.ZipInfo(prefix + source.relative_to(relative_to).as_posix(), (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | (source.stat().st_mode & 0o777)) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, source.read_bytes())


def main():
    version = (ROOT / 'VERSION').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('VERSION must contain a version such as 0.1.1')
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    files = [ROOT / name for name in tracked if name
             and Path(name).parts[0] not in ('packages', 'dist')]
    missing = [str(p.relative_to(ROOT)) for p in files if not p.is_file()]
    if missing:
        raise ValueError('Tracked source files are missing: ' + ', '.join(missing))
    output = ROOT / 'packages'
    output.mkdir(exist_ok=True)
    full = output / f'wx-agent-v{version}-macos-arm64-source.zip'
    skill = output / f'wechat-native-v{version}-skill.zip'
    skill_root = ROOT / 'skills/wechat-native'
    archive(full, files, 'wx-agent/', ROOT)
    archive(skill, [p for p in files if skill_root in p.parents], 'wechat-native/', skill_root)
    sums = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name for p in (full, skill)]
    (output / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n')
    for package in (full, skill):
        print(f'{package.relative_to(ROOT)} ({package.stat().st_size} bytes)')


if __name__ == '__main__':
    main()
