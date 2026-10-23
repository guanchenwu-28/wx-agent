"""Control helper fixtures: never read WeChat or change launchd/user configuration."""
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('skill_control', ROOT / 'skills/wechat-native/scripts/control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
ALICE, BOB, GROUP = 'wxid_alice', 'wxid_bob', '12345@chatroom'


def validate_rule(rule):
    if not isinstance(rule, dict) or rule.get('mode') not in ('all', 'mention', 'keywords', 'semantic'):
        raise ValueError('invalid fixture rule')


class FakeRegistry:
    def __init__(self, home, events):
        self.home, self.events = home, events
        self.config = {'account': 'fixture_account', 'self_username': 'fixture_self',
                       'enabled': True, 'targets': {}}
        self.configure_calls = []

    def target_dir(self, recipient):
        return self.home / recipient

    def load(self):
        if self.config is None:
            raise FileNotFoundError('fixture config absent')
        return copy.deepcopy(self.config)

    def save(self, config):
        self.config = copy.deepcopy(config)
        self.events.append(('save', copy.deepcopy(config)))

    def add(self, recipient=ALICE, enabled=False, condition=None, style='PRIVATE_STYLE', pending=None):
        self.config['targets'][recipient] = {
            'name': 'fixture name', 'enabled': enabled, 'revision': 'old-revision-' + recipient,
            'condition': condition or {'mode': 'all'}, 'style': style, 'quiet_seconds': 10,
            'kind': 'group' if recipient.endswith('@chatroom') else 'contact'}
        directory = self.target_dir(recipient)
        directory.mkdir(parents=True, exist_ok=True)
        state = {'account': 'fixture_account', 'recipient': recipient, 'generation': 'old-generation',
                 'cursors': {'message/message_0.db': 4390}, 'pending': pending, 'blocked': None,
                 'sent_keys': ['message/message_0.db:4388']}
        control.atomic_json(directory / 'state.json', state)
        control.atomic_json(directory / 'enabled.json', {'generation': 'old-generation'})
        return state

    def configure(self, recipient, name, condition, enabled, style, quiet_seconds):
        self.configure_calls.append((recipient, name, copy.deepcopy(condition), enabled, style, quiet_seconds))
        state = self.add(recipient, enabled=enabled, condition=condition, style=style or 'DEFAULT_STYLE')
        state['generation'] = 'configured-generation'
        control.atomic_json(self.target_dir(recipient) / 'state.json', state)
        control.atomic_json(self.target_dir(recipient) / 'enabled.json', {'generation': state['generation']})
        self.config['targets'][recipient].update(name=name, quiet_seconds=quiet_seconds)
        self.events.append(('configure', recipient))


class FakeService:
    SERVICE = 'gui/501/fixture.service'
    def __init__(self, registry, events):
        self.registry, self.events = registry, events
        self.loaded = False
        self.disabled = False
        self.running = True
        self.start_error = None
        self.stop_stuck = False
        self.bootout_timeout = False
        self.listener(123, 'listening')

    def listener(self, pid, state):
        self.registry.home.mkdir(parents=True, exist_ok=True)
        control.atomic_json(self.registry.home / 'service.json',
                            {'pid': pid, 'state': state, 'at': 12345, 'private': 'PRIVATE_LISTENER'})

    def service_status(self):
        return {'loaded': self.loaded, 'state': 'running' if self.running else 'waiting',
                'pid': '123' if self.running else None, 'disabled': self.disabled}

    def start(self):
        self.events.append(('start',))
        if self.start_error:
            raise self.start_error
        self.disabled = False
        self.loaded = True
        return self.service_status()

    def run(self, *arguments):
        self.events.append((arguments[0], arguments, copy.deepcopy(self.registry.config)))
        if arguments[0] == 'disable':
            self.disabled = True
        if arguments[0] == 'bootout' and not self.stop_stuck:
            self.loaded = False
        if arguments[0] == 'bootout' and self.bootout_timeout:
            raise subprocess.TimeoutExpired('fixture launchctl', 5)
        return SimpleNamespace(returncode=0)

    def unload(self):
        for command in ('disable', 'bootout'):
            try:
                self.run(command, self.SERVICE)
            except subprocess.TimeoutExpired:
                pass
        return self.service_status()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.events = []
        self.registry = FakeRegistry(self.home / 'queues', self.events)
        self.service = FakeService(self.registry, self.events)
        self.clock = 0
        def sleep(seconds):
            self.clock += seconds
        self.controller = control.Controller(self.registry, self.service, validate_rule,
                                             now=lambda: self.clock, sleep=sleep, verify_timeout=.3)

    def read_queue(self, recipient):
        return json.loads((self.registry.target_dir(recipient) / 'state.json').read_text())

    def test_pause_all_gates_and_changes_revisions_before_bootout(self):
        self.registry.add(ALICE, enabled=True); self.registry.add(BOB, enabled=True)
        self.service.loaded = True
        result = self.controller.pause()
        self.assertTrue(result['service_stopped'])
        boot = next(event for event in self.events if event[0] == 'bootout')
        saved = boot[2]
        self.assertFalse(saved['enabled'])
        self.assertTrue(all(not target['enabled'] for target in saved['targets'].values()))
        self.assertTrue(all(target['revision'] != 'old-revision-' + recipient
                            for recipient, target in saved['targets'].items()))
        self.assertEqual(self.events[0][0], 'save')
        self.assertEqual([event[0] for event in self.events], ['save', 'disable', 'bootout'])
        self.assertFalse(self.events[1][2]['enabled'])
        self.assertEqual(boot[1], ('bootout', self.service.SERVICE))
        self.assertTrue(result['service']['disabled'])

    def test_pause_without_config_still_boots_out(self):
        self.registry.config = None; self.service.loaded = True
        result = self.controller.pause()
        self.assertTrue(result['ok'])
        self.assertTrue(result['service_stopped'])
        self.assertEqual([event[0] for event in self.events], ['disable', 'bootout'])

    def test_pause_one_preserves_other_active_target_and_service(self):
        self.registry.add(ALICE, enabled=True); self.registry.add(BOB, enabled=True)
        before = copy.deepcopy(self.registry.config['targets'][BOB])
        self.service.loaded = True
        result = self.controller.pause(ALICE)
        self.assertTrue(result['ok'])
        self.assertFalse(result['service_stopped'])
        self.assertEqual(self.registry.config['targets'][BOB], before)
        self.assertTrue(self.registry.config['enabled'])
        self.assertNotIn('bootout', [event[0] for event in self.events])
        self.assertNotIn('disable', [event[0] for event in self.events])

    def test_pause_last_target_stops_service(self):
        self.registry.add(ALICE, enabled=True); self.service.loaded = True
        self.assertTrue(self.controller.pause(ALICE)['service_stopped'])
        self.assertEqual([event[0] for event in self.events], ['save', 'disable', 'bootout'])

    def test_stop_timeout_is_verified_and_stuck_service_is_not_reported_stopped(self):
        self.registry.add(ALICE, enabled=True); self.service.loaded = True
        self.service.bootout_timeout = True
        self.assertTrue(self.controller.pause()['service_stopped'])
        self.service.loaded = True; self.service.stop_stuck = True
        with self.assertRaisesRegex(RuntimeError, 'unloaded'):
            self.controller.pause()
        self.assertFalse(self.registry.config['enabled'])

    def test_resume_single_target_after_global_pause_does_not_reactivate_other_flags(self):
        self.registry.add(ALICE, enabled=True); self.registry.add(BOB, enabled=True)
        self.registry.config['enabled'] = False
        result = self.controller.listen(ALICE, 'Alice')
        self.assertTrue(result['ok'])
        self.assertTrue(self.registry.config['enabled'])
        self.assertTrue(self.registry.config['targets'][ALICE]['enabled'])
        self.assertFalse(self.registry.config['targets'][BOB]['enabled'])
        self.assertEqual([event[0] for event in self.events].count('start'), 1)

    def test_group_without_condition_has_zero_changes_and_no_start(self):
        before = self.registry.load()
        with self.assertRaisesRegex(ValueError, 'explicit condition'):
            self.controller.listen(GROUP, 'new group')
        self.assertEqual(self.registry.config, before)
        self.assertEqual(self.events, [])
        self.assertEqual(self.registry.configure_calls, [])

    def test_existing_group_preserves_owner_rule_style_and_queue(self):
        rule = {'mode': 'semantic', 'instruction': 'owner rule', 'senders': [ALICE]}
        pending = {'batch_id': 'durable-batch', 'incoming': [{'text': 'PRIVATE_CHAT'}], 'inflight': None}
        before = self.registry.add(GROUP, condition=rule, pending=pending)
        self.service.loaded = True
        result = self.controller.listen(GROUP, 'renamed group')
        self.assertTrue(result['ok']); self.assertTrue(result['queue_preserved'])
        self.assertEqual(self.read_queue(GROUP), before)
        self.assertEqual(self.registry.config['targets'][GROUP]['condition'], rule)
        self.assertEqual(self.registry.config['targets'][GROUP]['style'], 'PRIVATE_STYLE')
        self.assertEqual(self.registry.configure_calls, [])
        self.assertEqual([event[0] for event in self.events].count('start'), 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_resume_restores_marker_without_resetting_generation_cursor_or_pending(self):
        before = self.registry.add(ALICE, pending={'batch_id': 'pending', 'inflight': None})
        (self.registry.target_dir(ALICE) / 'enabled.json').unlink()
        result = self.controller.listen(ALICE, 'Alice')
        self.assertTrue(result['queue_preserved'])
        self.assertEqual(self.read_queue(ALICE), before)
        marker = json.loads((self.registry.target_dir(ALICE) / 'enabled.json').read_text())
        self.assertEqual(marker['generation'], before['generation'])

    def test_blocked_or_inflight_queue_refuses_before_any_mutation(self):
        for blocked, pending in (({'reason': 'unknown'}, None), (None, {'inflight': 'request-1'})):
            with self.subTest(blocked=blocked):
                state = self.registry.add(ALICE, pending=pending)
                state['blocked'] = blocked
                control.atomic_json(self.registry.target_dir(ALICE) / 'state.json', state)
                before = self.registry.load()
                with self.assertRaisesRegex(ValueError, 'blocked or unknown'):
                    self.controller.listen(ALICE, 'Alice')
                self.assertEqual(self.registry.config, before)
                self.assertEqual(self.events, [])

    def test_new_contact_defaults_all_ten_seconds_and_uses_registry_configure(self):
        result = self.controller.listen(ALICE, 'Alice')
        self.assertTrue(result['ok']); self.assertFalse(result['queue_preserved'])
        self.assertEqual(self.registry.configure_calls[0][2], {'mode': 'all'})
        self.assertEqual(result['target']['quiet_seconds'], 10)
        self.assertTrue(result['target']['enabled'])

    def test_explicit_rule_reconfigures_but_unspecified_style_is_preserved(self):
        self.registry.add(ALICE)
        rule = {'mode': 'keywords', 'keywords': ['test']}
        self.controller.listen(ALICE, 'Alice', condition=rule)
        self.assertEqual(self.registry.configure_calls[0][2], rule)
        self.assertEqual(self.registry.configure_calls[0][4], 'PRIVATE_STYLE')

    def test_explicit_same_rule_preserves_queue_and_does_not_reconfigure(self):
        rule = {'mode': 'keywords', 'keywords': ['same']}
        before = self.registry.add(ALICE, condition=rule, pending={'batch_id': 'pending', 'texts': ['draft']})
        result = self.controller.listen(ALICE, 'Alice', condition=copy.deepcopy(rule))
        self.assertTrue(result['queue_preserved'])
        self.assertEqual(self.read_queue(ALICE), before)
        self.assertEqual(self.registry.configure_calls, [])

    def test_style_or_quiet_change_preserves_queue_without_rule_reconfigure(self):
        before = self.registry.add(ALICE, pending={'batch_id': 'pending', 'inflight': None})
        result = self.controller.listen(ALICE, 'Alice', style='new style', quiet_seconds=20)
        self.assertTrue(result['queue_preserved'])
        self.assertEqual(self.read_queue(ALICE), before)
        self.assertEqual(self.registry.configure_calls, [])
        self.assertEqual(self.registry.config['targets'][ALICE]['style'], 'new style')
        self.assertEqual(result['target']['quiet_seconds'], 20)

    def test_start_failure_closes_target_and_never_reports_active(self):
        self.service.start_error = RuntimeError('fixture failed startup')
        result = self.controller.listen(ALICE, 'Alice')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['target_enabled'])
        self.assertFalse(self.registry.config['targets'][ALICE]['enabled'])

    def test_loaded_but_not_running_is_not_success_and_does_not_redeploy(self):
        self.service.loaded = True; self.service.running = False
        result = self.controller.listen(ALICE, 'Alice')
        self.assertFalse(result['ok'])
        self.assertFalse(self.registry.config['targets'][ALICE]['enabled'])
        self.assertEqual([event[0] for event in self.events].count('start'), 1)

    def test_running_with_old_or_waiting_listener_is_not_active(self):
        for pid, state in ((999, 'listening'), (123, 'waiting_for_database')):
            with self.subTest(pid=pid, state=state):
                self.service.loaded = True; self.service.running = True
                self.service.listener(pid, state)
                result = self.controller.listen(ALICE, 'Alice')
                self.assertFalse(result['ok'])
                self.assertFalse(result['target_enabled'])
                self.assertEqual(result['status'], 'error')
                self.assertEqual([event[0] for event in self.events].count('start'), 1 if pid == 999 else 2)

    def test_resume_loaded_but_disabled_service_reenables_it(self):
        self.registry.add(ALICE)
        self.service.loaded = True; self.service.disabled = True
        result = self.controller.listen(ALICE, 'Alice')
        self.assertTrue(result['ok'])
        self.assertIs(result['service']['disabled'], False)
        self.assertEqual([event[0] for event in self.events].count('start'), 1)

    def test_disabled_service_is_not_reported_active(self):
        self.registry.add(ALICE)
        self.service.loaded = True; self.service.disabled = True
        with patch.object(self.service, 'start', return_value=self.service.service_status()):
            result = self.controller.listen(ALICE, 'Alice')
        self.assertFalse(result['ok'])
        self.assertFalse(self.registry.config['targets'][ALICE]['enabled'])

    def test_unloaded_but_not_disabled_is_not_reported_persistently_paused(self):
        self.registry.add(ALICE, enabled=True)
        self.service.loaded = False; self.service.disabled = False
        with patch.object(self.service, 'unload', return_value=self.service.service_status()):
            with self.assertRaisesRegex(RuntimeError, 'persistently disabled'):
                self.controller.pause()
        self.assertFalse(self.registry.config['enabled'])

    def test_style_change_clears_only_unsent_plan_preserving_incoming_and_cursor(self):
        pending = {'batch_id': 'same-batch', 'incoming': [{'text': 'PRIVATE_CHAT'}],
                   'texts': ['old style draft'], 'sent': [], 'inflight': None, 'last_received_at': 1010}
        before = self.registry.add(ALICE, pending=pending)
        result = self.controller.listen(ALICE, 'Alice', style='new style')
        self.assertTrue(result['ok'])
        expected = copy.deepcopy(before); expected['pending']['texts'] = None
        self.assertEqual(self.read_queue(ALICE), expected)

    def test_partial_plan_style_change_is_rejected_but_plain_resume_preserves_it(self):
        pending = {'batch_id': 'partially-sent', 'incoming': [], 'texts': ['one', 'two'],
                   'sent': [{'request_id': 'sent-one'}], 'inflight': None}
        before = self.registry.add(ALICE, pending=pending)
        config = self.registry.load()
        with self.assertRaisesRegex(ValueError, 'partially sent'):
            self.controller.listen(ALICE, 'Alice', style='new style')
        self.assertEqual(self.events, [])
        self.assertEqual(self.registry.config, config)
        self.assertEqual(self.read_queue(ALICE), before)
        self.assertTrue(self.controller.listen(ALICE, 'Alice')['ok'])
        self.assertEqual(self.read_queue(ALICE), before)

    def test_style_change_reloads_queue_after_gate_and_keeps_just_completed_send(self):
        pending = {'batch_id': 'racing-batch', 'incoming': [], 'texts': ['one', 'two'],
                   'sent': [], 'inflight': None}
        self.registry.add(ALICE, pending=pending)
        save = self.registry.save
        changed = []
        def save_with_completed_send(config):
            save(config)
            if not changed:
                state = self.read_queue(ALICE)
                state['pending']['sent'] = [{'request_id': 'just-completed'}]
                control.atomic_json(self.registry.target_dir(ALICE) / 'state.json', state)
                changed.append(copy.deepcopy(state))
        with patch.object(self.registry, 'save', side_effect=save_with_completed_send):
            result = self.controller.listen(ALICE, 'Alice', style='new style')
        self.assertFalse(result['ok'])
        self.assertEqual(self.read_queue(ALICE), changed[0])
        self.assertFalse(self.registry.config['targets'][ALICE]['enabled'])

    def test_status_omits_style_chat_and_keys_and_handles_missing_config(self):
        self.registry.add(ALICE, pending={'incoming': [{'text': 'PRIVATE_CHAT'}],
                                        'batch_id': 'pending', 'sent': [{'text': 'PRIVATE_REPLY'}]})
        self.registry.config['db_keys'] = 'PRIVATE_KEY'
        result = self.controller.status()
        serialized = json.dumps(result)
        for token in ('PRIVATE_STYLE', 'PRIVATE_CHAT', 'PRIVATE_KEY', 'PRIVATE_REPLY', 'PRIVATE_LISTENER'):
            self.assertNotIn(token, serialized)
        self.assertEqual(result['listener'], {'pid': 123, 'state': 'listening', 'at': 12345})
        self.assertEqual(result['targets'][0]['queue']['pending_count'], 1)
        self.assertEqual(result['targets'][0]['queue']['sent_count'], 1)
        self.assertEqual(result['targets'][0]['queue']['cursors'], {'message/message_0.db': 4390})
        self.assertEqual(self.events, [])
        self.registry.config = None
        result = self.controller.status()
        self.assertFalse(result['configured'])
        self.assertEqual(result['targets'], [])
        self.assertEqual(self.events, [])

    def test_native_forwards_exact_argv_without_shell(self):
        runtime = self.home / 'runtime'; runtime.mkdir(); (runtime / 'wechat.py').write_text('')
        arguments = ['send', '--to', ALICE, '--text', 'literal $(echo x) `test`; 笑\n第二行', '--request-id', 'id']
        runner = Mock(return_value=SimpleNamespace(returncode=7))
        result = control.native(runtime, arguments, runner=runner, python='/usr/bin/python3')
        self.assertEqual(result, 7)
        called = runner.call_args
        self.assertEqual(called.args[0][-len(arguments):], arguments)
        self.assertEqual(called.kwargs, {'check': False})
        self.assertNotIn('shell', called.kwargs)
        with self.assertRaises(ValueError):
            control.native(runtime, ['exec', 'anything'], runner=runner)
        self.assertEqual(runner.call_count, 1)

    def test_native_uses_installed_python_configuration(self):
        runtime = self.home / 'runtime'; runtime.mkdir(); (runtime / 'wechat.py').write_text('')
        (runtime / 'runtime-local.json').write_text(json.dumps({'python': '/fixture/venv/bin/python'}))
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        with patch.dict(control.os.environ, {}, clear=True):
            control.native(runtime, ['status'], runner=runner)
        self.assertEqual(runner.call_args.args[0][0], '/fixture/venv/bin/python')
        with patch.dict(control.os.environ, {'WECHAT_PYTHON': '/override/python'}, clear=True):
            control.native(runtime, ['status'], runner=runner)
        self.assertEqual(runner.call_args.args[0][0], '/override/python')

    def test_default_runtime_prefers_deployment_and_override_is_explicit(self):
        deployment = self.home / 'deployment'; deployment.mkdir()
        override = self.home / 'override'; override.mkdir()
        with patch.object(control, 'DEFAULT_RUNTIME', deployment):
            self.assertEqual(control.runtime_path(), deployment)
            self.assertEqual(control.runtime_path(override), override.resolve())

    def test_cli_argument_errors_are_json_and_do_not_load_runtime(self):
        output = io.StringIO()
        with patch.object(control, 'load_runtime') as load, contextlib.redirect_stdout(output):
            code = control.main(['listen', '--to', ALICE])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())['ok'])
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
