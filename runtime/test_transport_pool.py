"""Exercise the actual Unix broker with mocked adapter subprocess and Frida.

No test attaches to an application or submits a real message.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
NODE = shutil.which('node')
PRELOAD = r'''
import fs from 'node:fs';
import path from 'node:path';
import child from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const read=fs.readFileSync.bind(fs);
const fixture=()=>JSON.parse(read(process.env.FIXTURE,'utf8'));
const record=value=>fs.appendFileSync(process.env.EVENTS,JSON.stringify(value)+'\n');
const stat=file=>{try{const s=fs.statSync(file,{bigint:true});return {dev:String(s.dev),ino:String(s.ino),size:String(s.size),mtime_ns:String(s.mtimeNs),ctime_ns:String(s.ctimeNs)};}catch(e){if(e.code==='ENOENT')return null;throw e;}};
fs.readFileSync=(filename,...args)=>{if(String(filename)===fixture().core)record({event:'core_read'});return read(filename,...args);};
child.execFileSync=(executable,args,options)=>{
 const f=fixture();record({event:'exec',executable,args});
 if(executable==='/bin/ps')return `${f.pid} ${f.start} ${f.exe}\n`;
 if(executable===process.env.WECHAT_PYTHON){
  const req=JSON.parse(args[args.indexOf('--runtime-request')+1]);
  const identity=JSON.parse(args[args.indexOf('--process-identity')+1]);
  const profileNames=fs.readdirSync(f.profiles).filter(x=>x.endsWith('.json')).sort();
  const files=[f.core,...f.dependencies,...profileNames.map(x=>path.join(f.profiles,x)),f.verified];
  const perms={native_calls:true,...Object.fromEntries(['text','image','voice'].map(k=>['send_'+k,f.capabilities[k]]))};
  if(req.construct_only)for(const k of ['text','image','voice'])perms['send_'+k]=false;
  if(req.adapter_test){
   const t=req.adapter_test;
   if(req.to!=='filehelper'||t.core_sha256!==f.digest||t.request_id!==req.request_id)throw Error('invalid adapter test');
   for(const k of ['text','image','voice'])perms['send_'+k]=!req.construct_only&&k===t.kind;
  }
  const descriptor={profile_id:'pool-fixture',core_sha256:f.digest,permissions:perms,...(req.adapter_test?{test_recipient:'filehelper'}:{})};
  return JSON.stringify({status:'exact',profile_id:'pool-fixture',identity:{core_sha256:f.digest},known_verified_image:true,
   capabilities:f.capabilities,descriptor,runtime_descriptor:descriptor,process_identity:{exe:identity.exe,pid:identity.pid,start:identity.start},
   core_stat:f.stale_stat||stat(f.core),dependency_stats:{files:Object.fromEntries(files.map(file=>[file,stat(file)])),profile_directory:f.profiles,profile_names:profileNames},
   cache_info:{core_hash_checked:!f.resolver_fast,fast_hit:!!f.resolver_fast}});
 }
 if(executable==='clang++'){fs.writeFileSync(args[args.indexOf('-o')+1],'mock bridge');return '';}
 throw Error('Forbidden subprocess '+executable);
};
syncBuiltinESMExports();
'''

FRIDA = r'''
import fs from 'node:fs';
const fixture=()=>JSON.parse(fs.readFileSync(process.env.FIXTURE,'utf8'));
const record=value=>fs.appendFileSync(process.env.EVENTS,JSON.stringify(value)+'\n');
export async function attach(pid){
 record({event:'attach',pid});let detached=false;const epoch=fixture().detach_epoch;
 if(fixture().attach_delay_ms)await new Promise(resolve=>setTimeout(resolve,fixture().attach_delay_ms));
 return {
  isDetached(){return detached||fixture().detach_epoch!==epoch;},
  async detach(){detached=true;record({event:'detach'});},
  async createScript(source){
   let callback,readyTimer;const descriptor=JSON.parse(source.slice('globalThis.REA_ADAPTER = '.length,source.indexOf(';\n')));
   record({event:'injection',descriptor});
   const invoke=async(method,request={})=>{
    const f=fixture();record({event:'rpc',method,request_id:request.request_id});
    if(f.delay_ms)await new Promise(resolve=>setTimeout(resolve,f.delay_ms));
    if(method==='status')return {logged_in:true};
    if(method==='probe')return {construct_only:request.construct_only===true,submitted:false};
    if(f.mode==='throw_without_event')throw Error('connection disappeared after dispatch');
    if(f.mode==='stale_event'){callback({type:'send',payload:{kind:'submitted',request_id:'previous-request'}});return {submitted:false};}
    callback({type:'send',payload:{kind:'submitted',request_id:request.request_id}});
    if(f.mode==='throw_after_event')throw Error('connection disappeared after event');
    return {submitted:true,enqueued:true};
   };
   return {message:{connect(fn){callback=fn;}},async load(){record({event:'load'});
    if(fixture().mode==='initialization_error')callback({type:'error',description:'Expected exactly one module matching adapter core_path',lineNumber:47,stack:'fixture stack'});
    else if(fixture().mode==='waiting_module'){
     callback({type:'send',payload:{kind:'rea_runtime_waiting'}});
     readyTimer=setTimeout(()=>callback({type:'send',payload:{kind:'rea_runtime_ready',exports:['status','probe','sendText','sendImage','sendVoice'],wait_ms:1000}}),1000);
    }
    else callback({type:'send',payload:{kind:'rea_runtime_ready',exports:fixture().mode==='missing_exports'?['status']:['status','probe','sendText','sendImage','sendVoice']}});
   },async unload(){clearTimeout(readyTimer);record({event:'unload'});},
    exports:{status:()=>invoke('status'),probe:req=>invoke('probe',req),sendText:req=>invoke('sendText',req),sendImage:req=>invoke('sendImage',req),sendVoice:req=>invoke('sendVoice',req)}};
  }
 };
}
'''


class TransportPoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wcpool-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        for name in ('transport.mjs', 'transport_session.mjs', 'runtime_config.mjs', 'runtime.js', 'native_message_bridge.cpp'):
            shutil.copy2(ROOT / name, self.runtime / name)
        (self.runtime / 'adapter.py').write_text('# fixture only\n')
        profiles = self.runtime / 'profiles'
        profiles.mkdir()
        (profiles / 'fixture.json').write_text('{}')
        self.core = self.root / 'Applications/WeChat REA Test.app/Contents/Resources/wechat.dylib'
        self.core.parent.mkdir(parents=True)
        self.core.write_bytes(b'fixture native image')
        self.fixture_path = self.root / 'fixture.json'
        self.events = self.root / 'events.jsonl'
        self.events.write_text('')
        self.fixture = {'core': str(self.core), 'digest': hashlib.sha256(self.core.read_bytes()).hexdigest(),
                        'exe': str(self.root / 'Applications/WeChat REA Test.app/Contents/MacOS/WeChat'),
                        'pid': 123, 'start': 'Fri Oct 16 12:00:00 2026', 'profiles': str(profiles),
                        'dependencies': [str(self.runtime / name) for name in ('adapter.py', 'runtime.js', 'native_message_bridge.cpp')],
                        'verified': str(self.root / 'verified.json'),
                        'capabilities': dict.fromkeys(('text', 'image', 'voice'), True)}
        self.update()
        (self.root / 'preload.mjs').write_text(PRELOAD)
        (self.root / 'frida.mjs').write_text(FRIDA)
        self.sockdir = self.root / 'socket'
        self.sockdir.mkdir(mode=0o700)
        self.socket = self.sockdir / 'transport.sock'
        self.server = None
        self.addCleanup(self.cleanup_server)

    def update(self, **changes):
        self.fixture.update(changes)
        temporary = self.fixture_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.fixture))
        temporary.replace(self.fixture_path)

    def start(self, idle_ms=5000):
        env = dict(os.environ, HOME=str(self.root), FIXTURE=str(self.fixture_path), EVENTS=str(self.events),
                   WECHAT_FRIDA_MODULE=str(self.root / 'frida.mjs'), WECHAT_PYTHON='/fixture/python3')
        self.server = subprocess.Popen([NODE, '--import', str(self.root / 'preload.mjs'),
                                       str(self.runtime / 'transport.mjs'), '--serve', '--socket',
                                       str(self.socket), '--idle-ms', str(idle_ms)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        line = self.server.stdout.readline()
        if not line:
            self.fail(self.server.stderr.read())
        self.assertTrue(json.loads(line).get('ready'), line)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)

    def cleanup_server(self):
        if self.server:
            if self.server.poll() is None:
                self.server.terminate()
                try:
                    self.server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.server.kill()
                    self.server.wait(timeout=3)
            self.server.stdout.close()
            self.server.stderr.close()

    def connect_send(self, request):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(str(self.socket))
        connection.sendall(json.dumps(request).encode() + b'\n')
        return connection

    @staticmethod
    def receive(connection):
        try:
            data = bytearray()
            while b'\n' not in data:
                part = connection.recv(65536)
                if not part:
                    raise AssertionError('Broker closed before response')
                data.extend(part)
            return json.loads(data.split(b'\n')[0])
        finally:
            connection.close()

    def call(self, request):
        return self.receive(self.connect_send(request))

    def traces(self, event=None):
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        return [row for row in rows if event is None or row['event'] == event]

    def wait_rpc(self, count=1):
        deadline = time.monotonic() + 3
        while len(self.traces('rpc')) < count:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)

    def test_warm_status_and_send_share_resolution_attach_and_script(self):
        self.start()
        cold = self.call({'action': 'status'})
        warm = self.call({'action': 'status'})
        sent = self.call({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'pool-1'})
        self.assertTrue(cold['logged_in'])
        self.assertTrue(sent['submitted'])
        for result in (warm, sent):
            self.assertTrue(result['transport']['cache_hit'])
            self.assertTrue(result['transport']['script_reused'])
            self.assertFalse(result['transport']['did_resolve'])
            self.assertFalse(result['transport']['core_hash_checked'])
            self.assertEqual(result['transport']['resolutions'], 1)
            self.assertEqual(result['transport']['attaches'], 1)
            self.assertEqual(result['transport']['script_loads'], 1)
        self.assertEqual(self.traces('core_read'), [])

    def test_process_start_change_with_same_pid_forces_resolution_and_reattach(self):
        self.start()
        self.call({'action': 'status'})
        self.update(start='Fri Oct 16 12:01:00 2026')
        result = self.call({'action': 'status'})
        self.assertEqual(result['transport']['resolutions'], 2)
        self.assertEqual(result['transport']['attaches'], 2)
        self.assertFalse(result['transport']['cache_hit'])

    def test_core_replacement_and_profile_addition_invalidate(self):
        self.start()
        self.call({'action': 'status'})
        self.core.write_bytes(b'a changed fixture image')
        self.update(digest=hashlib.sha256(self.core.read_bytes()).hexdigest())
        after_core = self.call({'action': 'status'})
        self.assertEqual(after_core['transport']['attaches'], 2)
        (Path(self.fixture['profiles']) / 'new.json').write_text('{}')
        after_profile = self.call({'action': 'status'})
        self.assertEqual(after_profile['transport']['attaches'], 3)
        self.assertEqual(after_profile['transport']['resolutions'], 3)

    def test_detached_session_is_revalidated_and_attached_again(self):
        self.start()
        self.call({'action': 'status'})
        self.update(detach_epoch=1, resolver_fast=True)
        result = self.call({'action': 'status'})
        self.assertTrue(result['process_alive'])
        self.assertEqual(result['transport']['attaches'], 2)
        self.assertEqual(result['transport']['resolutions'], 2)
        self.assertFalse(result['transport']['cache_hit'])

    def test_verified_receipt_change_reloads_permissions_without_stale_allow(self):
        self.start()
        self.call({'action': 'status'})
        self.update(capabilities=dict.fromkeys(('text', 'image', 'voice'), False), resolver_fast=True)
        Path(self.fixture['verified']).write_text('{}')
        result = self.call({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'blocked'})
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertFalse(any(row['method'] == 'sendText' for row in self.traces('rpc')))
        self.assertEqual(result['transport']['full_resolutions'], 1)

    def test_construction_permissions_reload_script_but_reuse_attach(self):
        self.start()
        self.call({'action': 'status'})
        probe = self.call({'action': 'probe', 'construct_only': True})
        self.assertTrue(probe['construct_only'])
        self.assertEqual(probe['transport']['attaches'], 1)
        self.assertEqual(probe['transport']['script_loads'], 2)
        perms = self.traces('injection')[-1]['descriptor']['permissions']
        self.assertFalse(any(perms['send_' + kind] for kind in ('text', 'image', 'voice')))
        result = self.call({'action': 'status'})
        self.assertEqual(result['transport']['resolutions'], 2)
        self.assertEqual(result['transport']['attaches'], 1)
        self.assertEqual(result['transport']['script_loads'], 3)

    def test_cached_test_authorization_still_checks_recipient_kind_and_id(self):
        self.start()
        test = {'kind': 'text', 'core_sha256': self.fixture['digest'], 'request_id': 'test-one'}
        request = {'action': 'status', 'to': 'filehelper', 'request_id': 'test-one', 'adapter_test': test}
        self.call(request)
        failed = self.call({**request, 'action': 'send-voice', 'voice_path': str(self.core)})
        self.assertEqual(failed['state'], 'failed')
        self.assertIn('kind does not match', failed['error'])
        self.assertEqual(failed['transport']['resolutions'], 1)
        self.assertFalse(any(row['method'] == 'sendVoice' for row in self.traces('rpc')))

    def test_send_disconnect_without_event_is_unknown_and_not_retried(self):
        self.update(mode='throw_without_event')
        self.start()
        result = self.call({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'once'})
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len(self.traces('rpc')), 1)
        self.assertEqual(len(self.traces('attach')), 1)
        self.assertFalse(result['transport']['attached'])

    def test_submitted_event_from_another_request_is_ignored(self):
        self.update(mode='stale_event')
        self.start()
        result = self.call({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'current'})
        self.assertFalse(result['submitted'])

    def test_stop_drops_queued_send_waits_active_and_keeps_socket_bound(self):
        self.update(delay_ms=500)
        self.start()
        active = self.connect_send({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'active'})
        self.wait_rpc()
        queued = self.connect_send({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'queued'})
        stop = self.connect_send({'action': '_transport_stop'})
        cancelled = self.receive(queued)
        self.assertEqual(cancelled['state'], 'failed')
        self.assertFalse(cancelled['submitted'])
        self.assertTrue(self.socket.exists(), 'stop must not expose a replacement socket before detach')
        self.assertTrue(self.receive(active)['submitted'])
        self.assertTrue(self.receive(stop)['stopped'])
        self.server.wait(timeout=3)
        self.assertFalse(self.socket.exists())
        self.assertEqual([row['request_id'] for row in self.traces('rpc')], ['active'])

    def test_self_code_change_requires_broker_restart_before_dispatch(self):
        self.start()
        self.call({'action': 'status'})
        with (self.runtime / 'transport_session.mjs').open('a') as stream:
            stream.write('\n// fixture replacement\n')
        result = self.call({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'blocked'})
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['restart_required'])
        self.assertFalse(result['submitted'])
        self.server.wait(timeout=3)
        self.assertFalse(self.socket.exists())
        self.assertEqual(len(self.traces('rpc')), 1)

    def test_stop_during_attach_cancels_active_request_before_rpc(self):
        self.update(attach_delay_ms=400)
        self.start()
        active = self.connect_send({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'not-dispatched'})
        deadline = time.monotonic() + 3
        while not self.traces('attach'):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        stop = self.connect_send({'action': '_transport_stop'})
        result = self.receive(active)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertIn('paused before dispatch', result['error'])
        self.assertTrue(self.receive(stop)['stopped'])
        self.server.wait(timeout=3)
        self.assertEqual(self.traces('rpc'), [])
        self.assertEqual(len(self.traces('detach')), 1)

    def test_top_level_runtime_error_is_reported_before_any_rpc(self):
        self.update(mode='initialization_error')
        self.start()
        result = self.call({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'no-exports'})
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertIn('Expected exactly one module', result['runtime_initialization']['description'])
        self.assertEqual(result['runtime_initialization']['line_number'], 47)
        self.assertEqual(self.traces('rpc'), [])
        self.assertEqual(len(self.traces('attach')), 1)

    def test_missing_exports_fails_before_rpc_and_is_not_retried(self):
        self.update(mode='missing_exports')
        self.start()
        result = self.call({'action': 'probe'})
        self.assertEqual(result['state'], 'failed')
        self.assertIn('missing required exports', result['error'])
        self.assertEqual(self.traces('rpc'), [])
        self.assertEqual(len(self.traces('load')), 1)

    def test_stop_cancels_module_readiness_wait_without_dispatch(self):
        self.update(mode='waiting_module')
        self.start()
        active = self.connect_send({'action': 'send', 'to': 'filehelper', 'text': 'fixture', 'request_id': 'waiting'})
        deadline = time.monotonic() + 3
        while not self.traces('load'):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        stop = self.connect_send({'action': '_transport_stop'})
        result = self.receive(active)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['submitted'])
        self.assertEqual(result['runtime_initialization']['code'], 'cancelled')
        self.assertTrue(self.receive(stop)['stopped'])
        self.server.wait(timeout=3)
        self.assertEqual(self.traces('rpc'), [])

    def test_module_bootstrap_with_controlled_clock(self):
        result = subprocess.run([NODE, '--test', '--test-reporter=tap', str(ROOT / 'test_transport_pool_bootstrap.mjs')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('# tests 6', result.stdout)

    def test_health_and_idle_exit_never_attach(self):
        self.start(idle_ms=150)
        result = self.call({'action': '_transport_health'})
        self.assertTrue(result['running'])
        self.assertFalse(result['transport']['attached'])
        self.server.wait(timeout=3)
        self.assertEqual(self.traces(), [])
        self.assertFalse(self.socket.exists())

    def test_malformed_and_oversized_frames_never_attach(self):
        self.start()
        for payload in (b'[]\n', b'not JSON\n', b'{"action":"status"}\n{}\n', b'x' * (2 * 1024 * 1024 + 1)):
            with self.subTest(size=len(payload)):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(5)
                connection.connect(str(self.socket))
                connection.sendall(payload)
                result = self.receive(connection)
                self.assertEqual(result['state'], 'failed')
                self.assertFalse(result['submitted'])
        self.assertEqual(self.traces(), [])


if __name__ == '__main__':
    unittest.main()
