"""Version identity, automatic static matching, and per-capability verification.

Scanning never attaches to WeChat or sends. A new native image remains blocked
until an explicitly requested filehelper test has an attributed server receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

ROOT = Path(__file__).resolve().parent
from runtime_config import TEST_APP, COMPATIBILITY_CACHE
DEFAULT_CORE = TEST_APP / 'Contents/Resources/wechat.dylib'
DEFAULT_CACHE = COMPATIBILITY_CACHE
KINDS = ('text', 'image', 'voice')
SCHEMA = 1
VERIFIED_BASELINE_CORE_SHA256 = '6e82322680d7747020c305f67e932b235fe68b22578c24d9c524dabb31a4694a'
VERIFIED_BASELINE_ARM64_SHA256 = '413c124643903640946836d631fb4185133a8ffccbf2863e77b9a4b70568abd6'
SEND_ACTIONS = {'send': 'text', 'send-image': 'image', 'send-voice': 'voice'}
IMPLEMENTATION_FILES = ('adapter.py', 'adapter_matcher.py', 'transport.mjs', 'runtime.js',
                        'native_message_bridge.cpp', 'wechat.py', 'media.py', 'adapter_workflow.py')
# Older deployments may not have these yet; both absence and later creation are
# part of the identity, so adding the session broker invalidates prior results.
OPTIONAL_IMPLEMENTATION_FILES = ('transport_session.mjs', 'native_client.py',
                                 'runtime_config.py', 'runtime_config.mjs')
STAT_FIELDS = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')
FAST_CACHE_SCHEMA = 1


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_profiles(directory=None):
    paths = sorted(Path(directory or ROOT / 'profiles').glob('*.json'))
    if not paths:
        raise RuntimeError('No version profiles installed')
    profiles = []
    for path in paths:
        data = path.read_bytes()
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('schema_version') != SCHEMA or not value.get('profile_id'):
            raise ValueError('Invalid version profile: ' + path.name)
        profiles.append((value, hashlib.sha256(data).hexdigest()))
    return profiles


def implementation_digest():
    digest = hashlib.sha256()
    for name in IMPLEMENTATION_FILES + OPTIONAL_IMPLEMENTATION_FILES:
        path = ROOT / name
        digest.update(name.encode())
        if name in OPTIONAL_IMPLEMENTATION_FILES and not path.exists():
            digest.update(b'\x00missing\x00')
            continue
        digest.update(path.read_bytes())
    return digest.hexdigest()


def stat_fingerprint(path):
    """Values are decimal strings for exact comparison with Node bigint stats."""
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return None
    return dict(zip(STAT_FIELDS, map(str, (stat.st_dev, stat.st_ino, stat.st_size,
                                          stat.st_mtime_ns, stat.st_ctime_ns))))


def dependency_stats(core, profiles_dir, cache):
    directory = Path(profiles_dir).resolve(strict=True)
    profile_paths = sorted(directory.glob('*.json'))
    files = {str(core): stat_fingerprint(core)}
    for path in profile_paths:
        files[str(path)] = stat_fingerprint(path)
    for name in IMPLEMENTATION_FILES + OPTIONAL_IMPLEMENTATION_FILES:
        path = ROOT / name
        files[str(path)] = stat_fingerprint(path)
    verification_path = str(Path(cache).resolve() / 'verified.json') if cache is not None else None
    if verification_path:
        files[verification_path] = stat_fingerprint(verification_path)
    return {'files': files, 'profile_directory': str(directory),
            'profile_names': [path.name for path in profile_paths],
            'verification_path': verification_path}


def validation_stats(snapshot):
    """Receipt changes refresh permissions without rehashing the native image."""
    return {**snapshot, 'files': {path: value for path, value in snapshot['files'].items()
                                 if path != snapshot.get('verification_path')}}


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _valid_report(saved, *, key, core_hash, profile_digest, implementation):
    if (not isinstance(saved, dict) or saved.get('schema_version') != SCHEMA
            or saved.get('status') not in ('exact', 'matched', 'needs_review')
            or not isinstance(saved.get('identity'), dict)
            or saved.get('cache_key') != key or saved['identity'].get('core_sha256') != core_hash
            or saved.get('profile_digest') != profile_digest
            or saved.get('implementation_digest') != implementation):
        return False
    descriptor = saved.get('descriptor')
    if saved['status'] == 'needs_review':
        return descriptor is None
    return (isinstance(saved.get('profile_id'), str) and isinstance(descriptor, dict)
            and descriptor.get('core_sha256') == core_hash
            and all(isinstance(descriptor.get(field), dict) for field in ('offsets', 'vtables', 'layout')))


def _canonical_process_identity(value):
    if value is not None and not isinstance(value, dict):
        raise ValueError('Process identity must be an object or null')
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def scan(core=DEFAULT_CORE, *, profiles_dir=None, cache=DEFAULT_CACHE, refresh=False,
         process_identity=None):
    """Reuse verified files by stat identity; hash again on process/file changes.

    The fast index is only a pointer to a validated content report. Runtime
    loaded-image checks remain separate. Verification receipts are always read
    afresh, including on a fast hit, so revoking a capability takes effect now.
    """
    core = Path(core).resolve(strict=True)
    profiles_dir = Path(profiles_dir or ROOT / 'profiles').resolve(strict=True)
    process_identity = _canonical_process_identity(process_identity)
    before = dependency_stats(core, profiles_dir, cache)
    fast_key = hashlib.sha256(json.dumps({'core': str(core), 'profiles': str(profiles_dir),
            'implementation_root': str(ROOT), 'process_identity': process_identity}, sort_keys=True).encode()).hexdigest()
    fast_path = Path(cache) / 'fast' / (fast_key + '.json') if cache is not None else None
    result = None
    report_path = None
    cache_info = {'fast_hit': False, 'core_hash_checked': False, 'matched_recomputed': False}
    if fast_path and not refresh:
        index = _read_json(fast_path)
        if (isinstance(index, dict) and index.get('schema_version') == FAST_CACHE_SCHEMA
                and index.get('process_identity') == process_identity
                and index.get('dependency_stats') == validation_stats(before)
                and isinstance(index.get('cache_key'), str)
                and len(index['cache_key']) == 64
                and all(char in '0123456789abcdef' for char in index['cache_key'])):
            report_path = Path(cache) / 'matches' / (index['cache_key'] + '.json')
            if index.get('report_stat') == stat_fingerprint(report_path):
                saved = _read_json(report_path)
                if (index.get('report_stat') == stat_fingerprint(report_path)
                        and _valid_report(saved, key=index['cache_key'], core_hash=index.get('core_sha256'),
                                 profile_digest=index.get('profile_digest'),
                                 implementation=index.get('implementation_digest'))):
                    result = saved
                    cache_info['fast_hit'] = True
    profiles = load_profiles(profiles_dir)
    profile_digest = hashlib.sha256(''.join(x[1] for x in profiles).encode()).hexdigest()
    # The small profile digest also checks the fast index against the files
    # actually read in this invocation, not a stored permission bit.
    if result is not None and result['profile_digest'] != profile_digest:
        result = None
        cache_info['fast_hit'] = False
    if result is None:
        core_hash = digest_file(core)
        cache_info['core_hash_checked'] = True
        implementation = implementation_digest()
        key = hashlib.sha256((core_hash + profile_digest + implementation).encode()).hexdigest()
        report_path = Path(cache) / 'matches' / (key + '.json') if cache is not None else None
        if report_path and not refresh:
            saved = _read_json(report_path)
            if _valid_report(saved, key=key, core_hash=core_hash, profile_digest=profile_digest,
                             implementation=implementation):
                result = saved
    if result is None:
        from adapter_matcher import match_profile
        attempts = [match_profile(core, profile) for profile, _ in profiles]
        successful = [x for x in attempts if x.get('status') in ('exact', 'matched') and x.get('descriptor')]
        exact = [x for x in successful if x['status'] == 'exact']
        choices = exact or successful
        # Disagreeing profiles cannot select an arbitrary first match.
        identities = {json.dumps({k: x['descriptor'][k] for k in ('offsets', 'vtables', 'layout')},
                                 sort_keys=True) for x in choices}
        if choices and len(identities) == 1:
            result = choices[0]
        else:
            result = attempts[0]
            result['status'] = 'needs_review'
            result['descriptor'] = None
            if len(identities) > 1:
                result.setdefault('unresolved', []).append('profiles_disagree')
            result['attempts'] = [{k: x.get(k) for k in ('profile_id', 'status', 'unresolved')} for x in attempts]
        result.update(schema_version=SCHEMA, cache_key=key, profile_digest=profile_digest,
                      implementation_digest=implementation, scanned_at=time.time())
        if not isinstance(result.get('identity'), dict):
            result['identity'] = {'core_sha256': core_hash}
        if result['identity'].get('core_sha256') != core_hash:
            raise RuntimeError('Core image changed during version validation')
        cache_info['matched_recomputed'] = True
    after = dependency_stats(core, profiles_dir, cache)
    if validation_stats(before) != validation_stats(after):
        raise RuntimeError('Version files changed during validation; retry on a stable process/version')
    # Commit only after checking both ends of all reads and matching work.
    if report_path and cache_info['matched_recomputed']:
        atomic_json(report_path, result)
    if fast_path and not cache_info['fast_hit']:
        atomic_json(fast_path, {'schema_version': FAST_CACHE_SCHEMA,
            'process_identity': process_identity, 'dependency_stats': validation_stats(after),
            'cache_key': result['cache_key'], 'core_sha256': result['identity']['core_sha256'],
            'profile_digest': result['profile_digest'],
            'implementation_digest': result['implementation_digest'],
            'report_stat': stat_fingerprint(report_path)})
    # The same file bytes may be in a new path. Never inject a cached old path.
    result['identity']['core_path'] = str(core)
    if result.get('descriptor'):
        result['descriptor']['core_path'] = str(core)
    # "Exact" only means identical to one installed profile. A newly generated
    # profile is not a delivery test. Trust only the historical verified image's
    # ARM64 slice, allowing a changed outer universal binary/signature envelope.
    result['known_verified_image'] = known_verified_image(result, profiles)
    result['capabilities'] = capabilities(result, cache=cache)
    result['delivery_verified'] = dict(result['capabilities'])
    result['validation_scope'] = ('previously_verified_native_image' if result['known_verified_image']
                                  else 'static_match_only_until_explicit_filehelper_test')
    result['report_path'] = str(report_path) if report_path else None
    result['core_stat'] = after['files'][str(core)]
    result['dependency_stats'] = after
    result['cache_info'] = cache_info
    result['process_identity'] = process_identity
    return result


def verification_key(report):
    return ':'.join(str(report.get(k, '')) for k in ('cache_key', 'profile_id'))


def known_verified_image(report, profiles):
    if not report.get('descriptor') or report.get('status') not in ('exact', 'matched'):
        return False
    for profile, _ in profiles:
        baseline = profile.get('baseline')
        if not isinstance(baseline, dict) or profile.get('profile_id') != report.get('profile_id'):
            continue
        arm64_hash = baseline.get('arm64_sha256')
        if (baseline.get('core_sha256') == VERIFIED_BASELINE_CORE_SHA256
                and arm64_hash == VERIFIED_BASELINE_ARM64_SHA256
                and arm64_hash == report['identity'].get('arm64_sha256')):
            return True
    return False


def capabilities(report, *, cache=DEFAULT_CACHE):
    if (report.get('known_verified_image') is True and report.get('descriptor')
            and report.get('status') in ('exact', 'matched')):
        return {kind: True for kind in KINDS}
    result = {kind: False for kind in KINDS}
    if report.get('status') not in ('exact', 'matched') or not report.get('descriptor') or cache is None:
        return result
    path = Path(cache) / 'verified.json'
    try:
        saved = json.loads(path.read_text()).get(verification_key(report), {})
    except (OSError, ValueError, AttributeError):
        return result
    if not isinstance(saved, dict):
        return result
    for kind in KINDS:
        entry = saved.get(kind, {})
        result[kind] = bool(isinstance(entry, dict)
                            and entry.get('core_sha256') == report['identity']['core_sha256']
                            and isinstance(entry.get('request_id'), str) and entry['request_id'].strip()
                            and positive_server_id(entry.get('server_id'))
                            and entry.get('recipient') == 'filehelper'
                            and entry.get('construction_verified') is True)
    return result


def runtime_descriptor(report, *, test_kind=None, construct=False):
    if not report.get('descriptor') or report.get('status') not in ('exact', 'matched'):
        raise RuntimeError('Version adaptation requires review: ' + ', '.join(report.get('unresolved', [])))
    if test_kind is not None and test_kind not in KINDS:
        raise ValueError('Invalid adapter test kind')
    descriptor = json.loads(json.dumps(report['descriptor']))
    permitted = report['capabilities']
    descriptor['permissions'] = {'native_calls': report.get('known_verified_image') is True or any(permitted.values()) or construct or test_kind is not None,
                                 **{'send_' + k: (False if construct else test_kind == k
                                     if test_kind is not None else permitted[k]) for k in KINDS}}
    if test_kind is not None:
        descriptor['test_recipient'] = 'filehelper'
    return descriptor


def prepare_runtime(report, request):
    """Authorize the host request and produce the sole runtime permission set."""
    if not isinstance(request, dict):
        raise ValueError('Runtime request must be an object')
    action = request.get('action')
    if action not in (*SEND_ACTIONS, 'status', 'probe'):
        raise ValueError('Unsupported runtime action')
    construct = request.get('construct_only', False)
    if type(construct) is not bool or (construct and action != 'probe'):
        raise ValueError('Construction checks require action=probe')
    test_kind = None
    test = request.get('adapter_test')
    if test is not None:
        request_id = request.get('request_id')
        if (not isinstance(test, dict) or test.get('kind') not in KINDS
                or request.get('to') != 'filehelper'
                or not isinstance(request_id, str) or not request_id.strip()
                or test.get('request_id') != request_id
                or test.get('core_sha256') != report.get('identity', {}).get('core_sha256')):
            raise ValueError('Adapter tests require the current core, matching request ID, and filehelper')
        test_kind = test['kind']
        if action in SEND_ACTIONS and SEND_ACTIONS[action] != test_kind:
            raise ValueError('Adapter test kind does not match the requested send action')
    if not report.get('descriptor') or report.get('status') not in ('exact', 'matched'):
        if action in ('status', 'probe') and not construct and test_kind is None:
            return None
        raise RuntimeError('Version adaptation requires review before native calls')
    descriptor = runtime_descriptor(report, test_kind=test_kind, construct=construct)
    if action in SEND_ACTIONS and not descriptor['permissions']['send_' + SEND_ACTIONS[action]]:
        raise RuntimeError('This version has not passed an explicit filehelper test for ' + SEND_ACTIONS[action])
    return descriptor


def summary(report):
    return {key: report.get(key) for key in ('schema_version', 'status', 'profile_id', 'identity',
            'capabilities', 'known_verified_image', 'validation_scope', 'unresolved', 'report_path', 'scanned_at',
            'cache_info', 'process_identity')}


def positive_server_id(value):
    if type(value) is int:
        return value > 0
    if isinstance(value, str):
        try:
            return int(value) > 0
        except ValueError:
            return False
    return False


def record_verified(report, kind, result, *, construction_verified, cache=DEFAULT_CACHE):
    """Only the explicit test workflow calls this after native readback/ack checks."""
    if (kind not in KINDS or construction_verified is not True
            or report.get('status') not in ('exact', 'matched') or not report.get('descriptor')):
        raise ValueError('A verified construction and known capability are required')
    identity = report['identity']
    try:
        current_hash = digest_file(identity['core_path'])
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError('Cannot verify the current core image') from exc
    if current_hash != identity.get('core_sha256'):
        raise ValueError('Core image changed after adaptation; repeat the explicit test on the current version')
    if not isinstance(result, dict):
        raise ValueError('Invalid native receipt')
    native = result.get('result') or {}
    if not isinstance(native, dict):
        raise ValueError('Invalid native receipt')
    record = native.get('native_record') or {}
    attestation = native.get('adapter_test') or {}
    if (not all(isinstance(item, dict) for item in (native, record, attestation))
            or result.get('state') != 'sent' or native.get('delivered') is not True
            or not isinstance(result.get('request_id'), str) or not result['request_id'].strip()
            or attestation.get('request_id') != result['request_id']
            or not positive_server_id(record.get('server_id'))
            or ('recipient' in result and result['recipient'] != 'filehelper')
            or ('recipient' in record and record['recipient'] != 'filehelper')
            or ('local_type' in record and record['local_type'] != {'text': 1, 'image': 3, 'voice': 34}[kind])
            or attestation.get('core_sha256') != report['identity']['core_sha256']
            or attestation.get('kind') != kind or attestation.get('recipient') != 'filehelper'):
        raise ValueError('No attributed server acknowledgment for this version test')
    path = Path(cache) / 'verified.json'
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path.parent / 'verified.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            saved = json.loads(path.read_text())
        except FileNotFoundError:
            saved = {}
        if not isinstance(saved, dict) or not isinstance(saved.get(verification_key(report), {}), dict):
            raise ValueError('Invalid verification cache; existing data was preserved')
        saved.setdefault(verification_key(report), {})[kind] = {
            'core_sha256': report['identity']['core_sha256'], 'request_id': result['request_id'],
            'recipient': 'filehelper', 'server_id': str(record['server_id']),
            'database': record.get('database'), 'local_id': record.get('local_id'),
            'construction_verified': True, 'verified_at': time.time()}
        atomic_json(path, saved)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', default=str(DEFAULT_CORE))
    parser.add_argument('--profiles-dir')
    parser.add_argument('--cache', default=str(DEFAULT_CACHE))
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--descriptor', action='store_true', help='Internal transport output; no runtime calls')
    parser.add_argument('--runtime-request', help='Internal JSON request; validate permissions without native calls')
    parser.add_argument('--process-identity', help='Internal JSON process identity; changes force a fresh core hash')
    args = parser.parse_args(argv)
    report = scan(args.core, profiles_dir=args.profiles_dir, cache=args.cache, refresh=args.refresh,
                  process_identity=json.loads(args.process_identity) if args.process_identity is not None else None)
    if args.runtime_request is not None:
        report['runtime_descriptor'] = prepare_runtime(report, json.loads(args.runtime_request))
    print(json.dumps(report if args.descriptor or args.runtime_request is not None else summary(report), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
