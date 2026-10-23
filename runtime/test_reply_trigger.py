"""Fixture-only tests: no native reader, subprocess, service, or WeChat send."""
import copy
import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reply_trigger import Trigger, key

PEER = 'wxid_fixture_peer'

DB0 = 'message/message_0.db'
DB1 = 'message/message_1.db'
SELF = 'fixture-self'


def message(local_id, sender=PEER, content='fixture message', kind=1, database=DB0, timestamp=None):
    return dict(database=database, local_id=local_id, sender_username=sender,
                content=content, local_type=kind, create_time=timestamp or local_id,
                server_id=100000 + local_id)


class FakeReader:
    def __init__(self):
        self.rows = {DB0: [message(10, sender=SELF, content='old reply')]}
        self.calls = []
        self.fail_database = None
        self.overflow_database = None
        self.scan_overflow_database = None
        self.integrity = 'ok'
        self.missing = []
        self.account = 'fixture-account'

    def read_messages(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        assert kwargs['recipient'] == PEER
        db = kwargs.get('database')
        if db and db == self.fail_database:
            raise RuntimeError('fixture shard read interrupted')
        databases = [db] if db else sorted(self.rows)
        rows = [copy.deepcopy(r) for name in databases for r in self.rows[name]]
        after = kwargs.get('after_local_id')
        if after is not None:
            rows = [r for r in rows if r['local_id'] > after]
        rows.sort(key=lambda r: (r['create_time'], r['database'], r['local_id']), reverse=after is None)
        rows = rows[:kwargs['limit']]
        return dict(account=self.account, messages=rows,
                    validation=[{'database': name, 'integrity': self.integrity} for name in databases],
                    missing_key_databases=self.missing,
                    result_limit_reached=bool(db and db == self.overflow_database),
                    scan_limit_reached=bool(db and db == self.scan_overflow_database))

    def add(self, row):
        self.rows.setdefault(row['database'], []).append(row)


class ProcessInterrupted(BaseException):
    pass


class ReplyTriggerTests(unittest.TestCase):
    def test_new_queue_requires_explicit_recipient_before_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / 'must-not-exist'
            with self.assertRaisesRegex(ValueError, 'explicit recipient'):
                Trigger(directory=queue, reader=FakeReader())
            self.assertFalse(queue.exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'queue'
        self.reader = FakeReader()
        self.sent = []
        self.clock = 1000
        self.trigger = Trigger(directory=self.directory, reader=self.reader, recipient=PEER,
                               sender=self.send_fixture, now=lambda: self.clock)

    def send_fixture(self, text, request_id):
        state = self.trigger.load()
        self.assertEqual(state['pending']['inflight'], request_id)
        self.assertEqual(state['pending']['texts'][len(state['pending']['sent'])], text)
        self.sent.append((text, request_id))
        local_id = max(r['local_id'] for r in self.reader.rows[DB0]) + 1
        outgoing = message(local_id, sender=SELF, content=text)
        self.reader.add(outgoing)
        return {'state': 'sent', 'record_key': key(outgoing), 'server_id': outgoing['server_id']}

    def ready(self):
        self.trigger.enable()
        self.reader.add(message(11))
        result = self.trigger.check()
        self.assertEqual(result['status'], 'ready')
        return result['batch_id']

    def legacy_unanswered(self):
        self.reader.rows[DB0] = [message(8), message(9, sender=SELF), message(10), message(11)]
        self.trigger.enable()
        state = self.trigger.load()
        state.pop('startup_catch_up_done', None)
        state['pending'] = None
        self.trigger.save(state)
        return copy.deepcopy(state)

    def test_enable_enqueues_only_unanswered_tail_after_last_self(self):
        self.reader.rows[DB0] = [message(7), message(8, sender=SELF), message(9), message(10)]
        result = self.trigger.enable()
        self.assertEqual(result['startup_pending_count'], 2)
        self.assertEqual(result['cursors'], {DB0: 10})
        state = self.trigger.load()
        self.assertTrue(state['startup_catch_up_done'])
        self.assertEqual([r['local_id'] for r in state['pending']['incoming']], [9, 10])
        self.assertEqual([r['local_id'] for r in self.trigger.check()['incoming']], [9, 10])
        self.assertEqual(self.sent, [])

    def test_enable_with_last_message_from_self_stays_idle(self):
        self.reader.rows[DB0] = [message(7), message(8), message(9, sender=SELF)]
        self.assertEqual(self.trigger.enable()['startup_pending_count'], 0)
        self.assertEqual(self.trigger.check()['status'], 'idle')
        self.assertEqual(self.trigger.catch_up()['status'], 'idle')

    def test_enable_unanswered_tail_across_shards_uses_chronological_order(self):
        self.reader.rows[DB0] = [message(10, sender=SELF, timestamp=100), message(11, timestamp=101)]
        self.reader.rows[DB1] = [message(1, database=DB1, timestamp=102)]
        self.assertEqual(self.trigger.enable()['startup_pending_count'], 2)
        pending = self.trigger.check()
        self.assertEqual([key(r) for r in pending['incoming']], [DB0 + ':11', DB1 + ':1'])

    def test_legacy_catch_up_is_once_only_without_cursor_rewind(self):
        before = self.legacy_unanswered()
        first = self.trigger.catch_up(); second = self.trigger.catch_up()
        self.assertEqual(first['status'], 'ready')
        self.assertEqual([r['local_id'] for r in first['incoming']], [10, 11])
        self.assertEqual(second['batch_id'], first['batch_id'])
        self.assertEqual(second['incoming'], first['incoming'])
        self.assertEqual(self.trigger.load()['cursors'], before['cursors'])
        self.assertTrue(self.trigger.load()['startup_catch_up_done'])
        self.assertEqual(self.sent, [])

    def test_legacy_catch_up_after_skip_does_not_replay(self):
        self.legacy_unanswered(); first = self.trigger.catch_up()
        self.trigger.skip(first['batch_id'], 'already handled')
        # Legacy event history must also prevent replay when the marker is absent.
        state = self.trigger.load(); state.pop('startup_catch_up_done', None); self.trigger.save(state)
        self.assertEqual(self.trigger.catch_up()['status'], 'idle')
        self.assertEqual(self.trigger.catch_up()['status'], 'idle')
        self.assertEqual(self.sent, [])

    def test_legacy_catch_up_after_sent_does_not_replay(self):
        self.legacy_unanswered(); first = self.trigger.catch_up()
        self.assertEqual(self.trigger.reply(first['batch_id'], ['answer'])['status'], 'sent')
        state = self.trigger.load(); state.pop('startup_catch_up_done', None); self.trigger.save(state)
        self.assertEqual(self.trigger.catch_up()['status'], 'idle')
        self.assertEqual(self.trigger.catch_up()['status'], 'idle')
        self.assertEqual(len(self.sent), 1)

    def test_legacy_catch_up_rechecks_manual_reply_and_does_not_backfill(self):
        self.legacy_unanswered(); self.reader.add(message(12, sender=SELF))
        self.assertEqual(self.trigger.catch_up()['status'], 'idle')
        self.assertIsNone(self.trigger.load()['pending'])
        self.assertEqual(self.trigger.load()['cursors'], {DB0: 12})
        self.assertTrue(self.trigger.load()['startup_catch_up_done'])
        self.assertEqual(self.sent, [])

    def test_legacy_catch_up_preserves_old_unanswered_tail_when_new_peer_arrives(self):
        self.legacy_unanswered(); self.reader.add(message(12))
        result = self.trigger.catch_up()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual([r['local_id'] for r in result['incoming']], [10, 11, 12])
        self.assertEqual(self.trigger.load()['cursors'], {DB0: 12})
        self.assertTrue(self.trigger.load()['startup_catch_up_done'])

    def test_legacy_catch_up_after_manual_reply_only_enqueues_subsequent_peer(self):
        self.legacy_unanswered()
        self.reader.add(message(12, sender=SELF)); self.reader.add(message(13))
        result = self.trigger.catch_up()
        self.assertEqual([r['local_id'] for r in result['incoming']], [13])
        self.assertEqual(self.sent, [])

    def test_enable_baselines_every_shard_without_replaying_history(self):
        self.reader.rows[DB1] = [message(40, sender=SELF, database=DB1)]
        result = self.trigger.enable()
        self.assertEqual(result['startup_pending_count'], 0)
        self.assertEqual(result['cursors'], {DB0: 10, DB1: 40})
        self.assertEqual(self.trigger.check()['status'], 'idle')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.trigger.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.trigger.state_path.stat().st_mode & 0o777, 0o600)

    def test_repeated_enable_does_not_reset_pending_or_generation(self):
        batch = self.ready(); generation = self.trigger.load()['generation']
        self.trigger.enable()
        self.assertEqual(self.trigger.load()['generation'], generation)
        self.assertEqual(self.trigger.check()['batch_id'], batch)

    def test_only_peer_new_message_creates_pending(self):
        self.trigger.enable()
        self.reader.add(message(11, sender=SELF))
        self.assertEqual(self.trigger.check()['status'], 'idle')
        self.reader.add(message(12))
        result = self.trigger.check()
        self.assertEqual([r['local_id'] for r in result['incoming']], [12])
        self.assertEqual(self.sent, [])

    def test_manual_outgoing_clears_pending_and_old_draft_cannot_send(self):
        batch = self.ready()
        self.reader.add(message(12, sender=SELF))
        self.assertEqual(self.trigger.reply(batch, ['old draft'])['status'], 'idle')
        self.assertIsNone(self.trigger.load()['pending'])
        self.assertEqual(self.sent, [])

    def test_self_then_peer_in_same_refresh_retains_only_new_peer(self):
        self.ready()
        self.reader.add(message(12, sender=SELF)); self.reader.add(message(13))
        self.assertEqual([r['local_id'] for r in self.trigger.check()['incoming']], [13])

    def test_unresolved_sender_blocks_without_sending(self):
        batch = self.ready(); self.reader.add(message(12, sender=None))
        result = self.trigger.reply(batch, ['draft'])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'unresolved_sender')
        self.assertEqual(self.sent, [])

    def test_check_is_durable_and_repeatable_after_caller_interrupts(self):
        batch = self.ready()
        restarted = Trigger(self.directory, self.reader, self.send_fixture, lambda: 1001, recipient=PEER)
        result = restarted.check()
        self.assertEqual(result['batch_id'], batch)
        self.assertEqual([r['local_id'] for r in result['incoming']], [11])
        self.assertEqual(result['planned_texts'], None)

    def test_interruption_after_check_state_save_does_not_lose_pending(self):
        self.trigger.enable(); self.reader.add(message(11))
        save = self.trigger.save
        def interrupted(state):
            save(state)
            raise ProcessInterrupted()
        with patch.object(self.trigger, 'save', side_effect=interrupted):
            with self.assertRaises(ProcessInterrupted): self.trigger.check()
        result = self.trigger.check()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual([r['local_id'] for r in result['incoming']], [11])

    def test_new_inbound_supersedes_old_batch_before_first_send(self):
        batch = self.ready(); self.reader.add(message(12))
        result = self.trigger.reply(batch, ['old draft'])
        self.assertEqual(result['status'], 'stale')
        self.assertNotEqual(result['batch_id'], batch)
        self.assertEqual(self.sent, [])
        self.assertEqual([r['local_id'] for r in self.trigger.check()['incoming']], [11, 12])

    def test_duplicate_reply_never_resends(self):
        batch = self.ready()
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'sent')
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'idle')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], 'auto-' + batch + '-1')

    def test_unknown_outcome_is_durable_and_blocks_retries(self):
        batch = self.ready()
        def unknown(text, request_id):
            self.sent.append((text, request_id)); return {'state': 'unknown'}
        self.trigger.sender = unknown
        self.assertEqual(self.trigger.reply(batch, ['answer'])['reason'], 'send_outcome_unknown')
        self.assertEqual(self.trigger.check()['status'], 'blocked')
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'blocked')
        self.assertEqual(len(self.sent), 1)

    def test_sender_exception_is_unknown_and_does_not_retry(self):
        batch = self.ready()
        def failed(text, request_id):
            self.sent.append((text, request_id)); raise TimeoutError('fixture')
        self.trigger.sender = failed
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'blocked')
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'blocked')
        self.assertEqual(len(self.sent), 1)

    def test_interrupted_inflight_blocks_after_restart(self):
        batch = self.ready()
        def interrupted(text, request_id):
            self.sent.append((text, request_id)); raise ProcessInterrupted()
        self.trigger.sender = interrupted
        with self.assertRaises(ProcessInterrupted): self.trigger.reply(batch, ['answer'])
        restarted = Trigger(self.directory, self.reader, self.send_fixture, recipient=PEER)
        result = restarted.check()
        self.assertEqual(result['reason'], 'interrupted_send_unknown')
        self.assertEqual(restarted.reply(batch, ['answer'])['status'], 'blocked')
        self.assertEqual(len(self.sent), 1)

    def test_sent_without_record_identity_still_blocks(self):
        batch = self.ready()
        self.trigger.sender = lambda *args: {'state': 'sent'}
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'blocked')

    def test_disable_then_enable_cannot_bypass_unknown_outcome(self):
        batch = self.ready()
        self.trigger.sender = lambda *args: {'state': 'unknown'}
        self.trigger.reply(batch, ['answer']); self.trigger.disable()
        with self.assertRaisesRegex(RuntimeError, 'Unresolved previous'):
            self.trigger.enable()
        self.assertFalse(self.trigger.active(self.trigger.load()))

    def test_disable_then_enable_cannot_bypass_interrupted_inflight(self):
        batch = self.ready()
        def interrupted(*args): raise ProcessInterrupted()
        self.trigger.sender = interrupted
        with self.assertRaises(ProcessInterrupted): self.trigger.reply(batch, ['answer'])
        self.trigger.disable()
        with self.assertRaisesRegex(RuntimeError, 'Unresolved previous'):
            self.trigger.enable()
        self.assertFalse(self.trigger.active(self.trigger.load()))

    def test_queue_lock_is_held_during_sender_and_inflight_is_durable(self):
        batch = self.ready()
        def locked_sender(text, request_id):
            with (self.directory / 'queue.lock').open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self.send_fixture(text, request_id)
        self.trigger.sender = locked_sender
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'sent')

    def test_disabled_trigger_cannot_send_old_batch(self):
        batch = self.ready(); self.trigger.disable()
        self.assertEqual(self.trigger.reply(batch, ['answer'])['status'], 'disabled')
        self.assertEqual(self.trigger.check()['status'], 'disabled')
        self.assertEqual(self.sent, [])

    def test_stop_between_bubbles_prevents_second_send(self):
        batch = self.ready()
        def send_then_stop(text, request_id):
            result = self.send_fixture(text, request_id); self.trigger.disable(); return result
        self.trigger.sender = send_then_stop
        result = self.trigger.reply(batch, ['one', 'two'])
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual([text for text, _ in self.sent], ['one'])

    def test_new_inbound_between_bubbles_stales_remainder(self):
        batch = self.ready()
        def send_then_incoming(text, request_id):
            result = self.send_fixture(text, request_id)
            self.reader.add(message(13, content='new question')); return result
        self.trigger.sender = send_then_incoming
        result = self.trigger.reply(batch, ['one', 'two'])
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(result['sent_count'], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertNotEqual(self.trigger.check()['batch_id'], batch)

    def test_manual_reply_between_bubbles_cancels_remainder(self):
        batch = self.ready()
        def send_then_manual(text, request_id):
            result = self.send_fixture(text, request_id)
            self.reader.add(message(13, sender=SELF, content='manual takeover')); return result
        self.trigger.sender = send_then_manual
        self.assertEqual(self.trigger.reply(batch, ['one', 'two'])['status'], 'stale')
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.trigger.load()['pending'])

    def test_own_confirmed_bubbles_do_not_invalidate_remaining_plan(self):
        batch = self.ready()
        result = self.trigger.reply(batch, ['one', 'two', 'three'])
        self.assertEqual(result, {'status': 'sent', 'batch_id': batch, 'count': 3})
        self.assertEqual([text for text, _ in self.sent], ['one', 'two', 'three'])
        self.assertEqual(self.trigger.check()['status'], 'idle')

    def test_four_topic_replies_keep_four_original_messages(self):
        batch = self.ready()
        texts = ['第一件事我知道了', '第二件事明天再说\n先不用急', '第三件事可以呀', '第四件事你再讲讲']
        result = self.trigger.reply(batch, texts)
        self.assertEqual(result, {'status': 'sent', 'batch_id': batch, 'count': 4})
        self.assertEqual([text for text, _ in self.sent], texts)
        self.assertEqual(len({request_id for _, request_id in self.sent}), 4)
        self.assertEqual(self.trigger.check()['status'], 'idle')

    def test_twelve_messages_are_supported_without_joining_or_repeating(self):
        batch = self.ready()
        texts = [f'话题 {i}' for i in range(1, 13)]
        self.assertEqual(self.trigger.reply(batch, texts)['count'], 12)
        self.assertEqual([text for text, _ in self.sent], texts)
        self.assertEqual(self.trigger.reply(batch, texts)['status'], 'idle')
        self.assertEqual(len(self.sent), 12)

    def test_persisted_batch_plan_cannot_be_replaced_after_interruption(self):
        batch = self.ready(); save = self.trigger.save
        def stop_after_plan(state):
            save(state)
            if (state['pending'] or {}).get('texts') is not None: raise ProcessInterrupted()
        with patch.object(self.trigger, 'save', side_effect=stop_after_plan):
            with self.assertRaises(ProcessInterrupted): self.trigger.reply(batch, ['original'])
        self.assertEqual(self.trigger.check()['planned_texts'], ['original'])
        with self.assertRaises(ValueError): self.trigger.reply(batch, ['replacement'])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.trigger.reply(batch, ['original'])['status'], 'sent')

    def test_media_context_is_redacted_in_output_and_persisted_state(self):
        self.trigger.enable()
        for local_id, kind in [(11, 3), (12, 34), (13, 47), (14, 49)]:
            self.reader.add(message(local_id, kind=kind, content='<xml aeskey="fixture-secret" cdnurl="fixture-private"/>'))
        result = self.trigger.check()
        self.assertTrue(result['requires_media_review'])
        self.assertEqual([r['text'] for r in result['incoming']], ['[图片]', '[语音]', '[表情]', '[非文字消息]'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertNotIn('fixture-private', self.trigger.state_path.read_text())

    def test_undecoded_text_requires_review(self):
        self.trigger.enable(); self.reader.add(message(11, content=None))
        result = self.trigger.check()
        self.assertTrue(result['requires_media_review'])
        self.assertEqual(self.sent, [])

    def test_shard_overflow_keeps_all_cursors_and_pending_unchanged(self):
        for attr in ('overflow_database', 'scan_overflow_database', 'fail_database'):
            with self.subTest(attr=attr):
                self.reader.rows[DB1] = [message(40, sender=SELF, database=DB1)]
                self.trigger.enable()
                before = self.trigger.load()
                self.reader.add(message(11)); self.reader.add(message(41, database=DB1))
                setattr(self.reader, attr, DB1)
                with self.assertRaises(RuntimeError): self.trigger.check()
                self.assertEqual(self.trigger.load(), before)
                setattr(self.reader, attr, None)
                result = self.trigger.check()
                self.assertEqual(result['status'], 'ready')
                self.assertIn(11, [r['local_id'] for r in result['incoming']])
                self.trigger.disable()
                self.reader.rows = {DB0: [message(10, sender=SELF)]}

    def test_bad_integrity_or_missing_keys_do_not_advance_state(self):
        self.ready(); before = self.trigger.load(); self.reader.add(message(12))
        for attr, value in [('integrity', 'failed'), ('missing', [DB0])]:
            with self.subTest(attr=attr):
                old = getattr(self.reader, attr); setattr(self.reader, attr, value)
                with self.assertRaises(RuntimeError): self.trigger.check()
                self.assertEqual(self.trigger.load(), before)
                setattr(self.reader, attr, old)

    def test_skip_is_durable_and_does_not_send(self):
        batch = self.ready()
        self.assertEqual(self.trigger.skip(batch, 'fixture reason')['status'], 'skipped')
        self.assertEqual(self.trigger.check()['status'], 'idle')
        self.assertEqual(self.sent, [])

    def test_invalid_reply_texts_do_not_send_or_change_pending(self):
        batch = self.ready(); before = self.trigger.load()
        for texts in ([], ['x'] * 13, [''], ['  '], ['x\0y'], [1], ['界' * 1366], 'string'):
            with self.subTest(texts=str(texts)[:40]), self.assertRaises(ValueError):
                self.trigger.reply(batch, texts)
        self.assertEqual(self.trigger.load(), before)
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
