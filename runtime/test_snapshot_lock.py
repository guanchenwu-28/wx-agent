"""Cross-process snapshot locking with fake processes/signals and fixture files.

These tests NEVER suspend or otherwise signal a real WeChat process.
"""
import fcntl
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import reader


def snapshot_worker(root, name, events, gate=None, fail=False, valid=True, pid=123456789):
    root = Path(root)
    stopped = False
    def log(operation, **extra):
        events.put({'worker': name, 'operation': operation, 'at': time.monotonic_ns(), **extra})
    def check_output(command, **_):
        if command[-1] == 'comm=':
            log('validate')
            return '/Applications/微信.app/Contents/MacOS/WeChat' if valid else '/unrelated/process'
        if command[-1] == 'stat=':
            return 'T' if stopped else 'R'
        raise AssertionError(command)
    def kill(actual_pid, action):
        nonlocal stopped
        if actual_pid != pid:
            raise AssertionError('Wrong fake PID')
        log('stop' if action == signal.SIGSTOP else 'resume')
        stopped = action == signal.SIGSTOP
    def clone(command, **_):
        log('clone')
        if gate is not None and not gate.wait(5):
            raise RuntimeError('fixture clone gate timed out')
        if fail:
            raise subprocess.CalledProcessError(1, ['/bin/cp', '-c'])
        Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
    log('attempt')
    try:
        with patch.object(reader.subprocess, 'check_output', side_effect=check_output), \
             patch.object(reader.subprocess, 'run', side_effect=clone), \
             patch.object(reader.os, 'kill', side_effect=kill):
            result = reader.copy_snapshots(root / 'db', ['message/message_0.db'],
                root / name, pid, lock_dir=root / 'locks')
        log('done', state='ok', main_pid=result['main_pid'])
    except BaseException as error:
        log('done', state=type(error).__name__)


class SnapshotLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wechat-snapshot-lock-fixture-')
        self.root = Path(self.temp.name)
        (self.root / 'db/message').mkdir(parents=True)
        (self.root / 'db/message/message_0.db').write_bytes(b'fixture only')
        self.context = multiprocessing.get_context('spawn')
        self.events = self.context.Queue()
        self.processes = []
        self.gates = []
        self.log = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for process in self.processes:
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail('fixture process did not exit')
            self.assertEqual(process.exitcode, 0)
        self.events.close()
        self.events.join_thread()
        self.temp.cleanup()

    def spawn(self, name, **options):
        process = self.context.Process(target=snapshot_worker, args=(str(self.root), name, self.events), kwargs=options)
        self.processes.append(process)
        process.start()
        return process

    def wait_for(self, worker, operation):
        deadline = time.monotonic() + 5
        while True:
            for entry in self.log:
                if entry['worker'] == worker and entry['operation'] == operation:
                    return entry
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, self.log)
            self.log.append(self.events.get(timeout=remaining))

    def run_overlap(self, fail=False):
        gate = self.context.Event()
        self.gates.append(gate)
        self.spawn('first', gate=gate, fail=fail)
        self.wait_for('first', 'clone')
        self.spawn('second')
        self.wait_for('second', 'attempt')
        # The second process has entered copy_snapshots but must not validate,
        # suspend, or resume the PID while the first process is cloning.
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=0.15)
        gate.set()
        first = self.wait_for('first', 'done')
        second = self.wait_for('second', 'done')
        resume = self.wait_for('first', 'resume')
        validate = self.wait_for('second', 'validate')
        self.assertLess(resume['at'], validate['at'])
        self.assertEqual(second['state'], 'ok')
        return first

    def test_same_pid_cross_process_snapshots_serialize_until_resume(self):
        first = self.run_overlap()
        self.assertEqual(first['state'], 'ok')
        self.assertEqual((self.root / 'first/message/message_0.db').read_bytes(), b'fixture only')
        self.assertEqual((self.root / 'second/message/message_0.db').read_bytes(), b'fixture only')
        self.assertEqual((self.root / 'locks').stat().st_mode & 0o777, 0o700)
        lock = self.root / 'locks/snapshot-main-123456789.lock'
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((self.root / 'db').rglob('*.lock')), [])

    def test_clone_failure_resumes_and_unlocks_for_next_process(self):
        first = self.run_overlap(fail=True)
        self.assertEqual(first['state'], 'CalledProcessError')
        self.assertEqual(sum(entry['worker'] == 'first' and entry['operation'] == 'resume' for entry in self.log), 1)

    def test_pid_is_revalidated_after_lock_wait_and_never_signaled_if_invalid(self):
        (self.root / 'locks').mkdir(mode=0o700)
        fd = os.open(self.root / 'locks/snapshot-main-123456789.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.spawn('invalid-after-wait', valid=False)
            self.wait_for('invalid-after-wait', 'attempt')
            with self.assertRaises(queue.Empty):
                self.events.get(timeout=0.15)
            fcntl.flock(fd, fcntl.LOCK_UN)
            done = self.wait_for('invalid-after-wait', 'done')
            self.assertEqual(done['state'], 'ReaderError')
            self.assertTrue(any(entry['operation'] == 'validate' for entry in self.log))
            self.assertFalse(any(entry['operation'] in ('stop', 'resume', 'clone') for entry in self.log))
            # Failed validation also releases the lock for subsequent readers.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_distinct_main_pids_do_not_block_each_other(self):
        gate = self.context.Event()
        self.gates.append(gate)
        self.spawn('first', gate=gate, pid=123456789)
        self.wait_for('first', 'clone')
        self.spawn('different-pid', pid=123456790)
        self.assertEqual(self.wait_for('different-pid', 'done')['state'], 'ok')
        self.assertFalse(any(entry['worker'] == 'first' and entry['operation'] == 'resume' for entry in self.log))
        gate.set()
        self.wait_for('first', 'done')


if __name__ == '__main__':
    unittest.main()
