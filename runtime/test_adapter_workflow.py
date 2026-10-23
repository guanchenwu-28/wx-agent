"""Adapt CLI tests use fake transports only; no native calls or real sends."""
import argparse
import copy
import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adapter
import adapter_workflow as w


HASH = 'a' * 64


def good_probe():
    return dict(construct_only=True, submitted=False, passive=False,
                adapter={'identity': {'core_sha256': HASH}, 'profile_id': 'fixture'},
                checks={k: True for k in ('module_path', 'module_size', 'loaded_uuid', 'executable_addresses', 'readable_vtables')},
                constructions={kind: dict(constructed=True, submitted=False, recipient_verified=True,
                                          type_verified=True, native_type=typ, **{field: True})
                               for kind, typ, field in [('text', 1, 'text_verified'), ('image', 3, 'image_path_verified'), ('voice', 34, 'payload_verified')]})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = self.root / 'wechat.dylib'
        self.core.write_bytes(b'offline fixture only')
        self.report = dict(status='matched', descriptor={'offsets': {}}, profile_id='fixture',
                           identity={'core_sha256': HASH}, capabilities={x: False for x in adapter.KINDS}, unresolved=[])
        patches = [patch.object(adapter, 'DEFAULT_CORE', self.core),
                   patch.object(adapter, 'scan', return_value=self.report),
                   patch('wechat.transport', return_value=good_probe()),
                   patch('wechat.send_message', return_value={'state': 'sent', 'request_id': 'unique-id',
                                                              'result': {'adapter': good_probe()['adapter']}}),
                   patch('media.send_media', return_value={'state': 'sent', 'request_id': 'unique-id',
                                                          'result': {'adapter': good_probe()['adapter']}}),
                   patch.object(adapter, 'record_verified')]
        self.default_core, self.scan, self.transport, self.send, self.media, self.record = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        for p in [patch('wechat.CACHE', self.root / 'outbox'),
                  patch.object(adapter, 'DEFAULT_CACHE', self.root / 'compatibility')]:
            p.start()
            self.addCleanup(p.stop)

    def args(self, *argv):
        parser = argparse.ArgumentParser()
        w.register_parser(parser.add_subparsers(dest='command'))
        return parser.parse_args(['adapt', *argv])

    def test_offline_default_never_touches_transport(self):
        result = w.run(self.args())
        self.transport.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(result['autoreply_changed'])

    def test_other_core_is_offline_only(self):
        with self.assertRaisesRegex(ValueError, 'offline-only'):
            w.run(self.args('--core', str(self.root / 'other'), '--verify-runtime'))
        self.scan.assert_not_called()

    def test_custom_profiles_and_cache_are_offline_only(self):
        for option in ('--profiles-dir', '--cache'):
            with self.assertRaisesRegex(ValueError, 'offline-only'):
                w.run(self.args(option, str(self.root / 'custom'), '--verify-runtime'))
        self.scan.assert_not_called()
        self.transport.assert_not_called()
        w.run(self.args('--profiles-dir', str(self.root / 'custom'), '--cache', str(self.root / 'custom-cache')))
        self.scan.assert_called_once()
        self.transport.assert_not_called()

    def test_explicit_default_profile_directory_uses_runtime(self):
        result = w.run(self.args('--profiles-dir', str(adapter.ROOT / 'profiles'), '--verify-runtime'))
        self.assertTrue(result['runtime_verified'])

    def test_construction_only_never_sends_or_enables(self):
        result = w.run(self.args('--verify-runtime'))
        self.assertTrue(result['runtime_verified'])
        self.send.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(self.transport.call_args.args[0]['to'], 'filehelper')

    def test_unknown_profile_never_constructs(self):
        self.report.update(status='needs_review', descriptor=None)
        result = w.run(self.args('--verify-runtime'))
        self.assertEqual(result['state'], 'needs_review')
        self.transport.assert_not_called()

    def test_explicit_hash_and_id_required(self):
        for argv in [('--test-send', 'text'), ('--test-send', 'text', '--confirm-core-sha256', HASH)]:
            with self.assertRaises(ValueError):
                w.run(self.args(*argv))
        self.transport.assert_not_called()

    def test_hash_change_stops_before_runtime(self):
        with self.assertRaisesRegex(ValueError, 'differs'):
            w.run(self.args('--test-send', 'text', '--confirm-core-sha256', 'b' * 64, '--request-id', 'unique-id'))
        self.transport.assert_not_called()

    def test_fixed_text_filehelper_and_verified_receipt(self):
        result = w.run(self.args('--test-send', 'text', '--confirm-core-sha256', HASH, '--request-id', 'unique-id'))
        self.assertTrue(result['sent'])
        self.send.assert_called_once_with('filehelper', 'REA-版本适配验证', 'unique-id',
                                         adapter_test={'kind': 'text', 'core_sha256': HASH, 'request_id': 'unique-id'})
        self.record.assert_called_once()
        self.assertFalse(result['autoreply_changed'])

    def test_text_send_holds_shared_outbox_lock(self):
        receipt = self.send.return_value
        def send_while_locked(*args, **kwargs):
            with (self.root / 'outbox' / 'tool-send.lock').open('a') as second:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return receipt
        self.send.side_effect = send_while_locked
        self.assertTrue(w.run(self.args('--test-send', 'text', '--confirm-core-sha256', HASH,
                                        '--request-id', 'unique-id'))['sent'])

    def test_probe_identity_mismatch_blocks_send(self):
        for field in ('core_sha256', 'profile_id'):
            probe = good_probe()
            if field == 'core_sha256':
                probe['adapter']['identity'][field] = 'b' * 64
            else:
                probe['adapter'][field] = 'other-profile'
            self.transport.return_value = probe
            result = w.run(self.args('--test-send', 'text', '--confirm-core-sha256', HASH, '--request-id', 'unique-id'))
            self.assertEqual(result['state'], 'runtime_validation_failed')
        self.send.assert_not_called()
        self.record.assert_not_called()

    def test_send_receipt_identity_mismatch_cannot_promote(self):
        for field in ('core_sha256', 'profile_id'):
            receipt = {'state': 'sent', 'request_id': 'unique-id', 'result': {'adapter': good_probe()['adapter']}}
            if field == 'core_sha256':
                receipt['result']['adapter']['identity'][field] = 'b' * 64
            else:
                receipt['result']['adapter'][field] = 'other-profile'
            self.send.return_value = receipt
            result = w.run(self.args('--test-send', 'text', '--confirm-core-sha256', HASH, '--request-id', 'unique-id'))
            self.assertEqual(result['state'], 'sent_version_mismatch')
            self.assertFalse(result['capability_verified'])
            self.assertTrue(result['do_not_resend'])
        self.record.assert_not_called()

    def test_failed_constructor_never_sends(self):
        probe = good_probe()
        probe['constructions']['text']['text_verified'] = False
        self.transport.return_value = probe
        result = w.run(self.args('--test-send', 'text', '--confirm-core-sha256', HASH, '--request-id', 'unique-id'))
        self.assertEqual(result['state'], 'runtime_validation_failed')
        self.send.assert_not_called()
        self.record.assert_not_called()

    def test_submitted_probe_is_rejected(self):
        self.transport.return_value['submitted'] = True
        self.assertFalse(w.run(self.args('--verify-runtime'))['runtime_verified'])

    def test_unknown_outcome_no_retry_or_promotion(self):
        self.send.return_value = {'state': 'unknown', 'request_id': 'unique-id'}
        result = w.run(self.args('--test-send', 'text', '--confirm-core-sha256', HASH, '--request-id', 'unique-id'))
        self.assertEqual(self.send.call_count, 1)
        self.record.assert_not_called()
        self.assertEqual(result['next_action'], 'reconcile --request-id unique-id')
        self.assertTrue(result['do_not_resend'])

    def test_image_requires_file_and_uses_exact_fixture(self):
        file = self.root / 'test.png'
        file.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        result = w.run(self.args('--test-send', 'image', '--confirm-core-sha256', HASH,
                                 '--request-id', 'img-id', '--file', str(file)))
        self.assertTrue(result['sent'])
        self.assertEqual(self.media.call_args.args[:3], ('filehelper', str(file.resolve()), 'img-id'))
        self.assertEqual(self.media.call_args.kwargs['kind'], 3)

    def test_voice_requires_valid_duration_before_probe(self):
        file = self.root / 'test.silk'
        file.write_bytes(b'\x02#!SILK_V3fixture')
        with self.assertRaisesRegex(ValueError, 'duration'):
            w.run(self.args('--test-send', 'voice', '--confirm-core-sha256', HASH,
                            '--request-id', 'voice-id', '--file', str(file)))
        self.transport.assert_not_called()
        result = w.run(self.args('--test-send', 'voice', '--confirm-core-sha256', HASH,
                                 '--request-id', 'voice-id', '--file', str(file), '--duration-ms', '4000'))
        self.assertTrue(result['sent'])
        self.assertEqual(self.media.call_args.kwargs['duration_ms'], 4000)

    def test_unrelated_options_cannot_imply_send(self):
        with self.assertRaises(ValueError):
            w.run(self.args('--request-id', 'x'))
        self.scan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
