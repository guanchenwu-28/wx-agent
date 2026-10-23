"""Fixture-only media tests. No live reader, transport, or message send is used."""
import base64
import contextlib
import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
EXPECTED_MD5 = hashlib.md5(PNG).hexdigest()
BASELINE = {'account': 'fixture', 'cursors': {'message/message_0.db': 10}}
IDENTITY = {'count': 1, 'strings': ['fixture-recipient', 'fixture-native-uuid'],
            'words': [str((11 << 32) | 1), '1000']}


def row(local_id=11, kind=3, server_id=101, **extra):
    return dict(database='message/message_0.db', local_id=local_id,
                local_type=kind, server_id=server_id, content='<msg><img md5="' + EXPECTED_MD5 + '" cdnaeskey="redacted"/></msg>',
                create_time=1, **extra)


def window(messages, **flags):
    return dict(messages=messages, missing_key_databases=[], result_limit_reached=False,
                scan_limit_reached=False, **flags)


class VerifyMediaTests(unittest.TestCase):
    def setUp(self):
        self.reader_patch = patch.object(media, 'WeChatReader')
        self.reader = self.reader_patch.start().return_value
        self.addCleanup(self.reader_patch.stop)
        self.sleep = patch.object(media.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def verify(self, messages, identity=IDENTITY, kind=3, **kwargs):
        self.reader.read_messages.return_value = window(messages)
        with patch.object(media.time, 'monotonic', side_effect=[0, 0, 30]):
            return media.verify('fixture-recipient', BASELINE, kind, timeout=25,
                                media_md5=EXPECTED_MD5, native_identity=identity, **kwargs)

    def test_other_types_cannot_confirm_image(self):
        result = self.verify([row(kind=1), row(12, kind=34)])
        self.assertFalse(result['delivered'])
        self.assertEqual(result['state'], 'unknown')
        call = self.reader.read_messages.call_args.kwargs
        self.assertEqual(call['after_local_id'], 10)
        self.assertEqual(call['account'], 'fixture')
        self.assertEqual(call['recipient'], 'fixture-recipient')

    def test_two_native_identity_matches_are_ambiguous_even_when_one_has_ack(self):
        other = row(server_id=0); other['database'] = 'message/message_1.db'
        result = self.verify([row(), other])
        self.assertFalse(result['delivered'])
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('native_record', result)

    def test_unacknowledged_image_remains_unknown_and_redacts_xml(self):
        result = self.verify([row(server_id=0)])
        self.assertFalse(result['delivered'])
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('content', result['native_record'])

    def test_limit_or_key_gaps_reject_verification(self):
        for flag, value in [('missing_key_databases', ['missing']), ('result_limit_reached', True), ('scan_limit_reached', True)]:
            with self.subTest(flag=flag):
                data = window([row()]); data[flag] = value
                self.reader.read_messages.return_value = data
                with patch.object(media.time, 'monotonic', side_effect=[0, 0]):
                    with self.assertRaises(RuntimeError):
                        media.verify('fixture-recipient', BASELINE, 3, media_md5=EXPECTED_MD5, native_identity=IDENTITY)

    def test_same_image_with_different_local_id_cannot_confirm(self):
        self.assertFalse(self.verify([row(12)])['delivered'])

    def test_matching_local_id_with_different_create_time_cannot_confirm(self):
        other = row(); other['create_time'] = 2
        self.assertFalse(self.verify([other])['delivered'])

    def test_identity_recipient_mismatch_cannot_confirm(self):
        identity = dict(IDENTITY, strings=['other-recipient', 'fixture-native-uuid'])
        self.assertFalse(self.verify([row()], identity=identity)['delivered'])

    def test_absent_identity_cannot_confirm(self):
        self.assertFalse(self.verify([row()], identity=None)['delivered'])

    def test_unrelated_new_image_does_not_hide_exact_native_identity(self):
        result = self.verify([row(12), row()])
        self.assertTrue(result['delivered'])
        self.assertEqual(result['native_record']['local_id'], 11)

    def test_voice_requires_native_identity_duration_payload_and_format(self):
        voice = row(kind=34)
        voice['content'] = '<msg><voicemsg voicelength="1500" length="128" voiceformat="4"/></msg>'
        metadata = {'duration_ms': 1500, 'payload_bytes': 128}
        result = self.verify([voice], kind=34, voice_metadata=metadata)
        self.assertTrue(result['delivered'])
        self.assertNotIn('content', result['native_record'])
        for field, value in [('duration_ms', 1600), ('payload_bytes', 129)]:
            with self.subTest(field=field):
                self.assertFalse(self.verify([voice], kind=34, voice_metadata=dict(metadata, **{field:value}))['delivered'])
        voice['local_id'] = 12
        self.assertFalse(self.verify([voice], kind=34, voice_metadata=metadata)['delivered'])

    def test_matching_type_alone_cannot_attribute_an_unrelated_image(self):
        # A peer/manual send can be the only type-3 row after the baseline.
        # It is acknowledged, but contains no proof tying it to this request.
        unrelated = row(sender='other-user')
        unrelated['content'] = '<msg><img md5="' + ('0' * 32) + '"/></msg>'
        result = self.verify([unrelated])
        self.assertFalse(result['delivered'], 'Type plus server ID falsely attributes an unrelated image')
        self.assertEqual(result['state'], 'unknown')

    def test_matching_image_md5_and_ack_confirm_without_exposing_xml(self):
        result = self.verify([row()])
        self.assertTrue(result['delivered'])
        self.assertNotIn('content', result['native_record'])

    def test_missing_or_malformed_md5_does_not_confirm(self):
        for content in ['<msg><img/></msg>', '<not-xml', '<msg><img md5=""/></msg>']:
            with self.subTest(content=content):
                image = row(); image['content'] = content
                result = self.verify([image])
                self.assertFalse(result['delivered'])
                self.assertEqual(result['state'], 'unknown')


class ImageOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache'
        self.image = self.root / 'one.png'; self.image.write_bytes(PNG)
        self.cache_patch = patch.object(media.wechat, 'CACHE', self.cache)
        self.cache_patch.start(); self.addCleanup(self.cache_patch.stop)
        self.baseline_patch = patch.object(media, 'baseline_for', return_value=BASELINE)
        self.baseline = self.baseline_patch.start(); self.addCleanup(self.baseline_patch.stop)
        self.verify_patch = patch.object(media, 'verify', return_value={'delivered': True, 'native_record': {'local_id': 11, 'server_id': 101}})
        self.verify = self.verify_patch.start(); self.addCleanup(self.verify_patch.stop)
        self.transport_patch = patch.object(media.wechat, 'transport', side_effect=[{'logged_in': True}, {'enqueued': True, 'submitted': True, 'identity': IDENTITY}])
        self.transport = self.transport_patch.start(); self.addCleanup(self.transport_patch.stop)

    def send(self, request_id='fixture-request', recipient='fixture-recipient', image=None):
        return media.send_image(recipient, image or self.image, request_id)

    def test_duplicate_id_reuses_result_without_transport_or_verification(self):
        self.assertEqual(self.send()['state'], 'sent')
        again = self.send()
        self.assertTrue(again['reused']); self.assertFalse(again['resent'])
        self.assertEqual(self.transport.call_count, 2)
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args.kwargs['native_identity'], IDENTITY)

    def test_duplicate_content_with_different_request_id_is_a_new_request(self):
        self.transport.side_effect = [{'logged_in': True}, {'enqueued': True, 'submitted': True, 'identity': IDENTITY}] * 2
        first = self.send('first'); second = self.send('second')
        self.assertEqual(first['state'], 'sent'); self.assertEqual(second['state'], 'sent')
        self.assertEqual(self.transport.call_count, 4)

    def test_unknown_transport_outcome_never_resends(self):
        self.transport.side_effect = [{'logged_in': True}, TimeoutError('fixture')]
        first = self.send(); again = self.send()
        self.assertEqual(first['state'], 'unknown'); self.assertEqual(again['state'], 'unknown')
        self.assertTrue(again['reused']); self.assertFalse(again['resent'])
        self.assertEqual(self.transport.call_count, 2); self.verify.assert_not_called()

    def test_ambiguous_verification_is_durable_unknown_without_resend(self):
        self.verify.return_value = {'delivered': False, 'state': 'unknown'}
        first = self.send(); again = self.send()
        self.assertEqual(first['state'], 'unknown'); self.assertEqual(again['state'], 'unknown')
        self.assertEqual(self.transport.call_count, 2); self.verify.assert_called_once()

    def test_request_id_cannot_change_recipient_or_file(self):
        self.send()
        other = self.root / 'two.png'; other.write_bytes(PNG + b'other')
        for kwargs in [{'recipient': 'other-recipient'}, {'image': other}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.send(**kwargs)
        self.assertEqual(self.transport.call_count, 2)

    def test_not_logged_in_does_not_stage_or_send(self):
        self.transport.side_effect = None; self.transport.return_value = {'logged_in': False}
        with self.assertRaises(RuntimeError): self.send()
        self.assertEqual(self.transport.call_count, 1); self.baseline.assert_not_called()
        self.assertFalse((self.cache / 'media').exists())

    def test_invalid_recipient_is_rejected_before_transport(self):
        for recipient in ['', 'has space', 'x\x00y', '../bad']:
            with self.subTest(recipient=recipient), self.assertRaises(ValueError): self.send(recipient=recipient)
        self.transport.assert_not_called()

    def test_empty_unknown_format_and_oversize_are_rejected(self):
        for name, data in [('empty', b''), ('gif', b'GIF89a'), ('large', b'\x89PNG\r\n\x1a\n' + bytes(20 * 1024 * 1024))]:
            file = self.root / name; file.write_bytes(data)
            with self.subTest(name=name), self.assertRaises(ValueError): self.send(image=file)
        self.transport.assert_not_called()

    def test_staging_uses_content_digest_and_private_permissions(self):
        self.send()
        req = self.transport.call_args.args[0]
        staged = Path(req['image_path'])
        self.assertEqual(staged.parent, self.cache / 'media')
        self.assertEqual(staged.read_bytes(), PNG)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual(req['action'], 'send-image')
        self.assertEqual((self.cache / 'tool-media-outbox.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_lock_and_committed_submitting_row_precede_external_send(self):
        def transport(req):
            with (self.cache / 'tool-send.lock').open('a') as competing:
                with self.assertRaises(BlockingIOError):
                    media.fcntl.flock(competing, media.fcntl.LOCK_EX | media.fcntl.LOCK_NB)
            if req['action'] == 'status': return {'logged_in': True}
            with contextlib.closing(sqlite3.connect(self.cache / 'tool-media-outbox.sqlite3')) as con, con:
                saved = con.execute('SELECT state,result FROM media_sends WHERE request_id=?', ('fixture-request',)).fetchone()
            self.assertEqual(saved[0], 'submitting')
            self.assertEqual(json.loads(saved[1])['baseline'], BASELINE)
            return {'enqueued': True, 'submitted': True, 'identity': IDENTITY}
        self.transport.side_effect = transport
        self.assertEqual(self.send()['state'], 'sent')

    def test_interrupted_submitting_row_is_reused_without_resend(self):
        self.send()
        with contextlib.closing(sqlite3.connect(self.cache / 'tool-media-outbox.sqlite3')) as con, con:
            con.execute("UPDATE media_sends SET state='submitting', result=?", (json.dumps({'baseline': BASELINE}),))
        self.transport.reset_mock(); self.verify.reset_mock()
        again = self.send()
        self.assertEqual(again['state'], 'submitting'); self.assertTrue(again['reused'])
        self.assertFalse(again['resent']); self.transport.assert_not_called(); self.verify.assert_not_called()


if __name__ == '__main__': unittest.main()
