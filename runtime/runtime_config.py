"""Portable, local-only runtime settings. Importing never starts a service."""
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('WECHAT_CONFIG', ROOT / 'runtime-local.json')).expanduser()
try:
    SETTINGS = json.loads(CONFIG_PATH.read_text())
except FileNotFoundError:
    SETTINGS = {}
if not isinstance(SETTINGS, dict):
    raise ValueError('Runtime configuration must be a JSON object')


def setting(name, default=None):
    return os.environ.get('WECHAT_' + name.upper(), SETTINGS.get(name, default))


def path_setting(name, default):
    return Path(setting(name, str(default))).expanduser().resolve()


def executable(name, fallback):
    value = str(setting(name, shutil.which(fallback) or fallback))
    return shutil.which(value) or str(Path(value).expanduser())


PYTHON = executable('python', sys.executable)
NODE = executable('node', 'node')
CODEX = executable('codex', 'codex')
CLANG = executable('clang', 'clang++')
TEST_APP = path_setting('test_app', Path.home() / 'Applications/WeChat REA Test.app')
ORIGINAL_APP = path_setting('original_app', '/Applications/微信.app')
DATA_ROOT = path_setting('data_root', Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data')
CACHE = path_setting('cache', DATA_ROOT / 'Library/Caches/REAResearch')
COMPATIBILITY_CACHE = path_setting('compatibility_cache', Path.home() / 'Library/Application Support/WeChatLocalTool/compatibility')
INSTALLED_RUNTIME = path_setting('installed_runtime', Path.home() / 'Library/Application Support/WeChatLocalTool/runtime')
SOURCE_RUNTIME = path_setting('source_runtime', ROOT)
LOGS = path_setting('logs', Path.home() / 'Library/Logs/WeChatLocalTool')
SERVICE_LABEL = setting('service_label', 'com.wxagent.wechat-autoreply')


def environment():
    """Pass the same selected configuration into Node and launchd children."""
    values = dict(SETTINGS)
    for name in values:
        override = os.environ.get('WECHAT_' + name.upper())
        if override is not None:
            values[name] = override
    values.update(python=PYTHON, node=NODE, codex=CODEX, clang=CLANG,
                  test_app=str(TEST_APP), original_app=str(ORIGINAL_APP),
                  data_root=str(DATA_ROOT), cache=str(CACHE),
                  compatibility_cache=str(COMPATIBILITY_CACHE),
                  installed_runtime=str(INSTALLED_RUNTIME), source_runtime=str(SOURCE_RUNTIME),
                  logs=str(LOGS), service_label=SERVICE_LABEL,
                  frida_module=str(path_setting('frida_module', ROOT / 'node_modules/frida/build/src/frida.js')))
    return {'WECHAT_' + name.upper(): str(value) for name, value in values.items()
            if value is not None}
