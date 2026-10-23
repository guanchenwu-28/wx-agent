"""Installation acceptance uses only temporary paths; no WeChat or launchd."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wx_install', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wx-agent-install-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.prefix = self.base / 'installed with spaces'
        arguments = ['--prefix', str(self.prefix), '--python', sys.executable, '--no-deps',
                     '--cache', str(self.base / 'private-cache'),
                     '--compatibility-cache', str(self.base / 'compatibility'),
                     '--logs', str(self.base / 'logs'),
                     '--data-root', str(self.base / 'wechat-data'),
                     '--test-app', str(self.base / 'WeChat Test.app'),
                     '--frida-module', str(self.base / 'sdk/frida.js'),
                     '--service-label', 'com.example.wxagent-fixture']
        self.args = installer.parser().parse_args(arguments)

    def install(self):
        actual_run = subprocess.run
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(installer.subprocess, 'run', wraps=actual_run) as run:
            result = installer.install(self.args)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], '-c')
        return result

    def test_source_only_install_isolated_config_and_usable_launchers(self):
        result = self.install()
        runtime = self.prefix / 'runtime'
        config = json.loads((runtime / 'runtime-local.json').read_text())
        self.assertEqual(config['cache'], str(self.base / 'private-cache'))
        self.assertEqual(config['installed_runtime'], str(runtime))
        self.assertEqual(config['service_label'], 'com.example.wxagent-fixture')
        self.assertFalse(result['listener_started'])
        self.assertFalse(result['launchd_installed'])
        self.assertFalse(result['wechat_started'])
        self.assertFalse(list(runtime.glob('test_*')))
        self.assertFalse((self.base / 'private-cache').exists())
        self.assertEqual((runtime / 'runtime-local.json').stat().st_mode & 0o777, 0o600)
        for command in ('wx-agent', 'wechat', 'wechat-autoreply', 'wechat-service',
                        'wx-agent-doctor', 'wx-agent-build-bridge'):
            process = subprocess.run([str(self.prefix / 'bin' / command), '--help'],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
        # --help never constructs the registry, starts a broker or opens a chat.
        self.assertFalse((self.base / 'private-cache').exists())

    def test_upgrade_preserves_local_configuration(self):
        self.install()
        path = self.prefix / 'runtime/runtime-local.json'
        config = json.loads(path.read_text())
        config['codex'] = '/fixture/custom-codex'
        config['service_label'] = 'com.example.preserved'
        path.write_text(json.dumps(config))
        self.args.service_label = None
        self.install()
        latest = json.loads(path.read_text())
        self.assertEqual(latest['codex'], '/fixture/custom-codex')
        self.assertEqual(latest['service_label'], 'com.example.preserved')

    def test_environment_overrides_installed_config_in_both_runtimes(self):
        self.install()
        runtime = self.prefix / 'runtime'
        environment = dict(os.environ, WECHAT_CACHE=str(self.base / 'override-cache'))
        python = subprocess.run([sys.executable, '-c',
            'import json, runtime_config as c; print(json.dumps([str(c.CACHE), c.environment()["WECHAT_CACHE"]]))'],
            cwd=runtime, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(python.returncode, 0, python.stderr)
        self.assertEqual(json.loads(python.stdout), [str(self.base / 'override-cache')] * 2)
        node = subprocess.run([self.args.node or 'node', '--input-type=module', '-e',
            "import {cache} from './runtime_config.mjs'; console.log(JSON.stringify(cache))"],
            cwd=runtime, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(node.returncode, 0, node.stderr)
        self.assertEqual(json.loads(node.stdout), str(self.base / 'override-cache'))

    def test_doctor_reports_missing_external_prerequisites_without_reading_keys(self):
        self.install()
        cache = self.base / 'private-cache'
        cache.mkdir()
        (cache / 'verified-dbkeys.json').write_text('PRIVATE_KEY_CONTENT_MUST_NOT_BE_READ')
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/doctor.py'),
                                  '--runtime', str(self.prefix / 'runtime')],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 1, process.stderr)
        result = json.loads(process.stdout)
        self.assertFalse(result['prerequisites_present'])
        checks = {entry['name']: entry for entry in result['checks']}
        self.assertTrue(checks['verified_keys']['ok'])
        self.assertFalse(checks['frida_module']['ok'])
        self.assertFalse(checks['test_app']['ok'])
        self.assertNotIn('PRIVATE_KEY_CONTENT', process.stdout)
        self.assertFalse(result['end_to_end_verified'])

    def test_skill_install_and_existing_skill_preflight(self):
        self.args.install_skill = True
        self.args.skill_root = self.base / 'skills'
        self.install()
        installed = self.args.skill_root / 'wechat-native'
        self.assertTrue((installed / 'SKILL.md').is_file())
        marker = installed / 'local-marker'
        marker.write_text('preserve existing installation')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.install()
        self.assertTrue(marker.exists())

    def test_old_python_fails_before_writing_installation(self):
        completed = subprocess.CompletedProcess([], 0, stdout='[3, 9]\n', stderr='')
        with patch.object(installer.subprocess, 'run', return_value=completed), \
                self.assertRaisesRegex(ValueError, 'Python >=3.11'):
            installer.install(self.args)
        self.assertFalse(self.prefix.exists())


if __name__ == '__main__':
    unittest.main()
