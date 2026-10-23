"""Bounded, opt-in local conversation polling; no background process or sending.

Cursor acknowledgement happens when poll_messages returns successfully, not when
an external consumer finishes processing the result. The caller should persist
its own output if it needs replay after downstream failures.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from reader import ReaderError, WeChatReader


class PollerError(ReaderError):
    pass


def _load(path):
    if not path.exists():
        return {'version': 1, 'accounts': {}}
    try:
        state = json.loads(path.read_text())
        if state.get('version') != 1 or not isinstance(state.get('accounts'), dict):
            raise ValueError()
        for account, recipients in state['accounts'].items():
            if not isinstance(account, str) or not isinstance(recipients, dict):
                raise ValueError()
            for recipient, cursors in recipients.items():
                if not isinstance(recipient, str) or not isinstance(cursors, dict):
                    raise ValueError()
                for database, cursor in cursors.items():
                    if not isinstance(database, str) or not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                        raise ValueError()
        return state
    except (OSError, ValueError, AttributeError, TypeError):
        raise PollerError('Cursor file is invalid; it was not changed') from None


def _save(path, state):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _validate_result(result, account, database):
    if result.get('account') != account or result.get('missing_key_databases'):
        raise PollerError('Message read was incomplete or selected a different account')
    if not any(v.get('database') == database for v in result.get('validation', [])):
        raise PollerError('Message read did not validate the requested database')
    rows = result.get('messages')
    if not isinstance(rows, list):
        raise PollerError('Message read returned invalid rows')
    for row in rows:
        identifier = row.get('local_id')
        if row.get('database') != database or not isinstance(identifier, int) or isinstance(identifier, bool) or identifier <= 0:
            raise PollerError('Message read returned an invalid cursor')
    return rows


def poll_messages(recipient, cursor_path=None, account=None, limit=100, from_start=False):
    """Poll only the explicitly named conversation and atomically acknowledge it.

    First invocation baselines each shard at its latest local_id and returns
    initialized=True with no historical messages. from_start=True starts new
    conversation cursors at zero; it never rewinds an existing cursor. A shard
    appearing after initialization is read from zero. limit is a total bound.
    Any read failure leaves the entire saved state unchanged.
    """
    if not isinstance(recipient, str) or not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', recipient):
        raise PollerError('recipient must be an explicit WeChat identifier')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise PollerError('limit must be 1..500')
    if not isinstance(from_start, bool):
        raise PollerError('from_start must be boolean')
    reader = WeChatReader()
    accounts = reader.accounts()
    if account is None:
        if len(accounts) != 1:
            raise PollerError('Select an account explicitly; discovery did not find exactly one')
        selected = accounts[0]
    else:
        selected = next((entry for entry in accounts if entry['account'] == account), None)
        if selected is None:
            raise PollerError('Requested account was not found')
    account = selected['account']
    storage = Path(selected['db_storage'])
    databases = sorted(str(p.relative_to(storage)) for p in (storage / 'message').glob('message_*.db')
                       if re.fullmatch(r'message_\d+\.db', p.name) and p.is_file() and not p.is_symlink())
    if not databases:
        raise PollerError('No message databases were discovered')
    path = Path(cursor_path) if cursor_path is not None else reader.cache / 'message-cursors.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cursor_path is None:
        path.parent.chmod(0o700)
    lock_fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            original = _load(path)
            state = copy.deepcopy(original)
            recipients = state['accounts'].setdefault(account, {})
            initialized = recipient not in recipients
            cursors = recipients.setdefault(recipient, {})
            messages, unsupported, has_more = [], [], False
            if initialized and not from_start:
                for database in databases:
                    result = reader.read_messages(recipient=recipient, account=account, limit=1, database=database)
                    rows = _validate_result(result, account, database)
                    cursors[database] = max((row['local_id'] for row in rows), default=0)
            else:
                for database in databases:
                    previous = cursors.setdefault(database, 0)
                    remaining = limit - len(messages)
                    if remaining == 0:
                        # Conservatively report possible work in the next shard;
                        # its rows and cursor are untouched until the next call.
                        has_more = True
                        break
                    result = reader.read_messages(recipient=recipient, account=account,
                                                  limit=remaining, after_local_id=previous,
                                                  database=database)
                    rows = _validate_result(result, account, database)
                    if any(row['local_id'] <= previous for row in rows) or len(rows) > remaining:
                        raise PollerError('Message read returned rows outside the requested cursor range')
                    rows.sort(key=lambda row: row['local_id'])
                    messages.extend(rows)
                    for row in rows:
                        if row.get('content') is None and row.get('content_status') != 'null':
                            unsupported.append({key: row.get(key) for key in
                                                ('database', 'local_id', 'server_id', 'local_type', 'create_time', 'content_status')})
                    if rows:
                        cursors[database] = max(row['local_id'] for row in rows)
                    has_more = has_more or bool(result.get('scan_limit_reached') or result.get('result_limit_reached'))
            # Preserve all unrelated accounts/conversations, write only when state
            # changes, and do not expose a newly advanced cursor before replace.
            if state != original:
                _save(path, state)
            return {'recipient': recipient, 'account': account, 'messages': messages,
                    'cursor': dict(cursors), 'has_more': has_more,
                    'initialized': initialized, 'unsupported': unsupported}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
