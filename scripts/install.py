#!/usr/bin/env python3
"""Install source and local configuration; never load launchd or start WeChat."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
FILES = (
    'adapter.py', 'adapter_matcher.py', 'adapter_workflow.py', 'autoreply.py',
    'contacts.py', 'encode_voice.py', 'event_source.py', 'media.py', 'model_reply.py',
    'native_client.py', 'poller.py', 'reader.py', 'reply_trigger.py', 'service_manager.py',
    'wechat.py', 'runtime.js', 'transport.mjs', 'transport_session.mjs',
    'runtime_config.py', 'runtime_config.mjs', 'native_message_bridge.cpp',
    'requirements.txt', 'requirements-voice.txt', 'package.json',
)


def executable(value):
    candidate = shutil.which(str(value))
    if not candidate:
        raise ValueError('Executable is unavailable: ' + str(value))
    return str(Path(candidate).absolute())


def copy(source, destination):
    if destination.exists() and source.samefile(destination):
        return
    shutil.copy2(source, destination)


def launcher(bin_dir, command, runtime, script, arguments=()):
    target = bin_dir / command
    target.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
        + 'from pathlib import Path\n'
        + 'runtime = Path(' + repr(str(runtime)) + ')\n'
        + 'config = json.loads((runtime / "runtime-local.json").read_text())\n'
        + 'python = os.environ.get("WECHAT_PYTHON", config["python"])\n'
        + 'os.execv(python, [python, ' + repr(str(script)) + ', *'
        + repr(list(arguments)) + ', *sys.argv[1:]])\n')
    target.chmod(0o700)


def install(args):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('The native runtime supports macOS arm64 only')
    base_python = executable(args.python or sys.executable)
    version = subprocess.run([base_python, '-c',
                              'import json, sys; print(json.dumps(list(sys.version_info[:2])))'],
                             capture_output=True, text=True, check=True)
    if tuple(json.loads(version.stdout)) < (3, 11):
        raise ValueError('Python >=3.11 is required; select a newer interpreter with --python')
    prefix = args.prefix.expanduser().resolve()
    source_skill = SOURCE / 'skills/wechat-native'
    destination_skill = args.skill_root.expanduser().resolve() / 'wechat-native'
    if args.install_skill:
        if not source_skill.is_dir():
            raise ValueError('Packaged skills/wechat-native is unavailable')
        if destination_skill.exists() and not args.replace_skill:
            raise ValueError('Skill already exists; use --replace-skill to explicitly replace it: ' + str(destination_skill))
    runtime = prefix / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    config_file = runtime / 'runtime-local.json'
    config = json.loads(config_file.read_text()) if config_file.exists() else {}
    if not isinstance(config, dict):
        raise ValueError('Existing runtime-local.json must be an object')
    if args.no_deps:
        python = base_python
    else:
        venv = prefix / 'venv'
        subprocess.run([base_python, '-m', 'venv', str(venv)], check=True)
        python = str(venv / 'bin/python3')
        requirement = SOURCE / 'runtime' / ('requirements-voice.txt' if args.voice else 'requirements.txt')
        subprocess.run([python, '-m', 'pip', 'install', '-r', str(requirement)], check=True)
    defaults = {
        'node': shutil.which('node') or 'node', 'codex': shutil.which('codex') or 'codex',
        'clang': shutil.which('clang++') or 'clang++',
        'frida_module': str(prefix / 'sdk/node_modules/frida/build/src/frida.js'),
        'test_app': str(Path.home() / 'Applications/WeChat REA Test.app'),
        'original_app': '/Applications/微信.app',
        'data_root': str(Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data'),
        'compatibility_cache': str(prefix / 'compatibility'),
        'logs': str(Path.home() / 'Library/Logs/WeChatLocalTool'),
        'service_label': 'com.wxagent.wechat-autoreply',
    }
    config = {**defaults, **config, 'python': python, 'installed_runtime': str(runtime),
              'source_runtime': str(SOURCE / 'runtime')}
    for name in defaults:
        value = getattr(args, name, None)
        if value is not None:
            config[name] = value
    for name in ('node', 'clang'):
        config[name] = executable(config[name])
    if args.codex is not None:
        config['codex'] = executable(args.codex)
    for name in ('test_app', 'original_app', 'data_root', 'frida_module', 'compatibility_cache', 'logs'):
        config[name] = str(Path(config[name]).expanduser().resolve())
    if args.cache is not None:
        config['cache'] = str(Path(args.cache).expanduser().resolve())
    config.setdefault('cache', str(Path(config['data_root']) / 'Library/Caches/REAResearch'))
    # Install a private SDK under the stable prefix. A supplied module path is
    # owned by the caller and is never copied or replaced here.
    default_sdk = prefix / 'sdk/node_modules/frida/build/src/frida.js'
    if (not args.no_deps and Path(config['frida_module']) == default_sdk
            and not default_sdk.is_file()):
        sdk = prefix / 'sdk'
        sdk.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy(SOURCE / 'runtime/package.json', sdk / 'package.json')
        npm_cli = str(Path(executable(args.npm or 'npm')).resolve())
        subprocess.run([config['node'], npm_cli, 'install', '--prefix', str(sdk),
                        '--omit=dev', '--no-audit', '--no-fund'], check=True)
    for name in FILES:
        copy(SOURCE / 'runtime' / name, runtime / name)
    profiles = runtime / 'profiles'
    profiles.mkdir(exist_ok=True, mode=0o700)
    copy(SOURCE / 'runtime/profiles/wechat-4.1.13-arm64.json', profiles / 'wechat-4.1.13-arm64.json')
    temporary = config_file.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(config_file)
    # The source CLI shares configuration too; writing it is opt-in because
    # checkout location and installed runtime are independent execution homes.
    bin_dir = prefix / 'bin'
    bin_dir.mkdir(exist_ok=True, mode=0o700)
    for command, module in (('wechat', 'wechat.py'), ('wechat-autoreply', 'autoreply.py'),
                            ('wechat-service', 'service_manager.py')):
        launcher(bin_dir, command, runtime, runtime / module)
    installed_skill = None
    if args.install_skill:
        destination = destination_skill
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_skill, destination,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        installed_skill = str(destination)
    tools_dir = prefix / 'tools'
    tools_dir.mkdir(exist_ok=True, mode=0o700)
    for script, command in (('doctor.py', 'wx-agent-doctor'), ('build_bridge.py', 'wx-agent-build-bridge')):
        copy(SOURCE / 'scripts' / script, tools_dir / script)
        launcher(bin_dir, command, runtime, tools_dir / script, ('--runtime', str(runtime)))
    control_source = source_skill / 'scripts/control.py'
    if control_source.is_file():
        control = tools_dir / 'control.py'
        copy(control_source, control)
        launcher(bin_dir, 'wx-agent', runtime, control, ('--runtime', str(runtime)))
    if args.build_bridge:
        subprocess.run([python, str(SOURCE / 'scripts/build_bridge.py'), '--runtime', str(runtime)], check=True)
    result = {'installed_runtime': str(runtime), 'python': python, 'bin': str(bin_dir),
              'skill': installed_skill, 'listener_started': False, 'launchd_installed': False,
              'wechat_started': False,
              'next_check': [str(bin_dir / 'wx-agent-doctor')]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / 'Library/Application Support/WeChatLocalTool')
    parser.add_argument('--python', help='Base interpreter for the installation venv; Python >=3.11')
    parser.add_argument('--npm', help='npm CLI path; launched by the selected --node to install the private Frida SDK')
    for option in ('node', 'codex', 'clang', 'frida-module', 'test-app', 'original-app',
                   'data-root', 'cache', 'compatibility-cache', 'logs', 'service-label'):
        parser.add_argument('--' + option)
    parser.add_argument('--voice', action='store_true', help='Also install optional pilk audio encoder')
    parser.add_argument('--no-deps', action='store_true', help='Use --python directly; caller supplies dependencies')
    parser.add_argument('--build-bridge', action='store_true', help='Compile the native bridge; does not attach to WeChat')
    parser.add_argument('--install-skill', action='store_true')
    parser.add_argument('--skill-root', type=Path, default=Path.home() / '.codex/skills')
    parser.add_argument('--replace-skill', action='store_true', help='Explicitly replace an existing wechat-native skill')
    return parser


if __name__ == '__main__':
    try:
        install(parser().parse_args())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
