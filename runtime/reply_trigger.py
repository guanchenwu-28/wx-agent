#!/usr/bin/env python3
"""Durable, single-contact auto-reply queue. No UI automation or model API."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
from runtime_config import CACHE


def atomic(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def key(row):
    return f"{row['database']}:{row['local_id']}"


def sanitized(row):
    kind = int(row['local_type'])
    return {k: row.get(k) for k in
            ('database', 'local_id', 'create_time', 'local_type', 'sender_username')} | {
        'mentioned_usernames': row.get('mentioned_usernames') or [],
        'mention_metadata_available': bool(row.get('mention_metadata_available')),
        'text': row.get('content') if kind == 1 else
        {3: '[图片]', 34: '[语音]', 47: '[表情]'}.get(kind, '[非文字消息]')}


def native_send(text, request_id, recipient):
    proc = subprocess.run([sys.executable, str(ROOT / 'wechat.py'), 'send',
                           '--to', recipient, '--text', text, '--request-id', request_id],
                          capture_output=True, text=True, timeout=100)
    result = json.loads(proc.stdout)
    record = (result.get('result') or {}).get('native_record')
    return {'state': result.get('state', 'unknown'),
            'record_key': key(record) if record else None,
            'server_id': record.get('server_id') if record else None}


class Trigger:
    def __init__(self, directory=None, reader=None, sender=None, now=None,
                 recipient=None, self_username=None, condition=None, gate=None, quiet_seconds=0):
        if not isinstance(recipient, str) or not recipient:
            raise ValueError("An explicit recipient is required; use autoreply.py configure")
        self.recipient = recipient
        self.self_username = self_username
        self.condition = condition or {'mode': 'all'}
        self.gate = gate
        if not isinstance(quiet_seconds, (float, int)) or not 0 <= quiet_seconds <= 300:
            raise ValueError('quiet_seconds must be between 0 and 300')
        self.quiet_seconds = float(quiet_seconds)
        if recipient.endswith('@chatroom') and not self_username:
            raise ValueError('Group requires verified self_username')
        self.directory = Path(directory or CACHE / 'auto-reply' / 'queues' / hashlib.sha256(recipient.encode()).hexdigest()[:24])
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.state_path = self.directory / 'state.json'
        self.enabled_path = self.directory / 'enabled.json'
        if reader is None:
            from reader import WeChatReader
            reader = WeChatReader()
        self.reader = reader
        self.sender = sender or (lambda text, request_id: native_send(text, request_id, self.recipient))
        self.now = now or time.time

    @contextlib.contextmanager
    def locked(self):
        with (self.directory / 'queue.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self):
        return json.loads(self.state_path.read_text())

    def save(self, state):
        atomic(self.state_path, state)

    def active(self, state):
        try:
            return (json.loads(self.enabled_path.read_text())['generation'] == state['generation']
                    and (self.gate is None or self.gate()))
        except FileNotFoundError:
            return False

    def read(self, **kwargs):
        result = self.reader.read_messages(recipient=self.recipient, **kwargs)
        if result.get('missing_key_databases'):
            raise RuntimeError('Message shard key unavailable; cursor unchanged')
        if any(v.get('integrity') != 'ok' for v in result['validation']):
            raise RuntimeError('Message database integrity unavailable')
        return result

    def enable(self):
        with self.locked():
            if self.state_path.exists() and self.active(self.load()):
                return self.status_unlocked(self.load())
            if self.state_path.exists():
                previous = self.load()
                if previous['blocked'] or (previous['pending'] or {}).get('inflight'):
                    raise RuntimeError('Unresolved previous send or sender; inspect before re-enabling')
            tail = self.read(limit=30)
            state = {'version': 1, 'recipient': self.recipient, 'account': tail['account'],
                     'generation': uuid.uuid4().hex, 'enabled_at': self.now(),
                     'cursors': {}, 'context': [], 'pending': None, 'blocked': None,
                     'sent_keys': [], 'events': []}
            rows = []
            for db in [v['database'] for v in tail['validation']]:
                part = self.read(account=state['account'], database=db, limit=30)
                state['cursors'][db] = max((r['local_id'] for r in part['messages']), default=0)
                rows.extend(sanitized(r) for r in part['messages'])
            state['context'] = sorted(rows, key=self.order)[-30:]
            self.seed_unanswered(state)
            self.save(state)
            atomic(self.enabled_path, {'generation': state['generation']})
            return self.status_unlocked(state) | {'startup_pending_count': len((state['pending'] or {}).get('incoming', []))}

    def seed_unanswered(self, state):
        """Preserve the trailing peer turn when establishing a read cursor.

        A read watermark means observed, not answered. Only the trailing turn
        is considered, never messages preceding the last account-side reply.
        """
        incoming = []
        for row in reversed(state['context']):
            if not row.get('sender_username'):
                state['blocked'] = {'reason': 'unresolved_sender', 'record_key': key(row)}
                break
            if self.is_self(row):
                break
            if not self.is_incoming(row):
                state['blocked'] = {'reason': 'unexpected_sender', 'record_key': key(row)}
                break
            incoming.append(row)
        incoming.reverse()
        # Only retain from the first qualifying message in the trailing turn.
        incoming = next((incoming[i:] for i, row in enumerate(incoming) if self.qualifies(row)), [])
        if incoming and not state['blocked']:
            digest = hashlib.sha256(json.dumps([state['generation'], [key(r) for r in incoming]]).encode()).hexdigest()[:24]
            state['pending'] = {'batch_id': digest, 'incoming': incoming,
                                'texts': None, 'sent': [], 'inflight': None,
                                'last_received_at': self.now()}
        state['startup_catch_up_done'] = True

    def catch_up(self):
        """Migrate a legacy empty startup queue once, without rewinding cursors."""
        with self.locked():
            state = self.load()
            if not self.active(state):
                return {'status': 'disabled'}
            if state.get('startup_catch_up_done') or state['events'] or state['pending'] or state['blocked']:
                return self.check_unlocked(state)
            # Seed the observed tail, then merge fresh messages. A manual reply
            # since startup cancels it before anything can be sent.
            self.seed_unanswered(state)
            self.refresh(state)
            return self.check_unlocked(state)

    def disable(self):
        # Independent of the queue lock, so stop can interrupt a multi-bubble reply.
        # A native send already in progress cannot be recalled.
        self.enabled_path.unlink(missing_ok=True)
        return {'status': 'disabled', 'recipient': self.recipient}

    def is_self(self, row):
        sender = row.get('sender_username')
        return sender == self.self_username if self.self_username else bool(sender and sender != self.recipient)

    def is_incoming(self, row):
        sender = row.get('sender_username')
        return bool(sender and sender != self.self_username) if self.recipient.endswith('@chatroom') else sender == self.recipient

    def qualifies(self, row):
        rule = self.condition
        if rule.get('senders') and row.get('sender_username') not in rule['senders']:
            return False
        mode = rule.get('mode')
        if mode == 'all' or mode == 'semantic':
            return True
        if mode == 'mention':
            return bool(row.get('mention_metadata_available')) and self.self_username in row.get('mentioned_usernames', [])
        if mode == 'keywords':
            return row.get('local_type') == 1 and any(word in (row.get('text') or '') for word in rule.get('keywords', []))
        return False

    @staticmethod
    def order(row):
        return (row['create_time'], row['database'], row['local_id'])

    def event(self, state, kind, **kwargs):
        state['events'] = (state['events'] + [{'at': self.now(), 'kind': kind, **kwargs}])[-50:]

    def refresh(self, state):
        tail = self.read(account=state['account'], limit=1)
        if tail['account'] != state['account']:
            raise RuntimeError('Account changed')
        rows, cursors = [], dict(state['cursors'])
        for db in [v['database'] for v in tail['validation']]:
            part = self.read(account=state['account'], database=db,
                             after_local_id=cursors.get(db, 0), limit=500)
            if part.get('scan_limit_reached') or part.get('result_limit_reached'):
                raise RuntimeError('More than 500 unseen messages; manual review required, cursor unchanged')
            rows.extend(part['messages'])
            cursors[db] = max([cursors.get(db, 0)] + [r['local_id'] for r in part['messages']])
        incoming = list((state['pending'] or {}).get('incoming', []))
        changed = False
        for row in sorted(rows, key=self.order):
            clean = sanitized(row)
            state['context'].append(clean)
            if key(row) in state['sent_keys']:
                continue
            sender = row.get('sender_username')
            if not sender:
                state['blocked'] = {'reason': 'unresolved_sender', 'record_key': key(row)}
                continue
            if self.is_incoming(clean):
                sender_allowed = not self.condition.get('senders') or sender in self.condition['senders']
                if sender_allowed and (incoming or self.qualifies(clean)):
                    incoming.append(clean)
                    changed = True
            elif self.is_self(clean):
                incoming = []
                changed = True
            else:
                state['blocked'] = {'reason': 'unexpected_sender', 'record_key': key(row)}
        state['context'] = state['context'][-30:]
        state['cursors'] = cursors
        if changed:
            if state['pending']:
                self.event(state, 'superseded', batch_id=state['pending']['batch_id'])
            if incoming:
                digest = hashlib.sha256(json.dumps([state['generation'], [key(r) for r in incoming]]).encode()).hexdigest()[:24]
                state['pending'] = {'batch_id': digest, 'incoming': incoming,
                                    'texts': None, 'sent': [], 'inflight': None,
                                    'last_received_at': self.now()}
            else:
                state['pending'] = None
        self.save(state)

    def status_unlocked(self, state):
        return {'status': 'enabled' if self.active(state) else 'disabled',
                'recipient': self.recipient, 'account': state['account'],
                'pending_batch': (state['pending'] or {}).get('batch_id'),
                'blocked': state['blocked'], 'cursors': state['cursors']}

    def status(self):
        with self.locked():
            if not self.state_path.exists():
                return {'status': 'disabled', 'recipient': self.recipient}
            return self.status_unlocked(self.load())

    def check_unlocked(self, state):
        if not self.active(state):
            return {'status': 'disabled'}
        if state['blocked']:
            return {'status': 'blocked', **state['blocked']}
        if (state['pending'] or {}).get('inflight'):
            state['blocked'] = {'reason': 'interrupted_send_unknown',
                                'request_id': state['pending']['inflight']}
            self.save(state)
            return {'status': 'blocked', **state['blocked']}
        self.refresh(state)
        if state['blocked']:
            return {'status': 'blocked', **state['blocked']}
        pending = state['pending']
        if not pending:
            return {'status': 'idle'}
        if self.quiet_seconds:
            if 'last_received_at' not in pending:
                # Older queues have no arrival timestamp. Start one quiet
                # window without replaying history or resetting their cursor.
                pending['last_received_at'] = self.now()
                self.save(state)
            ready_at = pending['last_received_at'] + self.quiet_seconds
            remaining = ready_at - self.now()
            if remaining > 0:
                return {'status': 'waiting', 'recipient': self.recipient,
                        'batch_id': pending['batch_id'], 'ready_at': ready_at,
                        'retry_after': remaining, 'incoming_count': len(pending['incoming'])}
        return {'status': 'ready', 'recipient': self.recipient, 'batch_id': pending['batch_id'],
                'incoming': pending['incoming'], 'context': state['context'],
                'planned_texts': pending['texts'], 'sent_count': len(pending['sent']),
                'requires_media_review': any(r['local_type'] != 1 or not isinstance(r['text'], str)
                                            for r in pending['incoming'])}

    def check(self):
        with self.locked():
            if not self.state_path.exists():
                return {'status': 'disabled'}
            return self.check_unlocked(self.load())

    def reply(self, batch_id, texts):
        if not isinstance(texts, list) or not 1 <= len(texts) <= 12 or any(
                not isinstance(t, str) or not t.strip() or '\0' in t or len(t.encode()) > 4096 for t in texts):
            raise ValueError('texts must contain 1 to 12 nonempty strings of at most 4096 bytes')
        with self.locked():
            state = self.load()
            check = self.check_unlocked(state)
            if check['status'] != 'ready':
                return check
            if check['batch_id'] != batch_id:
                return {'status': 'stale', 'batch_id': check['batch_id']}
            pending = state['pending']
            if pending['texts'] is not None and pending['texts'] != texts:
                raise ValueError('Batch already has a persisted reply; reuse planned_texts')
            pending['texts'] = texts
            self.save(state)
            while len(pending['sent']) < len(texts):
                # Re-read between bubbles as well as before the first send.
                if not self.active(state):
                    return {'status': 'disabled', 'sent_count': len(pending['sent'])}
                self.refresh(state)
                if state['blocked']:
                    return {'status': 'blocked', **state['blocked']}
                if not state['pending'] or state['pending']['batch_id'] != batch_id:
                    return {'status': 'stale', 'sent_count': len(pending['sent'])}
                if not self.active(state):
                    return {'status': 'disabled', 'sent_count': len(pending['sent'])}
                index = len(pending['sent'])
                prefix = 'auto'
                request_id = f'{prefix}-{batch_id}-{index + 1}'
                pending['inflight'] = request_id
                self.save(state)
                try:
                    result = self.sender(texts[index], request_id)
                except Exception:
                    result = {'state': 'unknown'}
                if result.get('state') != 'sent' or not result.get('record_key'):
                    state['blocked'] = {'reason': 'send_outcome_unknown', 'request_id': request_id}
                    self.save(state)
                    return {'status': 'blocked', **state['blocked']}
                pending['sent'].append({'request_id': request_id, **result})
                pending['inflight'] = None
                state['sent_keys'] = (state['sent_keys'] + [result['record_key']])[-100:]
                self.save(state)
            self.event(state, 'replied', batch_id=batch_id, count=len(texts))
            state['pending'] = None
            self.save(state)
            return {'status': 'sent', 'batch_id': batch_id, 'count': len(texts)}

    def skip(self, batch_id, reason):
        with self.locked():
            state = self.load()
            check = self.check_unlocked(state)
            if check['status'] != 'ready':
                return check
            if check['batch_id'] != batch_id:
                return {'status': 'stale'}
            self.event(state, 'skipped', batch_id=batch_id, reason=reason[:500])
            state['pending'] = None
            self.save(state)
            return {'status': 'skipped', 'batch_id': batch_id}



if __name__ == '__main__':
    raise SystemExit('Use autoreply.py with an explicitly configured conversation; the legacy single-contact CLI is retired')
