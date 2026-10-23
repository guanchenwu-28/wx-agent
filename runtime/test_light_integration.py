"""Lightweight entrypoints keep one authoritative scan and explicit cleanup."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import wechat
import service_manager


class LightweightEntryTests(unittest.TestCase):
    def test_transport_delegates_to_shared_client_without_spawning_per_call(self):
        payload = {'action': 'status'}
        with patch('native_client.request', return_value={'logged_in': True}) as request, \
                patch.object(wechat.subprocess, 'run') as subprocess:
            self.assertEqual(wechat.transport(payload), {'logged_in': True})
        request.assert_called_once_with(payload, runtime=wechat.ROOT)
        subprocess.assert_not_called()

    def test_running_native_status_uses_transport_report_without_second_scan(self):
        output = io.StringIO()
        with patch.object(wechat, 'processes', return_value={'test': 12}), \
                patch.object(wechat, 'transport', return_value={'adapter': {'status': 'exact'}}) as transport, \
                patch('adapter.scan') as scan, contextlib.redirect_stdout(output):
            wechat.main(['status'])
        scan.assert_not_called()
        transport.assert_called_once_with({'action': 'status'})
        self.assertEqual(json.loads(output.getvalue())['compatibility'], {'status': 'exact'})

    def test_connection_introspection_never_uses_native_transport(self):
        for command, method in [('transport-status', 'status'), ('transport-stop', 'stop')]:
            with self.subTest(command=command), \
                    patch('native_client.'+method, return_value={'running': False}) as operation, \
                    patch.object(wechat, 'transport') as transport, contextlib.redirect_stdout(io.StringIO()):
                wechat.main([command])
            operation.assert_called_once_with(runtime=wechat.ROOT)
            transport.assert_not_called()

    def test_app_switch_waits_for_native_connection_cleanup(self):
        with patch('service_manager.close_native_connections', return_value=[{'ok': True}, {'ok': False, 'error': 'in-flight'}]) as stop, \
                patch.object(wechat, 'processes') as processes, patch.object(wechat, 'quit_app') as quit_app:
            with self.assertRaisesRegex(RuntimeError, 'connection has not finished'):
                wechat.start(original=True)
        stop.assert_called_once_with()
        processes.assert_not_called()
        quit_app.assert_not_called()

    def test_restore_from_installed_closes_both_runtimes_before_app_switch(self):
        events = []
        installed, source = Path('/private/tmp/fixture-installed'), Path('/private/tmp/fixture-source')
        def stop(**arguments):
            events.append(('stop', arguments['runtime']))
            return {'ok': True, 'running': False}
        with patch.object(service_manager, 'ROOT', installed), \
                patch.object(service_manager, 'RUNTIME', installed), \
                patch.object(service_manager, 'SOURCE_RUNTIME', source), \
                patch('native_client.stop', side_effect=stop), \
                patch.object(wechat, 'processes', side_effect=[{'test': 12}, {'original': 13}, {'original': 13}, {'original': 13}]), \
                patch.object(wechat, 'quit_app', side_effect=lambda pid: events.append(('quit', pid))), \
                patch.object(wechat.subprocess, 'run') as run:
            result = wechat.start(original=True)
        self.assertEqual(events, [('stop', installed), ('stop', source), ('quit', 12)])
        self.assertEqual(result['running'], {'original': 13})
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
