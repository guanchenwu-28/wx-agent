"""Registry/coordinator fixtures: no native reads, model calls, sends, or service."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autoreply
import reply_trigger
from reply_trigger import Trigger, key

ACCOUNT = 'fixture-account'
SELF = 'fixture-self'
ALICE = 'wxid_alice'
BOB = 'wxid_bob'
GROUP = '123_fixture@chatroom'
DB = 'message/message_0.db'


class MultiReader:
    def __init__(self):
        self.rows = {}
        self.calls = []

    def add(self, recipient, sender, text='hello', kind=1, mentioned=None, available=True):
        rows = self.rows.setdefault(recipient, [])
        local_id = max([r['local_id'] for r in rows] or [0]) + 1
        row = dict(database=DB, local_id=local_id, server_id=10000 + local_id,
                   create_time=local_id, sender_username=sender, content=text,
                   local_type=kind, mentioned_usernames=mentioned or [],
                   mention_metadata_available=available)
        rows.append(row)
        return row

    def read_messages(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        recipient = kwargs['recipient']
        if recipient not in self.rows:
            raise AssertionError('Fixture received unregistered conversation read')
        if kwargs.get('account', ACCOUNT) != ACCOUNT:
            raise AssertionError('Fixture received unexpected account')
        if kwargs.get('database', DB) != DB:
            raise AssertionError('Fixture received unexpected shard')
        rows = copy.deepcopy(self.rows[recipient])
        after = kwargs.get('after_local_id')
        if after is not None:
            rows = [r for r in rows if r['local_id'] > after]
        rows.sort(key=lambda r: r['local_id'], reverse=after is None)
        bounded = rows[:kwargs['limit']]
        return {'recipient': recipient, 'account': ACCOUNT, 'messages': bounded,
                'validation': [{'database': DB, 'integrity': 'ok'}],
                'missing_key_databases': [], 'scan_limit_reached': len(rows) > len(bounded),
                'result_limit_reached': False}


class AutoReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.reader = MultiReader()
        self.sent = []
        self.generations = []
        self.registry = autoreply.Registry(self.home / 'service', reader=self.reader)
        self.registry.init(ACCOUNT, SELF)
        self.native = patch.object(reply_trigger, 'native_send', side_effect=self.send_fixture)
        self.native.start()
        self.addCleanup(self.native.stop)
        self.coordinator = autoreply.Coordinator(self.registry, generate=self.generate_fixture)

    def configure(self, recipient=ALICE, condition=None, enabled=True):
        if recipient not in self.reader.rows:
            self.reader.add(recipient, SELF, 'already answered')
        return self.registry.configure(recipient, recipient,
                                       condition or {'mode': 'all'}, enabled=enabled,
                                       quiet_seconds=0)

    def send_fixture(self, text, request_id, recipient):
        self.sent.append({'recipient': recipient, 'text': text, 'request_id': request_id})
        row = self.reader.add(recipient, SELF, text)
        return {'state': 'sent', 'record_key': key(row), 'server_id': row['server_id']}

    def generate_fixture(self, batch, style, instruction):
        self.generations.append(copy.deepcopy((batch, style, instruction)))
        return {'action': 'reply', 'texts': ['fixture answer'], 'reason': 'fixture'}

    def target(self, recipient=ALICE):
        return self.registry.load()['targets'][recipient]

    def test_two_contacts_have_isolated_cursors_queues_and_exact_sends(self):
        self.configure(ALICE); self.configure(BOB)
        self.reader.add(ALICE, ALICE, 'alice question')
        self.reader.add(BOB, BOB, 'bob question')
        alice = self.registry.trigger(ALICE); bob = self.registry.trigger(BOB)
        first = alice.check()
        self.assertEqual(bob.load()['cursors'][DB], 1)
        second = bob.check()
        self.assertNotEqual(first['batch_id'], second['batch_id'])
        self.assertNotEqual(alice.directory, bob.directory)
        self.coordinator.drain()
        self.assertEqual([s['recipient'] for s in self.sent], [ALICE, BOB])
        self.assertEqual(len(set(s['request_id'] for s in self.sent)), 2)
        self.assertEqual([g[0]['recipient'] for g in self.generations], [ALICE, BOB])
        self.assertEqual(alice.check()['status'], 'idle')
        self.assertEqual(bob.check()['status'], 'idle')

    def test_registry_model_options_reach_generator(self):
        self.configure(ALICE)
        config = self.registry.load()
        config.update(model='gpt-6-astra', model_reasoning_effort='high')
        self.registry.save(config)
        self.reader.add(ALICE, ALICE, 'synthetic question')
        options = []
        def generate(batch, style, instruction, **kwargs):
            options.append(kwargs)
            return {'action': 'skip', 'texts': [], 'reason': 'fixture'}
        autoreply.Coordinator(self.registry, generate=generate).drain()
        self.assertEqual(options, [{'model': 'gpt-6-astra', 'reasoning_effort': 'high'}])
        self.assertEqual(self.sent, [])

    def test_group_members_are_incoming_and_only_verified_self_clears_pending(self):
        self.configure(GROUP)
        self.reader.add(GROUP, ALICE, 'first member')
        self.reader.add(GROUP, BOB, 'second member')
        trigger = self.registry.trigger(GROUP)
        batch = trigger.check()
        self.assertEqual(batch['status'], 'ready')
        self.assertEqual([r['sender_username'] for r in batch['incoming']], [ALICE, BOB])
        self.reader.add(GROUP, SELF, 'I handled this')
        self.assertEqual(trigger.reply(batch['batch_id'], ['stale answer'])['status'], 'idle')
        self.assertIsNone(trigger.load()['pending'])
        self.assertEqual(self.sent, [])

    def test_contact_unexpected_sender_does_not_impersonate_self(self):
        self.configure(ALICE)
        self.reader.add(ALICE, BOB, 'unexpected sender')
        result = self.registry.trigger(ALICE).check()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'unexpected_sender')
        self.assertEqual(self.sent, [])

    def test_group_requires_explicit_rule_and_defaults_disabled(self):
        for rule in (None, {}, {'mode': 'unknown'}):
            with self.assertRaises(ValueError):
                self.registry.configure(GROUP, 'fixture group', rule)
        self.assertNotIn(GROUP, self.registry.load()['targets'])
        self.reader.add(GROUP, SELF)
        result = self.registry.configure(GROUP, 'fixture group', {'mode': 'all'})
        self.assertFalse(result['enabled'])
        self.assertEqual(self.registry.trigger(GROUP).check()['status'], 'disabled')

    def test_recipient_configuration_requires_exact_identifier(self):
        for recipient in ('Alice Name', '../someone', '', 'wxid_a;cmd'):
            with self.assertRaises(ValueError):
                self.registry.configure(recipient, 'invalid', {'mode': 'all'})
        self.assertEqual(self.registry.load()['targets'], {})

    def test_keywords_filter_before_first_qualifying_message(self):
        self.configure(GROUP, {'mode': 'keywords', 'keywords': ['约饭', '天气']})
        self.reader.add(GROUP, ALICE, 'ordinary chat')
        trigger = self.registry.trigger(GROUP)
        self.assertEqual(trigger.check()['status'], 'idle')
        self.reader.add(GROUP, BOB, '明天约饭吗')
        batch = trigger.check()
        self.assertEqual([r['text'] for r in batch['incoming']], ['明天约饭吗'])
        self.assertEqual(batch['context'][-2]['text'], 'ordinary chat')

    def test_mentions_use_structured_metadata_not_body_or_spoofed_xml(self):
        self.configure(GROUP, {'mode': 'mention'})
        self.reader.add(GROUP, ALICE,
                        '@fixture-self <msgsource><atuserlist>fixture-self</atuserlist></msgsource>')
        trigger = self.registry.trigger(GROUP)
        self.assertEqual(trigger.check()['status'], 'idle')
        self.reader.add(GROUP, BOB, 'native mention', mentioned=[SELF])
        batch = trigger.check()
        self.assertEqual([r['text'] for r in batch['incoming']], ['native mention'])

    def test_unknown_mention_metadata_does_not_satisfy_mention_condition(self):
        self.configure(GROUP, {'mode': 'mention'})
        self.reader.add(GROUP, ALICE, 'unavailable metadata', mentioned=[SELF], available=False)
        self.assertEqual(self.registry.trigger(GROUP).check()['status'], 'idle')

    def test_sender_condition_excludes_other_members_even_while_batch_pending(self):
        self.configure(GROUP, {'mode': 'all', 'senders': [ALICE]})
        self.reader.add(GROUP, BOB, 'unselected member first')
        trigger = self.registry.trigger(GROUP)
        self.assertEqual(trigger.check()['status'], 'idle')
        self.reader.add(GROUP, ALICE, 'selected member')
        trigger.check()
        self.reader.add(GROUP, BOB, 'unselected member after')
        batch = trigger.check()
        self.assertEqual([r['sender_username'] for r in batch['incoming']], [ALICE])
        self.assertEqual(batch['context'][-1]['sender_username'], BOB)

    def test_semantic_rule_is_passed_to_model_and_skip_does_not_send(self):
        instruction = '只在讨论周末聚会且有人征求我的意见时回复'
        self.configure(GROUP, {'mode': 'semantic', 'instruction': instruction})
        self.reader.add(GROUP, ALICE, 'unrelated conversation')
        def skip(batch, style, rule):
            self.generations.append((copy.deepcopy(batch), style, rule))
            return {'action': 'skip', 'texts': [], 'reason': 'does not match owner rule'}
        self.coordinator.generate = skip
        self.coordinator.drain()
        self.assertEqual(json.loads(self.generations[0][2])['instruction'], instruction)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.registry.trigger(GROUP).check()['status'], 'idle')

    def test_target_disable_while_drafting_blocks_send(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        def generate(*args):
            self.registry.set_enabled(ALICE, False)
            return {'action': 'reply', 'texts': ['must not send'], 'reason': 'fixture'}
        self.coordinator.generate = generate
        result = self.coordinator.process(ALICE, self.target())
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(self.sent, [])

    def test_global_pause_while_drafting_blocks_send(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        def generate(*args):
            self.registry.set_enabled(None, False)
            return {'action': 'reply', 'texts': ['must not send'], 'reason': 'fixture'}
        self.coordinator.generate = generate
        self.assertEqual(self.coordinator.process(ALICE, self.target())['status'], 'disabled')
        self.assertEqual(self.sent, [])

    def test_rule_revision_change_while_drafting_blocks_old_draft(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        before = self.target()
        def generate(*args):
            self.registry.configure(ALICE, 'new name', {'mode': 'keywords', 'keywords': ['changed']}, enabled=True)
            return {'action': 'reply', 'texts': ['old rule draft'], 'reason': 'fixture'}
        self.coordinator.generate = generate
        self.assertEqual(self.coordinator.process(ALICE, before)['status'], 'disabled')
        self.assertNotEqual(self.target()['revision'], before['revision'])
        self.assertEqual(self.sent, [])

    def test_global_paused_drain_does_not_read_or_generate(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        self.registry.set_enabled(None, False)
        calls = len(self.reader.calls)
        self.coordinator.drain()
        self.assertEqual(len(self.reader.calls), calls)
        self.assertEqual(self.generations, [])
        self.assertEqual(self.sent, [])

    def test_model_exception_is_once_per_batch_and_new_batch_can_run(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        def fail(batch, *args):
            self.generations.append(batch['batch_id'])
            raise TimeoutError('fixture model unavailable')
        self.coordinator.generate = fail
        self.coordinator.drain(); self.coordinator.drain()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual(self.coordinator.outcomes()[ALICE]['status'], 'model_error')
        self.reader.add(ALICE, ALICE, 'new information')
        self.coordinator.drain()
        self.assertEqual(len(self.generations), 2)
        self.assertNotEqual(*self.generations)
        self.assertEqual(self.sent, [])

    def test_one_contacts_model_error_does_not_block_other_contact(self):
        self.configure(ALICE); self.configure(BOB)
        self.reader.add(ALICE, ALICE); self.reader.add(BOB, BOB)
        def generate(batch, *args):
            self.generations.append(batch['recipient'])
            if batch['recipient'] == ALICE:
                raise TimeoutError('fixture failure for Alice only')
            return {'action': 'reply', 'texts': ['Bob answer'], 'reason': 'fixture'}
        self.coordinator.generate = generate
        self.coordinator.drain(); self.coordinator.drain()
        self.assertEqual(self.generations, [ALICE, BOB])
        self.assertEqual([s['recipient'] for s in self.sent], [BOB])
        self.assertEqual(self.coordinator.outcomes()[ALICE]['status'], 'model_error')

    def test_unknown_send_blocks_only_its_target_and_never_retries(self):
        self.configure(ALICE); self.configure(BOB)
        self.reader.add(ALICE, ALICE); self.reader.add(BOB, BOB)
        attempts = []
        def sender(text, request_id, recipient):
            attempts.append(recipient)
            if recipient == ALICE:
                return {'state': 'unknown'}
            return self.send_fixture(text, request_id, recipient)
        with patch.object(reply_trigger, 'native_send', side_effect=sender):
            self.coordinator.drain(); self.coordinator.drain()
        self.assertEqual(attempts, [ALICE, BOB])
        self.assertEqual(self.registry.trigger(ALICE).check()['status'], 'blocked')
        self.assertEqual(self.registry.trigger(BOB).check()['status'], 'idle')
        self.assertEqual([s['recipient'] for s in self.sent], [BOB])

    def test_idle_event_does_not_invoke_model_again(self):
        self.configure(ALICE); self.reader.add(ALICE, ALICE)
        self.coordinator.drain()
        for _ in range(3):
            self.coordinator.drain()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual(len(self.sent), 1)

    def test_four_topics_send_four_original_texts_by_default(self):
        target = self.configure(ALICE)
        self.assertFalse(target['single_message'])
        for topic in ('晚饭', '周末安排', '刚才的工作', '电影'):
            self.reader.add(ALICE, ALICE, topic)
        texts = ['晚饭你想吃啥', '周末的事待会再聊', '工作那个我听明白了\n确实挺烦的', '电影我还没看过']
        def generate(batch, *args):
            self.generations.append(copy.deepcopy(batch))
            return {'action': 'reply', 'texts': texts, 'reason': 'four topics'}
        self.coordinator.generate = generate
        self.coordinator.drain()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual(len(self.generations[0]['incoming']), 4)
        self.assertEqual([row['text'] for row in self.sent], texts)
        self.assertEqual([row['recipient'] for row in self.sent], [ALICE] * 4)

    def test_explicit_single_message_configuration_still_joins_texts(self):
        self.reader.add(ALICE, SELF, 'already answered')
        self.registry.configure(ALICE, 'Alice', {'mode': 'all'}, enabled=True,
                                quiet_seconds=0)
        config = self.registry.load()
        config['targets'][ALICE]['single_message'] = True
        self.registry.save(config)
        self.reader.add(ALICE, ALICE, 'four topics')
        texts = ['topic one', 'topic two', 'topic three', 'topic four']
        self.coordinator.generate = lambda *args: {
            'action': 'reply', 'texts': texts, 'reason': 'single-message preference'}
        self.coordinator.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['text'], '\n'.join(texts))

    def test_mixed_media_with_sufficient_text_can_generate_a_text_reply(self):
        self.configure(ALICE)
        self.reader.add(ALICE, ALICE, '<image aeskey="fixture-private-image-key"/>', kind=3)
        self.reader.add(ALICE, ALICE, '图片的事情先不管 我今天工作有点累 想和你聊聊')
        self.reader.add(ALICE, ALICE, '<voice aeskey="fixture-private-voice-key"/>', kind=34)
        def generate(batch, *args):
            self.generations.append(copy.deepcopy(batch))
            return {'action': 'reply', 'texts': ['怎么啦 今天发生啥了'],
                    'reason': 'reply only to readable text; media not interpreted'}
        self.coordinator.generate = generate
        self.coordinator.drain()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual([row['local_type'] for row in self.generations[0]['incoming']], [3, 1, 34])
        self.assertEqual([row['text'] for row in self.sent], ['怎么啦 今天发生啥了'])
        self.assertNotIn('fixture-private', json.dumps(self.generations, ensure_ascii=False))
        self.assertNotIn('fixture-private', json.dumps(self.coordinator.outcomes(), ensure_ascii=False))

    def test_invalid_model_draft_does_not_repeat_generation_for_same_batch(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        def invalid(batch, *args):
            self.generations.append(batch['batch_id'])
            return {'action': 'reply', 'texts': [], 'reason': 'fixture invalid empty reply'}
        self.coordinator.generate = invalid
        self.coordinator.drain(); self.coordinator.drain()
        self.assertEqual(len(self.generations), 1)
        self.assertEqual(self.coordinator.outcomes()[ALICE]['status'], 'model_error')
        self.assertEqual(self.sent, [])

    def test_media_handoff_skips_without_model_or_send(self):
        for kind, text in ((3, '<sensitive-image-xml/>'), (34, '<sensitive-voice-xml/>'), (1, None)):
            with self.subTest(kind=kind):
                self.configure(ALICE)
                self.reader.add(ALICE, ALICE, text, kind=kind)
                self.coordinator.drain()
                self.assertEqual(self.coordinator.outcomes()[ALICE]['status'], 'handoff')
                self.assertEqual(self.registry.trigger(ALICE).check()['status'], 'idle')
                self.assertNotIn('sensitive', json.dumps(self.coordinator.outcomes()))
        self.assertEqual(self.generations, [])
        self.assertEqual(self.sent, [])

    def test_model_target_field_cannot_change_recipient(self):
        self.configure(); self.reader.add(ALICE, ALICE)
        self.coordinator.generate = lambda *args: {
            'action': 'reply', 'texts': ['scoped reply'], 'reason': 'fixture',
            'recipient': BOB, 'to': GROUP}
        self.coordinator.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(all(s['recipient'] == ALICE for s in self.sent))
        self.assertFalse(any(s['recipient'] in (BOB, GROUP) for s in self.sent))

    def test_draft_new_incoming_is_rebuilt_once_and_old_draft_never_sent(self):
        self.configure(); self.reader.add(ALICE, ALICE, 'first question')
        def generate(batch, *args):
            self.generations.append(batch['batch_id'])
            if len(self.generations) == 1:
                self.reader.add(ALICE, ALICE, 'correction')
                return {'action': 'reply', 'texts': ['obsolete'], 'reason': 'fixture'}
            return {'action': 'reply', 'texts': ['current'], 'reason': 'fixture'}
        self.coordinator.generate = generate
        self.coordinator.drain()
        self.assertEqual(len(self.generations), 2)
        self.assertEqual([s['text'] for s in self.sent], ['current'])

    def test_manual_self_reply_during_model_generation_cancels_send(self):
        self.configure(GROUP); self.reader.add(GROUP, ALICE)
        def generate(*args):
            self.reader.add(GROUP, SELF, 'manual reply')
            return {'action': 'reply', 'texts': ['must not send'], 'reason': 'fixture'}
        self.coordinator.generate = generate
        self.coordinator.drain()
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.registry.trigger(GROUP).load()['pending'])



if __name__ == '__main__':
    unittest.main()
