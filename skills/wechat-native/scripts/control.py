#!/usr/bin/env python3
"""Small JSON control entrypoint for the installed native WeChat runtime."""
import argparse
import copy
import contextlib
import fcntl
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

DEFAULT_RUNTIME = Path.home() / 'Library/Application Support/WeChatLocalTool/runtime'
NATIVE_COMMANDS = frozenset(('status', 'adapt', 'read', 'contacts', 'send', 'send-image',
    'send-voice', 'reconcile', 'reconcile-media', 'start', 'restart',
    'restore-original', 'poll', 'watch', 'transport-status', 'transport-stop'))
IDENTIFIER = re.compile(r'[A-Za-z0-9_@.\-]{1,128}')


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_path(override=None):
    runtime = Path(override).expanduser().resolve() if override else DEFAULT_RUNTIME
    if not runtime.is_dir():
        raise ValueError('Native runtime is unavailable; specify --runtime explicitly')
    return runtime


def native(runtime, arguments, runner=subprocess.run, python=None):
    if not arguments or arguments[0] not in NATIVE_COMMANDS:
        raise ValueError('Unsupported native command')
    executable = runtime / 'wechat.py'
    if not executable.is_file():
        raise ValueError('Selected runtime has no wechat.py')
    if python is None:
        config_path = runtime / 'runtime-local.json'
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
        python = os.environ.get('WECHAT_PYTHON') or config.get('python') or sys.executable
    # Inherit stdout/stderr: native JSON and watch's JSON stream remain intact.
    # No shell, command interpolation, or interpretation of message text.
    return runner([python, str(executable), *arguments], check=False).returncode


def load_runtime(runtime):
    for filename in ('autoreply.py', 'service_manager.py'):
        if not (runtime / filename).is_file():
            raise ValueError('Selected runtime is missing ' + filename)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(runtime))
    autoreply = importlib.import_module('autoreply')
    service = importlib.import_module('service_manager')
    # All launchctl calls made by the existing service implementation, including
    # bootstrap and status, are bounded. Deployment stays owned by that module.
    def bounded_launchctl(*arguments):
        return subprocess.run(['/bin/launchctl', *arguments], capture_output=True,
                              text=True, timeout=5)
    service.run = bounded_launchctl
    return Controller(autoreply.Registry(), service, autoreply.validate_rule)


class Controller:
    def __init__(self, registry, service, validate_rule, *, now=time.monotonic,
                 sleep=time.sleep, verify_timeout=3):
        self.registry, self.service, self.validate_rule = registry, service, validate_rule
        self.now, self.sleep, self.verify_timeout = now, sleep, verify_timeout

    def _load(self):
        try:
            return self.registry.load()
        except FileNotFoundError:
            return None

    @staticmethod
    def _target_summary(recipient, target, globally_enabled):
        return {'recipient': recipient, 'name': target.get('name', recipient),
                'kind': target.get('kind', 'group' if recipient.endswith('@chatroom') else 'contact'),
                'enabled': bool(globally_enabled and target.get('enabled')),
                'quiet_seconds': target.get('quiet_seconds', 10),
                'condition_mode': (target.get('condition') or {}).get('mode')}

    def status(self):
        config = self._load()
        targets = []
        for recipient, target in (config or {}).get('targets', {}).items():
            summary = self._target_summary(recipient, target, config['enabled'])
            summary['queue'] = self._queue_summary(recipient)
            targets.append(summary)
        return {'ok': True, 'configured': config is not None,
                'enabled': bool(config and config.get('enabled')),
                'targets': targets, 'listener': self._listener(),
                'service': self.service.service_status()}

    def _listener(self):
        try:
            value = json.loads((self.registry.home / 'service.json').read_text())
            return {key: value.get(key) for key in ('pid', 'state', 'at')}
        except (FileNotFoundError, ValueError, TypeError, AttributeError):
            return None

    def _queue_summary(self, recipient):
        path = self.registry.target_dir(recipient) / 'state.json'
        if not path.exists():
            return {'exists': False}
        try:
            state = json.loads(path.read_text())
            pending, blocked = state.get('pending') or {}, state.get('blocked')
            return {'exists': True, 'pending': bool(pending),
                    'pending_batch': pending.get('batch_id'),
                    'pending_count': len(pending.get('incoming') or []),
                    'sent_count': len(pending.get('sent') or []),
                    'blocked': ({key: blocked[key] for key in ('reason', 'request_id', 'record_key')
                                 if key in blocked} if isinstance(blocked, dict) else bool(blocked)),
                    'cursors': state.get('cursors', {})}
        except (ValueError, TypeError, AttributeError):
            return {'exists': True, 'error': 'invalid_queue_state'}

    @contextlib.contextmanager
    def _queue_lock(self, recipient):
        path = self.registry.target_dir(recipient) / 'queue.lock'
        with path.open('a') as lock:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Queue is busy; wait for the current operation before resuming') from None
            yield

    def _queue_state(self, recipient, config):
        path = self.registry.target_dir(recipient) / 'state.json'
        if not path.exists():
            return None
        state = json.loads(path.read_text())
        if state.get('blocked') or (state.get('pending') or {}).get('inflight'):
            raise ValueError('Resolve blocked or unknown send state before resuming this target')
        if (state.get('account') != config['account'] or state.get('recipient') != recipient
                or not isinstance(state.get('generation'), str) or not state['generation']):
            raise ValueError('Queue identity does not match the configured account and target')
        return state

    def _wait_service(self, running):
        deadline = self.now() + self.verify_timeout
        while True:
            status = self.service.service_status()
            listener = self._listener() or {}
            ready = (status.get('loaded') and status.get('disabled') is False and status.get('state') == 'running'
                     and str(status.get('pid', '')).isdigit() and int(status['pid']) > 0
                     and str(listener.get('pid')) == str(status['pid'])
                     and listener.get('state') == 'listening')
            if (ready if running else not status.get('loaded') and status.get('disabled') is True):
                return status
            if self.now() >= deadline:
                raise RuntimeError('Service did not reach ' +
                                   ('running with a matching live listener' if running else 'unloaded and persistently disabled') + ' state')
            self.sleep(.1)

    def _stop_service(self):
        stopped = self.service.unload()
        status = self._wait_service(False)
        if stopped.get('native_connections_closed') is True:
            status['native_connections_closed'] = True
        return status

    def listen(self, recipient, name, *, condition=None, style=None, quiet_seconds=None):
        if not isinstance(recipient, str) or IDENTIFIER.fullmatch(recipient) is None:
            raise ValueError('An exact WeChat recipient identifier is required')
        if not isinstance(name, str) or not name.strip() or '\0' in name:
            raise ValueError('A nonempty display name is required')
        config = self._load()
        if config is None:
            raise ValueError('Initialize the native registry with a verified account and self identity first')
        prior = config['targets'].get(recipient)
        if condition is None:
            if prior:
                rule = copy.deepcopy(prior['condition'])
            elif recipient.endswith('@chatroom'):
                raise ValueError('A new group requires an explicit condition; nothing was changed')
            else:
                rule = {'mode': 'all'}
        else:
            rule = copy.deepcopy(condition)
        self.validate_rule(rule)
        quiet = quiet_seconds if quiet_seconds is not None else (prior or {}).get('quiet_seconds', 10)
        if type(quiet) not in (int, float) or not 0 <= quiet <= 300:
            raise ValueError('quiet_seconds must be between 0 and 300')
        if style is not None and (not isinstance(style, str) or not style.strip()):
            raise ValueError('Style file must contain nonempty text')
        effective_style = style if style is not None else (prior or {}).get('style')
        state = self._queue_state(recipient, config)
        style_changed = bool(prior and style is not None and style != prior.get('style'))
        if style_changed and ((state or {}).get('pending') or {}).get('sent'):
            raise ValueError('Resolve the partially sent reply before changing its style')
        must_configure = prior is None or rule != prior.get('condition') or state is None
        # All validation precedes the first mutation. A global resume scopes the
        # enabled set to this one explicitly requested target.
        if not config['enabled']:
            for target in config['targets'].values():
                target.update(enabled=False, revision=uuid.uuid4().hex)
        if prior:
            config['targets'][recipient].update(enabled=False, revision=uuid.uuid4().hex)
        self.registry.save(config)
        try:
            # The revision gate is now closed. Recheck in case an earlier
            # worker entered a native send between preflight and that gate.
            state = self._queue_state(recipient, config)
            if must_configure:
                self.registry.configure(recipient, name, rule, enabled=True,
                                        style=effective_style, quiet_seconds=quiet)
            else:
                # Restore only the enable marker; never call enable(), which
                # would establish a new generation/cursor and discard pending.
                with self._queue_lock(recipient):
                    state = self._queue_state(recipient, config)
                    if state is None:
                        raise ValueError('Queue disappeared before resume')
                    pending = state.get('pending') or {}
                    if style_changed and pending.get('sent'):
                        raise ValueError('Resolve the partially sent reply before changing its style')
                    directory = self.registry.target_dir(recipient)
                    if style_changed and pending.get('texts') is not None:
                        pending['texts'] = None
                        atomic_json(directory / 'state.json', state)
                    atomic_json(directory / 'enabled.json', {'generation': state['generation']})
                    current = self.registry.load()
                    target = current['targets'][recipient]
                    target.update(name=name, enabled=True, quiet_seconds=quiet,
                                  revision=uuid.uuid4().hex)
                    if style is not None:
                        target['style'] = style
                    self.registry.save(current)
            current = self.registry.load()
            current['enabled'] = True
            self.registry.save(current)
            # start() re-enables an explicitly resumed service even if it was
            # already loaded, while skipping deployment/bootstrap in that case.
            self.service.start()
            service = self._wait_service(True)
            target = self.registry.load()['targets'][recipient]
            return {'ok': True, 'status': 'active',
                    'target': self._target_summary(recipient, target, True),
                    'queue_preserved': not must_configure, 'service': service,
                    'listener': self._listener()}
        except Exception as exc:
            current = self.registry.load()
            if recipient in current['targets']:
                current['targets'][recipient].update(enabled=False, revision=uuid.uuid4().hex)
                self.registry.save(current)
            return {'ok': False, 'status': 'error', 'recipient': recipient,
                    'target_enabled': False, 'error': str(exc)}

    def pause(self, recipient=None):
        config = self._load()
        if recipient is not None:
            if IDENTIFIER.fullmatch(recipient) is None:
                raise ValueError('An exact WeChat recipient identifier is required')
            if config is None or recipient not in config['targets']:
                raise ValueError('Target is not registered')
            config['targets'][recipient].update(enabled=False, revision=uuid.uuid4().hex)
        elif config is not None:
            config['enabled'] = False
            for target in config['targets'].values():
                target.update(enabled=False, revision=uuid.uuid4().hex)
        if config is not None:
            self.registry.save(config)  # Gate first; never wait on a reader/login.
        active = bool(config and config.get('enabled') and
                      any(target.get('enabled') for target in config['targets'].values()))
        service = self.service.service_status() if active else self._stop_service()
        return {'ok': True, 'status': 'paused', 'recipient': recipient,
                'service_stopped': not service.get('loaded'), 'service': service}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    forwarding = commands.add_parser('native')
    forwarding.add_argument('arguments', nargs=argparse.REMAINDER)
    listen = commands.add_parser('listen')
    listen.add_argument('--to', required=True)
    listen.add_argument('--name', required=True)
    listen.add_argument('--condition-json')
    listen.add_argument('--style-file', type=Path)
    listen.add_argument('--quiet-seconds', type=float, help='New targets default to 10 seconds; existing value is preserved')
    pause = commands.add_parser('pause')
    pause.add_argument('--to')
    try:
        args = parser.parse_args(argv)
        runtime = runtime_path(args.runtime)
        if args.command == 'native':
            return native(runtime, args.arguments)
        controller = load_runtime(runtime)
        if args.command == 'listen':
            result = controller.listen(args.to, args.name,
                condition=json.loads(args.condition_json) if args.condition_json is not None else None,
                style=args.style_file.read_text() if args.style_file else None,
                quiet_seconds=args.quiet_seconds)
        elif args.command == 'pause':
            result = controller.pause(args.to)
        else:
            result = controller.status()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') else 1
    except Exception as exc:
        print(json.dumps({'ok': False, 'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
