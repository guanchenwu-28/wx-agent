#!/usr/bin/env python3
"""One event-driven auto-reply service, with an explicit conversation registry."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from reply_trigger import Trigger, CACHE, ROOT, atomic

HOME = CACHE / 'auto-reply'
from runtime_config import NODE
DEFAULT_STYLE = ('中文自然口语，根据用户在当前会话中的真实表达调整，不要机械重复笑声。'
                 '完整接住本轮的不同主题，每个主题可以多说几句，按话题自然分成多条消息；'
                 '不用只说一句，也不用硬凑条数或逐条复述对方。'
                 '不编造用户的行程、经历、决定或承诺；无法理解的媒体交给本人处理。')


def validate_rule(rule):
    if not isinstance(rule, dict) or rule.get('mode') not in ('all', 'mention', 'keywords', 'semantic'):
        raise ValueError('Condition must explicitly select all, mention, keywords, or semantic')
    if rule['mode'] == 'keywords' and (not isinstance(rule.get('keywords'), list)
            or not rule['keywords'] or any(not isinstance(x, str) or not x.strip() for x in rule['keywords'])):
        raise ValueError('keywords requires a nonempty literal keyword list')
    if rule['mode'] == 'semantic' and not str(rule.get('instruction', '')).strip():
        raise ValueError('semantic requires the owner-defined instruction')
    if 'senders' in rule and (not isinstance(rule['senders'], list) or
            any(not isinstance(x, str) or not x for x in rule['senders'])):
        raise ValueError('senders must contain exact sender IDs')
    return rule


class Registry:
    def __init__(self, home=HOME, reader=None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.path = self.home / 'config.json'
        self.reader = reader

    def load(self):
        return json.loads(self.path.read_text())

    def save(self, config):
        atomic(self.path, config)

    def init(self, account, self_username):
        if self.path.exists():
            raise ValueError('Already initialized; existing configuration preserved')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', account) or not re.fullmatch(r'[A-Za-z0-9_.-]+', self_username):
            raise ValueError('Explicit account and verified self username required')
        self.save({'version': 1, 'account': account, 'self_username': self_username,
                   'enabled': True, 'targets': {}})
        return self.status()

    def target_dir(self, recipient):
        return self.home / 'queues' / hashlib.sha256(recipient.encode()).hexdigest()[:24]

    def trigger(self, recipient, target=None, sender=None):
        config = self.load()
        target = target or config['targets'][recipient]
        revision = target['revision']
        def gate():
            fresh = self.load()
            current = fresh['targets'].get(recipient, {})
            return (fresh['enabled'] and current.get('enabled')
                    and current.get('revision') == revision)
        return Trigger(self.target_dir(recipient), reader=self.reader, sender=sender,
                       recipient=recipient, self_username=config['self_username'],
                       condition=target['condition'], gate=gate,
                       quiet_seconds=target.get('quiet_seconds', 10))

    def configure(self, recipient, name, condition, enabled=False, style=None, quiet_seconds=10):
        if not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', recipient):
            raise ValueError('Exact recipient ID required')
        validate_rule(condition)
        if not isinstance(quiet_seconds, (int, float)) or not 0 <= quiet_seconds <= 300:
            raise ValueError('quiet_seconds must be between 0 and 300')
        config = self.load()
        prior = config['targets'].get(recipient)
        entry = {'name': name, 'kind': 'group' if recipient.endswith('@chatroom') else 'contact',
                 'condition': condition, 'style': style or DEFAULT_STYLE,
                 'enabled': False, 'revision': uuid.uuid4().hex,
                 'quiet_seconds': quiet_seconds, 'single_message': False}
        config['targets'][recipient] = entry
        # Gate existing workers before changing any queue or rule.
        self.save(config)
        trigger = self.trigger(recipient, entry)
        trigger.disable()
        if enabled:
            # Baseline under the new rule while still gated from live sends.
            trigger.enable()
            state = trigger.load()
            if state['account'] != config['account']:
                trigger.disable()
                raise RuntimeError('Queue account does not match registry')
            entry['enabled'] = True
            self.save(config)
        return {'recipient': recipient, **entry}

    def set_enabled(self, recipient, enabled):
        config = self.load()
        if recipient is None:
            config['enabled'] = enabled
        else:
            if recipient not in config['targets']:
                raise ValueError('Unregistered conversation')
            config['targets'][recipient]['enabled'] = enabled
            if enabled:
                trigger = self.trigger(recipient)
                if not trigger.state_path.exists():
                    raise ValueError('Use configure --enable to initialize this conversation')
                state = trigger.load()
                if state['blocked'] or (state['pending'] or {}).get('inflight'):
                    raise ValueError('Resolve unknown state before resuming')
        self.save(config)
        return self.status()

    def status(self):
        config = self.load()
        result = {'enabled': config['enabled'], 'account': config['account'], 'targets': []}
        for recipient, target in config['targets'].items():
            queue = self.trigger(recipient, target)
            result['targets'].append({'recipient': recipient, **target, 'queue': queue.status()})
        for filename in ('service.json', 'outcomes.json'):
            p = self.home / filename
            if p.exists():
                result[filename[:-5]] = json.loads(p.read_text())
        return result


class Coordinator:
    def __init__(self, registry, generate=None):
        self.registry = registry
        if generate is None:
            from model_reply import generate_reply
            generate = generate_reply
        self.generate = generate
        self.path = registry.home / 'outcomes.json'

    def outcomes(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def record(self, recipient, value):
        values = self.outcomes()
        # Metadata only, never log prompts or generated text.
        values[recipient] = {'at': time.time(), **value}
        atomic(self.path, values)

    def process(self, recipient, target):
        trigger = self.registry.trigger(recipient, target)
        batch = trigger.check()
        if batch['status'] != 'ready':
            if batch['status'] == 'blocked':
                self.record(recipient, batch)
            return batch
        config = self.registry.load()
        batch['self_username'] = config['self_username']
        batch['conversation_kind'] = target['kind']
        previous = self.outcomes().get(recipient, {})
        if previous.get('status') == 'model_error' and previous.get('batch_id') == batch['batch_id']:
            return {'status': 'model_error', 'retry_requires_operator': True}
        # Text in a mixed batch may be sufficient to respond without claiming
        # to understand its media. Only a wholly unreadable batch skips here.
        unsupported = [r for r in batch['incoming'] if r['local_type'] not in (1, 47)
                       or (r['local_type'] == 1 and not isinstance(r['text'], str))]
        readable_text = any(r['local_type'] == 1 and isinstance(r['text'], str) and r['text'].strip()
                            for r in batch['incoming'])
        if unsupported and not readable_text:
            result = trigger.skip(batch['batch_id'], '图片、语音或其他内容需本人查看')
            self.record(recipient, {'status': 'handoff', 'batch_id': batch['batch_id'], 'reason': 'unreadable_media'})
            return result
        try:
            model_options = {}
            if 'model' in config:
                model_options['model'] = config['model']
            if 'model_reasoning_effort' in config:
                model_options['reasoning_effort'] = config['model_reasoning_effort']
            decision = ({'action': 'reply', 'texts': batch['planned_texts'], 'reason': 'persisted_plan'}
                        if batch['planned_texts'] else self.generate(
                            batch, target['style'], json.dumps(target['condition'], ensure_ascii=False),
                            **model_options))
            if (not isinstance(decision, dict) or decision.get('action') not in ('reply', 'skip', 'handoff')
                    or not isinstance(decision.get('reason'), str) or not isinstance(decision.get('texts'), list)):
                raise ValueError('Invalid model decision')
            texts = decision['texts']
            if decision['action'] == 'reply':
                if not 1 <= len(texts) <= 12 or any(not isinstance(t, str) or not t.strip()
                        or '\0' in t or len(t.encode()) > 4096 for t in texts):
                    raise ValueError('Invalid reply texts')
                if target.get('single_message') and not batch['planned_texts']:
                    combined = '\n'.join(texts)
                    if len(combined.encode()) > 4096:
                        raise ValueError('Combined reply exceeds native text limit')
                    decision['texts'] = [combined]
            elif texts:
                raise ValueError('Non-reply decision cannot contain texts')
        except Exception as exc:
            self.record(recipient, {'status': 'model_error', 'batch_id': batch['batch_id'],
                                    'reason': type(exc).__name__})
            return {'status': 'model_error'}
        if decision['action'] == 'reply':
            result = trigger.reply(batch['batch_id'], decision['texts'])
        else:
            result = trigger.skip(batch['batch_id'], decision['reason'])
        self.record(recipient, {'status': result['status'], 'batch_id': batch['batch_id'],
                                'action': decision['action']})
        return result

    def observe(self):
        """Sync arrivals even while the main thread is generating a draft.

        check holds the per-conversation lock only for native snapshot/queue
        updates. It never generates, sends, or consumes a ready batch.
        """
        config = self.registry.load()
        if not config['enabled']:
            return
        for recipient, target in config['targets'].items():
            if target['enabled']:
                try:
                    self.registry.trigger(recipient, target).check()
                except Exception:
                    pass  # drain records/report errors; observation never discards state

    def drain(self):
        config = self.registry.load()
        if not config['enabled']:
            return
        deadlines = []
        for recipient, target in config['targets'].items():
            if not target['enabled']:
                continue
            try:
                result = self.process(recipient, target)
                # A newer arrival invalidates a draft. One fresh attempt only;
                # subsequent events will drive additional work, not a busy loop.
                if result['status'] == 'stale':
                    result = self.process(recipient, target)
                if result['status'] == 'waiting':
                    deadlines.append(result['ready_at'])
            except Exception as exc:
                self.record(recipient, {'status': 'error', 'reason': type(exc).__name__})
        return min(deadlines) if deadlines else None


class EventWake:
    """Wait for a real event or a pending conversation's silence deadline."""
    def __init__(self):
        self.condition = threading.Condition()
        self.dirty = False
        self.deadline = None
        self.stopped = False

    def notify(self):
        with self.condition:
            self.dirty = True
            self.condition.notify_all()

    def schedule(self, ready_at):
        with self.condition:
            self.deadline = (time.monotonic() + max(0, ready_at - time.time())) if ready_at is not None else None
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()

    def wait(self):
        with self.condition:
            while not self.stopped:
                if self.dirty:
                    self.dirty = False
                    return True
                if self.deadline is None:
                    self.condition.wait()  # no periodic wakeup when idle
                    continue
                remaining = self.deadline - time.monotonic()
                if remaining > 0:
                    self.condition.wait(remaining)
                    continue
                self.deadline = None
                return True
            return False


def serve(registry):
    from reader import WeChatReader
    config = registry.load()
    account = WeChatReader()._account(config['account'])
    db_dir = str(Path(account['db_storage']) / 'message')
    with (registry.home / 'daemon.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Auto-reply service is already running')
        queue = EventWake()
        worker = Coordinator(registry)
        stopped = threading.Event()
        child = [None]
        def status(state, **extra):
            atomic(registry.home / 'service.json', {'pid': os.getpid(), 'at': time.time(), 'state': state, **extra})
        def shutdown(*_):
            stopped.set()
            queue.stop()
            if child[0] and child[0].poll() is None:
                child[0].terminate()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        def listen():
            delay = 1
            while not stopped.is_set():
                try:
                    child[0] = subprocess.Popen([sys.executable, str(ROOT / 'event_source.py'),
                        '--message-dir', db_dir, '--control-dir', str(registry.home)],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    for line in child[0].stdout:
                        event = json.loads(line)
                        kind = event.get('kind') or event.get('type') or event.get('event')
                        if kind in ('ready', 'changed', 'reconnected', 'config_changed'):
                            delay = 1
                            status('listening', source='filesystem_write')
                            worker.observe()
                            queue.notify()
                        elif kind == 'unavailable':
                            status('waiting_for_database')
                        if stopped.is_set():
                            break
                    child[0].wait()
                    child[0].stdout.close()
                except Exception as exc:
                    status('watcher_error', error=type(exc).__name__)
                if not stopped.is_set():
                    status('watcher_restarting', retry_seconds=delay)
                    stopped.wait(delay)  # failure recovery only; no message polling
                    delay = min(delay * 2, 60)
        thread = threading.Thread(target=listen, daemon=True)
        status('starting')
        thread.start()
        queue.notify()  # durable queue catch-up after crash/restart
        try:
            while queue.wait():
                queue.schedule(worker.drain())
        finally:
            shutdown()
            thread.join(timeout=5)
            status('stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=HOME)
    sub = parser.add_subparsers(dest='cmd', required=True)
    init = sub.add_parser('init')
    init.add_argument('--account', required=True)
    init.add_argument('--self-username', required=True)
    for cmd in ('status', 'serve', 'retry'):
        sub.add_parser(cmd)
    for cmd in ('pause', 'resume'):
        action = sub.add_parser(cmd)
        action.add_argument('--to')
    setup = sub.add_parser('configure')
    setup.add_argument('--to', required=True)
    setup.add_argument('--name', required=True)
    setup.add_argument('--condition-json', required=True)
    setup.add_argument('--style')
    setup.add_argument('--enable', action='store_true')
    setup.add_argument('--quiet-seconds', type=float, default=10)
    args = parser.parse_args()
    registry = Registry(args.home)
    if args.cmd == 'init':
        result = registry.init(args.account, args.self_username)
    elif args.cmd == 'serve':
        serve(registry)
        return
    elif args.cmd == 'configure':
        result = registry.configure(args.to, args.name, json.loads(args.condition_json), args.enable, args.style, args.quiet_seconds)
    elif args.cmd in ('pause', 'resume'):
        result = registry.set_enabled(args.to, args.cmd == 'resume')
    elif args.cmd == 'retry':
        # Explicitly retry model generation; never clear native unknown states.
        outcomes = Coordinator(registry).outcomes()
        atomic(registry.home / 'outcomes.json', {k:v for k,v in outcomes.items() if v.get('status') != 'model_error'})
        registry.save(registry.load())
        result = {'retry_requested': True}
    else:
        result = registry.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
