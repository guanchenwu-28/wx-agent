"""Service fixtures use temporary files and never operate a real service."""
from pathlib import Path
import plistlib
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

import service_manager


RUNNING_JOB = '''gui/501/com.wxagent.wechat-autoreply = {
\tactive count = 1
\tpath = /Users/fixture/Library/LaunchAgents/com.wxagent.wechat-autoreply.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /Users/fixture/.local/bin/python3
\targuments = {
\t\t/Users/fixture/.local/bin/python3
\t\t/Users/fixture/Library/Application Support/WeChatLocalTool/runtime/autoreply.py
\t\tserve
\t}

\truns = 2
\tpid = 24680
\tlast exit code = 0
\tresource coalition = {
\t\tID = 101
\t\ttype = resource
\t\tstate = active
\t\tactive count = 1
\t\tname = com.wxagent.wechat-autoreply
\t}
\tjetsam coalition = {
\t\tID = 102
\t\ttype = jetsam
\t\tstate = active
\t\tactive count = 1
\t\tname = com.wxagent.wechat-autoreply
\t}
\tproperties = keepalive | runatload
}
'''


class ServiceStatusTests(unittest.TestCase):
    def status(self, output, returncode=0, disabled_output='disabled services = {\n}\n', disabled_returncode=0):
        completed = SimpleNamespace(returncode=returncode, stdout=output, stderr='fixture')
        disabled = SimpleNamespace(returncode=disabled_returncode, stdout=disabled_output, stderr='fixture')
        with patch.object(service_manager, 'run', side_effect=[completed, disabled]) as run:
            result = service_manager.service_status()
        self.assertEqual(run.call_args_list, [call('print', service_manager.SERVICE),
                                             call('print-disabled', service_manager.DOMAIN)])
        return result

    def test_running_is_not_overwritten_by_active_nested_coalitions(self):
        result = self.status(RUNNING_JOB)
        self.assertTrue(result['loaded'])
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['pid'], '24680')
        self.assertEqual(result['runs'], '2')
        self.assertEqual(result['last exit code'], '0')
        self.assertEqual(result['label'], service_manager.LABEL)
        self.assertEqual(result['plist'], str(service_manager.PLIST))

    def test_all_tracked_fields_are_restricted_to_outer_job_block(self):
        output = '''state = outside-before
gui/501/job = {
    state = waiting
    pid = 123
    runs = 4
    last exit code = 78
    nested object = {
        state = active
        pid = 999
        runs = 999
        last exit code = 999
        child = {
            state = another-nested-value
        }
    }
}
state = outside-after
pid = 456
'''
        result = self.status(output)
        self.assertEqual({key: result[key] for key in ('state', 'pid', 'runs', 'last exit code')},
                         {'state': 'waiting', 'pid': '123', 'runs': '4', 'last exit code': '78'})

    def test_nested_fields_cannot_supply_missing_job_fields(self):
        output = '''gui/501/job = {
    state = waiting
    resource coalition = {
        pid = 999
        runs = 99
        last exit code = 7
    }
}
'''
        result = self.status(output)
        self.assertEqual(result['state'], 'waiting')
        self.assertNotIn('pid', result)
        self.assertNotIn('runs', result)
        self.assertNotIn('last exit code', result)

    def test_nonzero_print_result_reports_unloaded_without_parsing_partial_output(self):
        result = self.status(RUNNING_JOB, returncode=113)
        self.assertFalse(result['loaded'])
        self.assertEqual(set(result), {'loaded', 'label', 'plist', 'disabled'})

    def test_literal_braces_in_path_do_not_change_structural_depth(self):
        output = '''gui/501/job = {
    path = /Users/fixture/{research}/service.plist
    state = running
    arguments = {
        /Users/fixture/{research}/service.py
    }
    pid = 456
}
'''
        result = self.status(output)
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['pid'], '456')

    def test_success_without_fields_does_not_invent_runtime_state(self):
        result = self.status('gui/501/job = {\n}\n')
        self.assertTrue(result['loaded'])
        self.assertEqual(set(result), {'loaded', 'label', 'plist', 'disabled'})

    def test_disabled_override_uses_exact_label_and_boolean(self):
        for value in ('true', 'false'):
            output = 'disabled services = {\n    "' + service_manager.LABEL + '" => ' + value + '\n}\n'
            self.assertIs(self.status('', returncode=113, disabled_output=output)['disabled'], value == 'true')
        other = 'disabled services = {\n    "' + service_manager.LABEL + '.other" => true\n}\n'
        self.assertIs(self.status('', returncode=113, disabled_output=other)['disabled'], False)

    def test_macos_named_disabled_enabled_values_are_read_back(self):
        for value, expected in (('disabled', True), ('enabled', False)):
            output = ('disabled services = {\n'
                      '    "com.example.other" => disabled\n'
                      '    "' + service_manager.LABEL + '" => ' + value + '\n'
                      '    "com.example.another" => enabled\n}\n')
            with self.subTest(value=value):
                self.assertIs(self.status('', returncode=113, disabled_output=output)['disabled'], expected)

    def test_unrecognized_value_for_exact_label_is_unknown_not_enabled(self):
        output = 'disabled services = {\n    "' + service_manager.LABEL + '" => unknown-format\n}\n'
        self.assertIsNone(self.status('', returncode=113, disabled_output=output)['disabled'])

    def test_failed_or_unrecognized_disabled_readback_is_unknown(self):
        for output, code in (('', 0), ('partial output', 0), ('disabled services = {\n}\n', 113)):
            self.assertIsNone(self.status('', disabled_output=output, disabled_returncode=code)['disabled'])


class ServiceStartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / 'project'
        self.runtime = self.base / 'runtime'
        self.home = self.base / 'queue-home'
        self.logs = self.base / 'logs'
        self.plist = self.base / 'LaunchAgents' / 'fixture.plist'
        for name, value in (('ROOT', self.project), ('RUNTIME', self.runtime),
                            ('HOME', self.home), ('LOGS', self.logs), ('PLIST', self.plist)):
            self.enterContext(patch.object(service_manager, name, value))
        self.registry = self.enterContext(patch.object(service_manager, 'Registry'))
        self.registry.return_value.load.return_value = {'enabled': False}
        self.run = self.enterContext(patch.object(service_manager, 'run', return_value=
            SimpleNamespace(returncode=0, stdout='', stderr='')))
        self.copy = self.enterContext(patch.object(service_manager.shutil, 'copy2', wraps=shutil.copy2))
        self.loaded = {'loaded': True, 'state': 'running', 'pid': 'fixture', 'disabled': False}

    def start_unloaded(self):
        with patch.object(service_manager, 'service_status', side_effect=[{'loaded': False}, self.loaded]):
            result = service_manager.start()
        self.assertEqual(result, self.loaded)
        self.registry.return_value.load.assert_called_once_with()
        self.registry.return_value.set_enabled.assert_not_called()
        self.assertEqual(self.run.call_args_list, [call('enable', service_manager.SERVICE),
                         call('bootstrap', service_manager.DOMAIN, str(self.plist))])
        config = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(config['WorkingDirectory'], str(self.runtime))
        self.assertEqual(config['ProgramArguments'][1], str(self.runtime / 'autoreply.py'))
        return result

    def test_start_from_runtime_skips_copying_onto_itself(self):
        self.runtime.mkdir()
        source = self.runtime / 'autoreply.py'
        source.write_text('# deployed runtime\n')
        with patch.object(service_manager, 'ROOT', self.runtime):
            self.start_unloaded()
        self.copy.assert_not_called()
        self.assertEqual(source.read_text(), '# deployed runtime\n')

    def test_start_from_project_copies_and_upgrades_runtime(self):
        self.project.mkdir()
        self.runtime.mkdir()
        (self.project / 'autoreply.py').write_text('# new runtime\n')
        (self.project / 'transport.mjs').write_text('// transport\n')
        (self.project / 'test_fixture.py').write_text('# excluded test\n')
        (self.project / 'README.md').write_text('excluded documentation\n')
        (self.runtime / 'autoreply.py').write_text('# old runtime\n')
        self.start_unloaded()
        self.assertEqual(self.copy.call_count, 2)
        self.assertEqual((self.runtime / 'autoreply.py').read_text(), '# new runtime\n')
        self.assertEqual((self.runtime / 'transport.mjs').read_text(), '// transport\n')
        self.assertFalse((self.runtime / 'test_fixture.py').exists())
        self.assertFalse((self.runtime / 'README.md').exists())

    def test_same_file_via_hard_link_also_skips_copy(self):
        self.project.mkdir()
        self.runtime.mkdir()
        source = self.project / 'autoreply.py'
        source.write_text('# shared inode\n')
        (self.runtime / source.name).hardlink_to(source)
        self.start_unloaded()
        self.copy.assert_not_called()

    def test_already_loaded_does_not_copy_or_bootstrap(self):
        with patch.object(service_manager, 'service_status', return_value=self.loaded):
            self.assertEqual(service_manager.start(), self.loaded)
        self.registry.return_value.load.assert_called_once_with()
        self.registry.return_value.set_enabled.assert_not_called()
        self.copy.assert_not_called()
        self.run.assert_called_once_with('enable', service_manager.SERVICE)
        for path in (self.runtime, self.home, self.logs, self.plist.parent):
            self.assertFalse(path.exists())

    def test_loaded_but_disabled_service_is_enabled_without_bootstrap(self):
        with patch.object(service_manager, 'service_status', side_effect=[
                {**self.loaded, 'disabled': True}, self.loaded]):
            result = service_manager.start()
        self.assertIs(result['disabled'], False)
        self.run.assert_called_once_with('enable', service_manager.SERVICE)
        self.copy.assert_not_called()

    def test_enable_failure_does_not_bootstrap(self):
        self.project.mkdir()
        self.run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='denied')
        with patch.object(service_manager, 'service_status', return_value={'loaded': False}):
            with self.assertRaisesRegex(RuntimeError, 'enable failed'):
                service_manager.start()
        self.run.assert_called_once_with('enable', service_manager.SERVICE)

    def test_profiles_only_direct_json_files_are_deployed(self):
        profiles = self.project / 'profiles'; profiles.mkdir(parents=True)
        (profiles / 'verified.json').write_text('{"profile":"verified"}')
        (profiles / 'private.txt').write_text('do not deploy')
        (profiles / 'nested').mkdir()
        (profiles / 'nested' / 'private.json').write_text('{}')
        outside = self.project / 'private.json'; outside.write_text('{}')
        (profiles / 'linked.json').symlink_to(outside)
        self.start_unloaded()
        self.assertEqual(sorted(path.name for path in (self.runtime / 'profiles').iterdir()), ['verified.json'])
        self.assertEqual((self.runtime / 'profiles' / 'verified.json').read_text(), '{"profile":"verified"}')
        self.assertFalse((self.runtime / 'private.json').exists())

    def test_profiles_start_from_runtime_skips_same_file(self):
        profiles = self.runtime / 'profiles'; profiles.mkdir(parents=True)
        (profiles / 'verified.json').write_text('{}')
        with patch.object(service_manager, 'ROOT', self.runtime):
            self.start_unloaded()
        self.copy.assert_not_called()


class ServiceStopTests(unittest.TestCase):
    def setUp(self):
        self.connections = self.enterContext(patch.object(service_manager, 'close_native_connections',
                                                          return_value=[{'ok': True, 'running': False}]))

    def test_stop_gates_then_disables_then_unloads(self):
        events = []
        with patch.object(service_manager, 'Registry') as registry, \
                patch.object(service_manager, 'run', side_effect=lambda *args: events.append(args)), \
                patch.object(service_manager, 'service_status', return_value={'loaded': False, 'disabled': True}):
            registry.return_value.set_enabled.side_effect = lambda *args: events.append(('gate', *args))
            result = service_manager.stop()
        self.assertEqual(events, [('gate', None, False), ('disable', service_manager.SERVICE),
                                  ('bootout', service_manager.SERVICE)])
        self.assertTrue(result['disabled'])
        self.connections.assert_called_once_with()
        self.assertTrue(result['native_connections_closed'])

    def test_pending_connection_shutdown_does_not_claim_complete_pause(self):
        self.connections.return_value = [{'ok': False, 'running': None}]
        with patch.object(service_manager, 'run') as run, \
                patch.object(service_manager, 'service_status', return_value={'loaded': False, 'disabled': True}):
            with self.assertRaisesRegex(RuntimeError, 'shutdown is not yet confirmed'):
                service_manager.unload()
        self.assertEqual(run.call_args_list, [call('disable', service_manager.SERVICE), call('bootout', service_manager.SERVICE)])

    def test_disable_failure_still_unloads_but_is_not_reported_persistent(self):
        with patch.object(service_manager, 'run', return_value=SimpleNamespace(returncode=1)) as run, \
                patch.object(service_manager, 'service_status', return_value={'loaded': False, 'disabled': False}):
            with self.assertRaisesRegex(RuntimeError, 'persistent launchd pause'):
                service_manager.unload()
        self.assertEqual(run.call_args_list, [call('disable', service_manager.SERVICE), call('bootout', service_manager.SERVICE)])

    def test_still_loaded_is_not_reported_stopped(self):
        with patch.object(service_manager, 'run'), \
                patch.object(service_manager, 'service_status', return_value={'loaded': True, 'disabled': True}):
            with self.assertRaisesRegex(RuntimeError, 'still loaded'):
                service_manager.unload()

    def test_timed_out_commands_are_verified_by_readback(self):
        with patch.object(service_manager, 'run', side_effect=service_manager.subprocess.TimeoutExpired('fixture', 5)) as run, \
                patch.object(service_manager, 'service_status', return_value={'loaded': False, 'disabled': True}):
            self.assertTrue(service_manager.unload()['disabled'])
        self.assertEqual(run.call_count, 2)


class NativeConnectionCleanupTests(unittest.TestCase):
    def test_pause_closes_project_and_installed_brokers_without_starting_them(self):
        with patch('native_client.stop', return_value={'ok': True, 'running': False}) as stop, \
                patch.object(service_manager, 'ROOT', Path('/private/tmp/fixture-project')), \
                patch.object(service_manager, 'SOURCE_RUNTIME', Path('/private/tmp/fixture-project')), \
                patch.object(service_manager, 'RUNTIME', Path('/private/tmp/fixture-installed')):
            result = service_manager.close_native_connections()
        self.assertEqual(len(result), 2)
        self.assertEqual(stop.call_args_list, [call(runtime=Path('/private/tmp/fixture-project'), timeout=5),
                                              call(runtime=Path('/private/tmp/fixture-installed'), timeout=5)])

    def test_pause_from_installed_runtime_also_closes_source_broker(self):
        with patch('native_client.stop', return_value={'ok': True, 'running': False}) as stop, \
                patch.object(service_manager, 'ROOT', Path('/private/tmp/fixture-installed')), \
                patch.object(service_manager, 'SOURCE_RUNTIME', Path('/private/tmp/fixture-project')), \
                patch.object(service_manager, 'RUNTIME', Path('/private/tmp/fixture-installed')):
            service_manager.close_native_connections()
        self.assertEqual(stop.call_args_list, [call(runtime=Path('/private/tmp/fixture-installed'), timeout=5),
                                              call(runtime=Path('/private/tmp/fixture-project'), timeout=5)])

    def test_same_known_runtime_is_closed_once(self):
        with patch('native_client.stop', return_value={'ok': True, 'running': False}) as stop, \
                patch.object(service_manager, 'ROOT', Path('/private/tmp/fixture-installed')), \
                patch.object(service_manager, 'SOURCE_RUNTIME', Path('/private/tmp/fixture-installed')), \
                patch.object(service_manager, 'RUNTIME', Path('/private/tmp/fixture-installed')):
            service_manager.close_native_connections()
        self.assertEqual(stop.call_count, 1)


if __name__ == '__main__':
    unittest.main()
