#!/usr/bin/env python3
"""Native image sending with durable request IDs and read-only confirmation."""
import argparse, contextlib, fcntl, hashlib, json, os, re, sqlite3, time
import xml.etree.ElementTree as ET
from pathlib import Path
import wechat
from reader import WeChatReader

def baseline_for(recipient):
    reader=WeChatReader();account=reader._account(None)
    databases=sorted(str(p.relative_to(account['db_storage'])) for p in (Path(account['db_storage'])/'message').glob('message_*.db') if re.fullmatch(r'message_\d+\.db',p.name))
    cursors={}
    for database in databases:
        result=reader.read_messages(recipient=recipient,database=database,limit=1)
        if result['missing_key_databases']:raise RuntimeError('Missing verified database keys')
        cursors[database]=max((m['local_id'] for m in result['messages']),default=0)
    if not cursors:raise RuntimeError('No message databases available')
    return {'account':account['account'],'cursors':cursors}

def verify(recipient,baseline,kind,timeout=25,media_md5=None,voice_metadata=None,native_identity=None):
    try:
        if native_identity['count']!=1 or native_identity['strings'][0]!=recipient:raise ValueError()
        key=int(native_identity['words'][0]);expected_id=key>>32;expected_time=key&0xffffffff
        if expected_id<=0:raise ValueError()
    except (TypeError,KeyError,ValueError,IndexError):
        return {'delivered':False,'state':'unknown','reason':'No valid native request identity; cannot attribute media record'}
    deadline=time.monotonic()+timeout;reader=WeChatReader();last=None
    while time.monotonic()<deadline:
        matches=[]
        for database,cursor in baseline['cursors'].items():
            result=reader.read_messages(recipient=recipient,account=baseline['account'],database=database,after_local_id=cursor,limit=100)
            if result['missing_key_databases'] or result['result_limit_reached'] or result['scan_limit_reached']:raise RuntimeError('Incomplete media verification window')
            matches.extend(m for m in result['messages'] if m['local_type']==kind and m['local_id']==expected_id and m['create_time']==expected_time)
        if len(matches)>1:return {'delivered':False,'state':'unknown','reason':'Multiple new media records; cannot attribute safely'}
        if matches:
            last=matches[0]
            if kind==3:
                try:
                    img=ET.fromstring(last.get('content') or '').find('.//img')
                    digest=img.attrib.get('md5') if img is not None else None
                except ET.ParseError:digest=None
                if not media_md5 or digest!=media_md5:
                    time.sleep(.75)
                    continue
            if kind==34:
                try:
                    voice=ET.fromstring(last.get('content') or '').find('.//voicemsg')
                    matched=voice is not None and voice_metadata is not None and int(voice.get('voicelength','-1'))==voice_metadata['duration_ms'] and int(voice.get('length','-1'))==voice_metadata['payload_bytes'] and voice.get('voiceformat')=='4'
                except (ET.ParseError,ValueError):matched=False
                if not matched:
                    time.sleep(.75)
                    continue
            if int(last.get('server_id') or 0)>0:
                # Keep server/CDN key-bearing XML out of console and evidence.
                safe={k:v for k,v in last.items() if k!='content'}
                return {'delivered':True,'native_record':safe,'verification':'native_request_identity_digest_and_server_id' if kind==3 else 'native_request_identity_duration_length_and_server_id'}
        time.sleep(.75)
    return {'delivered':False,'state':'unknown','reason':'No unique acknowledged media record before deadline','native_record':{k:v for k,v in last.items() if k!='content'} if last else None}

def send_media(recipient,file,request_id,kind=3,duration_ms=None,*,adapter_test=None):
    if not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}',recipient):raise ValueError('Use an exact recipient ID')
    if kind not in (3,34):raise ValueError('Unsupported media kind')
    if adapter_test is not None and (recipient!='filehelper' or adapter_test.get('kind')!=('voice' if kind==34 else 'image') or adapter_test.get('request_id')!=request_id):
        raise ValueError('Version tests must bind filehelper, media kind and this request ID')
    test_fields={'adapter_test':adapter_test,'to':recipient,'request_id':request_id} if adapter_test is not None else {}
    source=Path(file).resolve();data=source.read_bytes()
    if not 0<len(data)<=20*1024*1024:raise ValueError('Image must be 1 byte to 20 MiB')
    if kind==34:
        if not data.startswith(b'\x02#!SILK_V3') or not isinstance(duration_ms,int) or not 1000<=duration_ms<=60000:raise ValueError('Tencent SILK and duration 1000..60000 ms required')
        suffix='.silk'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):suffix='.png'
    elif data.startswith(b'\xff\xd8\xff'):suffix='.jpg'
    else:raise ValueError('Only PNG/JPEG supported by this adapter')
    digest=hashlib.sha256(data).hexdigest()
    wechat.CACHE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (wechat.CACHE/'tool-send.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=wechat.CACHE/'tool-media-outbox.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as con,con:
            path.chmod(0o600)
            con.execute('CREATE TABLE IF NOT EXISTS media_sends (request_id TEXT PRIMARY KEY, recipient TEXT, sha256 TEXT, state TEXT, result TEXT)')
            row=con.execute('SELECT recipient,sha256,state,result FROM media_sends WHERE request_id=?',(request_id,)).fetchone()
            if row:
                if row[:2]!=(recipient,digest) or (kind==34 and json.loads(row[3]).get('duration_ms')!=duration_ms):raise ValueError('Request ID already belongs to another recipient or file')
                return {'request_id':request_id,'state':row[2],'result':json.loads(row[3]),'reused':True,'resent':False}
            status=wechat.transport({'action':'status',**test_fields})
            wechat.capability_preflight(status,'voice' if kind==34 else 'image',adapter_test)
            if not status.get('logged_in'):raise RuntimeError('Test copy not logged in')
            baseline=baseline_for(recipient)
            staging=wechat.CACHE/'media';staging.mkdir(exist_ok=True,mode=0o700)
            target=staging/(digest+suffix)
            with target.open('wb') as out:os.chmod(target,0o600);out.write(data)
            initial={'baseline':baseline,'sha256':digest,'native_type':kind,'media_md5':hashlib.md5(data).hexdigest(),'payload_bytes':len(data),**({'duration_ms':duration_ms} if kind==34 else {})}
            con.execute('INSERT INTO media_sends VALUES(?,?,?,?,?)',(request_id,recipient,digest,'submitting',json.dumps(initial)));con.commit()
            try:
                request={'action':'send-voice' if kind==34 else 'send-image','to':recipient,'request_id':request_id,**test_fields}
                request.update({'voice_path':str(target),'duration_ms':duration_ms} if kind==34 else {'image_path':str(target)})
                result=wechat.transport(request)
                if result.get('enqueued'):result.update(verify(recipient,baseline,kind,native_identity=result.get('identity'),media_md5=hashlib.md5(data).hexdigest(),voice_metadata={'duration_ms':duration_ms,'payload_bytes':len(data)} if kind==34 else None))
            except Exception as e:result={'state':'unknown','error':str(e)}
            result.update(initial)
            state='sent' if result.get('delivered') else 'unknown' if result.get('submitted') or result.get('state')=='unknown' else 'failed'
            con.execute('UPDATE media_sends SET state=?,result=? WHERE request_id=?',(state,json.dumps(result),request_id))
            return {'request_id':request_id,'recipient':recipient,'state':state,'result':result,'resent':False}

def reconcile_media(request_id):
    with (wechat.CACHE/'tool-send.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with contextlib.closing(sqlite3.connect(wechat.CACHE/'tool-media-outbox.sqlite3')) as con,con:
            row=con.execute('SELECT recipient,state,result FROM media_sends WHERE request_id=?',(request_id,)).fetchone()
            if not row:raise ValueError('Unknown media request ID')
            result=json.loads(row[2])
            if row[1]=='sent':return {'request_id':request_id,'state':'sent','result':result,'resent':False}
            if not result.get('baseline'):raise ValueError('Missing baseline; cannot reconcile')
            kind=result['native_type']
            result.update(verify(row[0],result['baseline'],kind,timeout=2,native_identity=result.get('identity'),media_md5=result.get('media_md5'),voice_metadata={'duration_ms':result.get('duration_ms'),'payload_bytes':result.get('payload_bytes')} if kind==34 else None))
            state='sent' if result.get('delivered') else 'unknown'
            con.execute('UPDATE media_sends SET state=?,result=? WHERE request_id=?',(state,json.dumps(result),request_id))
            return {'request_id':request_id,'state':state,'result':result,'resent':False}

def send_image(recipient,file,request_id):
    return send_media(recipient,file,request_id,kind=3)

def send_voice(recipient,file,request_id,duration_ms):
    return send_media(recipient,file,request_id,kind=34,duration_ms=duration_ms)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--to',required=True);p.add_argument('--file',required=True);p.add_argument('--request-id',required=True)
    p.add_argument('--kind',choices=['image','voice'],default='image');p.add_argument('--duration-ms',type=int)
    args=p.parse_args();result=send_voice(args.to,args.file,args.request_id,args.duration_ms) if args.kind=='voice' else send_image(args.to,args.file,args.request_id)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':
    try:main()
    except Exception as e:print(json.dumps({'error':str(e)},ensure_ascii=False));raise SystemExit(1)
