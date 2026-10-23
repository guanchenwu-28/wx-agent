"""Short-lived local socket client for the lazily started native transport.

The broker has no launchd job and exits after one idle minute. Each request gets
one connection; a request whose write has started is never automatically replayed.
"""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import time

ROOT = Path(__file__).resolve().parent
from runtime_config import NODE as CONFIGURED_NODE, environment
NODE = Path(CONFIGURED_NODE)
SOCKET_ROOT = Path('/private/tmp')
MAX_FRAME = 2 * 1024 * 1024
IDLE_MS = 60000
SEND_ACTIONS = frozenset(('send', 'send-image', 'send-voice'))
ABSENT_ERRORS = frozenset((errno.ENOENT, errno.ECONNREFUSED))


def _deadline(timeout):
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a finite positive number')
    return time.monotonic() + timeout


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Native transport request timed out')
    return remaining


def _owned(path, kind):
    info = path.lstat()
    if info.st_uid != os.getuid() or not kind(info.st_mode):
        raise RuntimeError('Unsafe native transport path: ' + str(path))
    return info


def _paths(runtime=None, *, create=False):
    runtime = Path(runtime or ROOT).resolve()
    key = hashlib.sha256(os.fsencode(str(runtime))).hexdigest()[:16]
    directory = SOCKET_ROOT / ('wechat-native-' + str(os.getuid()) + '-' + key)
    if create:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        info = _owned(directory, stat.S_ISDIR)
        if create and stat.S_IMODE(info.st_mode) != 0o700:
            directory.chmod(0o700)
        elif not create and stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('Native transport directory must have mode 0700')
    return runtime, directory, directory / 'transport.sock'


def _connect(path, deadline):
    try:
        info = _owned(path, stat.S_ISSOCK)
    except FileNotFoundError:
        return None
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('Native transport socket must have mode 0600')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(min(2.0, _remaining(deadline)))
        connection.connect(str(path))
        return connection
    except OSError as error:
        connection.close()
        if error.errno in ABSENT_ERRORS:
            return None
        raise


@contextmanager
def _startup_lock(directory, deadline):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(directory / 'startup.lock', flags, 0o600)
    locked = False
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('Unsafe native transport startup lock')
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                time.sleep(min(0.025, _remaining(deadline)))
        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _connection(runtime, directory, path, deadline):
    connection = _connect(path, deadline)
    if connection is not None:
        return connection
    with _startup_lock(directory, deadline):
        connection = _connect(path, deadline)
        if connection is not None:
            return connection
        # Only an owned socket confirmed absent/refused under the startup lock
        # may be removed. A connected or unresponsive server is never replaced.
        try:
            _owned(path, stat.S_ISSOCK)
            path.unlink()
        except FileNotFoundError:
            pass
        child = subprocess.Popen(
            [str(NODE), str(runtime / 'transport.mjs'), '--serve', '--socket',
             str(path), '--idle-ms', str(IDLE_MS)],
            cwd=str(runtime), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            env={**os.environ, **environment()})
        startup_deadline = min(deadline, time.monotonic() + 15)
        while True:
            connection = _connect(path, startup_deadline)
            if connection is not None:
                return connection
            if child.poll() is not None:
                raise RuntimeError('Native transport broker exited during startup')
            time.sleep(min(0.025, _remaining(startup_deadline)))


def _frame(payload):
    if not isinstance(payload, dict):
        raise ValueError('Native transport request must be an object')
    frame = json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                       allow_nan=False).encode('utf-8') + b'\n'
    if len(frame) > MAX_FRAME:
        raise ValueError('Native transport request exceeds 2 MiB')
    return frame


def _exchange(connection, frame, deadline, writing):
    offset = 0
    while offset < len(frame):
        connection.settimeout(_remaining(deadline))
        # A failed write is deliberately treated as potentially transmitted.
        # Reconnecting here could duplicate a send after an uncertain outcome.
        writing[0] = True
        count = connection.send(frame[offset:])
        if count <= 0:
            raise ConnectionError('Native transport disconnected while receiving request')
        offset += count
    chunks = bytearray()
    while True:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(chunks)))
        if not chunk:
            raise ConnectionError('Native transport disconnected before returning a result')
        chunks.extend(chunk)
        newline = chunks.find(b'\n')
        if newline >= 0:
            if newline + 1 > MAX_FRAME:
                raise ValueError('Native transport response exceeds 2 MiB')
            result = json.loads(chunks[:newline])
            if not isinstance(result, dict):
                raise ValueError('Native transport response must be an object')
            return result
        if len(chunks) >= MAX_FRAME:
            raise ValueError('Native transport response exceeds 2 MiB')


def _failure(error, payload, writing=False):
    uncertain = writing and isinstance(payload, dict) and payload.get('action') in SEND_ACTIONS
    return {'error': str(error), 'state': 'unknown' if uncertain else 'failed',
            'submitted': bool(uncertain), 'transport_error': True}


def request(payload, *, runtime=None, timeout=90):
    """Dispatch once, reusing the runtime's broker and native connection."""
    connection = None
    writing = [False]
    try:
        deadline = _deadline(timeout)
        frame = _frame(payload)
        runtime, directory, path = _paths(runtime, create=True)
        connection = _connection(runtime, directory, path, deadline)
        return _exchange(connection, frame, deadline, writing)
    except Exception as error:
        return _failure(error, payload, writing[0])
    finally:
        if connection is not None:
            connection.close()


def status(*, runtime=None, timeout=2):
    """Inspect only an existing broker; never start one or create its directory."""
    connection = None
    try:
        deadline = _deadline(timeout)
        _, _, path = _paths(runtime)
        connection = _connect(path, deadline)
        if connection is None:
            return {'ok': True, 'running': False}
        result = _exchange(connection, _frame({'action': '_transport_health'}), deadline, [False])
        if result.get('ok') is not True:
            return {**result, 'ok': False, 'running': None}
        return {**result, 'running': True, 'broker_pid': result.get('pid')}
    except Exception as error:
        return {'ok': False, 'running': None, 'error': str(error)}
    finally:
        if connection is not None:
            connection.close()


def stop(*, runtime=None, timeout=5):
    """Ask an existing broker to detach and exit; do not launch or force-kill."""
    connection = None
    try:
        deadline = _deadline(timeout)
        _, _, path = _paths(runtime)
        connection = _connect(path, deadline)
        if connection is None:
            return {'ok': True, 'running': False, 'stopped': True}
        result = _exchange(connection, _frame({'action': '_transport_stop'}), deadline, [False])
        if result.get('ok') is not True or result.get('stopped') is not True:
            return {**result, 'ok': False, 'running': None, 'stopped': False}
        connection.close()
        connection = None
        while path.exists():
            time.sleep(min(0.025, _remaining(deadline)))
        return {**result, 'ok': True, 'running': False, 'stopped': True}
    except Exception as error:
        return {'ok': False, 'running': None, 'stopped': False, 'error': str(error)}
    finally:
        if connection is not None:
            connection.close()
