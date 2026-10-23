"""Read-only, authenticated WeChat SQLCipher4 snapshots for an explicitly selected conversation.

No source DB writes, key acquisition, app launch, or background work. Plaintext
exists only in a private temporary directory during each query, removed finally.
WCDB format: Tencent/wcdb src/common/core/compression/CompressionConst.hpp.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVED = 80
from runtime_config import DATA_ROOT as DEFAULT_ROOT, CACHE, TEST_APP, ORIGINAL_APP
MAIN_EXECUTABLES = {str(app / 'Contents/MacOS/WeChat') for app in (TEST_APP, ORIGINAL_APP)}
TABLE = 'Msg_' + hashlib.md5(b'filehelper').hexdigest()
MAX_DECOMPRESSED = 16 * 1024 * 1024
MAX_MENTION_SOURCE = 256 * 1024
USERNAME_PATTERN = re.compile(r'[A-Za-z0-9_@.\-]{1,128}')


class ReaderError(RuntimeError):
    """Safe error messages never contain key material or unrelated content."""


def mention_metadata(source, *, column_present=True, decode_status='text'):
    """Extract only structured native source metadata, never message text.

    Reject DTDs/entities before using ElementTree so no external resources or
    entity expansions can affect a local read. A valid msgsource with no
    atuserlist is a known empty list; absent/unsupported data stays unknown.
    """
    result = {'mentioned_usernames': [], 'mention_metadata_available': False,
              'mention_metadata_status': 'missing_source_column'}
    if not column_present:
        return result
    if source is None:
        result['mention_metadata_status'] = ('missing_source' if decode_status == 'null'
                                             else decode_status)
        return result
    if not isinstance(source, str) or not source.strip():
        result['mention_metadata_status'] = 'empty_or_unsupported_source'
        return result
    if len(source.encode('utf-8')) > MAX_MENTION_SOURCE:
        result['mention_metadata_status'] = 'source_size_limit'
        return result
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', source, re.IGNORECASE):
        result['mention_metadata_status'] = 'unsupported_xml_declaration'
        return result
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, ValueError):
        result['mention_metadata_status'] = 'invalid_source_xml'
        return result
    if root.tag != 'msgsource':
        result['mention_metadata_status'] = 'unsupported_source_root'
        return result
    usernames = []
    for node in root.findall('atuserlist'):
        if len(node):
            result['mention_metadata_status'] = 'unsupported_atuserlist_structure'
            return result
        for username in (node.text or '').split(','):
            username = username.strip()
            if not username:
                continue
            if USERNAME_PATTERN.fullmatch(username) is None:
                result['mention_metadata_status'] = 'invalid_mentioned_username'
                return result
            if username not in usernames:
                usernames.append(username)
    result.update(mentioned_usernames=usernames, mention_metadata_available=True,
                  mention_metadata_status='parsed_source_xml')
    return result


def checksum(data: bytes, endian: str, state=(0, 0)):
    if len(data) % 8:
        raise ReaderError('WAL checksum input is not aligned')
    a, b = state
    words = struct.unpack(endian + str(len(data) // 4) + 'I', data)
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xffffffff
        b = (b + words[i + 1] + a) & 0xffffffff
    return a, b


def committed_wal_frames(wal: bytes):
    """Recover SQLite's valid prefix, only through the last commit marker.

    Old-generation salts, incomplete frames, or invalid checksums terminate the
    recoverable prefix. They are reported, never applied or called committed.
    """
    stats = {'wal_present': bool(wal), 'wal_valid_frames': 0,
             'committed_wal_frames': 0, 'wal_tail': 'none'}
    if not wal:
        return [], None, stats
    if len(wal) < 32:
        raise ReaderError('WAL header truncated')
    magic, version, page_size = struct.unpack('>III', wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != PAGE_SIZE:
        raise ReaderError('Unsupported WAL header')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(wal[:24], endian)
    if state != struct.unpack('>II', wal[24:32]):
        raise ReaderError('WAL header checksum failed')
    frames, commit, final_size, off = [], 0, None, 32
    while off + 24 + PAGE_SIZE <= len(wal):
        head = wal[off:off + 24]
        data = wal[off + 24:off + 24 + PAGE_SIZE]
        if head[8:16] != wal[16:24]:
            stats['wal_tail'] = 'old_generation'
            break
        expected = checksum(head[:8] + data, endian, state)
        if expected != struct.unpack('>II', head[16:24]):
            stats['wal_tail'] = 'invalid_checksum'
            break
        number, size = struct.unpack('>II', head[:8])
        if number == 0:
            raise ReaderError('Invalid WAL page number')
        state = expected
        frames.append((number, data))
        if size:
            commit, final_size = len(frames), size
        off += 24 + PAGE_SIZE
    if stats['wal_tail'] == 'none' and off != len(wal):
        stats['wal_tail'] = 'incomplete_frame'
    if stats['wal_tail'] == 'none' and len(frames) > commit:
        stats['wal_tail'] = 'uncommitted_frames'
    stats.update(wal_valid_frames=len(frames), committed_wal_frames=commit)
    return frames[:commit], final_size, stats


def decrypt_snapshot(source: Path, destination: Path, key: bytes):
    """Authenticate every main DB page and every committed WAL frame, then write."""
    raw = source.read_bytes()
    if len(key) != 32:
        raise ReaderError('Cached database key has invalid length')
    if not raw or len(raw) % PAGE_SIZE:
        raise ReaderError('Encrypted database is empty or has a partial page')
    salt = raw[:16]
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in salt), 2, 32)

    def page(data, number):
        start = 16 if number == 1 else 0
        mac = hmac.new(mac_key, data[start:-64] + struct.pack('<I', number), hashlib.sha512).digest()
        if not hmac.compare_digest(mac, data[-64:]):
            raise ReaderError(f'SQLCipher page authentication failed (page {number})')
        dec = Cipher(algorithms.AES(key), modes.CBC(data[-80:-64])).decryptor()
        clear = (b'SQLite format 3\x00' if number == 1 else b'') + dec.update(data[start:-80]) + dec.finalize() + bytes(80)
        if number == 1:
            if clear[16:18] != b'\x10\x00' or clear[20] != RESERVED:
                raise ReaderError('Unsupported SQLCipher page layout')
        return clear

    wal_path = Path(str(source) + '-wal')
    frames, final_size, stats = committed_wal_frames(wal_path.read_bytes() if wal_path.exists() else b'')
    try:
        fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w+b') as output:
            for index in range(len(raw) // PAGE_SIZE):
                output.write(page(raw[index * PAGE_SIZE:(index + 1) * PAGE_SIZE], index + 1))
            for number, data in frames:
                clear = page(data, number)  # authenticate even pages truncated by a later commit
                if number <= final_size:
                    output.seek((number - 1) * PAGE_SIZE)
                    output.write(clear)
            if final_size is not None:
                if final_size * PAGE_SIZE > output.seek(0, os.SEEK_END):
                    raise ReaderError('Committed WAL leaves missing database pages')
                output.truncate(final_size * PAGE_SIZE)
        stats.update(database_pages=len(raw) // PAGE_SIZE, page_hmac_verified=True)
        return stats
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def discover_main_pid():
    output = subprocess.check_output(['/bin/ps', '-axo', 'pid=,comm='], text=True)
    matches = [int(parts[0]) for line in output.splitlines()
               if len(parts := line.strip().split(None, 1)) == 2 and parts[1] in MAIN_EXECUTABLES]
    if len(matches) != 1:
        raise ReaderError('Expected exactly one running WeChat main process')
    return matches[0]


@contextlib.contextmanager
def stopped_main(pid):
    command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'comm='], text=True).strip()
    status = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'stat='], text=True).strip()
    if command not in MAIN_EXECUTABLES or not status or 'T' in status:
        raise ReaderError('Snapshot requires a running, verified WeChat main process')
    stopped = False
    try:
        os.kill(pid, signal.SIGSTOP)
        stopped = True
        # Wait for delivery before copying; SIGSTOP is asynchronous.
        for _ in range(100):
            status = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'stat='], text=True).strip()
            if 'T' in status:
                break
            time.sleep(0.001)
        else:
            raise ReaderError('WeChat did not suspend for the snapshot')
        yield
    finally:
        if stopped:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass


def copy_snapshots(db_root, relative_paths, destination, pid, *, lock_dir=None):
    """Serialize one main PID's suspend/clone/resume across all reader processes.

    Revalidate the process only after obtaining the lock: it may have exited or
    changed while another reader held the snapshot lock. Decryption is outside
    this function and never holds the process suspension or snapshot lock.
    """
    import fcntl

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ReaderError('Snapshot requires a positive main process ID')
    lock_root = Path(lock_dir) if lock_dir is not None else CACHE
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_root.chmod(0o700)
    # Keep the lock inode stable: never unlink it after releasing flock.
    lock_fd = os.open(lock_root / f'snapshot-main-{pid}.lock',
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    started = time.monotonic()
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with stopped_main(pid):
                for relative in relative_paths:
                    source, target = db_root / relative, destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    for suffix in ('', '-wal'):
                        source_file, target_file = Path(str(source) + suffix), Path(str(target) + suffix)
                        if source_file.exists():
                            subprocess.run(['/bin/cp', '-c', str(source_file), str(target_file)], check=True,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                            target_file.chmod(0o600)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
    return {'snapshot_method': 'main_SIGSTOP_APFS_clone_finally_SIGCONT',
            'snapshot_seconds': round(time.monotonic() - started, 4), 'main_pid': pid}


class ZstdDecoder:
    def __init__(self):
        self.lib = None
        self.python_zstd = None
        try:
            from compression import zstd
            self.python_zstd = zstd
        except ImportError:
            pass
        names = [ctypes.util.find_library('zstd'), '/opt/homebrew/lib/libzstd.dylib', '/usr/local/lib/libzstd.dylib']
        for name in filter(None, names):
            try:
                lib = ctypes.CDLL(name)
                lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
                lib.ZSTD_getDictID_fromFrame.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                lib.ZSTD_getDictID_fromFrame.restype = ctypes.c_uint
                lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
                lib.ZSTD_decompress.restype = ctypes.c_size_t
                lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                lib.ZSTD_isError.restype = ctypes.c_uint
                self.lib = lib
                break
            except (OSError, AttributeError):
                pass

    def decode(self, value, compression_type=None):
        if value is None:
            return None, 'null'
        kind = 0 if compression_type is None else int(compression_type) >> 1
        if kind == 1:
            return None, 'unsupported_zstd_dictionary'
        if kind not in (0, 2):
            return None, 'unsupported_compression_type'
        if isinstance(value, str):
            return (value, 'text') if kind == 0 else (None, 'invalid_compressed_storage')
        if not isinstance(value, bytes):
            return None, 'unsupported_content_type'
        compressed = kind == 2 or value.startswith(b'\x28\xb5\x2f\xfd')
        if compressed:
            if self.lib is None:
                if self.python_zstd is None:
                    return None, 'unsupported_zstd_unavailable'
                try:
                    info = self.python_zstd.get_frame_info(value)
                    if info.dictionary_id:
                        return None, 'unsupported_zstd_dictionary'
                    if info.decompressed_size is None or info.decompressed_size > MAX_DECOMPRESSED:
                        return None, 'unsupported_zstd_frame_size'
                    decompressor = self.python_zstd.ZstdDecompressor()
                    clear = decompressor.decompress(value, max_length=MAX_DECOMPRESSED + 1)
                    if not decompressor.eof or len(clear) != info.decompressed_size:
                        return None, 'invalid_zstd_content'
                    return clear.decode('utf-8'), 'zstd_text'
                except UnicodeDecodeError:
                    return None, 'unsupported_non_utf8_content'
                except Exception:
                    return None, 'invalid_zstd_content'
            buffer = ctypes.create_string_buffer(value)
            if self.lib.ZSTD_getDictID_fromFrame(buffer, len(value)):
                return None, 'unsupported_zstd_dictionary'
            size = self.lib.ZSTD_getFrameContentSize(buffer, len(value))
            if size >= (1 << 64) - 2 or size > MAX_DECOMPRESSED:
                return None, 'unsupported_zstd_frame_size'
            output = ctypes.create_string_buffer(max(size, 1))
            result = self.lib.ZSTD_decompress(output, size, buffer, len(value))
            if self.lib.ZSTD_isError(result) or result != size:
                return None, 'invalid_zstd_content'
            value = output.raw[:size]
        try:
            return value.decode('utf-8'), 'zstd_text' if compressed else 'utf8_blob'
        except UnicodeDecodeError:
            return None, 'unsupported_non_utf8_content'


class WeChatReader:
    def __init__(self, root=DEFAULT_ROOT, cache=None):
        self.root = Path(root)
        self.cache = Path(cache) if cache is not None else (CACHE if self.root == DEFAULT_ROOT else self.root / 'Library/Caches/REAResearch')
        self.account_root = self.root / 'Documents/xwechat_files'
        self.decoder = ZstdDecoder()

    def accounts(self):
        return [{'account': p.parent.name, 'db_storage': str(p)}
                for p in sorted(self.account_root.glob('*/db_storage')) if p.is_dir() and not p.is_symlink()]

    def _account(self, account):
        entries = self.accounts()
        if account is None:
            if len(entries) != 1:
                raise ReaderError('Select an account explicitly; discovery did not find exactly one')
            return entries[0]
        for entry in entries:
            if account == entry['account']:
                return entry
        raise ReaderError('Account not found within xwechat_files/*/db_storage')

    def resolve_self_from_verified_send(self, verified_send, *, account=None, pid=None):
        """Resolve identity from this tool's already acknowledged outgoing send.

        The caller supplies its existing send journal/receipt, never an incoming
        message. Bind both shard-local ID and server acknowledgement before
        trusting Name2Id; neither account folder names nor delivery status are
        an identity source. The returned evidence contains no message body.
        """
        if not isinstance(verified_send, dict) or verified_send.get('state') != 'sent':
            raise ReaderError('Self identity requires a verified outgoing send receipt')
        result = verified_send.get('result')
        if (not isinstance(result, dict) or not result.get('delivered') or
                result.get('verification') != 'native_database_server_id'):
            raise ReaderError('Self identity requires native server acknowledgement')
        record = result.get('native_record')
        if not isinstance(record, dict):
            raise ReaderError('Verified outgoing send has no native record')
        local_id, server_id = record.get('local_id'), record.get('server_id')
        database, recipient = record.get('database'), verified_send.get('recipient')
        if (type(local_id) is not int or local_id < 1 or type(server_id) is not int or
                server_id < 1 or not isinstance(database, str) or not database):
            raise ReaderError('Verified outgoing send has invalid native identity')
        snapshot = self.read_messages(recipient=recipient, account=account, limit=1,
                                      after_local_id=local_id - 1, database=database, pid=pid)
        rows = snapshot['messages']
        if (len(rows) != 1 or rows[0]['local_id'] != local_id or
                rows[0]['server_id'] != server_id):
            raise ReaderError('Verified outgoing send no longer matches its native record')
        username = rows[0].get('sender_username')
        if not isinstance(username, str) or USERNAME_PATTERN.fullmatch(username) is None:
            raise ReaderError('Verified outgoing sender identity is unavailable')
        return {'self_username': username, 'account': snapshot['account'],
                'verification': 'verified_outgoing_native_record_sender',
                'evidence': {'recipient': recipient, 'database': database,
                             'local_id': local_id, 'server_id': server_id,
                             'request_id': verified_send.get('request_id')}}

    def read_messages(self, recipient='filehelper', account=None, limit=50,
                      after_local_id=None, exact_text=None, scan_limit=5000, pid=None, database=None):
        if not isinstance(recipient, str) or not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', recipient):
            raise ReaderError('recipient must be an explicit WeChat identifier')
        table = 'Msg_' + hashlib.md5(recipient.encode('utf-8')).hexdigest()
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ReaderError('limit must be 1..500')
        if not isinstance(scan_limit, int) or not limit <= scan_limit <= 50000:
            raise ReaderError('scan_limit must be between limit and 50000')
        if after_local_id is not None and (not isinstance(after_local_id, int) or after_local_id < 0):
            raise ReaderError('after_local_id must be a nonnegative integer')
        if exact_text is not None and not isinstance(exact_text, str):
            raise ReaderError('exact_text must be a string')
        selected = self._account(account)
        db_root = Path(selected['db_storage'])
        try:
            keys = json.loads((self.cache / 'verified-dbkeys.json').read_text())
        except (OSError, ValueError):
            raise ReaderError('Verified key cache is unavailable or invalid') from None
        candidates = sorted(str(p.relative_to(db_root)) for p in (db_root / 'message').glob('message_*.db')
                            if re.fullmatch(r'message_\d+\.db', p.name) and not p.is_symlink())
        if database is not None:
            if database not in candidates:
                raise ReaderError('Requested message database is not available')
            candidates = [database]
        available = [r for r in candidates if r in keys]
        missing = [r for r in candidates if r not in keys]
        if not available:
            raise ReaderError('No message databases have verified cached keys')
        if after_local_id is not None and len(available) > 1:
            raise ReaderError('local_id is shard-local; select database for cursor queries')
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache.chmod(0o700)
        messages, validation, unsupported, scanned, capped = [], [], 0, 0, False
        with tempfile.TemporaryDirectory(prefix='wechat-reader-', dir=self.cache) as temp:
            temporary = Path(temp)
            snapshot_stats = copy_snapshots(db_root, available, temporary, pid or discover_main_pid())
            for relative in available:
                try:
                    key = bytes.fromhex(keys[relative]['key'])
                except (ValueError, KeyError, TypeError):
                    raise ReaderError('Invalid verified key cache entry') from None
                plaintext = temporary / (Path(relative).stem + '.plain.db')
                stats = decrypt_snapshot(temporary / relative, plaintext, key)
                with contextlib.closing(sqlite3.connect(plaintext.as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
                    con.execute('PRAGMA query_only=ON')
                    if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ReaderError('Decrypted database integrity check failed')
                    stats['database'] = relative
                    stats['integrity'] = 'ok'
                    validation.append(stats)
                    exists = con.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()
                    if not exists:
                        continue
                    columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
                    required = {'local_id', 'server_id', 'local_type', 'create_time', 'message_content'}
                    if not required.issubset(columns):
                        raise ReaderError('Unsupported conversation message schema')
                    local_status = '"status"' if 'status' in columns else 'NULL'
                    stats['status_column_present'] = 'status' in columns
                    real_sender = 'message."real_sender_id"' if 'real_sender_id' in columns else 'NULL'
                    sender_username = 'NULL'
                    stats['sender_column_present'] = 'real_sender_id' in columns
                    stats['sender_mapping_available'] = False
                    # Actual message DB schema verified 2026-10-16:
                    # Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER),
                    # with the implicit SQLite rowid referenced by real_sender_id.
                    # This correlated lookup touches only each requested message's
                    # sender key; it never enumerates names or reads contacts.
                    if 'real_sender_id' in columns:
                        mapping = con.execute(
                            "SELECT name, sql FROM sqlite_master WHERE type='table' AND lower(name)='name2id'"
                        ).fetchone()
                        if mapping and 'WITHOUT ROWID' not in (mapping[1] or '').upper():
                            mapping_name = mapping[0].replace('"', '""')
                            mapping_columns = {entry[1] for entry in con.execute(
                                f'PRAGMA table_info("{mapping_name}")')}
                            if 'user_name' in mapping_columns:
                                sender_username = (f'(SELECT names."user_name" FROM "{mapping_name}" AS names '
                                                   f'WHERE names.rowid = {real_sender})')
                                stats['sender_mapping_available'] = True
                    compression = '"WCDB_CT_message_content"' if 'WCDB_CT_message_content' in columns else 'NULL'
                    # Verified on the selected group in WeChat 4.1.13:
                    # source TEXT contains <msgsource>; WCDB_CT_source 0/4
                    # means plain text/Zstd respectively. Do not guess other
                    # column names or scrape visible @names from the body.
                    source_present = 'source' in columns
                    source = 'message."source"' if source_present else 'NULL'
                    source_compression = ('message."WCDB_CT_source"'
                                          if 'WCDB_CT_source' in columns else 'NULL')
                    stats['mention_source_column_present'] = source_present
                    where = 'WHERE local_id > ?' if after_local_id is not None else ''
                    params = [after_local_id] if after_local_id is not None else []
                    # Compressed equality must be tested after decoding. Fetch bounded
                    # selected-conversation rows only; report when older rows remain unexamined.
                    bound = scan_limit if exact_text is not None else limit
                    params.append(bound + 1)
                    direction = 'ASC' if after_local_id is not None else 'DESC'
                    rows = con.execute(f'SELECT local_id, server_id, local_type, create_time, message_content, {compression}, {local_status}, {real_sender}, {sender_username}, {source}, {source_compression} '
                                       f'FROM "{table}" AS message {where} ORDER BY local_id {direction} LIMIT ?', params).fetchall()
                    if len(rows) > bound:
                        capped = True
                    for row in rows[:bound]:
                        scanned += 1
                        content, status = self.decoder.decode(row[4], row[5])
                        if content is None and status != 'null':
                            unsupported += 1
                        if exact_text is not None and content != exact_text:
                            continue
                        try:
                            source_text, source_status = self.decoder.decode(row[9], row[10])
                        except (TypeError, ValueError, OverflowError):
                            source_text, source_status = None, 'unsupported_source_compression'
                        mentions = mention_metadata(source_text, column_present=source_present,
                                                    decode_status=source_status)
                        messages.append(dict(database=relative, local_id=row[0], server_id=row[1],
                                             local_type=row[2], create_time=row[3], content=content,
                                             content_status=status, status=row[6],
                                             real_sender_id=row[7], sender_username=row[8], **mentions))
                plaintext.unlink()
        messages.sort(key=lambda m: (m['create_time'], m['local_id']), reverse=True)
        result_truncated = len(messages) > limit
        return {'recipient': recipient, 'account': selected['account'], 'messages': messages[:limit],
                'validation': validation, 'snapshot': snapshot_stats, 'scanned_rows': scanned,
                'unsupported_content_rows': unsupported, 'missing_key_databases': missing,
                'scan_limit_reached': capped, 'result_limit_reached': result_truncated,
                'complete': not (capped or result_truncated or missing or unsupported),
                'temporary_plaintext_removed': True}
