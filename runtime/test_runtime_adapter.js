'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const SOURCE=fs.readFileSync(__dirname+'/runtime.js','utf8');
const FUNCTIONS=['service_getter','executor_getter','context_factory','context_mid','manager_from_mid','message_submit','result_destroy','executor_schedule','text_factory','image_factory','voice_factory'];
const LAYOUT=['recipient','text','type','text_length','image_path','voice_payload','voice_duration','manager_adjust','manager_ready','service_manager_slot','service_login_slot','result_begin','result_end'];
function profile(native=true){
 return {schema_version:1,core_path:'/Applications/Fixture.app/Contents/Resources/wechat.dylib',core_sha256:'a'.repeat(64),arm64_sha256:'b'.repeat(64),profile_id:'fixture',uuid:'00112233-4455-6677-8899-aabbccddeeff',arch:'arm64',module_size:4096,image_size:8192,offsets:Object.fromEntries(FUNCTIONS.map((k,i)=>[k,0x100+i*4])),vtables:{manager:0x1800,text:0x1810,image:0x1820,voice:0x1830},layout:{recipient:144,text:1480,type:228,text_length:368,image_path:240,voice_payload:1480,voice_duration:1504,manager_adjust:-16,manager_ready:24,service_manager_slot:24,service_login_slot:32,result_begin:32,result_end:40},permissions:{native_calls:native,send_text:native,send_image:native,send_voice:native}};
}
function harness(a=profile(),options={}){
 const memory=new Map(),base=0x10000000,heap=0x50000000;let next=heap,clock=0;
 const seen={native:[],loaded:[],events:[],destroyed:[],factories:[],schedules:[]},boxes=new Map();
 class P{
  constructor(value){this.value=Number(value);}
  add(n){return new P(this.value+Number(n));} sub(n){return this.add(-Number(n));}
  compare(p){return Math.sign(this.value-p.value);} equals(p){return this.value===p.value;} isNull(){return this.value===0;} toString(){return '0x'+this.value.toString(16);}
  readU8(){return memory.get(this.value)||0;} readS8(){const v=this.readU8();return v>127?v-256:v;} writeU8(v){memory.set(this.value,v&255);return this;}
  readU32(){let v=0;for(let i=0;i<4;i++)v+=(memory.get(this.value+i)||0)*2**(i*8);return v;}
  writeU32(v){for(let i=0;i<4;i++)this.add(i).writeU8(v>>>i*8);return this;} writeS32(v){return this.writeU32(v>>>0);}
  readU64(){let v=0n;for(let i=0;i<8;i++)v|=BigInt(this.add(i).readU8())<<BigInt(i*8);return {toNumber:()=>Number(v),toString:()=>v.toString()};}
  writeU64(v){v=BigInt(v);for(let i=0;i<8;i++)this.add(i).writeU8(Number(v>>BigInt(i*8)&255n));return this;}
  readPointer(){return new P(this.readU64().toNumber());} writePointer(p){return this.writeU64(p.value);}
  readByteArray(n){return Uint8Array.from({length:n},(_,i)=>this.add(i).readU8()).buffer;}
  writeByteArray(bytes){for(let i=0;i<bytes.length;i++)this.add(i).writeU8(bytes[i]);return this;}
  readUtf8String(n){if(n===undefined){n=0;while(this.add(n).readU8())n++;}return Buffer.from(this.readByteArray(n)).toString('utf8');}
 }
 const p=v=>new P(v);
 const alloc=n=>{const r=p(next);next+=Math.ceil(n/8)*8;return r;};
 const utf8=s=>{const bytes=Buffer.from(s+'\0');return alloc(bytes.length).writeByteArray(bytes);};
 const string=(address,data)=>{const bytes=typeof data==='string'?Buffer.from(data):Buffer.from(data);if(bytes.length<=22){address.writeByteArray(bytes);address.add(23).writeU8(bytes.length);}else{address.writePointer(alloc(bytes.length).writeByteArray(bytes));address.add(8).writeU64(bytes.length);address.add(23).writeU8(128);}};
 p(base).writeU32(0xfeedfacf);p(base+4).writeU32(0x0100000c);p(base+16).writeU32(1);p(base+20).writeU32(24);
 p(base+32).writeU32(0x1b);p(base+36).writeU32(24);p(base+40).writeByteArray(Buffer.from((options.loadedUuid||a.uuid||'00112233-4455-6677-8899-aabbccddeeff').replaceAll('-',''),'hex'));
 const service=alloc(64),serviceTable=alloc(64),manager=alloc(64);
 service.writePointer(serviceTable);serviceTable.add(a.layout?.service_manager_slot||24).writePointer(p(base+0x700));serviceTable.add(a.layout?.service_login_slot||32).writePointer(p(base+0x704));
 manager.writePointer(p(base+(a.vtables?.manager||0x1800)));manager.add(8).writeU8(1);
 for(const [key,offset] of Object.entries(a.vtables||{}))p(base+offset).writePointer(p(base+0x600));
 for(const offset of Object.values(a.offsets||{}))p(base+offset).writeU32(0xd503201f);
 const range=address=>{
  const n=address.value;
  if(n>=base&&n<base+4096)return {base:p(base),size:4096,protection:options.noExec?'r--':'r-x'};
  if(n>=base+4096&&n<base+8192)return options.noData?null:{base:p(base+4096),size:4096,protection:'r--'};
  if(n>=heap&&n<heap+0x1000000)return {base:p(heap),size:0x1000000,protection:'rw-'};
  return null;
 };
 const make=(kind,args)=>{
  const layout=args.at(-1),l=Object.fromEntries(LAYOUT.map((key,i)=>{let v=layout.add(i*4).readU32();if(v>0x7fffffff)v-=0x100000000;return [key,v];}));
  seen.factories.push({kind,address:args[0].value,layout:l});
  if(options.nullFactory)return p(0);
  const input=alloc(8192),box=alloc(8),recipient=args[1].readUtf8String();
  input.writePointer(p(base+a.vtables[kind]));input.add(l.type).writeU32({text:1,image:3,voice:34}[kind]);string(input.add(l.recipient),options.badRecipient?'wrong':recipient);
  if(kind==='text'){string(input.add(l.text),args[2].readUtf8String());input.add(l.text_length).writeU64(Buffer.byteLength(args[2].readUtf8String()));}
  if(kind==='image')string(input.add(l.image_path),args[2].readUtf8String());
  if(kind==='voice'){string(input.add(l.voice_payload),new Uint8Array(args[2].readByteArray(args[3])));input.add(l.voice_duration).writeU32(args[4]);}
  boxes.set(box.value,{input,state:0});return box;
 };
 const call=(key,args)=>{
  seen.native.push(key);
  if(typeof key==='number'){
   if(key===base+a.offsets.service_getter)return service;
   if(key===base+0x700)return manager;
   if(key===base+0x704)return options.loggedOut?0:1;
   throw Error('Unexpected native address '+key);
  }
  if(key==='rea_bridge_abi_version')return options.abi||2;
  if(key==='rea_make_text_input')return make('text',args);
  if(key==='rea_make_image_input')return make('image',args);
  if(key==='rea_make_voice_input')return make('voice',args);
  if(key==='rea_text_input_object')return boxes.get(args[0].value).input;
  if(key==='rea_destroy_text_input'){seen.destroyed.push(args[0].value);return;}
  if(key==='rea_schedule_text'){
   seen.schedules.push({functions:FUNCTIONS.slice(0,8).map((_,i)=>args[0].add(i*8).readPointer().value),begin:args[2].readU32(),end:args[2].add(4).readU32()});
   boxes.get(args[1].value).state=options.timeout?1:2;return;
  }
  if(key==='rea_work_state')return boxes.get(args[0].value).state;
  if(key==='rea_work_identity_count')return {toNumber:()=>1};
  if(key==='rea_work_identity_string')return utf8('fixture-'+args[1]);
  if(key==='rea_work_identity_word')return {toString:()=>String(100+args[1])};
  throw Error('Unexpected bridge function '+key);
 };
 const module={path:a.core_path,base:p(base),size:options.moduleSize||4096};
 const context={REA_ADAPTER:a,Process:{id:123,arch:'arm64',pointerSize:8,enumerateModules:()=>options.wrongPath?[{...module,path:'/wrong/wechat.dylib'}]:options.duplicates?[module,module]:[module],findRangeByAddress:range},Memory:{alloc,allocUtf8String:utf8},Module:{load:path=>{seen.loaded.push(path);return {getExportByName:name=>name};}},NativeFunction:function(address){return (...args)=>call(address instanceof P?address.value:address,args);},rpc:{},send:event=>seen.events.push(event),setTimeout:fn=>setImmediate(fn),Uint8Array,Date:options.timeout?{now:()=>{clock+=10000;return clock;}}:Date};
 vm.runInNewContext(SOURCE,context,{filename:'runtime.js'});
 return {rpc:context.rpc.exports,seen,context,base};
}
const req={bridge:'/private/tmp/fixture.dylib',to:'filehelper',text:'你好 测试',image_path:'/private/tmp/test.png',voice_bytes:[2,35,33,83,73,76,75,95,86,51,0,1],duration_ms:1000,request_id:'fixture-1'};
test('unknown profile status/probe are passive with vtables outside __TEXT',()=>{
 const h=harness(profile(false));assert.equal(h.rpc.status().logged_in,null);assert.equal(h.rpc.probe().checks.readable_vtables,true);assert.equal(h.seen.native.length,0);assert.equal(h.seen.loaded.length,0);
});
test('unknown profile cannot invoke any send or construct',async()=>{
 const h=harness(profile(false));for(const method of ['sendText','sendImage','sendVoice']){await assert.rejects(h.rpc[method](req),/passive/);await assert.rejects(h.rpc[method]({...req,construct_only:true}),/passive/);}
 assert.throws(()=>h.rpc.probe({...req,construct_only:true}),/passive/);assert.equal(h.seen.native.length,0);assert.equal(h.seen.events.length,0);
});
test('missing/invalid descriptor fields fail before invoking native code',()=>{
 for(const mutate of [a=>{delete a.offsets.text_factory;},a=>{a.offsets.text_factory=1;},a=>{a.layout.text=-1;},a=>{a.layout.recipient=0;},a=>{a.layout.text=a.layout.recipient;},a=>{a.layout.result_end=a.layout.result_begin;},a=>{a.core_sha256='wrong';},a=>{a.arch='x64';},a=>{a.permissions.native_calls=false;},a=>{delete a.permissions.send_voice;}]){const a=profile();mutate(a);assert.throws(()=>harness(a),/adapter|permission/);}
});
test('module matching is full path and unique with matching __TEXT size',()=>{
 assert.throws(()=>harness(profile(),{wrongPath:true}),/core_path/);assert.throws(()=>harness(profile(),{duplicates:true}),/core_path/);assert.throws(()=>harness(profile(),{moduleSize:5000}),/size mismatch/);
});
test('mapped protections and image extent are checked',()=>{
 assert.throws(()=>harness(profile(),{noExec:true}),/not executable/);assert.throws(()=>harness(profile(),{noData:true}),/Unreadable/);const a=profile();a.image_size=4096;assert.throws(()=>harness(a),/vtables/);
});
test('default passive probe never calls getters even for verified profile',()=>{
 const h=harness();assert.equal(h.rpc.probe().passive,true);assert.equal(h.seen.native.length,0);
});
test('verified profile status performs service/manager/login readback',()=>{
 const h=harness();const result=h.rpc.status();assert.equal(result.logged_in,true);assert.equal(result.manager_found,true);assert.equal(result.ready,true);assert.equal(result.passive,false);assert.equal(h.seen.schedules.length,0);
});
test('construct probe reads/destroys text, image and voice without any scheduling',()=>{
 const h=harness();const result=h.rpc.probe({...req,construct_only:true});assert.equal(result.submitted,false);assert.equal(result.constructions.text.text_verified,true);assert.equal(result.constructions.image.image_path_verified,true);assert.equal(result.constructions.voice.recipient_verified,true);assert.equal(h.seen.destroyed.length,3);assert.equal(h.seen.schedules.length,0);assert.equal(h.seen.events.length,0);assert(!h.seen.native.includes(h.base+profile().offsets.service_getter));
});
test('individual construct_only works without send permission and without login',async()=>{
 const a=profile();a.permissions.send_text=a.permissions.send_image=a.permissions.send_voice=false;const h=harness(a,{loggedOut:true});
 for(const method of ['sendText','sendImage','sendVoice'])assert.equal((await h.rpc[method]({...req,construct_only:true})).submitted,false);
 assert.equal(h.seen.schedules.length,0);assert.equal(h.seen.destroyed.length,3);
});
test('each media send checks its own permission before any native call',async()=>{
 for(const [method,key] of [['sendText','send_text'],['sendImage','send_image'],['sendVoice','send_voice']]){const a=profile();a.permissions[key]=false;const h=harness(a);await assert.rejects(h.rpc[method](req),/sending is disabled/);assert.equal(h.seen.native.length,0);assert.equal(h.seen.events.length,0);}
});
test('adapter injected offsets/layout drive calls and readbacks',async()=>{
 const a=profile();for(const key of FUNCTIONS)a.offsets[key]+=0x200;for(const key of ['recipient','text','type','text_length','image_path','voice_payload','voice_duration'])a.layout[key]+=2048;
 const h=harness(a);const result=await h.rpc.sendText({...req,construct_only:true});assert.equal(result.text_verified,true);assert.equal(h.seen.factories[0].address,h.base+a.offsets.text_factory);assert.equal(h.seen.factories[0].layout.text,a.layout.text);assert.equal(h.seen.factories[0].layout.manager_adjust,-16);
});
test('factory recipient mismatch destroys input without a submitted event',async()=>{
 const h=harness(profile(),{badRecipient:true});await assert.rejects(h.rpc.sendVoice({...req,construct_only:true}),/readback mismatch/);assert.equal(h.seen.destroyed.length,1);assert.equal(h.seen.events.length,0);
});
test('bridge ABI mismatch and null factory never schedule',async()=>{
 const h=harness(profile(),{abi:1});await assert.rejects(h.rpc.sendText({...req,construct_only:true}),/ABI v2/);assert.equal(h.seen.factories.length,0);
 const n=harness(profile(),{nullFactory:true});await assert.rejects(n.rpc.sendText({...req,construct_only:true}),/returned null/);assert.equal(n.seen.schedules.length,0);
});
test('native send passes absolute function table and result layout to ABI v2',async()=>{
 const a=profile(),h=harness(a);const r=await h.rpc.sendText(req);assert.equal(r.submitted,true);assert.equal(r.identity.count,1);assert.equal(h.seen.destroyed.length,1);assert.equal(h.seen.events.length,1);assert.deepEqual(h.seen.schedules[0].functions,FUNCTIONS.slice(0,8).map(key=>h.base+a.offsets[key]));assert.equal(h.seen.schedules[0].begin,32);assert.equal(h.seen.schedules[0].end,40);
});
test('native executor timeout keeps box alive and returns unknown',async()=>{
 const h=harness(profile(),{timeout:true});const r=await h.rpc.sendText(req);assert.equal(r.state,'unknown');assert.equal(r.submitted,true);assert.equal(h.seen.destroyed.length,0);assert.equal(h.seen.schedules.length,1);
});
test('descriptor object is snapshotted and cannot gain send permission later',async()=>{
 const a=profile();a.permissions.send_text=false;const h=harness(a);a.permissions.send_text=true;await assert.rejects(h.rpc.sendText(req),/disabled/);
});
test('voice bytes and duration validation occurs before bridge loading',async()=>{
 const h=harness();await assert.rejects(h.rpc.sendVoice({...req,construct_only:true,voice_bytes:[...req.voice_bytes,256]}),/Invalid voice/);assert.equal(h.seen.loaded.length,0);
});

test('loaded image UUID mismatch rejects same path and same size',()=>{
 assert.throws(()=>harness(profile(),{loadedUuid:'ffffffff-4455-6677-8899-aabbccddeeff'}),/UUID does not match/);
 const a=profile();delete a.uuid;assert.throws(()=>harness(a),/adapter uuid/);
});
test('test descriptor only permits filehelper before calling any native method',async()=>{
 const a=profile();a.test_recipient='filehelper';const h=harness(a);
 for(const method of ['sendText','sendImage','sendVoice'])await assert.rejects(h.rpc[method]({...req,to:'someone_else'}),/restricted to filehelper/);
 assert.equal(h.seen.native.length,0);assert.equal(h.seen.events.length,0);
 const result=await h.rpc.sendText(req);assert.equal(result.submitted,true);
 const invalid=profile();invalid.test_recipient='someone_else';assert.throws(()=>harness(invalid),/test_recipient/);
});
