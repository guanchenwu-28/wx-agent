"""Ten-second conversation batching with an injected clock and fixture I/O only."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autoreply
from autoreply import Coordinator
from reply_trigger import Trigger, key


SELF = 'fixture_self'
ALICE = 'wxid_alice'
BOB = 'wxid_bob'
DB = 'message/message_0.db'
ACCOUNT = 'fixture_account'


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def at(self, elapsed):
        self.value = 1000.0 + elapsed


class FakeReader:
    def __init__(self):
        self.rows = {}
        self.calls = []

    def add(self, recipient, sender, text='fixture message'):
        rows = self.rows.setdefault(recipient, [])
        local_id = max([row['local_id'] for row in rows] or [0]) + 1
        # Deliberately unrelated to wall clock: quiet time starts when newly
        # observed incoming messages are merged, not from old server timestamps.
        row = {'database': DB, 'local_id': local_id, 'server_id': 10000 + local_id,
               'create_time': 90000 + local_id, 'sender_username': sender,
               'content': text, 'local_type': 1, 'mentioned_usernames': [],
               'mention_metadata_available': True}
        rows.append(row)
        return row

    def read_messages(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs.get('account', ACCOUNT) != ACCOUNT:
            raise AssertionError('unexpected account')
        if kwargs.get('database', DB) != DB:
            raise AssertionError('unexpected database')
        rows = copy.deepcopy(self.rows[kwargs['recipient']])
        after = kwargs.get('after_local_id')
        if after is not None:
            rows = [row for row in rows if row['local_id'] > after]
        rows.sort(key=lambda row: row['local_id'], reverse=after is None)
        return {'account': ACCOUNT, 'messages': rows[:kwargs['limit']],
                'validation': [{'database': DB, 'integrity': 'ok'}],
                'missing_key_databases': [], 'scan_limit_reached': False,
                'result_limit_reached': False}


class FakeRegistry:
    """Only coordinator wiring; Trigger itself is the production implementation."""
    def __init__(self, home, triggers):
        self.home = home
        self.triggers = triggers
        self.config = {
            'enabled': True, 'self_username': SELF, 'targets': {
                recipient: {'name': recipient, 'enabled': True, 'kind': 'contact',
                            'condition': {'mode': 'all'}, 'style': 'fixture style',
                            'quiet_seconds': 10, 'revision': 'fixture-revision'}
                for recipient in triggers}}

    def load(self):
        return copy.deepcopy(self.config)

    def trigger(self, recipient, target=None, sender=None):
        return self.triggers[recipient]


class QuietBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.clock = FakeClock()
        self.reader = FakeReader()
        self.sent = []
        self.triggers = {}

    def make_trigger(self, recipient=ALICE):
        if recipient not in self.reader.rows:
            self.reader.add(recipient, SELF, 'already answered')
        trigger = Trigger(self.home / recipient, reader=self.reader,
                          sender=lambda text, request_id: self.send(recipient, text, request_id),
                          now=self.clock, recipient=recipient, self_username=SELF,
                          condition={'mode': 'all'}, quiet_seconds=10)
        if not trigger.state_path.exists():
            trigger.enable()
        self.triggers[recipient] = trigger
        return trigger

    def send(self, recipient, text, request_id):
        self.sent.append({'recipient': recipient, 'text': text, 'request_id': request_id,
                          'at': self.clock()})
        row = self.reader.add(recipient, SELF, text)
        return {'state': 'sent', 'record_key': key(row), 'server_id': row['server_id']}

    def incoming(self, trigger, elapsed=0, text='new incoming'):
        self.clock.at(elapsed)
        self.reader.add(trigger.recipient, trigger.recipient, text)
        return trigger.check()

    def assert_waiting(self, result, deadline, remaining=None):
        self.assertEqual(result['status'], 'waiting')
        self.assertAlmostEqual(result['ready_at'], 1000 + deadline)
        self.assertAlmostEqual(result['retry_after'],
                               1000 + deadline - self.clock() if remaining is None else remaining)

    def test_no_reply_at_zero_or_nine_point_nine_ready_at_ten(self):
        trigger = self.make_trigger()
        self.assert_waiting(self.incoming(trigger), 10, 10)
        self.clock.at(9.9)
        self.assert_waiting(trigger.check(), 10, .1)
        self.assertEqual(self.sent, [])
        self.clock.at(10)
        batch = trigger.check()
        self.assertEqual(batch['status'], 'ready')
        self.assertEqual(trigger.reply(batch['batch_id'], ['one batch reply'])['status'], 'sent')
        self.assertEqual([row['at'] for row in self.sent], [1010])

    def test_production_registry_configure_defaults_to_ten_seconds(self):
        self.reader.add(ALICE, SELF, 'already answered')
        registry = autoreply.Registry(self.home / 'registry', reader=self.reader)
        registry.init(ACCOUNT, SELF)
        def constructor(*args, **kwargs):
            kwargs['now'] = self.clock
            kwargs['sender'] = lambda text, rid: self.send(kwargs['recipient'], text, rid)
            return Trigger(*args, **kwargs)
        with patch.object(autoreply, 'Trigger', side_effect=constructor):
            target = registry.configure(ALICE, 'Alice', {'mode': 'all'}, enabled=True)
            self.assertEqual(target['quiet_seconds'], 10)
            # Existing registries without the new field inherit the same
            # production window instead of silently falling back to zero.
            config = registry.load()
            del config['targets'][ALICE]['quiet_seconds']
            registry.save(config)
            trigger = registry.trigger(ALICE)
            self.assertEqual(trigger.quiet_seconds, 10)
            self.assert_waiting(self.incoming(trigger), 10, 10)
            self.clock.at(9.9)
            self.assert_waiting(trigger.check(), 10, .1)
            self.clock.at(10)
            self.assertEqual(trigger.check()['status'], 'ready')

    def test_message_at_nine_moves_deadline_to_nineteen_and_merges_batch(self):
        trigger = self.make_trigger()
        self.incoming(trigger, 0, 'part one')
        self.assert_waiting(self.incoming(trigger, 9, 'part two'), 19, 10)
        self.clock.at(10)
        self.assert_waiting(trigger.check(), 19, 9)
        self.clock.at(18.999)
        self.assert_waiting(trigger.check(), 19, .001)
        self.clock.at(19)
        batch = trigger.check()
        self.assertEqual(batch['status'], 'ready')
        self.assertEqual([row['text'] for row in batch['incoming']], ['part one', 'part two'])
        trigger.reply(batch['batch_id'], ['answer both'])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['at'], 1019)

    def test_continuous_messages_for_forty_seconds_have_no_maximum_forced_send(self):
        trigger = self.make_trigger()
        for elapsed in range(0, 41, 5):
            with self.subTest(elapsed=elapsed):
                result = self.incoming(trigger, elapsed, f'part {elapsed}')
                self.assert_waiting(result, elapsed + 10, 10)
                pending = trigger.load()['pending']
                self.assertEqual(trigger.reply(pending['batch_id'], ['too soon'])['status'], 'waiting')
                self.assertEqual(self.sent, [])
        self.clock.at(49.9)
        self.assert_waiting(trigger.check(), 50, .1)
        self.clock.at(50)
        batch = trigger.check()
        self.assertEqual(batch['status'], 'ready')
        self.assertEqual(len(batch['incoming']), 9)

    def test_repeated_database_events_do_not_extend_quiet_deadline(self):
        trigger = self.make_trigger()
        self.incoming(trigger)
        for elapsed in (1, 2, 5, 8, 9.9):
            self.clock.at(elapsed)
            self.assert_waiting(trigger.check(), 10)
        self.clock.at(10)
        self.assertEqual(trigger.check()['status'], 'ready')
        self.assertEqual(self.sent, [])

    def test_self_reply_cancels_pending_instead_of_starting_another_wait(self):
        trigger = self.make_trigger()
        self.incoming(trigger)
        self.clock.at(7)
        self.reader.add(ALICE, SELF, 'handled personally')
        self.assertEqual(trigger.check()['status'], 'idle')
        self.assertIsNone(trigger.load()['pending'])
        self.clock.at(20)
        self.assertEqual(trigger.check()['status'], 'idle')
        self.assert_waiting(self.incoming(trigger, 21, 'new turn'), 31)
        self.assertEqual(self.sent, [])

    def test_two_conversation_quiet_clocks_are_independent(self):
        alice = self.make_trigger(ALICE); bob = self.make_trigger(BOB)
        self.assert_waiting(self.incoming(alice, 0), 10)
        self.assert_waiting(self.incoming(bob, 6), 16)
        self.assert_waiting(alice.check(), 10, 4)
        self.assert_waiting(self.incoming(bob, 9), 19)
        self.clock.at(10)
        self.assertEqual(alice.check()['status'], 'ready')
        self.assert_waiting(bob.check(), 19, 9)
        self.clock.at(19)
        self.assertEqual(bob.check()['status'], 'ready')
        self.assertEqual(self.sent, [])

    def test_restart_preserves_existing_deadline_and_batch(self):
        trigger = self.make_trigger()
        self.incoming(trigger, 0, 'persist this turn')
        before = copy.deepcopy(trigger.load()['pending'])
        self.clock.at(9)
        restarted = self.make_trigger()
        self.assert_waiting(restarted.check(), 10, 1)
        self.assertEqual(restarted.load()['pending'], before)
        self.clock.at(10)
        self.assertEqual(restarted.check()['batch_id'], before['batch_id'])
        self.assertEqual(self.sent, [])

    def test_restart_new_message_extends_from_new_arrival_only(self):
        trigger = self.make_trigger()
        self.incoming(trigger, 0)
        self.clock.at(5)
        restarted = self.make_trigger()
        self.assert_waiting(restarted.check(), 10, 5)
        self.assert_waiting(self.incoming(restarted, 8), 18, 10)
        self.clock.at(10)
        self.assert_waiting(restarted.check(), 18, 8)

    def test_restart_after_persisted_quiet_deadline_does_not_start_new_window(self):
        trigger = self.make_trigger()
        self.incoming(trigger, 0)
        before = copy.deepcopy(trigger.load()['pending'])
        self.clock.at(25)
        restarted = self.make_trigger()
        self.assertEqual(restarted.check()['status'], 'ready')
        self.assertEqual(restarted.load()['pending'], before)

    def test_coordinator_waits_without_model_then_drafts_once_for_entire_turn(self):
        trigger = self.make_trigger()
        registry = FakeRegistry(self.home, self.triggers)
        generated = []
        def generate(batch, *_):
            generated.append(copy.deepcopy(batch))
            return {'action': 'reply', 'texts': ['all parts together'], 'reason': 'fixture'}
        coordinator = Coordinator(registry, generate=generate)
        self.reader.add(ALICE, ALICE, 'part one')
        self.assertEqual(coordinator.drain(), 1010)
        self.clock.at(5)
        self.reader.add(ALICE, ALICE, 'part two')
        self.assertEqual(coordinator.drain(), 1015)
        self.clock.at(14.9)
        self.assertEqual(coordinator.drain(), 1015)
        self.assertEqual(generated, [])
        self.clock.at(15)
        self.assertIsNone(coordinator.drain())
        self.assertEqual(len(generated), 1)
        self.assertEqual([r['text'] for r in generated[0]['incoming']], ['part one', 'part two'])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['at'], 1015)

    def test_four_topics_after_quiet_window_remain_four_distinct_messages(self):
        trigger = self.make_trigger()
        registry = FakeRegistry(self.home, self.triggers)
        generated = []
        texts = ['主题一的回复', '主题二的回复\n还有一句', '主题三的回复', '主题四的回复']
        def generate(batch, *_):
            generated.append(copy.deepcopy(batch))
            return {'action': 'reply', 'texts': texts, 'reason': 'four distinct topics'}
        coordinator = Coordinator(registry, generate=generate)
        for elapsed in range(4):
            self.clock.at(elapsed)
            self.reader.add(ALICE, ALICE, f'主题 {elapsed + 1}')
            self.assertEqual(coordinator.drain(), 1010 + elapsed)
        self.clock.at(12.9)
        self.assertEqual(coordinator.drain(), 1013)
        self.assertEqual(generated, [])
        self.assertEqual(self.sent, [])
        self.clock.at(13)
        self.assertIsNone(coordinator.drain())
        self.assertEqual(len(generated), 1)
        self.assertEqual(len(generated[0]['incoming']), 4)
        self.assertEqual([row['text'] for row in self.sent], texts)
        self.assertEqual([row['at'] for row in self.sent], [1013] * 4)

    def test_new_message_during_model_discards_draft_and_waits_new_ten_seconds(self):
        trigger = self.make_trigger()
        self.incoming(trigger, 0, 'first question')
        registry = FakeRegistry(self.home, self.triggers)
        generated = []
        def generate(batch, *_):
            generated.append(copy.deepcopy(batch))
            if len(generated) == 1:
                self.clock.at(14)
                self.reader.add(ALICE, ALICE, 'important correction')
                return {'action': 'reply', 'texts': ['obsolete draft'], 'reason': 'fixture'}
            return {'action': 'reply', 'texts': ['updated draft'], 'reason': 'fixture'}
        coordinator = Coordinator(registry, generate=generate)
        self.clock.at(10)
        self.assertEqual(coordinator.drain(), 1024)
        self.assertEqual(len(generated), 1)
        self.assertEqual(self.sent, [])
        self.clock.at(23.9)
        self.assertEqual(coordinator.drain(), 1024)
        self.assertEqual(len(generated), 1)
        self.clock.at(24)
        self.assertIsNone(coordinator.drain())
        self.assertEqual(len(generated), 2)
        self.assertEqual([r['text'] for r in generated[1]['incoming']],
                         ['first question', 'important correction'])
        self.assertEqual([r['text'] for r in self.sent], ['updated draft'])
        self.assertEqual(self.sent[0]['at'], 1024)

    def test_coordinator_returns_earliest_of_independent_wait_deadlines(self):
        alice = self.make_trigger(ALICE); bob = self.make_trigger(BOB)
        self.incoming(alice, 0)
        self.incoming(bob, 3)
        coordinator = Coordinator(FakeRegistry(self.home, self.triggers),
                                  generate=lambda *_: {'action': 'reply', 'texts': ['reply'], 'reason': 'fixture'})
        self.assertEqual(coordinator.drain(), 1010)
        self.clock.at(10)
        self.assertEqual(coordinator.drain(), 1013)
        self.assertEqual([row['recipient'] for row in self.sent], [ALICE])
        self.clock.at(13)
        self.assertIsNone(coordinator.drain())
        self.assertEqual([row['recipient'] for row in self.sent], [ALICE, BOB])

    def test_observed_arrival_during_slow_model_keeps_its_actual_quiet_deadline(self):
        trigger = self.make_trigger()
        self.incoming(trigger, 0, 'initial question')
        registry = FakeRegistry(self.home, self.triggers)
        generated = []
        def generate(batch, *_):
            generated.append(copy.deepcopy(batch))
            if len(generated) == 1:
                self.clock.at(14)
                self.reader.add(ALICE, ALICE, 'observed while model is busy')
                coordinator.observe()
                self.assertEqual(trigger.load()['pending']['last_received_at'], 1014)
                self.clock.at(30)
                return {'action': 'reply', 'texts': ['obsolete'], 'reason': 'fixture'}
            return {'action': 'reply', 'texts': ['updated'], 'reason': 'fixture'}
        coordinator = Coordinator(registry, generate=generate)
        self.clock.at(10)
        self.assertIsNone(coordinator.drain())
        self.assertEqual(len(generated), 2)
        self.assertEqual([r['text'] for r in self.sent], ['updated'])
        self.assertEqual(self.sent[0]['at'], 1030)


class EventWakeTests(unittest.TestCase):
    class FakeCondition:
        def __init__(self, callback):
            self.callback = callback
            self.waits = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def notify_all(self):
            pass

        def wait(self, timeout=None):
            self.waits.append(timeout)
            self.callback(timeout)

    def test_idle_wait_is_unbounded_and_has_no_periodic_poll_timeout(self):
        wake = autoreply.EventWake()
        condition = self.FakeCondition(lambda _: setattr(wake, 'stopped', True))
        wake.condition = condition
        self.assertFalse(wake.wait())
        self.assertEqual(condition.waits, [None])

    def test_known_deadline_waits_until_due_without_eight_second_cap(self):
        wall, mono = [1000.0], [500.0]
        with patch.object(autoreply.time, 'time', side_effect=lambda: wall[0]), \
                patch.object(autoreply.time, 'monotonic', side_effect=lambda: mono[0]):
            wake = autoreply.EventWake()
            def advance(seconds):
                self.assertIsNotNone(seconds)
                wall[0] += seconds
                mono[0] += seconds
            condition = self.FakeCondition(advance)
            wake.condition = condition
            wake.schedule(1010)
            self.assertTrue(wake.wait())
            self.assertEqual(condition.waits, [10])
            self.assertEqual(wall[0], 1010)
            self.assertIsNone(wake.deadline)

    def test_real_event_wakes_immediately_without_waiting_for_deadline(self):
        wake = autoreply.EventWake()
        condition = self.FakeCondition(lambda _: self.fail('Real event must not wait'))
        wake.condition = condition
        wake.schedule(autoreply.time.time() + 10)
        wake.notify()
        self.assertTrue(wake.wait())
        self.assertEqual(condition.waits, [])


if __name__ == '__main__':
    unittest.main()
