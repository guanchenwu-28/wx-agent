"""Adapter permission/cache fixtures; no real binary, Frida, or WeChat access."""
import copy
import hashlib
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import adapter
REAL_IMPLEMENTATION_DIGEST = adapter.implementation_digest


class AdapterGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / 'wechat.dylib'
        self.core.write_bytes(b'fixture native image 1')
        self.profiles = self.root / 'profiles'
        self.profiles.mkdir()
        self.profile_path = self.profiles / 'fixture.json'
        self.write_profile()
        self.cache = self.root / 'cache'
        self.match = Mock(side_effect=self.match_profile)
        module = ModuleType('adapter_matcher')
        module.match_profile = self.match
        self.enterContext(patch.dict(sys.modules, {'adapter_matcher': module}))
        self.implementation = self.enterContext(patch.object(adapter, 'implementation_digest', return_value='implementation-1'))

    def write_profile(self, **changes):
        value = {'schema_version': 1, 'profile_id': 'fixture-profile', 'layout': 'fixture-layout'}
        value.update(changes)
        self.profile_path.write_text(json.dumps(value))

    @staticmethod
    def match_profile(core, profile):
        core_hash = hashlib.sha256(Path(core).read_bytes()).hexdigest()
        return {'status': 'matched', 'profile_id': profile['profile_id'],
                'identity': {'core_sha256': core_hash, 'core_path': str(core),
                             'arm64_sha256': adapter.VERIFIED_BASELINE_ARM64_SHA256},
                'descriptor': {'schema_version': 1, 'core_sha256': core_hash, 'core_path': str(core),
                               'offsets': {'fixture': 16}, 'vtables': {'fixture': 32},
                               'layout': {'fixture': 48}}, 'unresolved': []}

    def scan(self, **kwargs):
        return adapter.scan(self.core, profiles_dir=self.profiles, cache=self.cache, **kwargs)

    @staticmethod
    def receipt(report, kind='text'):
        return {'state': 'sent', 'request_id': 'fixture-request-1',
                'result': {'delivered': True,
                           'native_record': {'server_id': '123456', 'database': 'message/message_0.db',
                                             'local_id': 3, 'recipient': 'filehelper'},
                           'adapter_test': {'core_sha256': report['identity']['core_sha256'],
                                            'kind': kind, 'recipient': 'filehelper',
                                            'request_id': 'fixture-request-1'}}}

    def verify(self, report, result=None, kind='text', construction_verified=True):
        return adapter.record_verified(report, kind, result or self.receipt(report, kind),
                                       construction_verified=construction_verified, cache=self.cache)

    def test_static_match_never_authorizes_send_or_native_calls(self):
        report = self.scan()
        self.assertEqual(report['status'], 'matched')
        self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, False))
        permissions = adapter.runtime_descriptor(report)['permissions']
        self.assertEqual(permissions, {'native_calls': False, 'send_text': False,
                                       'send_image': False, 'send_voice': False})
        self.assertEqual(report['delivery_verified'], dict.fromkeys(adapter.KINDS, False))

    def test_test_permission_is_limited_to_requested_kind(self):
        report = self.scan()
        original = copy.deepcopy(report)
        for kind in adapter.KINDS:
            descriptor = adapter.runtime_descriptor(report, test_kind=kind)
            self.assertEqual(descriptor['test_recipient'], 'filehelper')
            self.assertTrue(descriptor['permissions']['native_calls'])
            for candidate in adapter.KINDS:
                self.assertIs(descriptor['permissions']['send_' + candidate], candidate == kind)
        self.assertEqual(report, original)

    def test_constructor_check_enables_native_calls_but_no_send(self):
        report = self.scan()
        permissions = adapter.runtime_descriptor(report, construct=True)['permissions']
        self.assertTrue(permissions['native_calls'])
        self.assertTrue(all(not permissions['send_' + kind] for kind in adapter.KINDS))

    def test_invalid_kind_and_unresolved_version_have_no_runtime_descriptor(self):
        report = self.scan()
        with self.assertRaises(ValueError):
            adapter.runtime_descriptor(report, test_kind='video')
        report.update(status='needs_review', descriptor=None, unresolved=['layout_changed'])
        for arguments in ({}, {'construct': True}, {'test_kind': 'text'}):
            with self.assertRaises(RuntimeError):
                adapter.runtime_descriptor(report, **arguments)

    def test_attributed_receipt_promotes_only_verified_kind(self):
        report = self.scan()
        self.verify(report, kind='image')
        fresh = self.scan()
        self.assertEqual(fresh['capabilities'], {'text': False, 'image': True, 'voice': False})
        self.assertEqual(adapter.runtime_descriptor(fresh)['permissions'],
                         {'native_calls': True, 'send_text': False, 'send_image': True, 'send_voice': False})
        self.assertEqual(self.match.call_count, 1)
        saved = json.loads((self.cache / 'verified.json').read_text())
        entry = saved[adapter.verification_key(report)]['image']
        self.assertEqual(entry['recipient'], 'filehelper')
        self.assertEqual(entry['request_id'], 'fixture-request-1')

    def test_unknown_submission_does_not_promote_or_write_verification(self):
        report = self.scan()
        result = self.receipt(report)
        result['state'] = 'unknown'
        with self.assertRaises(ValueError):
            self.verify(report, result)
        self.assertFalse((self.cache / 'verified.json').exists())
        self.assertEqual(self.scan()['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_needs_review_report_cannot_record_a_verified_capability(self):
        report = self.scan()
        report.update(status='needs_review', descriptor=None)
        with self.assertRaises(ValueError):
            self.verify(report)
        self.assertFalse((self.cache / 'verified.json').exists())

    def test_missing_or_false_construction_verification_cannot_promote(self):
        report = self.scan()
        for value in (False, None, 1, 'true'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.verify(report, construction_verified=value)
        self.assertFalse((self.cache / 'verified.json').exists())

    def test_wrong_core_kind_recipient_and_missing_request_cannot_promote(self):
        report = self.scan()
        for key, value in (('core_sha256', 'other-core'), ('kind', 'voice'), ('recipient', 'wxid_other')):
            with self.subTest(key=key):
                result = self.receipt(report)
                result['result']['adapter_test'][key] = value
                with self.assertRaises(ValueError):
                    self.verify(report, result)
        result = self.receipt(report)
        result['request_id'] = ''
        with self.assertRaises(ValueError):
            self.verify(report, result)
        self.assertFalse((self.cache / 'verified.json').exists())

    def test_receipt_request_identity_must_match_native_test_attestation(self):
        report = self.scan()
        result = self.receipt(report)
        result['result']['adapter_test']['request_id'] = 'different-request'
        with self.assertRaises(ValueError):
            self.verify(report, result)
        self.assertFalse((self.cache / 'verified.json').exists())

    def test_current_core_must_still_match_before_recording_receipt(self):
        report = self.scan()
        self.core.write_bytes(b'a replacement core after the test began')
        with self.assertRaisesRegex(ValueError, 'Core image changed'):
            self.verify(report)
        self.assertFalse((self.cache / 'verified.json').exists())

    def test_receipt_needs_positive_server_id_and_delivered_true(self):
        report = self.scan()
        for value in (0, '0', None, '', -1, '-1', 'not-a-number'):
            with self.subTest(server_id=value):
                result = self.receipt(report)
                result['result']['native_record']['server_id'] = value
                with self.assertRaises((ValueError, TypeError)):
                    self.verify(report, result)
        result = self.receipt(report)
        result['result']['delivered'] = False
        with self.assertRaises(ValueError):
            self.verify(report, result)
        self.assertFalse((self.cache / 'verified.json').exists())

    def test_cache_hit_skips_matching_but_core_change_invalidates(self):
        first = self.scan()
        self.verify(first)
        self.assertEqual(self.scan()['cache_key'], first['cache_key'])
        self.assertEqual(self.match.call_count, 1)
        self.core.write_bytes(b'fixture native image 2')
        second = self.scan()
        self.assertNotEqual(second['cache_key'], first['cache_key'])
        self.assertEqual(self.match.call_count, 2)
        self.assertEqual(second['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_profile_change_invalidates_match_and_prior_verification(self):
        first = self.scan()
        self.verify(first)
        self.write_profile(layout='changed-layout')
        second = self.scan()
        self.assertNotEqual(second['cache_key'], first['cache_key'])
        self.assertEqual(self.match.call_count, 2)
        self.assertEqual(second['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_implementation_change_invalidates_match_and_prior_verification(self):
        sources = self.root / 'implementation-sources'; sources.mkdir()
        for name in adapter.IMPLEMENTATION_FILES:
            (sources / name).write_text('fixture ' + name)
        with patch.object(adapter, 'ROOT', sources):
            first = self.scan()
            self.verify(first)
            (sources / 'runtime.js').write_text('changed runtime implementation')
            self.implementation.return_value = 'implementation-2'
            second = self.scan()
        self.assertNotEqual(second['cache_key'], first['cache_key'])
        self.assertEqual(self.match.call_count, 2)
        self.assertEqual(second['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_force_refresh_rematches_same_identity(self):
        first = self.scan()
        second = self.scan(refresh=True)
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(self.match.call_count, 2)

    def test_same_bytes_at_new_path_never_inject_cached_old_path(self):
        first = self.scan()
        moved = self.root / 'new-wechat.dylib'
        moved.write_bytes(self.core.read_bytes())
        second = adapter.scan(moved, profiles_dir=self.profiles, cache=self.cache)
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(second['identity']['core_path'], str(moved.resolve()))
        self.assertEqual(second['descriptor']['core_path'], str(moved.resolve()))
        self.assertEqual(self.match.call_count, 1)

    def test_corrupt_or_mismatched_cached_report_is_recomputed(self):
        first = self.scan()
        path = Path(first['report_path'])
        for text in ('not json', '[]', 'null', json.dumps({**first, 'implementation_digest': 'wrong'})):
            path.write_text(text)
            self.assertEqual(self.scan()['cache_key'], first['cache_key'])
        self.assertEqual(self.match.call_count, 5)

    def test_disagreeing_profiles_are_not_arbitrarily_selected(self):
        (self.profiles / 'second.json').write_text(json.dumps({'schema_version': 1, 'profile_id': 'other-profile'}))
        def disagree(core, profile):
            report = self.match_profile(core, profile)
            if profile['profile_id'] == 'other-profile':
                report['descriptor']['layout']['fixture'] = 64
            return report
        self.match.side_effect = disagree
        report = self.scan()
        self.assertEqual(report['status'], 'needs_review')
        self.assertIsNone(report['descriptor'])
        self.assertIn('profiles_disagree', report['unresolved'])
        self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_unknown_verification_entry_is_not_accepted(self):
        report = self.scan()
        for entry in ({'core_sha256': 'wrong'}, {'core_sha256': report['identity']['core_sha256'],
            'request_id': 'r', 'server_id': '0', 'recipient': 'filehelper', 'construction_verified': True}):
            adapter.atomic_json(self.cache / 'verified.json', {adapter.verification_key(report): {'text': entry}})
            self.assertEqual(adapter.capabilities(report, cache=self.cache), dict.fromkeys(adapter.KINDS, False))

    def test_corrupted_verification_shapes_stay_unverified(self):
        report = self.scan()
        key = adapter.verification_key(report)
        for value in ([], {key: []}, {key: {'text': []}}, {key: {'text': {
                'core_sha256': report['identity']['core_sha256'], 'request_id': 'r',
                'server_id': 'not-a-number', 'recipient': 'filehelper', 'construction_verified': True}}}):
            with self.subTest(value=value):
                adapter.atomic_json(self.cache / 'verified.json', value)
                self.assertEqual(adapter.capabilities(report, cache=self.cache), dict.fromkeys(adapter.KINDS, False))

    def test_summary_omits_runtime_layout_and_internal_receipts(self):
        report = self.scan()
        result = adapter.summary(report)
        self.assertNotIn('descriptor', result)
        self.assertNotIn('cache_key', result)
        self.assertEqual(result['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_no_cache_mode_does_not_create_private_artifacts(self):
        report = adapter.scan(self.core, profiles_dir=self.profiles, cache=None)
        self.assertFalse(self.cache.exists())
        self.assertIsNone(report['report_path'])
        self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_arbitrary_new_exact_profile_is_not_delivery_authorization(self):
        def exact(core, profile):
            report = self.match_profile(core, profile)
            report['status'] = 'exact'
            return report
        self.match.side_effect = exact
        report = self.scan()
        self.assertFalse(report['known_verified_image'])
        self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, False))
        self.assertFalse(adapter.runtime_descriptor(report)['permissions']['native_calls'])
        self.verify(report)
        self.assertEqual(self.scan()['capabilities'], {'text': True, 'image': False, 'voice': False})

    def test_historical_baseline_with_identical_arm64_slice_is_trusted(self):
        self.write_profile(baseline={'core_sha256': adapter.VERIFIED_BASELINE_CORE_SHA256,
                                    'arm64_sha256': adapter.VERIFIED_BASELINE_ARM64_SHA256})
        report = self.scan()
        self.assertTrue(report['known_verified_image'])
        self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, True))
        self.assertEqual(report['validation_scope'], 'previously_verified_native_image')
        permissions = adapter.runtime_descriptor(report, construct=True)['permissions']
        self.assertTrue(permissions['native_calls'])
        self.assertTrue(all(not permissions['send_' + kind] for kind in adapter.KINDS))
        permissions = adapter.runtime_descriptor(report, test_kind='voice')['permissions']
        self.assertEqual(permissions, {'native_calls': True, 'send_text': False,
                                      'send_image': False, 'send_voice': True})

    def test_historical_baseline_does_not_trust_changed_slice_or_new_full_hash(self):
        for full, arm in ((adapter.VERIFIED_BASELINE_CORE_SHA256, 'a' * 64),
                          ('b' * 64, adapter.VERIFIED_BASELINE_ARM64_SHA256)):
            with self.subTest(full=full, arm=arm):
                self.write_profile(baseline={'core_sha256': full, 'arm64_sha256': arm})
                report = self.scan()
                self.assertFalse(report['known_verified_image'])
                self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_profile_cannot_claim_historical_full_hash_with_another_matching_slice(self):
        forged_arm = 'b' * 64
        self.write_profile(baseline={'core_sha256': adapter.VERIFIED_BASELINE_CORE_SHA256,
                                    'arm64_sha256': forged_arm})
        def forged(core, profile):
            report = self.match_profile(core, profile)
            report['identity']['arm64_sha256'] = forged_arm
            return report
        self.match.side_effect = forged
        report = self.scan()
        self.assertFalse(report['known_verified_image'])
        self.assertEqual(report['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_receipt_recipient_and_message_kind_when_present_must_agree(self):
        report = self.scan()
        for kind in adapter.KINDS:
            for where, field, bad_value in (('result', 'recipient', 'wxid_other'),
                    ('record', 'recipient', 'wxid_other'), ('record', 'local_type', 999)):
                with self.subTest(kind=kind, where=where, field=field):
                    result = self.receipt(report, kind)
                    destination = result if where == 'result' else result['result']['native_record']
                    destination[field] = bad_value
                    with self.assertRaises(ValueError):
                        self.verify(report, result, kind)
        self.assertFalse((self.cache / 'verified.json').exists())
        result = self.receipt(report, 'voice')
        result['recipient'] = 'filehelper'
        result['result']['native_record']['local_type'] = 34
        self.verify(report, result, 'voice')
        self.assertEqual(self.scan()['capabilities'], {'text': False, 'image': False, 'voice': True})

    def test_implementation_digest_includes_all_receipt_producers(self):
        sources = self.root / 'digest-sources'; sources.mkdir()
        names = ('adapter.py', 'adapter_matcher.py', 'transport.mjs', 'runtime.js',
                 'native_message_bridge.cpp', 'wechat.py', 'media.py', 'adapter_workflow.py')
        for name in names:
            (sources / name).write_text('fixture ' + name)
        with patch.object(adapter, 'ROOT', sources):
            # Call the saved function; all source reads remain in this fixture.
            digest = REAL_IMPLEMENTATION_DIGEST()
            for name in ('wechat.py', 'media.py', 'adapter_workflow.py'):
                path = sources / name
                before = path.read_text()
                path.write_text(before + ' changed')
                self.assertNotEqual(REAL_IMPLEMENTATION_DIGEST(), digest)
                path.write_text(before)

    def test_cached_known_image_flag_is_recomputed_from_current_profiles(self):
        report = self.scan()
        path = Path(report['report_path'])
        cached = json.loads(path.read_text())
        cached['known_verified_image'] = True
        cached['capabilities'] = dict.fromkeys(adapter.KINDS, True)
        path.write_text(json.dumps(cached))
        fresh = self.scan()
        self.assertFalse(fresh['known_verified_image'])
        self.assertEqual(fresh['capabilities'], dict.fromkeys(adapter.KINDS, False))

    def test_invalid_binary_match_still_returns_diagnostic_identity(self):
        self.match.return_value = {'status': 'needs_review', 'profile_id': 'fixture-profile',
                                  'identity': None, 'descriptor': None, 'unresolved': ['invalid-input']}
        self.match.side_effect = None
        report = self.scan()
        self.assertEqual(report['identity']['core_path'], str(self.core.resolve()))
        self.assertEqual(report['identity']['core_sha256'], adapter.digest_file(self.core))
        self.assertIsNone(adapter.prepare_runtime(report, {'action': 'status'}))

    def test_prepare_runtime_passive_status_and_constructor_probe(self):
        report = self.scan()
        passive = adapter.prepare_runtime(report, {'action': 'status'})
        self.assertFalse(passive['permissions']['native_calls'])
        constructor = adapter.prepare_runtime(report, {'action': 'probe', 'construct_only': True})
        self.assertTrue(constructor['permissions']['native_calls'])
        self.assertTrue(all(not constructor['permissions']['send_' + kind] for kind in adapter.KINDS))
        with self.assertRaises(ValueError):
            adapter.prepare_runtime(report, {'action': 'send', 'construct_only': True})

    def test_prepare_runtime_unverified_normal_send_is_rejected(self):
        report = self.scan()
        for action in adapter.SEND_ACTIONS:
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                adapter.prepare_runtime(report, {'action': action, 'to': 'filehelper'})

    def test_prepare_runtime_authorizes_only_attributed_filehelper_kind(self):
        report = self.scan()
        for action, kind in adapter.SEND_ACTIONS.items():
            with self.subTest(action=action):
                request = {'action': action, 'to': 'filehelper', 'request_id': 'fixture-request-1',
                           'adapter_test': self.receipt(report, kind)['result']['adapter_test']}
                descriptor = adapter.prepare_runtime(report, request)
                self.assertEqual(descriptor['test_recipient'], 'filehelper')
                self.assertTrue(descriptor['permissions']['send_' + kind])
                for field, value in (('to', 'wxid_alice'), ('request_id', 'other-request')):
                    wrong = copy.deepcopy(request); wrong[field] = value
                    with self.assertRaises(ValueError):
                        adapter.prepare_runtime(report, wrong)
                for field, value in (('kind', 'unknown'), ('core_sha256', 'other-core'), ('request_id', 'other-request')):
                    wrong = copy.deepcopy(request); wrong['adapter_test'][field] = value
                    with self.assertRaises(ValueError):
                        adapter.prepare_runtime(report, wrong)
                wrong = copy.deepcopy(request)
                wrong['action'] = 'send-image' if kind == 'text' else 'send'
                with self.assertRaises(ValueError):
                    adapter.prepare_runtime(report, wrong)

    def test_prepare_runtime_unknown_version_cannot_construct_or_send(self):
        report = self.scan()
        report.update(status='needs_review', descriptor=None)
        self.assertIsNone(adapter.prepare_runtime(report, {'action': 'status'}))
        self.assertIsNone(adapter.prepare_runtime(report, {'action': 'probe'}))
        for request in ({'action': 'probe', 'construct_only': True}, {'action': 'send', 'to': 'filehelper'}):
            with self.assertRaises(RuntimeError):
                adapter.prepare_runtime(report, request)

    def test_prepare_runtime_test_probe_requires_same_attribution_as_send(self):
        report = self.scan()
        for action in ('status', 'probe'):
            request = {'action': action, 'to': 'filehelper', 'request_id': 'fixture-request-1',
                       'adapter_test': self.receipt(report, 'voice')['result']['adapter_test']}
            self.assertEqual(adapter.prepare_runtime(report, request)['test_recipient'], 'filehelper')
            request['to'] = 'wxid_other'
            with self.assertRaises(ValueError):
                adapter.prepare_runtime(report, request)

    def test_runtime_request_cli_returns_authoritative_descriptor_without_native_access(self):
        report = self.scan()
        output = io.StringIO()
        with patch.object(adapter, 'scan', return_value=report) as scan, contextlib.redirect_stdout(output):
            adapter.main(['--runtime-request', '{"action":"status"}'])
        result = json.loads(output.getvalue())
        self.assertFalse(result['runtime_descriptor']['permissions']['native_calls'])
        self.assertIn('descriptor', result)
        scan.assert_called_once()


if __name__ == '__main__':
    unittest.main()
