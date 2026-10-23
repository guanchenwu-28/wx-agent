import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import poller


class FakeReader:
    def __init__(self, root):
        self.cache = root / 'cache'
        self.entries = []
        self.data = {}
        self.calls = []
        self.fail_database = None
        self.add_account(root, 'a')

    def add_account(self, root, account):
        storage = root / account / 'db_storage'
        self.entries.append({'account': account, 'db_storage': str(storage)})
        self.add_database(account, 'message/message_0.db')

    def add_database(self, account, database):
        entry = next(e for e in self.entries if e['account'] == account)
        path = Path(entry['db_storage']) / database
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def accounts(self):
        return self.entries

    def read_messages(self, recipient, account, limit, database, after_local_id=None):
        self.calls.append(dict(recipient=recipient, account=account, limit=limit,
                               database=database, after_local_id=after_local_id))
        if database == self.fail_database:
            raise poller.ReaderError('fixture read failure')
        rows = [dict(row) for row in self.data.get((account, recipient, database), [])]
        if after_local_id is not None:
            rows = [row for row in rows if row['local_id'] > after_local_id]
        rows.sort(key=lambda row: row['local_id'], reverse=after_local_id is None)
        return {'account': account, 'messages': rows[:limit],
                'validation': [{'database': database}], 'missing_key_databases': [],
                'scan_limit_reached': len(rows) > limit, 'result_limit_reached': False}

    def rows(self, ids, recipient='filehelper', database='message/message_0.db', account='a'):
        self.data[account, recipient, database] = [dict(database=database, local_id=i, server_id=1000+i,
            local_type=1, create_time=100+i, content=f'fixture-{i}', content_status='text') for i in ids]


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = FakeReader(self.root)
        self.cursor = self.root/'private'/'cursor.json'
        self.mock = patch.object(poller, 'WeChatReader', return_value=self.fake)
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.temp.cleanup()

    def poll(self, **kwargs):
        return poller.poll_messages('filehelper', cursor_path=self.cursor, **kwargs)

    def test_first_baseline_does_not_replay_then_reads_new(self):
        self.fake.rows([1, 2, 3])
        result = self.poll()
        self.assertTrue(result['initialized'])
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['cursor'], {'message/message_0.db': 3})
        self.fake.rows([1, 2, 3, 4])
        result = self.poll()
        self.assertFalse(result['initialized'])
        self.assertEqual([r['local_id'] for r in result['messages']], [4])
        self.assertFalse(result['has_more'])
        self.assertEqual(os.stat(self.cursor).st_mode & 0o777, 0o600)

    def test_from_start_bounded_batches_do_not_skip_or_reset(self):
        self.fake.rows([1, 2, 3, 4, 5])
        result = self.poll(limit=2, from_start=True)
        self.assertEqual([r['local_id'] for r in result['messages']], [1,2])
        self.assertTrue(result['has_more'])
        self.assertEqual([r['local_id'] for r in self.poll(limit=2, from_start=True)['messages']], [3,4])
        self.assertEqual([r['local_id'] for r in self.poll(limit=2)['messages']], [5])
        self.assertEqual(self.poll()['messages'], [])

    def test_shard_cursors_and_total_limit(self):
        second = 'message/message_1.db'
        self.fake.add_database('a', second)
        self.fake.rows([1,2])
        self.fake.rows([1,2,3], database=second)
        first = self.poll(limit=3, from_start=True)
        self.assertEqual([(r['database'],r['local_id']) for r in first['messages']],
                         [('message/message_0.db',1),('message/message_0.db',2),(second,1)])
        self.assertTrue(first['has_more'])
        second_result = self.poll(limit=3)
        self.assertEqual([r['local_id'] for r in second_result['messages']], [2,3])
        self.assertEqual(second_result['cursor'][second], 3)

    def test_account_and_recipient_isolation(self):
        self.fake.rows([5]); self.poll()
        self.fake.rows([9],recipient='other')
        result = poller.poll_messages('other', cursor_path=self.cursor)
        self.assertEqual(result['cursor']['message/message_0.db'],9)
        self.fake.add_account(self.root,'b')
        self.fake.rows([20], account='b')
        result = self.poll(account='b')
        self.assertEqual(result['cursor']['message/message_0.db'],20)
        state = json.loads(self.cursor.read_text())
        self.assertEqual(state['accounts']['a']['filehelper']['message/message_0.db'],5)
        self.assertEqual(state['accounts']['a']['other']['message/message_0.db'],9)
        self.assertEqual([c['recipient'] for c in self.fake.calls],['filehelper','other','filehelper'])

    def test_failure_in_later_shard_does_not_advance_any(self):
        self.fake.rows([1]); self.poll()
        before = self.cursor.read_bytes()
        self.fake.rows([1,2]); self.fake.add_database('a','message/message_1.db')
        self.fake.fail_database = 'message/message_1.db'
        with self.assertRaises(poller.ReaderError): self.poll()
        self.assertEqual(self.cursor.read_bytes(), before)

    def test_first_failure_creates_no_cursor(self):
        self.fake.fail_database = 'message/message_0.db'
        with self.assertRaises(poller.ReaderError): self.poll()
        self.assertFalse(self.cursor.exists())

    def test_unsupported_returns_metadata_and_advances(self):
        self.fake.rows([1])
        row = self.fake.data['a','filehelper','message/message_0.db'][0]
        row.update(content=None,content_status='unsupported_zstd_dictionary')
        result = self.poll(from_start=True)
        self.assertEqual(result['cursor']['message/message_0.db'],1)
        self.assertEqual(result['unsupported'][0]['local_id'],1)
        self.assertEqual(result['messages'][0]['content'],None)
        self.assertEqual(self.poll()['messages'],[])

    def test_new_shard_reads_from_zero(self):
        self.fake.rows([9]); self.poll()
        second = 'message/message_1.db'
        self.fake.add_database('a',second); self.fake.rows([1,2],database=second)
        result = self.poll()
        self.assertEqual([r['local_id'] for r in result['messages']],[1,2])

    def test_atomic_replace_failure_preserves_old_file(self):
        self.fake.rows([1]); self.poll()
        before = self.cursor.read_bytes(); self.fake.rows([1,2])
        with patch.object(poller.os,'replace',side_effect=OSError('fixture')):
            with self.assertRaises(OSError): self.poll()
        self.assertEqual(self.cursor.read_bytes(),before)
        self.assertEqual(list(self.cursor.parent.glob('*.tmp')),[])

    def test_corrupt_cursor_fails_without_reads(self):
        self.cursor.parent.mkdir(); self.cursor.write_text('{broken')
        with self.assertRaises(poller.PollerError): self.poll()
        self.assertEqual(self.fake.calls,[])
        self.assertEqual(self.cursor.read_text(),'{broken')

    def test_lock_held_during_read_and_released(self):
        events=[]
        original=self.fake.read_messages
        def read(**kwargs):
            self.assertEqual(events[-1],poller.fcntl.LOCK_EX)
            return original(**kwargs)
        with patch.object(poller.fcntl,'flock',side_effect=lambda fd, flag: events.append(flag)), patch.object(self.fake,'read_messages',side_effect=read):
            self.poll()
        self.assertEqual(events,[poller.fcntl.LOCK_EX,poller.fcntl.LOCK_UN])

if __name__ == '__main__':
    unittest.main()
