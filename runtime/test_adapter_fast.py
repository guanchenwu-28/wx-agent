"""Fast adapter identity cache fixtures; no WeChat, Frida, or real sends."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType
import sys
import unittest
from unittest.mock import Mock, patch

import adapter


class AdapterFastTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / 'core.dylib'
        self.core.write_bytes(b'first native image')
        self.profiles = self.root / 'profiles'; self.profiles.mkdir()
        self.profile = self.profiles / 'fixture.json'
        self.profile.write_text(json.dumps({'schema_version': 1, 'profile_id': 'fixture'}))
        self.sources = self.root / 'sources'; self.sources.mkdir()
        for name in adapter.IMPLEMENTATION_FILES:
            (self.sources / name).write_text('fixture ' + name)
        self.enterContext(patch.object(adapter, 'ROOT', self.sources))
        self.cache = self.root / 'cache'
        self.match = Mock(side_effect=self.match_profile)
        module = ModuleType('adapter_matcher'); module.match_profile = self.match
        self.enterContext(patch.dict(sys.modules, {'adapter_matcher': module}))
        self.hash = self.enterContext(patch.object(adapter, 'digest_file', wraps=adapter.digest_file))
        self.process = {'pid': 123, 'start': 'process-start-1', 'exe': '/fixture/WeChat'}

    @staticmethod
    def match_profile(core, profile):
        digest = hashlib.sha256(Path(core).read_bytes()).hexdigest()
        return {'status': 'matched', 'profile_id': profile['profile_id'],
                'identity': {'core_sha256': digest, 'arm64_sha256': 'fixture-arm64'},
                'descriptor': {'core_sha256': digest, 'offsets': {'entry': 16},
                               'vtables': {'entry': 32}, 'layout': {'recipient': 144}},
                'unresolved': []}

    def scan(self, **kwargs):
        kwargs.setdefault('process_identity', self.process)
        return adapter.scan(self.core, profiles_dir=self.profiles, cache=self.cache, **kwargs)

    def test_unchanged_process_and_files_skip_large_hash_and_match(self):
        first = self.scan(); second = self.scan()
        self.assertEqual(self.hash.call_count, 1)
        self.assertEqual(self.match.call_count, 1)
        self.assertEqual(first['cache_info'], {'fast_hit': False, 'core_hash_checked': True,
                                              'matched_recomputed': True})
        self.assertEqual(second['cache_info'], {'fast_hit': True, 'core_hash_checked': False,
                                               'matched_recomputed': False})
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(second['process_identity'], self.process)

    def test_process_restart_rehashes_but_reuses_content_match(self):
        first = self.scan()
        second = self.scan(process_identity={**self.process, 'pid': 456, 'start': 'process-start-2'})
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 1)
        self.assertEqual(second['cache_info'], {'fast_hit': False, 'core_hash_checked': True,
                                               'matched_recomputed': False})
        self.assertEqual(first['cache_key'], second['cache_key'])

    def test_pid_reuse_with_new_start_identity_rehashes(self):
        self.scan()
        self.scan(process_identity={**self.process, 'start': 'new-start-same-pid'})
        self.assertEqual(self.hash.call_count, 2)

    def test_refresh_rehashes_and_rematches_even_if_unchanged(self):
        self.scan(); result = self.scan(refresh=True)
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 2)
        self.assertTrue(result['cache_info']['matched_recomputed'])

    def test_core_change_with_restored_mtime_is_detected_by_ctime(self):
        first = self.scan()
        before = self.core.stat()
        self.core.write_bytes(b'other native image')
        os.utime(self.core, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = self.scan()
        self.assertNotEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(first['core_stat']['mtime_ns'], second['core_stat']['mtime_ns'])
        self.assertNotEqual(first['core_stat']['ctime_ns'], second['core_stat']['ctime_ns'])
        self.assertEqual(self.hash.call_count, 2)

    def test_inode_replacement_rehashes_even_for_same_content(self):
        first = self.scan()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.core.read_bytes())
        replacement.replace(self.core)
        second = self.scan()
        self.assertNotEqual(first['core_stat']['ino'], second['core_stat']['ino'])
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 1)

    def test_profile_addition_invalidates_dependencies(self):
        first = self.scan()
        (self.profiles / 'second.json').write_text(json.dumps({'schema_version': 1, 'profile_id': 'second'}))
        second = self.scan()
        self.assertEqual(second['dependency_stats']['profile_names'], ['fixture.json', 'second.json'])
        self.assertNotEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 3)

    def test_optional_broker_creation_invalidates_implementation(self):
        first = self.scan()
        broker = self.sources / 'transport_session.mjs'
        self.assertIsNone(first['dependency_stats']['files'][str(broker)])
        broker.write_text('new session broker')
        second = self.scan()
        self.assertNotEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 2)

    def test_native_client_change_invalidates_implementation(self):
        client = self.sources / 'native_client.py'; client.write_text('old client')
        first = self.scan(); client.write_text('new client')
        second = self.scan()
        self.assertNotEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(self.hash.call_count, 2)

    def test_receipt_change_refreshes_permissions_without_large_hash(self):
        first = self.scan()
        receipt = {'core_sha256': first['identity']['core_sha256'], 'request_id': 'fixture-id',
                   'server_id': '1', 'recipient': 'filehelper', 'construction_verified': True}
        adapter.atomic_json(self.cache / 'verified.json', {adapter.verification_key(first): {'text': receipt}})
        second = self.scan()
        self.assertTrue(second['capabilities']['text'])
        self.assertTrue(second['cache_info']['fast_hit'])
        adapter.atomic_json(self.cache / 'verified.json', {})
        third = self.scan()
        self.assertFalse(third['capabilities']['text'])
        self.assertEqual(self.hash.call_count, 1)
        self.assertEqual(self.match.call_count, 1)

    def test_corrupt_fast_index_falls_back_to_validated_content_report(self):
        first = self.scan()
        next((self.cache / 'fast').glob('*.json')).write_text('invalid json')
        second = self.scan()
        self.assertEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 1)

    def test_changed_report_is_validated_again_before_use(self):
        first = self.scan()
        path = Path(first['report_path'])
        cached = json.loads(path.read_text()); cached['descriptor']['offsets'] = None
        path.write_text(json.dumps(cached))
        second = self.scan()
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 2)
        self.assertEqual(second['descriptor']['offsets'], {'entry': 16})

    def test_core_change_during_hash_is_rejected_and_not_cached(self):
        original = adapter.digest_file.__wrapped__ if hasattr(adapter.digest_file, '__wrapped__') else self.hash._mock_wraps
        def change_after_hash(path):
            digest = original(path)
            self.core.write_bytes(b'changed during hash')
            return digest
        self.hash.side_effect = change_after_hash
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            self.scan()
        self.assertFalse((self.cache / 'matches').exists())
        self.assertFalse((self.cache / 'fast').exists())

    def test_profile_change_during_match_is_rejected_and_not_cached(self):
        def change_after_match(core, profile):
            result = self.match_profile(core, profile)
            self.profile.write_text(json.dumps({**profile, 'changed': True}))
            return result
        self.match.side_effect = change_after_match
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            self.scan()
        self.assertFalse((self.cache / 'matches').exists())
        self.assertFalse((self.cache / 'fast').exists())

    def test_fast_hit_rechecks_stats_after_small_profile_read(self):
        self.scan()
        load = adapter.load_profiles
        def change_after_load(directory):
            profiles = load(directory)
            self.core.write_bytes(b'changed during fast load')
            return profiles
        with patch.object(adapter, 'load_profiles', side_effect=change_after_load):
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                self.scan()
        self.assertEqual(self.hash.call_count, 1)

    def test_stats_are_exact_decimal_strings_and_include_receipts(self):
        result = self.scan()
        stat = self.core.stat()
        self.assertEqual(result['core_stat']['mtime_ns'], str(stat.st_mtime_ns))
        self.assertTrue(all(isinstance(value, str) and value.isdecimal()
                            for value in result['core_stat'].values()))
        snapshot = result['dependency_stats']
        self.assertEqual(snapshot['verification_path'], str(self.cache.resolve() / 'verified.json'))
        self.assertIsNone(snapshot['files'][snapshot['verification_path']])
        self.assertTrue(all(Path(path).is_absolute() for path in snapshot['files']))

    def test_invalid_process_identity_is_rejected(self):
        for identity in ([], '123', 123, {'pid': float('nan')}):
            with self.subTest(identity=identity), self.assertRaises((ValueError, TypeError)):
                self.scan(process_identity=identity)

    def test_no_cache_always_rechecks_and_never_creates_files(self):
        for _ in range(2):
            result = adapter.scan(self.core, profiles_dir=self.profiles, cache=None,
                                  process_identity=self.process)
            self.assertFalse(result['cache_info']['fast_hit'])
            self.assertIsNone(result['report_path'])
        self.assertEqual(self.hash.call_count, 2)
        self.assertEqual(self.match.call_count, 2)
        self.assertFalse(self.cache.exists())


if __name__ == '__main__':
    unittest.main()
