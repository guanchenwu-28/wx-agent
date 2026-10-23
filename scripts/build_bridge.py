#!/usr/bin/env python3
"""Compile only the local C++ bridge; no process attach, database read or send."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def build(runtime):
    runtime = runtime.expanduser().resolve()
    sys.path.insert(0, str(runtime))
    from runtime_config import CACHE, CLANG
    source = runtime / 'native_message_bridge.cpp'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    bridge = CACHE / ('native_message_bridge_' + digest + '.dylib')
    temporary = bridge.with_suffix('.dylib.tmp')
    try:
        subprocess.run([CLANG, '-std=c++17', '-dynamiclib', '-O2', '-fvisibility=hidden',
                        str(source), '-o', str(temporary)], check=True)
        temporary.replace(bridge)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({'bridge': str(bridge), 'source_sha256_prefix': digest}))
    return bridge


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parents[1] / 'runtime')
    build(parser.parse_args().runtime)
