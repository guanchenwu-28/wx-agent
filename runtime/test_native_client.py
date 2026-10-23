"""Unix socket fixtures only; no WeChat, launchd, or native broker is started."""
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_client


class FakeServer:
    def __init__(self, path, handler=None):
        self.path = path
        self.requests = []
        self.handler = handler or (lambda request: {'ok': True, 'echo_action': request['action']})
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            self.listener.bind(str(path))
        finally:
            os.umask(previous)
        self.listener.listen(20)
        self.listener.settimeout(.05)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.done.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(1)
                data = bytearray()
                try:
                    while b'\n' not in data:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                    if not data:
                        continue
                    request = json.loads(data.split(b'\n', 1)[0])
                    self.requests.append(request)
                    response = self.handler(request)
                    if response is not None:
                        connection.sendall(json.dumps(response).encode() + b'\n')
                    if request['action'] == '_transport_stop' and response and response.get('stopped'):
                        self.done.set()
                except (OSError, ValueError):
                    pass
        self.listener.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def close(self):
        self.done.set()
        self.thread.join(2)


class NativeClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wnc-', dir='/private/tmp')
        self.base = Path(self.temp.name)
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir()
        self.socket_root = self.base / 's'
        self.socket_root.mkdir()
        self.paths_patch = patch.object(native_client, 'SOCKET_ROOT', self.socket_root)
        self.paths_patch.start()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        self.paths_patch.stop()
        self.temp.cleanup()

    def server(self, handler=None):
        _, _, path = native_client._paths(self.runtime, create=True)
        server = FakeServer(path, handler)
        self.servers.append(server)
        return server

    def spawn(self, *args, **kwargs):
        self.server()
        return SimpleNamespace(poll=lambda: None)

    def test_cold_start_then_reuses_existing_server(self):
        with patch.object(native_client.subprocess, 'Popen', side_effect=self.spawn) as popen:
            first = native_client.request({'action': 'status'}, runtime=self.runtime)
            second = native_client.request({'action': 'probe'}, runtime=self.runtime)
        self.assertTrue(first['ok'])
        self.assertTrue(second['ok'])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual([r['action'] for r in self.servers[0].requests], ['status', 'probe'])
        args, kwargs = popen.call_args
        self.assertIn('--serve', args[0])
        self.assertEqual(args[0][-2:], ['--idle-ms', '60000'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdout'], native_client.subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], native_client.subprocess.DEVNULL)

    def test_simultaneous_cold_requests_start_only_one_server(self):
        results = []
        gate = threading.Barrier(6)
        def call():
            gate.wait()
            results.append(native_client.request({'action': 'status'}, runtime=self.runtime, timeout=3))
        with patch.object(native_client.subprocess, 'Popen', side_effect=self.spawn) as popen:
            threads = [threading.Thread(target=call) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(4)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result.get('ok') for result in results), results)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(len(self.servers[0].requests), 6)

    def test_disconnect_after_write_is_unknown_and_is_never_replayed(self):
        server = self.server(lambda request: None)
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.request({'action': 'send', 'text': 'fixture'}, runtime=self.runtime)
        self.assertEqual(result['state'], 'unknown')
        self.assertTrue(result['submitted'])
        self.assertEqual(len(server.requests), 1)
        popen.assert_not_called()

    def test_read_disconnect_is_failed_and_is_never_replayed(self):
        server = self.server(lambda request: None)
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.request({'action': 'status'}, runtime=self.runtime)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertEqual(len(server.requests), 1)
        popen.assert_not_called()

    def test_send_timeout_does_not_dispatch_again(self):
        def slow(request):
            time.sleep(.15)
            return {'ok': True}
        server = self.server(slow)
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.request({'action': 'send-image'}, runtime=self.runtime, timeout=.05)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len(server.requests), 1)
        popen.assert_not_called()

    def test_status_and_stop_absent_do_not_start_or_create(self):
        _, directory, _ = native_client._paths(self.runtime)
        with patch.object(native_client.subprocess, 'Popen') as popen:
            self.assertEqual(native_client.status(runtime=self.runtime), {'ok': True, 'running': False})
            self.assertEqual(native_client.stop(runtime=self.runtime), {'ok': True, 'running': False, 'stopped': True})
        self.assertFalse(directory.exists())
        popen.assert_not_called()

    def test_health_is_inspection_only(self):
        server = self.server(lambda request: {'ok': True, 'running': True, 'pid': 123,
                                               'transport': {'attached': False, 'requests': 2}})
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.status(runtime=self.runtime)
        self.assertTrue(result['running'])
        self.assertEqual(result['broker_pid'], 123)
        self.assertFalse(result['transport']['attached'])
        self.assertEqual(server.requests, [{'action': '_transport_health'}])
        popen.assert_not_called()

    def test_stop_waits_for_socket_to_disappear(self):
        server = self.server(lambda request: {'ok': True, 'stopped': True})
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.stop(runtime=self.runtime)
        self.assertTrue(result['ok'])
        self.assertTrue(result['stopped'])
        self.assertFalse(result['running'])
        self.assertFalse(server.path.exists())
        popen.assert_not_called()

    def test_stop_failure_is_not_reported_as_stopped(self):
        self.server(lambda request: {'ok': False, 'error': 'busy'})
        result = native_client.stop(runtime=self.runtime)
        self.assertFalse(result['ok'])
        self.assertFalse(result['stopped'])
        self.assertIsNone(result['running'])

    def test_stop_acknowledgment_without_exit_is_unconfirmed(self):
        self.server()
        with patch.object(native_client, '_exchange', return_value={'ok': True, 'stopped': True}):
            result = native_client.stop(runtime=self.runtime, timeout=.05)
        self.assertFalse(result['ok'])
        self.assertFalse(result['stopped'])
        self.assertIsNone(result['running'])

    def test_unresponsive_socket_is_never_removed_or_replaced(self):
        server = self.server()
        with patch.object(native_client, '_connect', side_effect=TimeoutError('busy')):
            with patch.object(native_client.subprocess, 'Popen') as popen:
                result = native_client.request({'action': 'send'}, runtime=self.runtime)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(server.path.exists())
        popen.assert_not_called()

    def test_node_error_response_is_preserved(self):
        expected = {'error': 'native fixture', 'state': 'failed', 'submitted': False, 'adapter': {'status': 'needs_review'}}
        self.server(lambda request: expected)
        result = native_client.request({'action': 'send'}, runtime=self.runtime)
        self.assertEqual(result, expected)

    def test_stale_socket_is_replaced_under_startup_lock(self):
        _, _, path = native_client._paths(self.runtime, create=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        path.chmod(0o600)
        stale.close()
        with patch.object(native_client.subprocess, 'Popen', side_effect=self.spawn) as popen:
            result = native_client.request({'action': 'status'}, runtime=self.runtime)
        self.assertTrue(result['ok'])
        self.assertEqual(popen.call_count, 1)

    def test_private_modes_and_runtime_specific_paths(self):
        with patch.object(native_client.subprocess, 'Popen', side_effect=self.spawn):
            native_client.request({'action': 'status'}, runtime=self.runtime)
        _, directory, path = native_client._paths(self.runtime)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / 'startup.lock').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotEqual(native_client._paths(self.runtime / 'other')[2], path)

    def test_directory_symlink_is_rejected_without_launch(self):
        _, directory, _ = native_client._paths(self.runtime)
        directory.symlink_to(self.runtime, target_is_directory=True)
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.request({'action': 'status'}, runtime=self.runtime)
        self.assertEqual(result['state'], 'failed')
        popen.assert_not_called()

    def test_non_socket_path_is_not_removed(self):
        _, _, path = native_client._paths(self.runtime, create=True)
        path.write_text('keep')
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.request({'action': 'status'}, runtime=self.runtime)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(path.read_text(), 'keep')
        popen.assert_not_called()

    def test_startup_failure_before_write_is_failed_not_unknown(self):
        with patch.object(native_client.subprocess, 'Popen', return_value=SimpleNamespace(poll=lambda: 1)) as popen:
            result = native_client.request({'action': 'send'}, runtime=self.runtime)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertEqual(popen.call_count, 1)

    def test_oversized_request_is_rejected_before_creating_path(self):
        _, directory, _ = native_client._paths(self.runtime)
        with patch.object(native_client.subprocess, 'Popen') as popen:
            result = native_client.request({'action': 'send', 'text': 'x' * native_client.MAX_FRAME}, runtime=self.runtime)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(directory.exists())
        popen.assert_not_called()

    @unittest.skipUnless(native_client.NODE.is_file(), 'Bundled Node runtime unavailable')
    def test_python_client_real_node_socket_reuses_mock_frida_then_stops(self):
        """Exercise only the Python/Node boundary with a deliberately fake SDK."""
        self.runtime.joinpath('core.fixture').write_bytes(b'fixture core')
        self.runtime.joinpath('runtime.js').write_text('// Not executed by the fake Frida SDK.\n')
        self.runtime.joinpath('profiles').mkdir()
        marker = self.runtime / 'mock-detached'
        self.runtime.joinpath('fake-sdk.mjs').write_text(textwrap.dedent('''\
            import fs from 'node:fs';
            export async function attach(pid) {
              if (pid !== 12345) throw Error('Fixture PID only');
              let detached = false;
              return {
                isDetached: () => detached,
                detach: async () => { detached = true; fs.writeFileSync(MARKER, 'done'); },
                createScript: async () => {
                  let receive;
                  return {
                    message: {connect: callback => { receive = callback; }},
                    load: async () => { receive({type: 'send', payload: {kind: 'rea_runtime_ready',
                      exports: ['status', 'probe', 'sendText', 'sendImage', 'sendVoice']}}); },
                    unload: async () => {},
                    exports: {status: async () => ({logged_in: true, mock_native: true})}
                  };
                }
              };
            }
            ''').replace('MARKER', json.dumps(str(marker))))
        program = textwrap.dedent('''\
            import path from 'node:path';
            import {fileURLToPath} from 'node:url';
            import {serve} from SERVER;
            import {TransportSession, fileStat} from SESSION;
            const root = path.dirname(fileURLToPath(import.meta.url));
            class FixtureSession extends TransportSession {
              constructor() { super({root, core: path.join(root, 'core.fixture'), sdk: path.join(root, 'fake-sdk.mjs')}); }
              identifyProcess() { this.stats.identity_checks++; return {pid: 12345, start: 'fixture', exe: '/fixture/WeChat'}; }
              async resolve(request, identity) {
                this.stats.resolutions++;
                this.stats.full_resolutions++;
                const descriptor = {permissions: {native_calls: true, send_text: false, send_image: false, send_voice: false}};
                this.report = {
                  identity: {core_sha256: 'fixture'}, core_stat: fileStat(this.core), process_identity: identity,
                  status: 'exact', profile_id: 'fixture', capabilities: {}, runtime_descriptor: descriptor,
                  dependency_stats: {files: {[this.core]: fileStat(this.core)}, profile_directory: path.join(root, 'profiles'), profile_names: []}
                };
                this.authorizations.set(JSON.stringify({construct: false, test: null}), descriptor);
                return descriptor;
              }
            }
            const args = process.argv.slice(2);
            await serve({socketPath: args[args.indexOf('--socket') + 1],
              idleMs: Number(args[args.indexOf('--idle-ms') + 1]), native: new FixtureSession()});
            ''')
        program = program.replace('SERVER', json.dumps((native_client.ROOT / 'transport.mjs').as_uri()))
        program = program.replace('SESSION', json.dumps((native_client.ROOT / 'transport_session.mjs').as_uri()))
        self.runtime.joinpath('transport.mjs').write_text(program)
        children = []
        real_popen = native_client.subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        try:
            with patch.object(native_client.subprocess, 'Popen', side_effect=spawn) as popen:
                first = native_client.request({'action': 'status'}, runtime=self.runtime, timeout=5)
                self.assertTrue(first.get('mock_native'), first)
                _, _, socket_path = native_client._paths(self.runtime)
                # Readiness/disconnect probes must not count as native requests.
                empty = native_client._connect(socket_path, time.monotonic() + 1)
                empty.close()
                second = native_client.request({'action': 'status'}, runtime=self.runtime, timeout=5)
                self.assertTrue(second.get('mock_native'), second)
                self.assertEqual(first['transport']['broker_pid'], second['transport']['broker_pid'])
                self.assertEqual(second['transport']['attaches'], 1)
                self.assertEqual(second['transport']['resolutions'], 1)
                self.assertEqual(second['transport']['requests'], 2)
                self.assertTrue(second['transport']['cache_hit'])
                self.assertTrue(second['transport']['script_reused'])
                self.assertTrue(native_client.status(runtime=self.runtime)['transport']['attached'])
                stopped = native_client.stop(runtime=self.runtime, timeout=3)
                self.assertTrue(stopped['ok'], stopped)
                self.assertFalse(stopped['running'])
                self.assertEqual(marker.read_text(), 'done')
                self.assertFalse(socket_path.exists())
                self.assertEqual(children[0].wait(timeout=2), 0)
                self.assertFalse(native_client.status(runtime=self.runtime)['running'])
                self.assertTrue(native_client.stop(runtime=self.runtime)['ok'])
                self.assertEqual(popen.call_count, 1)
        finally:
            native_client.stop(runtime=self.runtime, timeout=1)
            for child in children:
                if child.poll() is None:
                    child.terminate()  # This test owns only its isolated fake SDK process.
                    child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
