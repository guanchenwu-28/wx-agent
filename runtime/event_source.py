#!/usr/bin/env python3
"""macOS kqueue vnode notifications, with no polling or message/body reads.

Notifications are hints to drain durable white-listed conversation cursors.
They are NOT proof that a new incoming WeChat message exists. ready/reconnected
also require cursor synchronization to cover startup/reconnection gaps.
"""
from __future__ import annotations

import argparse
import datetime
import errno
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import sys
import time

DATABASE_FILE = re.compile(r'^(message_\d+\.db)(?:-wal)?$')


class EventSource:
    def __init__(self, message_dir, emit, *, control_dir=None, debounce_ms=300, max_delay_ms=1500):
        if not hasattr(select, 'kqueue'):
            raise ValueError('macOS_kqueue_required')
        if not Path(message_dir).is_absolute() or Path(message_dir).name != 'message':
            raise ValueError('absolute_message_directory_required')
        if control_dir is not None and not Path(control_dir).is_absolute():
            raise ValueError('absolute_control_directory_required')
        if not 10 <= debounce_ms <= 10000 or not debounce_ms <= max_delay_ms <= 30000:
            raise ValueError('invalid_coalescing_delay')
        self.emit = emit
        self.debounce = debounce_ms / 1000
        self.max_delay = max_delay_ms / 1000
        self.sequence = 0
        self.closed = False
        self.stop_reason = None
        self.pending_databases = set()
        self.pending_control = False
        self.pending_start = self.pending_latest = None
        self.kqueue = select.kqueue()
        self.descriptors = {}
        self.wake_read, self.wake_write = os.pipe()
        os.set_blocking(self.wake_write, False)
        self.kqueue.control([select.kevent(self.wake_read, filter=select.KQ_FILTER_READ,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR)], 0, 0)
        self.groups = {'message': {'path': Path(message_dir), 'generation': 0, 'files': {}, 'missing': False}}
        if control_dir is not None:
            self.groups['control'] = {'path': Path(control_dir), 'generation': 0, 'files': {}, 'missing': False}
        try:
            for group, item in self.groups.items():
                item['parent'] = self._watch(item['path'].parent, group, 'parent')
                self._refresh(group, initial=True)
        except BaseException:
            self.close(report=False)
            raise

    def _report(self, kind, **fields):
        self.sequence += 1
        self.emit({'schema': 1, 'source': 'filesystem_write', 'sequence': self.sequence,
            'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'kind': kind, **fields})

    def _watch(self, path, group, role, name=None):
        # O_EVTONLY opens an event descriptor without reading database contents.
        fd = os.open(path, getattr(os, 'O_EVTONLY', os.O_RDONLY) | os.O_NOFOLLOW)
        try:
            value = os.fstat(fd)
            if role in ('parent', 'directory') and not stat.S_ISDIR(value.st_mode):
                raise ValueError('watched_path_is_not_directory')
            if role == 'file' and not stat.S_ISREG(value.st_mode):
                raise ValueError('database_or_config_is_not_regular_file')
            notes = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB |
                     select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
            self.kqueue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=notes)], 0, 0)
            self.descriptors[fd] = {'group': group, 'role': role, 'name': name,
                                    'identity': (value.st_dev, value.st_ino)}
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _unwatch(self, fd):
        if fd is not None and fd in self.descriptors:
            self.descriptors.pop(fd)
            os.close(fd)  # Closing removes the kevent, including stale queued vnode changes.

    def _drop_directory(self, item):
        self._unwatch(item.pop('directory', None))
        for fd in item['files'].values():
            self._unwatch(fd)
        item['files'].clear()

    def _mark(self, group, name=None):
        if group == 'message' and name is not None:
            self.pending_databases.add('message/' + DATABASE_FILE.fullmatch(name).group(1))
        elif group == 'control':
            self.pending_control = True
        else:
            return
        now = time.monotonic()
        if self.pending_start is None:
            self.pending_start = now
        self.pending_latest = now

    def _refresh(self, group, initial=False):
        item = self.groups[group]
        try:
            value = item['path'].lstat()
        except FileNotFoundError:
            if not item['missing']:
                self._drop_directory(item)
                item['missing'] = True
                self._report('unavailable', scope=group, code='directory_missing', generation=item['generation'])
            return
        if not stat.S_ISDIR(value.st_mode):
            raise ValueError('watched_path_is_not_regular_directory')
        current_fd = item.get('directory')
        current_identity = self.descriptors[current_fd]['identity'] if current_fd is not None else None
        replacement = current_identity != (value.st_dev, value.st_ino)
        if replacement:
            self._drop_directory(item)
            item['directory'] = self._watch(item['path'], group, 'directory')
            item['generation'] += 1
            item['missing'] = False
        names = {entry.name for entry in item['path'].iterdir()
                 if DATABASE_FILE.fullmatch(entry.name)} if group == 'message' else {'config.json'}
        for name, fd in list(item['files'].items()):
            try:
                value = (item['path'] / name).lstat() if name in names else None
            except FileNotFoundError:
                value = None
            if value is None or self.descriptors[fd]['identity'] != (value.st_dev, value.st_ino):
                self._unwatch(fd)
                del item['files'][name]
                if not initial:
                    self._mark(group, name)
        for name in sorted(names):
            if name in item['files']:
                continue
            try:
                item['files'][name] = self._watch(item['path'] / name, group, 'file', name)
            except FileNotFoundError:
                continue  # A create/remove race generates the next directory event.
            if not initial and not replacement:
                self._mark(group, name)
        if replacement:
            if group == 'message':
                self._report('ready' if item['generation'] == 1 else 'reconnected',
                    generation=item['generation'], message_dir=str(item['path']),
                    databases=sorted({'message/' + DATABASE_FILE.fullmatch(name).group(1) for name in item['files']}),
                    watched_files=len(item['files']), requires_cursor_sync=True)
            else:
                self._report('control_ready', generation=item['generation'], control_dir=str(item['path']))
                if not initial:
                    self._mark('control')

    def _flush(self):
        if self.pending_databases:
            self._report('changed', generation=self.groups['message']['generation'],
                         databases=sorted(self.pending_databases))
        if self.pending_control:
            self._report('config_changed')
        self.pending_databases.clear()
        self.pending_control = False
        self.pending_start = self.pending_latest = None

    def stop(self, reason='requested'):
        if self.closed or self.stop_reason is not None:
            return
        self.stop_reason = reason
        try:
            os.write(self.wake_write, b'x')
        except (BlockingIOError, OSError):
            pass

    def run(self):
        try:
            while self.stop_reason is None:
                deadline = (min(self.pending_latest + self.debounce, self.pending_start + self.max_delay)
                            if self.pending_start is not None else None)
                timeout = max(0, deadline - time.monotonic()) if deadline is not None else None
                events = self.kqueue.control(None, 256, timeout)
                refresh = set()
                for event in events:
                    if event.ident == self.wake_read:
                        continue
                    entry = self.descriptors.get(event.ident)
                    if entry is None:
                        continue
                    if event.flags & select.KQ_EV_ERROR:
                        raise OSError(event.data, 'kqueue_watch_failed')
                    if entry['role'] == 'parent':
                        if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE):
                            raise ValueError('watched_parent_replaced_restart_required')
                        refresh.add(entry['group'])
                    elif entry['role'] == 'directory':
                        refresh.add(entry['group'])
                    else:
                        self._mark(entry['group'], entry['name'])
                        if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE):
                            refresh.add(entry['group'])
                for group in refresh:
                    self._refresh(group)
                if self.pending_start is not None and time.monotonic() >= min(
                        self.pending_latest + self.debounce, self.pending_start + self.max_delay):
                    self._flush()
        finally:
            self.close()

    def close(self, report=True):
        if self.closed:
            return
        self.closed = True
        for fd in list(self.descriptors):
            self._unwatch(fd)
        os.close(self.wake_read)
        os.close(self.wake_write)
        self.kqueue.close()
        if report:
            self._report('stopped', reason=self.stop_reason or 'error')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message-dir', required=True)
    parser.add_argument('--control-dir')
    parser.add_argument('--debounce-ms', type=int, default=300)
    parser.add_argument('--max-delay-ms', type=int, default=1500)
    args = parser.parse_args(argv)
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        source = EventSource(args.message_dir, emit, control_dir=args.control_dir,
                             debounce_ms=args.debounce_ms, max_delay_ms=args.max_delay_ms)
        signal.signal(signal.SIGTERM, lambda *_: source.stop('SIGTERM'))
        signal.signal(signal.SIGINT, lambda *_: source.stop('SIGINT'))
        source.run()
        return 0
    except BrokenPipeError:
        # Avoid a second flush traceback after the downstream consumer exits.
        sys.stdout = open(os.devnull, 'w')
        return 0
    except (OSError, ValueError) as error:
        emit({'schema': 1, 'source': 'filesystem_write', 'kind': 'error',
              'code': errno.errorcode.get(error.errno, 'watch_failed') if isinstance(error, OSError) else str(error)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
