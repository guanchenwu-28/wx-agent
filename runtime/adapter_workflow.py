"""Offline adaptation by default; explicit runtime/filehelper verification only."""
from __future__ import annotations

import re
import fcntl
from pathlib import Path

import adapter

TEST_TEXT = "REA-版本适配验证"


def register_parser(sub):
    parser = sub.add_parser('adapt', help='Match current version offline; optional explicit verification')
    parser.add_argument('--core', default=str(adapter.DEFAULT_CORE))
    parser.add_argument('--profiles-dir')
    parser.add_argument('--cache', default=str(adapter.DEFAULT_CACHE))
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--verify-runtime', action='store_true', help='Construct local message samples without submitting')
    parser.add_argument('--test-send', choices=adapter.KINDS, help='Explicitly send ONE fixed test to filehelper')
    parser.add_argument('--confirm-core-sha256', help='Current full core SHA256 acknowledged for this test')
    parser.add_argument('--request-id', help='Durable unique test request ID; unknown outcomes must reuse/reconcile this ID')
    parser.add_argument('--file', help='PNG/JPEG or Tencent SILK fixture for image/voice test')
    parser.add_argument('--duration-ms', type=int)
    return parser


def construction_ok(probe, kinds):
    if (probe.get('construct_only') is not True or probe.get('submitted') is not False
            or probe.get('passive') is not False or probe.get('error')):
        return False
    checks = probe.get('checks') or {}
    if not all(checks.get(k) is True for k in ('module_path', 'module_size', 'loaded_uuid',
                                              'executable_addresses', 'readable_vtables')):
        return False
    expected = {'text': (1, ('text_verified',)), 'image': (3, ('image_path_verified',)),
                'voice': (34, ('payload_verified',))}
    for kind in kinds:
        item = (probe.get('constructions') or {}).get(kind) or {}
        native_type, fields = expected[kind]
        if (item.get('constructed') is not True or item.get('submitted') is not False
                or item.get('recipient_verified') is not True or item.get('type_verified') is not True
                or item.get('native_type') != native_type or not all(item.get(k) is True for k in fields)):
            return False
    return True


def same_adapter(result, report):
    actual = result.get('adapter') or {}
    expected_hash = (report.get('identity') or {}).get('core_sha256')
    expected_profile = report.get('profile_id')
    return bool(expected_hash and expected_profile
                and (actual.get('identity') or {}).get('core_sha256') == expected_hash
                and actual.get('profile_id') == expected_profile)


def run(args):
    kind = getattr(args, 'test_send', None)
    runtime = bool(getattr(args, 'verify_runtime', False) or kind)
    core = Path(args.core).expanduser().resolve()
    if runtime and core != adapter.DEFAULT_CORE.resolve():
        raise ValueError('Runtime verification only supports the configured WeChat test-copy core; --core is offline-only')
    if runtime and args.profiles_dir is not None and Path(args.profiles_dir).expanduser().resolve() != (adapter.ROOT / 'profiles').resolve():
        raise ValueError('Custom --profiles-dir is offline-only; runtime uses the installed profiles')
    if runtime and Path(args.cache).expanduser().resolve() != adapter.DEFAULT_CACHE.resolve():
        raise ValueError('Custom --cache is offline-only; runtime uses the configured compatibility cache')
    if kind:
        if not re.fullmatch(r'[0-9a-f]{64}', args.confirm_core_sha256 or ''):
            raise ValueError('--test-send requires --confirm-core-sha256 with the current full SHA256')
        if not args.request_id or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', args.request_id):
            raise ValueError('--test-send requires a durable --request-id')
        if kind in ('image', 'voice') and not args.file:
            raise ValueError('Image/voice verification requires an explicit --file')
        if kind == 'voice' and (not isinstance(args.duration_ms, int) or not 1000 <= args.duration_ms <= 60000):
            raise ValueError('Voice verification requires --duration-ms from 1000 to 60000')
        if kind == 'text' and (args.file or args.duration_ms is not None):
            raise ValueError('Text verification uses its fixed text; media options do not apply')
        if kind == 'image' and args.duration_ms is not None:
            raise ValueError('--duration-ms applies to voice only')
    elif args.confirm_core_sha256 or args.request_id or args.file or args.duration_ms is not None:
        raise ValueError('Send-test arguments require --test-send')
    report = adapter.scan(core, profiles_dir=args.profiles_dir, cache=args.cache, refresh=args.refresh)
    output = {'scan': adapter.summary(report), 'runtime_verified': False, 'sent': False,
              'autoreply_changed': False}
    if not runtime:
        return output
    if not report.get('descriptor') or report.get('status') not in ('exact', 'matched'):
        output['state'] = 'needs_review'
        return output
    if kind and args.confirm_core_sha256 != report['identity']['core_sha256']:
        raise ValueError('Confirmed core hash differs from the scanned image; no runtime calls or send performed')
    if kind in ('image', 'voice'):
        file = Path(args.file).expanduser().resolve(strict=True)
        data = file.read_bytes()
        if not 0 < len(data) <= (1024 * 1024 if kind == 'voice' else 20 * 1024 * 1024):
            raise ValueError('Test media is empty or exceeds the supported size')
        if kind == 'voice' and not data.startswith(b'\x02#!SILK_V3'):
            raise ValueError('Voice test requires Tencent SILK bytes')
        if kind == 'image' and not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')):
            raise ValueError('Image test requires PNG or JPEG')
    import wechat
    request = {'action': 'probe', 'to': 'filehelper', 'construct_only': True}
    attestation = None
    if kind:
        attestation = dict(kind=kind, core_sha256=args.confirm_core_sha256, request_id=args.request_id)
        request.update(request_id=args.request_id, adapter_test=attestation)
        if kind == 'text':
            request['text'] = TEST_TEXT
        elif kind == 'image':
            request['image_path'] = str(file)
        else:
            request.update(voice_path=str(file), duration_ms=args.duration_ms)
    probe = wechat.transport(request)
    output['probe'] = probe
    output['runtime_verified'] = (same_adapter(probe, report)
                                  and construction_ok(probe, (kind,) if kind else adapter.KINDS))
    if not output['runtime_verified']:
        output['state'] = 'runtime_validation_failed'
        return output
    if not kind:
        output['state'] = 'constructed_not_sent'
        return output
    # Existing durable outboxes reconcile/reuse the SAME request ID. Never add a
    # retry loop or fabricate a new ID after a transport/acknowledgment failure.
    if kind == 'text':
        wechat.CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (wechat.CACHE / 'tool-send.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = wechat.send_message('filehelper', TEST_TEXT, args.request_id, adapter_test=attestation)
    else:
        import media
        result = media.send_media('filehelper', str(file), args.request_id,
                                  kind=3 if kind == 'image' else 34,
                                  duration_ms=args.duration_ms, adapter_test=attestation)
    output['test'] = result
    output['state'] = result.get('state', 'unknown')
    if result.get('state') == 'sent':
        if not same_adapter(result.get('result') or {}, report):
            output.update(state='sent_version_mismatch', sent=True,
                          capability_verified=False, do_not_resend=True)
            return output
        adapter.record_verified(report, kind, result, construction_verified=True, cache=args.cache)
        output['sent'] = True
        output['capability_verified'] = kind
    elif result.get('state') in ('unknown', 'submitting'):
        output['next_action'] = ('reconcile' if kind == 'text' else 'reconcile-media') + ' --request-id ' + args.request_id
        output['do_not_resend'] = True
    return output
