#!/usr/bin/env python3
"""Bounded read-only name lookup in the user's authenticated local contact DB.

Search is restricted to nick_name/remark. No enumeration, member lookup, chat
content lookup, sending, or implicit fuzzy fallback. Database schema inspected
2026-10-15 using PRAGMA table_info on a page-authenticated contact.db clone.
"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile

from reader import (DEFAULT_ROOT, ReaderError, WeChatReader, copy_snapshots,
                    decrypt_snapshot, discover_main_pid)

CONTACT_DATABASE = 'contact/contact.db'


class ContactReader:
    def __init__(self, root=DEFAULT_ROOT, cache=None):
        self.reader = WeChatReader(root=root, cache=cache)

    def search(self, query, fuzzy=False, limit=10, account=None, pid=None):
        if not isinstance(query, str) or not query.strip():
            raise ReaderError('An explicit nonempty contact name is required')
        if len(query) > 128 or '\x00' in query:
            raise ReaderError('Contact name must be at most 128 characters without NUL')
        if not isinstance(fuzzy, bool):
            raise ReaderError('fuzzy must be boolean')
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ReaderError('Contact result limit must be 1..10')
        selected = self.reader._account(account)
        cache = self.reader.cache
        source_root = Path(selected['db_storage'])
        source = source_root / CONTACT_DATABASE
        if not source.is_file() or source.is_symlink():
            raise ReaderError('Account contact database was not found')
        try:
            info = json.loads((cache / 'verified-dbkeys.json').read_text())[CONTACT_DATABASE]
            key = bytes.fromhex(info['key'])
            if len(key) != 32:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise ReaderError('Verified contact database key is unavailable or invalid') from None
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix='wechat-contacts-', dir=cache) as directory:
            temporary = Path(directory)
            snapshot = copy_snapshots(source_root, [CONTACT_DATABASE], temporary,
                                      pid if pid is not None else discover_main_pid())
            plaintext = temporary / 'contact.plain.db'
            validation = decrypt_snapshot(temporary / CONTACT_DATABASE, plaintext, key)
            with contextlib.closing(sqlite3.connect(plaintext.as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
                con.execute('PRAGMA query_only=ON')
                if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ReaderError('Contact database integrity check failed')
                columns = {row[1] for row in con.execute('PRAGMA table_info("contact")')}
                if not {'username', 'nick_name', 'remark', 'local_type'}.issubset(columns):
                    raise ReaderError('Unsupported contact database schema')
                if fuzzy:
                    # instr treats %, _, quotes and backslashes literally. The
                    # caller must explicitly request this case-sensitive mode.
                    where = 'instr(COALESCE(nick_name,\'\'), ?) > 0 OR instr(COALESCE(remark,\'\'), ?) > 0'
                else:
                    where = 'nick_name COLLATE BINARY = ? OR remark COLLATE BINARY = ?'
                rows = con.execute('SELECT username, nick_name, remark, local_type '
                                   'FROM contact WHERE (' + where + ') '
                                   'ORDER BY username COLLATE BINARY LIMIT ?',
                                   (query, query, limit + 1)).fetchall()
            contacts = []
            for wxid, nickname, remark, local_type in rows[:limit]:
                is_group = isinstance(wxid, str) and wxid.endswith('@chatroom')
                contacts.append({'wxid': wxid, 'nickname': nickname, 'remark': remark,
                                 'display_name': remark or nickname or wxid,
                                 'type': 'group' if is_group else 'contact', 'is_group': is_group,
                                 'local_type': local_type})
            validation.update(database=CONTACT_DATABASE, integrity='ok')
        return {'query': query, 'match_mode': 'literal_substring' if fuzzy else 'exact_name',
                'account': selected['account'], 'contacts': contacts, 'has_more': len(rows) > limit,
                'validation': validation, 'snapshot': snapshot, 'temporary_plaintext_removed': True}


def search_contacts(query, fuzzy=False, limit=10, account=None, pid=None):
    return ContactReader().search(query=query, fuzzy=fuzzy, limit=limit, account=account, pid=pid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', required=True, help='Explicit nickname or remark to find')
    parser.add_argument('--fuzzy', action='store_true', help='Explicit literal substring search; no wildcard expansion')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--account')
    args = parser.parse_args(argv)
    result = search_contacts(args.query, fuzzy=args.fuzzy, limit=args.limit, account=args.account)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ReaderError, OSError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
