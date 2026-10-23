"""Run the real ESM transport with mocked child_process/Frida, no native attach."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import adapter
import media
import wechat

ROOT = Path(__file__).resolve().parent
PRELOAD = r'''
import fs from 'node:fs';
import child from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const fixture=JSON.parse(fs.readFileSync(process.env.FIXTURE,'utf8'));
const record=value=>fs.appendFileSync(process.env.EVENTS,JSON.stringify(value)+'\n');
child.execFileSync=(executable,args,options)=>{
 record({event:'exec',executable,args});
 if(executable===process.env.WECHAT_PYTHON){
  if(args[0]!==fixture.adapter_path||!args.includes('--descriptor'))throw Error('missing adapter.py --descriptor contract');
  const index=args.indexOf('--runtime-request');if(index<0)throw Error('missing --runtime-request contract');
  if(JSON.stringify(JSON.parse(args[index+1]))!==JSON.stringify(fixture.request))throw Error('runtime request changed before adaptation');
  if(args[args.indexOf('--core')+1]!==fixture.core)throw Error('wrong test core');
  if(fixture.prepare_error){const error=new Error('adapter refused');error.stdout=JSON.stringify({error:fixture.prepare_error});throw error;}
  return JSON.stringify(fixture.report);
 }
 if(executable==='/bin/ps')return `123 Fri Oct 16 12:00:00 2026 ${process.env.HOME}/Applications/WeChat REA Test.app/Contents/MacOS/WeChat\n`;
 if(executable==='clang++'){fs.writeFileSync(args[args.indexOf('-o')+1],'fixture dylib, never loaded');return '';}
 throw Error('Unmocked subprocess is forbidden: '+executable);
};
syncBuiltinESMExports();
'''
FRIDA = r'''
import fs from 'node:fs';
const fixture=JSON.parse(fs.readFileSync(process.env.FIXTURE,'utf8'));
const record=value=>fs.appendFileSync(process.env.EVENTS,JSON.stringify(value)+'\n');
export async function attach(pid){
 record({event:'attach',pid});let callback,descriptor;
 const invoke=async(method,request={})=>{
  record({event:'rpc',method,request});
  if(method==='status')return {logged_in:descriptor.permissions.native_calls?true:null,passive:!descriptor.permissions.native_calls,submitted:false};
  if(method==='probe')return {construct_only:request.construct_only===true,passive:request.construct_only!==true,submitted:false};
  if(fixture.mode!=='result_without_event')callback({type:'send',payload:{kind:'submitted',request_id:request.request_id}});
  if(fixture.mode==='throw_after_submission')throw Error('fixture callback failed');
  return {enqueued:true,submitted:true,identity:{count:1,strings:['filehelper','fixture'],words:['1','2']}};
 };
 return {
  async createScript(source){
   const prefix='globalThis.REA_ADAPTER = ',end=source.indexOf(';\n');
   if(!source.startsWith(prefix)||end<0)throw Error('missing descriptor injection');
   descriptor=JSON.parse(source.slice(prefix.length,end));
   record({event:'injection',descriptor,runtime_present:source.includes('rpc.exports=')});
   return {message:{connect(fn){callback=fn;}},async load(){record({event:'load'});if(fixture.mode==='load_error')throw Error('fixture script load failed');callback({type:'send',payload:{kind:'rea_runtime_ready',exports:['status','probe','sendText','sendImage','sendVoice']}});},async unload(){record({event:'unload'});},exports:{status:()=>invoke('status'),probe:req=>invoke('probe',req),sendText:req=>invoke('sendText',req),sendImage:req=>invoke('sendImage',req),sendVoice:req=>invoke('sendVoice',req)}};
  },isDetached(){return false;},async detach(){record({event:'detach'});}
 };
}
'''


class TransportAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='rea-transport-adapter-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / 'Applications/WeChat REA Test.app/Contents/Resources/wechat.dylib'
        self.core.parent.mkdir(parents=True)
        self.core.write_bytes(b'fixture image for transport digest')
        self.digest = hashlib.sha256(self.core.read_bytes()).hexdigest()
        self.report = {'status': 'matched', 'profile_id': 'transport-fixture',
            'identity': {'core_sha256': self.digest}, 'known_verified_image': True,
            'capabilities': dict.fromkeys(adapter.KINDS, True), 'unresolved': [],
            'descriptor': {'schema_version': 1, 'core_sha256': self.digest, 'profile_id': 'transport-fixture'}}
        (self.root / 'preload.mjs').write_text(PRELOAD)
        (self.root / 'frida.mjs').write_text(FRIDA)
        self.events = self.root / 'events.jsonl'

    def run_transport(self, request, *, mode='success', report=None, changed_core=False):
        request = dict(request)
        if request.get('action') in adapter.SEND_ACTIONS:
            request.setdefault('request_id', 'transport-fixture-id')
        report = copy.deepcopy(report or self.report)
        info = self.core.stat()
        core_stat = {key: str(getattr(info, 'st_' + key)) for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}
        profiles = self.root / 'profiles'
        profiles.mkdir(exist_ok=True)
        report.update(core_stat=core_stat,
                      process_identity={'exe': str(self.root / 'Applications/WeChat REA Test.app/Contents/MacOS/WeChat'),
                                        'pid': 123, 'start': 'Fri Oct 16 12:00:00 2026'},
                      dependency_stats={'files': {str(self.core): core_stat},
                                        'profile_directory': str(profiles), 'profile_names': []})
        fixture = {'adapter_path': str(ROOT / 'adapter.py'), 'core': str(self.core),
                   'request': request, 'report': report, 'mode': mode}
        # Use the real authorization policy; the process boundary is mocked, not
        # reimplemented. Preload also asserts exact argv/request forwarding.
        try:
            report['runtime_descriptor'] = adapter.prepare_runtime(report, request)
        except (RuntimeError, ValueError) as error:
            fixture['prepare_error'] = str(error)
        if changed_core:
            self.core.write_bytes(b'changed during adaptation')
        (self.root / 'fixture.json').write_text(json.dumps(fixture))
        self.events.write_text('')
        env = dict(os.environ, HOME=str(self.root), FIXTURE=str(self.root / 'fixture.json'),
                   EVENTS=str(self.events), WECHAT_FRIDA_MODULE=str(self.root / 'frida.mjs'), WECHAT_PYTHON='/fixture/python3')
        result = subprocess.run([shutil.which('node'), '--import', str(self.root / 'preload.mjs'),
                                 str(ROOT / 'transport.mjs')], input=json.dumps(request),
                                text=True, capture_output=True, env=env, timeout=10)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout), events, result.returncode

    def test_exact_adapter_descriptor_is_injected_before_script_load(self):
        result, events, code = self.run_transport({'action': 'status'})
        self.assertEqual(code, 0)
        injection = next(event for event in events if event['event'] == 'injection')
        self.assertEqual(injection['descriptor']['profile_id'], 'transport-fixture')
        self.assertEqual(injection['descriptor']['permissions']['send_text'], True)
        self.assertTrue(injection['runtime_present'])
        self.assertTrue(result['logged_in'])
        self.assertEqual([e['event'] for e in events][-2:], ['unload', 'detach'])

    def test_unknown_version_send_fails_before_process_lookup_or_attach(self):
        report = {**self.report, 'status': 'needs_review', 'descriptor': None,
                  'known_verified_image': False, 'capabilities': dict.fromkeys(adapter.KINDS, False)}
        result, events, code = self.run_transport({'action': 'send', 'to': 'filehelper', 'text': 'fixture'}, report=report)
        self.assertEqual(code, 1)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertEqual(len(events), 2)
        self.assertIn('before native calls', result['error'])

    def test_static_match_without_verified_send_is_refused_before_attach(self):
        report = {**self.report, 'known_verified_image': False, 'capabilities': dict.fromkeys(adapter.KINDS, False)}
        result, events, code = self.run_transport({'action': 'send', 'to': 'filehelper', 'text': 'fixture'}, report=report)
        self.assertEqual(code, 1)
        self.assertIn('explicit filehelper test', result['error'])
        self.assertEqual(len(events), 2)

    def test_unknown_passive_probe_returns_diagnostic_without_attach(self):
        report = {**self.report, 'status': 'needs_review', 'descriptor': None,
                  'known_verified_image': False, 'capabilities': dict.fromkeys(adapter.KINDS, False)}
        result, events, code = self.run_transport({'action': 'probe'}, report=report)
        self.assertEqual(code, 0)
        self.assertTrue(result['passive'])
        self.assertFalse(result['native_available'])
        self.assertFalse(result['submitted'])
        self.assertEqual(len(events), 2)

    def test_passive_probe_does_not_compile_bridge_or_submit(self):
        result, events, code = self.run_transport({'action': 'probe'})
        self.assertEqual(code, 0)
        self.assertFalse(result['submitted'])
        calls = [event for event in events if event['event'] == 'rpc']
        self.assertEqual([event['method'] for event in calls], ['probe'])
        self.assertNotIn('bridge', calls[0]['request'])
        self.assertFalse(any(event.get('executable') == 'clang++' for event in events))

    def test_constructor_probe_injects_no_send_permissions_and_bridge(self):
        result, events, code = self.run_transport({'action': 'probe', 'construct_only': True})
        self.assertEqual(code, 0)
        self.assertTrue(result['construct_only'])
        self.assertFalse(result['submitted'])
        injection = next(event for event in events if event['event'] == 'injection')
        permissions = injection['descriptor']['permissions']
        self.assertTrue(permissions['native_calls'])
        self.assertFalse(any(permissions['send_' + kind] for kind in adapter.KINDS))
        calls = [event for event in events if event['event'] == 'rpc']
        self.assertEqual([event['method'] for event in calls], ['probe'])
        self.assertTrue(Path(calls[0]['request']['bridge']).is_file())

    def test_filehelper_test_binds_kind_core_and_request_id_and_attests_result(self):
        request = {'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'test-1',
                   'adapter_test': {'kind': 'text', 'core_sha256': self.digest, 'request_id': 'test-1'}}
        result, events, code = self.run_transport(request)
        self.assertEqual(code, 0)
        self.assertTrue(result['submitted'])
        injection = next(event for event in events if event['event'] == 'injection')
        self.assertEqual(injection['descriptor']['test_recipient'], 'filehelper')
        self.assertEqual(injection['descriptor']['permissions'], {'native_calls': True, 'send_text': True, 'send_image': False, 'send_voice': False})
        self.assertEqual(result['adapter_test'], {'kind': 'text', 'core_sha256': self.digest, 'recipient': 'filehelper', 'request_id': 'test-1'})
        for change in ({'to': 'someone_else'}, {'request_id': 'wrong'}, {'action': 'send-voice'}):
            with self.subTest(change=change):
                failed, traces, status = self.run_transport({**request, **change})
                self.assertEqual(status, 1)
                self.assertFalse(failed['submitted'])
                self.assertFalse(any(event['event'] == 'attach' for event in traces))

    def test_voice_request_loads_exact_file_bytes_and_dispatches_voice_rpc(self):
        voice = self.root / 'fixture.silk'
        payload = b'\x02#!SILK_V3\x00\x01'
        voice.write_bytes(payload)
        request = {'action': 'send-voice', 'to': 'filehelper', 'voice_path': str(voice),
                   'duration_ms': 1234, 'request_id': 'voice-1',
                   'adapter_test': {'kind': 'voice', 'core_sha256': self.digest, 'request_id': 'voice-1'}}
        result, events, code = self.run_transport(request)
        self.assertEqual(code, 0)
        self.assertTrue(result['submitted'])
        rpc = next(event for event in events if event['event'] == 'rpc')
        self.assertEqual(rpc['method'], 'sendVoice')
        self.assertEqual(rpc['request']['voice_bytes'], list(payload))
        self.assertEqual(rpc['request']['duration_ms'], 1234)
        self.assertEqual(result['adapter_test']['kind'], 'voice')

    def test_missing_image_file_fails_before_any_send_rpc(self):
        result, events, code = self.run_transport({'action': 'send-image', 'to': 'filehelper',
                                                  'image_path': str(self.root / 'missing.png')})
        self.assertEqual(code, 1)
        self.assertFalse(result['submitted'])
        self.assertFalse(any(event['event'] == 'rpc' for event in events))
        self.assertFalse(any(event['event'] == 'attach' for event in events))

    def test_disk_change_between_scan_and_attach_is_refused(self):
        result, events, code = self.run_transport({'action': 'status'}, changed_core=True)
        self.assertEqual(code, 1)
        self.assertIn('changed during adaptation', result['error'])
        self.assertFalse(any(event['event'] == 'attach' for event in events))

    def test_error_after_submitted_event_stays_unknown_and_detaches(self):
        result, events, code = self.run_transport({'action': 'send', 'to': 'filehelper', 'text': 'fixture'}, mode='throw_after_submission')
        self.assertEqual(code, 1)
        self.assertEqual(result['state'], 'unknown')
        self.assertTrue(result['submitted'])
        self.assertEqual([e['event'] for e in events][-2:], ['unload', 'detach'])

    def test_rpc_submitted_evidence_is_preserved_even_without_send_event(self):
        result, events, code = self.run_transport({'action': 'send', 'to': 'filehelper', 'text': 'fixture'}, mode='result_without_event')
        self.assertEqual(code, 0)
        self.assertTrue(result['submitted'], 'RPC submitted=true must not be replaced by a missing event')

    def test_script_load_error_is_failed_and_unloaded_without_rpc(self):
        result, events, code = self.run_transport({'action': 'status'}, mode='load_error')
        self.assertEqual(code, 1)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertFalse(any(e['event'] == 'rpc' for e in events))
        self.assertEqual([e['event'] for e in events][-2:], ['unload', 'detach'])


class AdapterOutboxContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='rea-adapter-outbox-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(wechat, 'CACHE', self.root))
        self.test = {'kind': 'text', 'core_sha256': 'a' * 64, 'request_id': 'fixture-request'}
        self.status = {'logged_in': True, 'adapter': {'capabilities': dict.fromkeys(adapter.KINDS, False)}}

    def test_text_adapter_test_fields_reach_both_preflight_and_send(self):
        with patch.object(wechat, 'read_exact', return_value={'account': 'fixture', 'messages': []}), patch.object(wechat, 'transport', side_effect=[self.status, {'submitted': True, 'state': 'unknown'}]) as transport:
            result = wechat.send_message('filehelper', 'fixture', 'fixture-request', adapter_test=self.test)
        self.assertEqual(result['state'], 'unknown')
        for call in transport.call_args_list:
            self.assertEqual(call.args[0]['adapter_test'], self.test)
            self.assertEqual(call.args[0]['to'], 'filehelper')
            self.assertEqual(call.args[0]['request_id'], 'fixture-request')

    def test_wrong_test_target_or_id_stops_before_transport(self):
        with patch.object(wechat, 'transport') as transport:
            with self.assertRaises(ValueError):
                wechat.send_message('other', 'fixture', 'fixture-request', adapter_test=self.test)
            with self.assertRaises(ValueError):
                wechat.send_message('filehelper', 'fixture', 'wrong', adapter_test=self.test)
        transport.assert_not_called()

    def test_media_adapter_test_fields_preserved_and_unknown_not_retried(self):
        image = self.root / 'fixture.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        authorization = {**self.test, 'kind': 'image'}
        with patch.object(media, 'baseline_for', return_value={'account': 'fixture', 'cursors': {'message/message_0.db': 1}}), patch.object(wechat, 'transport', side_effect=[self.status, {'submitted': True, 'state': 'unknown'}]) as transport:
            result = media.send_media('filehelper', image, 'fixture-request', adapter_test=authorization)
            repeated = media.send_media('filehelper', image, 'fixture-request', adapter_test=authorization)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(repeated['state'], 'unknown')
        self.assertTrue(repeated['reused'])
        self.assertEqual(transport.call_count, 2)
        for call in transport.call_args_list:
            self.assertEqual(call.args[0]['adapter_test'], authorization)
            self.assertEqual(call.args[0]['request_id'], 'fixture-request')
            self.assertEqual(call.args[0]['to'], 'filehelper')

    def test_unverified_capability_blocks_normal_send_before_history(self):
        with patch.object(wechat, 'transport', return_value=self.status), patch.object(wechat, 'read_exact') as history:
            with self.assertRaisesRegex(RuntimeError, 'not verified'):
                wechat.send_message('filehelper', 'fixture', 'normal-request')
        history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
