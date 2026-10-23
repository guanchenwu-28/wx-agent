#!/usr/bin/env python3
"""Install/start/stop the single user-owned event service with launchd."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

from autoreply import HOME, ROOT, Registry

from runtime_config import (SERVICE_LABEL as LABEL, INSTALLED_RUNTIME as RUNTIME,
                            SOURCE_RUNTIME, LOGS, PYTHON, environment)
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
DOMAIN = f'gui/{os.getuid()}'
SERVICE = DOMAIN + '/' + LABEL



def run(*args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True)


def service_status():
    result = run('print', SERVICE)
    fields = {'loaded': result.returncode == 0, 'label': LABEL, 'plist': str(PLIST)}
    disabled = run('print-disabled', DOMAIN)
    fields['disabled'] = None
    if disabled.returncode == 0:
        # An absent override means enabled. Require the expected outer block
        # so an unfamiliar/partial response is unknown, never guessed enabled.
        rows = disabled.stdout.strip().splitlines()
        if rows and rows[0].strip().startswith('disabled services = {') and rows[-1].strip() == '}':
            fields['disabled'] = False
            # macOS versions print either booleans or the words
            # disabled/enabled for the same persistent override.
            pattern = re.compile(r'^"' + re.escape(LABEL) + r'"\s*=>\s*(true|false|disabled|enabled)\s*$')
            label_pattern = re.compile(r'^"' + re.escape(LABEL) + r'"\s*=>')
            for row in rows[1:-1]:
                match = pattern.fullmatch(row.strip())
                if match:
                    fields['disabled'] = match[1] in ('true', 'disabled')
                elif label_pattern.match(row.strip()):
                    # An unrecognized value for this exact label is unknown,
                    # not equivalent to an absent override/default enabled.
                    fields['disabled'] = None
    if result.returncode == 0:
        depth = 0
        for row in result.stdout.splitlines():
            stripped = row.strip()
            # launchctl prints the job inside one outer block. Coalitions,
            # arguments and other nested blocks can repeat these field names;
            # they describe different objects, not the job's running state.
            if stripped.endswith(' = {'):
                depth += 1
                continue
            if stripped == '}':
                depth = max(0, depth - 1)
                continue
            if depth != 1:
                continue
            for name in ('state', 'pid', 'runs', 'last exit code'):
                if stripped.startswith(name + ' = '):
                    fields[name] = stripped.split(' = ', 1)[1]
    return fields


def start():
    Registry().load()  # Never install a service without an explicit registry.
    if service_status()['loaded']:
        result = run('enable', SERVICE)
        if result.returncode:
            raise RuntimeError('launchd enable failed: ' + result.stderr.strip())
        return service_status()
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    # launchd must not depend on a mounted work volume for its cwd or executable
    # sources. Deploy the small runtime, not fixtures/tests or research data.
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    LOGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOGS.chmod(0o700)
    for source in ROOT.iterdir():
        if source.is_file() and source.suffix in ('.py', '.mjs', '.js', '.cpp') and not source.name.startswith('test_'):
            destination = RUNTIME / source.name
            if destination.exists() and source.samefile(destination):
                continue
            shutil.copy2(source, destination)
    profiles = ROOT / 'profiles'
    if profiles.is_dir():
        destination_dir = RUNTIME / 'profiles'
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for source in profiles.glob('*.json'):
            if not source.is_file() or source.is_symlink():
                continue
            destination = destination_dir / source.name
            if destination.exists() and source.samefile(destination):
                continue
            shutil.copy2(source, destination)
    config_source = ROOT / 'runtime-local.json'
    config_destination = RUNTIME / 'runtime-local.json'
    if config_source.is_file() and not (config_destination.exists() and config_source.samefile(config_destination)):
        shutil.copy2(config_source, config_destination)
        config_destination.chmod(0o600)
    for name in ('daemon.stdout.log', 'daemon.stderr.log'):
        path = LOGS / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        path.chmod(0o600)
    value = {
        'Label': LABEL,
        'ProgramArguments': [str(PYTHON), str(RUNTIME / 'autoreply.py'), 'serve'],
        'WorkingDirectory': str(RUNTIME),
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
        'ProcessType': 'Background',
        'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'), **environment()},
        'StandardOutPath': str(LOGS / 'daemon.stdout.log'),
        'StandardErrorPath': str(LOGS / 'daemon.stderr.log'),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.exists():
        old = plistlib.loads(PLIST.read_bytes())
        if old.get('Label') != LABEL or old.get('ProgramArguments', [None, None])[1] not in (str(ROOT / 'autoreply.py'), str(RUNTIME / 'autoreply.py')):
            raise RuntimeError('Existing service configuration belongs to another installation')
    temporary = PLIST.with_suffix('.plist.tmp')
    with temporary.open('wb') as out:
        plistlib.dump(value, out)
    temporary.chmod(0o600)
    os.replace(temporary, PLIST)
    result = run('enable', SERVICE)
    if result.returncode:
        raise RuntimeError('launchd enable failed: ' + result.stderr.strip())
    result = run('bootstrap', DOMAIN, str(PLIST))
    if result.returncode:
        raise RuntimeError('launchd bootstrap failed: ' + result.stderr.strip())
    return service_status()


def unload():
    """Persist pause across login/reboot; caller must close send gates first."""
    # Even if disable fails or times out, still stop the current process. The
    # readback below distinguishes stopped-now from persistently disabled.
    for command in ('disable', 'bootout'):
        try:
            run(command, SERVICE)
        except subprocess.TimeoutExpired:
            pass
    status = service_status()
    # This is an on-demand native connection cache, independent of the reply
    # worker. Closing it never launches it or changes any conversation state.
    connections = close_native_connections()
    if status['loaded']:
        raise RuntimeError('launchd stop failed: service is still loaded')
    if status.get('disabled') is not True:
        raise RuntimeError('Service stopped, but persistent launchd pause was not verified')
    if not all(item.get('ok') for item in connections):
        raise RuntimeError('Automatic replies are paused; native connection shutdown is not yet confirmed')
    status['native_connections_closed'] = True
    return status


def close_native_connections():
    from native_client import stop
    paths = dict.fromkeys((ROOT.resolve(), SOURCE_RUNTIME.resolve(), RUNTIME.resolve()))
    return [stop(runtime=path, timeout=5) for path in paths]


def stop():
    Registry().set_enabled(None, False)  # Gate sends before terminating worker.
    return unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'stop', 'status'))
    args = parser.parse_args()
    result = {'start': start, 'stop': stop, 'status': service_status}[args.command]()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
