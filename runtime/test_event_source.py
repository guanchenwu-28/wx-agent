import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from event_source import EventSource


@unittest.skipUnless(hasattr(select, 'kqueue'), 'macOS kqueue fixture tests')
class EventSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wechat-events-fixture-')
        self.root = Path(self.temp.name)
        self.directory = self.root / 'message'
        self.directory.mkdir()
        (self.directory / 'message_0.db').write_bytes(b'synthetic database')
        (self.directory / 'message_0.db-wal').write_bytes(b'synthetic wal')
        self.condition = threading.Condition()
        self.events = []
        self.errors = []
        self.source = self.thread = None

    def tearDown(self):
        if self.source:
            self.source.stop()
            if self.thread:
                self.thread.join(2)
                self.assertFalse(self.thread.is_alive(), 'watcher failed to exit')
            else:
                self.source.close()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def emit(self, value):
        with self.condition:
            self.events.append(value)
            self.condition.notify_all()

    def start(self, **options):
        self.source = EventSource(self.directory, self.emit, debounce_ms=30, max_delay_ms=120, **options)
        def run():
            try:
                self.source.run()
            except BaseException as error:
                self.errors.append(error)
                with self.condition:
                    self.condition.notify_all()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, predicate, timeout=2):
        with self.condition:
            ok = self.condition.wait_for(lambda: any(predicate(event) for event in self.events) or self.errors, timeout)
            self.assertTrue(ok, self.events)
            self.assertEqual(self.errors, [])
            return next(event for event in self.events if predicate(event))

    def append(self, name, data=b'x'):
        with (self.directory / name).open('ab') as output:
            output.write(data)

    def test_ready_and_wal_write_metadata_only(self):
        self.start()
        self.assertEqual(self.events[0]['kind'], 'ready')
        self.assertEqual(self.events[0]['databases'], ['message/message_0.db'])
        self.assertTrue(self.events[0]['requires_cursor_sync'])
        self.append('message_0.db-wal', b'SECRET_BODY_DO_NOT_READ')
        event = self.wait(lambda event: event['kind'] == 'changed')
        self.assertEqual(event['databases'], ['message/message_0.db'])
        self.assertNotIn('SECRET_BODY', json.dumps(self.events))

    def test_duplicate_burst_coalesced(self):
        self.start()
        for _ in range(12):
            self.append('message_0.db-wal')
        self.wait(lambda event: event['kind'] == 'changed')
        time.sleep(0.15)
        self.assertEqual(sum(event['kind'] == 'changed' for event in self.events), 1)

    def test_continuous_writes_flush_by_max_delay(self):
        self.start()
        def write():
            for _ in range(25):
                self.append('message_0.db-wal')
                time.sleep(0.02)
        writer = threading.Thread(target=write)
        writer.start()
        try:
            self.wait(lambda event: event['kind'] == 'changed')
            self.assertTrue(writer.is_alive(), 'continuous writes must not defer a batch until the writer stops')
        finally:
            writer.join(2)

    def test_unrelated_and_shm_ignored(self):
        self.start()
        (self.directory / 'message_0.db-shm').write_bytes(b'locks')
        (self.directory / 'contact.db').write_bytes(b'unrelated')
        time.sleep(0.15)
        self.assertFalse(any(event['kind'] == 'changed' for event in self.events))

    def test_wal_replace_rearms_subsequent_writes(self):
        self.start()
        replacement = self.directory / 'replacement'
        replacement.write_bytes(b'new generation')
        replacement.replace(self.directory / 'message_0.db-wal')
        self.wait(lambda event: event['kind'] == 'changed')
        time.sleep(0.15)
        sequence = self.events[-1]['sequence']
        self.append('message_0.db-wal', b'later write')
        self.wait(lambda event: event['kind'] == 'changed' and event['sequence'] > sequence)

    def test_new_shard_is_watched(self):
        self.start()
        (self.directory / 'message_1.db').write_bytes(b'new shard')
        self.wait(lambda event: event['kind'] == 'changed' and 'message/message_1.db' in event['databases'])
        time.sleep(0.15)
        sequence = self.events[-1]['sequence']
        self.append('message_1.db')
        self.wait(lambda event: event['kind'] == 'changed' and event['sequence'] > sequence)

    def test_directory_replace_reconnects(self):
        self.start()
        self.directory.rename(self.root / 'old_message')
        self.wait(lambda event: event['kind'] == 'unavailable')
        self.directory.mkdir()
        (self.directory / 'message_2.db').write_bytes(b'replacement')
        event = self.wait(lambda event: event['kind'] == 'reconnected')
        self.assertEqual(event['generation'], 2)
        self.assertTrue(event['requires_cursor_sync'])
        self.append('message_2.db')
        self.wait(lambda event: event['kind'] == 'changed' and 'message/message_2.db' in event['databases'])

    def test_missing_directory_recovers_on_creation(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()
        self.start()
        self.assertEqual(self.events[0]['kind'], 'unavailable')
        self.directory.mkdir()
        (self.directory / 'message_0.db').write_bytes(b'new')
        self.wait(lambda event: event['kind'] == 'ready')

    def test_control_in_place_and_atomic_replace(self):
        control = self.root / 'control'
        control.mkdir()
        config = control / 'config.json'
        config.write_text('{}')
        self.start(control_dir=control)
        config.write_text('{"secret":"DO_NOT_LOG"}')
        self.wait(lambda event: event['kind'] == 'config_changed')
        time.sleep(0.15)
        sequence = self.events[-1]['sequence']
        replacement = control / 'newconfig'
        replacement.write_text('{}')
        replacement.replace(config)
        self.wait(lambda event: event['kind'] == 'config_changed' and event['sequence'] > sequence)
        self.assertNotIn('DO_NOT_LOG', json.dumps(self.events))

    def test_other_control_files_do_not_trigger(self):
        control = self.root / 'control'
        control.mkdir()
        (control / 'config.json').write_text('{}')
        self.start(control_dir=control)
        (control / 'daemon.log').write_text('some log')
        (control / 'state.sqlite3').write_bytes(b'some state')
        time.sleep(0.15)
        self.assertFalse(any(event['kind'] == 'config_changed' for event in self.events))

    def test_stop_is_prompt_idempotent_and_suppresses_future_changes(self):
        self.start()
        self.source.stop()
        self.source.stop()
        self.thread.join(1)
        self.assertFalse(self.thread.is_alive())
        self.append('message_0.db')
        time.sleep(0.15)
        self.assertEqual(sum(event['kind'] == 'stopped' for event in self.events), 1)
        self.assertFalse(any(event['kind'] == 'changed' for event in self.events))

    def test_symlink_database_rejected(self):
        (self.root / 'outside').write_text('private')
        (self.directory / 'message_0.db').unlink()
        (self.directory / 'message_0.db').symlink_to(self.root / 'outside')
        with self.assertRaises(OSError):
            self.source = EventSource(self.directory, self.emit)

    def test_cli_sigterm_exits_with_valid_jsonl(self):
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('event_source.py')),
                                  '--message-dir', str(self.directory)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            ready = json.loads(child.stdout.readline())
            self.assertEqual(ready['kind'], 'ready')
            child.send_signal(signal.SIGTERM)
            output, errors = child.communicate(timeout=2)
            self.assertEqual(child.returncode, 0)
            events = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(events[-1]['kind'], 'stopped')
            self.assertEqual(errors, '')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


if __name__ == '__main__':
    unittest.main()
