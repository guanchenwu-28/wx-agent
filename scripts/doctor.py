#!/usr/bin/env python3
"""Inspect prerequisites without attaching, reading chats or loading a service."""
import argparse
import ctypes
import ctypes.util
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def inspect(runtime):
    runtime = runtime.expanduser().resolve()
    sys.path.insert(0, str(runtime))
    import runtime_config as config
    checks = []
    def check(name, ok, detail, required=True):
        checks.append({'name': name, 'ok': bool(ok), 'required': required, 'detail': str(detail)})
    check('platform', sys.platform == 'darwin' and platform.machine() == 'arm64', platform.platform())
    check('python', sys.version_info >= (3, 11), sys.executable)
    check('configured_python', Path(config.PYTHON).is_file(), config.PYTHON)
    check('cryptography', importlib.util.find_spec('cryptography') is not None, 'Run with the configured Python')
    for name, executable in (('node', config.NODE), ('clang', config.CLANG), ('codex', config.CODEX)):
        found = shutil.which(str(executable))
        check(name, found is not None, executable)
    zstd_available = (importlib.util.find_spec('compression') is not None
                      and importlib.util.find_spec('compression.zstd') is not None)
    if not zstd_available:
        for library in (ctypes.util.find_library('zstd'), '/opt/homebrew/lib/libzstd.dylib', '/usr/local/lib/libzstd.dylib'):
            if not library:
                continue
            try:
                ctypes.CDLL(library)
                zstd_available = True
                break
            except OSError:
                pass
    check('zstd_decoder', zstd_available, 'Python compression.zstd (3.14+) or a local libzstd is required for compressed messages')
    if shutil.which(str(config.NODE)):
        result = subprocess.run([str(config.NODE), '--version'], text=True, capture_output=True, timeout=10)
        try:
            major = int(result.stdout.strip().lstrip('v').split('.')[0])
        except ValueError:
            major = 0
        check('node_version', result.returncode == 0 and major >= 22, result.stdout.strip())
    sdk = Path(config.setting('frida_module', str(runtime / 'node_modules/frida/build/src/frida.js'))).expanduser()
    check('frida_module', sdk.is_file(), sdk)
    # Import the SDK only: no attach or native operation occurs here.
    if sdk.is_file() and shutil.which(str(config.NODE)):
        program = "const m=await import(process.argv[1]); if(typeof m.attach!=='function')process.exit(2)"
        result = subprocess.run([str(config.NODE), '--input-type=module', '-e', program, sdk.resolve().as_uri()],
                                text=True, capture_output=True, timeout=20)
        check('frida_import', result.returncode == 0, 'SDK import only; no attach')
    core = config.TEST_APP / 'Contents/Resources/wechat.dylib'
    check('test_app', core.is_file() and (config.TEST_APP / 'Contents/MacOS/WeChat').is_file(), config.TEST_APP)
    check('profile', (runtime / 'profiles/wechat-4.1.13-arm64.json').is_file(), 'Only this profile is packaged')
    check('verified_keys', (config.CACHE / 'verified-dbkeys.json').is_file(),
          'Presence only; keys are neither read nor validated. Provision authorized local keys separately.')
    check('voice_encoder', importlib.util.find_spec('pilk') is not None, 'Optional: requirements-voice.txt', required=False)
    result = {'prerequisites_present': all(x['ok'] for x in checks if x['required']),
              'checks': checks, 'end_to_end_verified': False,
              'limits': ['No test WeChat copy is created by this package.',
                         'No database keys are acquired by this package.',
                         'SDK import and file presence do not verify app attachment or message delivery.',
                         'Codex is required for AI replies and must be authenticated separately.',
                         'No listener or launchd service is started by install or doctor.']}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parents[1] / 'runtime')
    result = inspect(parser.parse_args().runtime)
    raise SystemExit(0 if result['prerequisites_present'] else 1)
