'use strict';
// All version-specific addresses/fields come from the host-validated adapter.
// Unknown profiles are passive: not even the service getter is called.
const FUNCTION_FIELDS=['service_getter','executor_getter','context_factory','context_mid','manager_from_mid','message_submit','result_destroy','executor_schedule','text_factory','image_factory','voice_factory'];
const SCHEDULE_FIELDS=FUNCTION_FIELDS.slice(0,8);
const VTABLE_FIELDS=['manager','text','image','voice'];
const LAYOUT_FIELDS=['recipient','text','type','text_length','image_path','voice_payload','voice_duration','manager_adjust','manager_ready','service_manager_slot','service_login_slot','result_begin','result_end'];
const PERMISSION_FIELDS=['native_calls','send_text','send_image','send_voice'];
function fail(message){throw Error(message);}
function object(value,name){if(!value||typeof value!=='object'||Array.isArray(value))fail('Missing adapter '+name);return value;}
function integer(value,min,max,alignment,name){if(!Number.isSafeInteger(value)||value<min||value>max||value%alignment!==0)fail('Invalid adapter '+name);return value;}
function descriptor(source){
 object(source,'descriptor');
 // Clone and freeze to keep authorization/ABI fixed for this attached script.
 const a=JSON.parse(JSON.stringify(source));
 if(a.schema_version!==1||a.arch!=='arm64'||Process.arch!=='arm64'||Process.pointerSize!==8)fail('Unsupported adapter schema/architecture');
 if(typeof a.core_path!=='string'||!a.core_path.startsWith('/')||a.core_path.includes('\0'))fail('Invalid adapter core_path');
 for(const key of ['core_sha256','arm64_sha256'])if(typeof a[key]!=='string'||!/^[a-f0-9]{64}$/.test(a[key]))fail('Invalid adapter '+key);
 if(typeof a.profile_id!=='string'||!a.profile_id||a.profile_id.length>200)fail('Invalid adapter profile_id');
 if(typeof a.uuid!=='string'||!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(a.uuid))fail('Invalid adapter uuid');
 if(a.test_recipient!==undefined&&a.test_recipient!==null&&a.test_recipient!==''&&a.test_recipient!=='filehelper')fail('Adapter test_recipient must be filehelper');
 integer(a.module_size,4096,0xffffffff,1,'module_size');integer(a.image_size,a.module_size,0xffffffff,1,'image_size');
 object(a.offsets,'offsets');object(a.vtables,'vtables');object(a.layout,'layout');object(a.permissions,'permissions');
 for(const key of FUNCTION_FIELDS)integer(a.offsets[key],0,a.image_size-4,4,'offsets.'+key);
 for(const key of VTABLE_FIELDS)integer(a.vtables[key],0,a.image_size-8,8,'vtables.'+key);
 for(const key of ['recipient','text','image_path','voice_payload'])integer(a.layout[key],8,65536-24,8,'layout.'+key);
 for(const key of ['type','voice_duration'])integer(a.layout[key],8,65536-4,4,'layout.'+key);
 integer(a.layout.text_length,8,65536-8,8,'layout.text_length');
 const widths={recipient:24,text:24,type:4,text_length:8,image_path:24,voice_payload:24,voice_duration:4};
 for(const names of [['recipient','type','text','text_length'],['recipient','type','image_path'],['recipient','type','voice_payload','voice_duration']]){
  const intervals=names.map(key=>[a.layout[key],a.layout[key]+widths[key]]).sort((left,right)=>left[0]-right[0]);
  for(let index=1;index<intervals.length;index++)if(intervals[index][0]<intervals[index-1][1])fail('Overlapping adapter input fields');
 }
 integer(a.layout.manager_adjust,-4096,4096,8,'layout.manager_adjust');
 integer(a.layout.manager_ready,0,65535,1,'layout.manager_ready');
 for(const key of ['service_manager_slot','service_login_slot'])integer(a.layout[key],0,4096-8,8,'layout.'+key);
 for(const key of ['result_begin','result_end'])integer(a.layout[key],0,56,8,'layout.'+key);
 if(a.layout.result_begin===a.layout.result_end)fail('Overlapping adapter result fields');
 for(const key of PERMISSION_FIELDS)if(typeof a.permissions[key]!=='boolean')fail('Invalid adapter permissions.'+key);
 if(!a.permissions.native_calls&&PERMISSION_FIELDS.slice(1).some(key=>a.permissions[key]))fail('Send permission requires native_calls');
 for(const key of ['offsets','vtables','layout','permissions'])Object.freeze(a[key]);
 return Object.freeze(a);
}
const adapter=descriptor(globalThis.REA_ADAPTER);
const matches=Process.enumerateModules().filter(m=>m.path===adapter.core_path);
if(matches.length!==1)fail('Expected exactly one module matching adapter core_path');
const mod=matches[0],b=mod.base;
if(mod.size!==adapter.module_size)fail('Adapter module size mismatch');
function readable(address,length){
 if(address.isNull())fail('Null native pointer');
 const range=Process.findRangeByAddress(address);
 if(!range||!range.protection.includes('r')||address.add(length).compare(range.base.add(range.size))>0)fail('Unreadable native memory range');
 return range;
}
function imageAddress(address,width){
 if(address.compare(b)<0||address.add(width).compare(b.add(adapter.image_size))>0)fail('Native address outside adapter image');
 return address;
}
function executable(address){
 imageAddress(address,4);const range=readable(address,4);
 if(!range.protection.includes('x'))fail('Native function is not executable');
 return address;
}
function loadedUuid(){
 readable(b,32);
 if(b.readU32()!==0xfeedfacf||b.add(4).readU32()!==0x0100000c)fail('Loaded image is not arm64 Mach-O');
 const count=b.add(16).readU32(),size=b.add(20).readU32();
 if(count<1||count>4096||size<8||size>Math.min(1048576,adapter.module_size-32))fail('Invalid loaded Mach-O load commands');
 readable(b.add(32),size);let position=32,uuid=null;
 for(let index=0;index<count;index++){
  if(position+8>32+size)fail('Truncated loaded Mach-O command');
  const command=b.add(position),kind=command.readU32(),length=command.add(4).readU32();
  if(length<8||length%8||position+length>32+size)fail('Invalid loaded Mach-O command size');
  if(kind===0x1b){
   if(length!==24||uuid!==null)fail('Invalid loaded Mach-O UUID command');
   const hex=Array.from(new Uint8Array(command.add(8).readByteArray(16)),value=>value.toString(16).padStart(2,'0')).join('');
   uuid=[hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-');
  }
  position+=length;
 }
 if(position!==32+size||uuid!==adapter.uuid)fail('Loaded image UUID does not match adapter; restart/version selection required');
 return uuid;
}
const imageUuid=loadedUuid();
const addresses={};const vtables={};
for(const key of FUNCTION_FIELDS)addresses[key]=executable(b.add(adapter.offsets[key]));
// __DATA_CONST vtables can be beyond Frida's __TEXT module size. Validate them
// against the full mapped image extent and actual VM protections instead.
for(const key of VTABLE_FIELDS){
 const address=imageAddress(b.add(adapter.vtables[key]),8);readable(address,8);
 executable(address.readPointer());vtables[key]=address;
}
function passive(){
 return {pid:Process.id,core:mod.path,core_size:mod.size,image_size:adapter.image_size,profile_id:adapter.profile_id,uuid:imageUuid,core_sha256:adapter.core_sha256,arm64_sha256:adapter.arm64_sha256,permissions:adapter.permissions,passive:true,checks:{module_path:true,module_size:true,loaded_uuid:true,executable_addresses:true,readable_vtables:true},instructions:Object.fromEntries(FUNCTION_FIELDS.map(key=>[key,addresses[key].readU32()])),vtable_targets:Object.fromEntries(VTABLE_FIELDS.map(key=>[key,vtables[key].readPointer().toString()]))};
}
function nativeAllowed(){if(!adapter.permissions.native_calls)fail('Adapter is passive; native calls are disabled');}
function serviceState(){
 nativeAllowed();
 const service=new NativeFunction(addresses.service_getter,'pointer',[])();
 if(service.isNull())fail('WeChat service is not initialized');
 readable(service,8);const table=service.readPointer();
 const method=slot=>{readable(table.add(slot),8);return executable(table.add(slot).readPointer());};
 const manager=new NativeFunction(method(adapter.layout.service_manager_slot),'pointer',['pointer'])(service);
 if(manager.isNull())fail('WeChat manager is not initialized');
 readable(manager,8);if(!manager.readPointer().equals(vtables.manager))fail('Unexpected manager interface');
 const ready=manager.add(adapter.layout.manager_adjust).add(adapter.layout.manager_ready);readable(ready,1);
 const logged=new NativeFunction(method(adapter.layout.service_login_slot),'uint',['pointer'])(service)!==0;
 return {manager_found:true,ready:ready.readU8()===1,logged_in:logged};
}
function packedLayout(){
 const pointer=Memory.alloc(LAYOUT_FIELDS.length*4);
 LAYOUT_FIELDS.forEach((key,index)=>pointer.add(index*4).writeS32(adapter.layout[key]));return pointer;
}
function bridgeApi(path){
 nativeAllowed();
 if(typeof path!=='string'||!path.startsWith('/')||path.includes('\0'))fail('Invalid bridge path');
 const bridge=Module.load(path);
 const fn=(name,ret,args)=>new NativeFunction(bridge.getExportByName(name),ret,args);
 if(fn('rea_bridge_abi_version','uint',[])()!==2)fail('Native bridge ABI v2 required');
 return {fn,destroy:fn('rea_destroy_text_input','void',['pointer']),state:fn('rea_work_state','int',['pointer'])};
}
function stringValue(address,maxLength){
 readable(address,24);const tag=address.add(23).readU8();
 const length=(tag&128)?address.add(8).readU64().toNumber():tag;
 if(!Number.isSafeInteger(length)||length<0||length>maxLength||(tag<128&&length>22))fail('Invalid native string length');
 const data=(tag&128)?address.readPointer():address;
 if(length)readable(data,length);
 return {data,length,text:()=>length?data.readUtf8String(length):''};
}
function requestValid(kind,req){
 if(adapter.test_recipient&&(!req||req.to!==adapter.test_recipient))fail('Adapter test send is restricted to filehelper');
 if(!req||typeof req!=='object'||typeof req.to!=='string'||!/^[A-Za-z0-9_@.\-]{1,128}$/.test(req.to))fail('Invalid recipient');
 if(kind==='text'&&(typeof req.text!=='string'||!req.text||req.text.includes('\0')||unescape(encodeURIComponent(req.text)).length>4096))fail('Invalid text');
 if(kind==='image'&&(typeof req.image_path!=='string'||!req.image_path.startsWith('/')||req.image_path.includes('\0')))fail('Invalid image path');
 if(kind==='voice'){
  if(!Array.isArray(req.voice_bytes)||req.voice_bytes.length<10||req.voice_bytes.length>1048576||!req.voice_bytes.every(v=>Number.isInteger(v)&&v>=0&&v<=255)||!Number.isInteger(req.duration_ms)||req.duration_ms<1000||req.duration_ms>60000)fail('Invalid voice request');
  if(![2,35,33,83,73,76,75,95,86,51].every((v,i)=>req.voice_bytes[i]===v))fail('Tencent SILK V3 header required');
 }
}
function construct(kind,req){
 nativeAllowed();requestValid(kind,req);
 const api=bridgeApi(req.bridge),layout=packedLayout(),recipient=Memory.allocUtf8String(req.to);
 let box;
 if(kind==='text')box=api.fn('rea_make_text_input','pointer',['pointer','pointer','pointer','pointer'])(addresses.text_factory,recipient,Memory.allocUtf8String(req.text),layout);
 else if(kind==='image')box=api.fn('rea_make_image_input','pointer',['pointer','pointer','pointer','pointer'])(addresses.image_factory,recipient,Memory.allocUtf8String(req.image_path),layout);
 else{
  const bytes=Memory.alloc(req.voice_bytes.length);bytes.writeByteArray(req.voice_bytes);
  box=api.fn('rea_make_voice_input','pointer',['pointer','pointer','pointer','uint64','uint','pointer'])(addresses.voice_factory,recipient,bytes,req.voice_bytes.length,req.duration_ms,layout);
 }
 if(!box||box.isNull())fail('Native input factory returned null');
 try{
  const input=api.fn('rea_text_input_object','pointer',['pointer'])(box);readable(input,8);
  const type={text:1,image:3,voice:34}[kind],l=adapter.layout;
  readable(input.add(l.type),4);
  if(!input.readPointer().equals(vtables[kind])||input.add(l.type).readU32()!==type||stringValue(input.add(l.recipient),128).text()!==req.to)fail('Native '+kind+' recipient/type/vtable readback mismatch');
  const evidence={constructed:true,submitted:false,native_type:type,input_vtable:'0x'+adapter.vtables[kind].toString(16),recipient_verified:true,type_verified:true};
  if(kind==='text'){
   readable(input.add(l.text_length),8);
   if(stringValue(input.add(l.text),4096).text()!==req.text||input.add(l.text_length).readU64().toNumber()!==unescape(encodeURIComponent(req.text)).length)fail('Native text input readback mismatch');
   evidence.text_verified=true;
  }else if(kind==='image'){
   if(stringValue(input.add(l.image_path),65536).text()!==req.image_path)fail('Native image input readback mismatch');
   evidence.image_path_verified=true;
  }else{
   const payload=stringValue(input.add(l.voice_payload),1048576);readable(input.add(l.voice_duration),4);
   if(payload.length!==req.voice_bytes.length||input.add(l.voice_duration).readU32()!==req.duration_ms)fail('Native voice input readback mismatch');
   const copy=new Uint8Array(payload.data.readByteArray(payload.length));
   if(!copy.every((v,i)=>v===req.voice_bytes[i]))fail('Native voice payload readback mismatch');
   Object.assign(evidence,{payload_bytes:payload.length,duration_ms:req.duration_ms,payload_verified:true});
  }
  return {api,box,evidence};
 }catch(error){api.destroy(box);throw error;}
}
function identity(api,box){
 const count=api.fn('rea_work_identity_count','uint64',['pointer'])(box).toNumber();
 if(count!==1)return {count};
 const string=api.fn('rea_work_identity_string','pointer',['pointer','uint']);
 const word=api.fn('rea_work_identity_word','uint64',['pointer','uint']);
 return {count,strings:[string(box,0).readUtf8String(),string(box,1).readUtf8String()],words:[word(box,0).toString(),word(box,1).toString()]};
}
async function dispatch(kind,req){
 nativeAllowed();requestValid(kind,req);
 if(!req||req.construct_only!==true){
  if(!adapter.permissions['send_'+kind])fail('Adapter sending is disabled for '+kind);
  if(!serviceState().logged_in)fail('WeChat is not logged in');
 }
 const item=construct(kind,req),{api,box,evidence}=item;
 if(req.construct_only===true){api.destroy(box);return evidence;}
 const functions=Memory.alloc(SCHEDULE_FIELDS.length*8),results=Memory.alloc(8);
 SCHEDULE_FIELDS.forEach((key,index)=>functions.add(index*8).writePointer(addresses[key]));
 results.writeU32(adapter.layout.result_begin);results.add(4).writeU32(adapter.layout.result_end);
 // Once submitted, native execution may own the box. Never free unknown work or
 // automatically retry, even if a later callback/readback throws.
 send({kind:'submitted',request_id:req.request_id});
 api.fn('rea_schedule_text','void',['pointer','pointer','pointer'])(functions,box,results);
 const deadline=Date.now()+10000;
 while(Date.now()<deadline){
  if(api.state(box)===2){const result=identity(api,box);api.destroy(box);return {identity:result,enqueued:true,native_lifecycle:true,submitted:true,native_type:evidence.native_type,...(kind==='voice'?{payload_bytes:evidence.payload_bytes,duration_ms:evidence.duration_ms}:{})};}
  await new Promise(resolve=>setTimeout(resolve,50));
 }
 return {enqueued:false,submitted:true,state:'unknown',reason:'Native executor timed out; do not retry automatically'};
}
rpc.exports={
 status(){const metadata=passive();return {...metadata,...(adapter.permissions.native_calls?serviceState():{manager_found:null,ready:null,logged_in:null}),passive:!adapter.permissions.native_calls};},
 probe(req={}){
  const result=passive();
  if(req.construct_only!==true)return result;
  nativeAllowed();
  const samples={bridge:req.bridge,to:req.to||'filehelper',text:req.text||'REA adapter construct-only probe',image_path:req.image_path||'/private/tmp/rea-adapter-construct-only.png',voice_bytes:req.voice_bytes||[2,35,33,83,73,76,75,95,86,51],duration_ms:req.duration_ms||1000};
  const constructions={};
  for(const kind of ['text','image','voice']){const item=construct(kind,samples);item.api.destroy(item.box);constructions[kind]=item.evidence;}
  return {...result,passive:false,construct_only:true,submitted:false,constructions};
 },
 sendText(req){return dispatch('text',req);},
 sendImage(req){return dispatch('image',req);},
 sendVoice(req){return dispatch('voice',req);}
};
