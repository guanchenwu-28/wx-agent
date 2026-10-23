#!/usr/bin/env python3
"""Local WeChat text transport and read-only message access. No UI automation."""
import argparse, fcntl, hashlib, json, os, secrets, sqlite3, subprocess, sys, time, uuid
from pathlib import Path
from contextlib import closing

ROOT=Path(__file__).resolve().parent
HOME=Path.home()
from runtime_config import CACHE, NODE, TEST_APP as TEST, ORIGINAL_APP as ORIGINAL

def processes():
    found={}
    for row in subprocess.check_output(['/bin/ps','-axo','pid=,comm='],text=True).splitlines():
        parts=row.strip().split(None,1)
        if len(parts)!=2:continue
        for name,app in [('original',ORIGINAL),('test',TEST)]:
            if parts[1]==str(app/'Contents/MacOS/WeChat'):found[name]=int(parts[0])
    return found

def transport(request):
    from native_client import request as native_request
    return native_request(request,runtime=ROOT)

def read_exact(to,text):
    from reader import WeChatReader
    result=WeChatReader().read_messages(recipient=to,exact_text=text,limit=100)
    if not result.get('complete'):
        raise RuntimeError('Cannot uniquely verify native message: incomplete conversation read')
    return result

def verify_native_record(to,text,baseline,created_at,timeout=15):
    previous={(m['database'],m['local_id']) for m in baseline['messages']}
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        result=read_exact(to,text)
        if result['account']!=baseline['account']:
            raise RuntimeError('Account changed during send verification')
        rows=[m for m in result['messages'] if (m['database'],m['local_id']) not in previous]
        if len(rows)>1:
            return {'delivered':False,'state':'unknown','reason':'Multiple identical new messages; ambiguous acknowledgment'}
        if rows:
            last=rows[0]
            if int(last.get('server_id') or 0)>0:
                return {'delivered':True,'native_record':last,'verification':'native_database_server_id','database_integrity':'ok'}
        time.sleep(.75)
    return {'delivered':False,'state':'unknown','native_record':last,'reason':'No uniquely acknowledged native record before deadline; do not resend'}

def quit_app(pid):
    # AppKit lifecycle API only; no accessibility, screenshots, UI scripting.
    subprocess.run(['osascript','-l','JavaScript','-e',f'ObjC.import("AppKit"); $.NSRunningApplication.runningApplicationWithProcessIdentifier({int(pid)}).terminate;'],check=True,capture_output=True)
    deadline=time.monotonic()+15
    while pid in processes().values():
        if time.monotonic()>deadline:raise RuntimeError('WeChat did not finish normal shutdown; no force kill performed')
        time.sleep(.2)

def start(restart=False,original=False):
    from service_manager import close_native_connections
    connections=close_native_connections()
    if not all(connection.get('ok') for connection in connections):
        errors=[str(connection.get('error','shutdown unconfirmed')) for connection in connections if not connection.get('ok')]
        raise RuntimeError('Native connection has not finished closing: '+'; '.join(errors))
    running=processes();target='original' if original else 'test';other='test' if original else 'original'
    if other in running:quit_app(running[other])
    if restart and target in running:quit_app(running[target]);running.pop(target)
    if target not in processes():subprocess.run(['open','-n',str(ORIGINAL if original else TEST)],check=True)
    deadline=time.monotonic()+15
    while target not in processes():
        if time.monotonic()>deadline:raise RuntimeError('Application did not start')
        time.sleep(.2)
    return {'running':processes(),'login_may_require_phone_confirmation':not original}

def journal():
    CACHE.mkdir(parents=True,exist_ok=True,mode=0o700);CACHE.chmod(0o700)
    path=CACHE/'tool-outbox.sqlite3'
    con=sqlite3.connect(path);path.chmod(0o600)
    con.execute('CREATE TABLE IF NOT EXISTS sends(request_id TEXT PRIMARY KEY, recipient TEXT NOT NULL, text TEXT NOT NULL, client_msg_id INTEGER NOT NULL, created_at INTEGER NOT NULL, state TEXT NOT NULL, result TEXT)')
    return con

def capability_preflight(status, kind, adapter_test=None):
    if status.get('error'):
        raise RuntimeError(status['error'])
    compatibility=status.get('adapter')
    if compatibility is not None and not (compatibility.get('capabilities',{}).get(kind) or adapter_test):
        raise RuntimeError('Native '+kind+' is not verified for this version; run adapt first')

def send_message(to,text,request_id=None,*,adapter_test=None):
    request_id=request_id or str(uuid.uuid4())
    if not text or '\0' in text or len(text.encode())>4096:raise ValueError('Text must be 1–4096 UTF-8 bytes')
    import re
    if not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}',to):raise ValueError('Use an exact WeChat recipient ID, not a display name')
    if adapter_test is not None and (to!='filehelper' or adapter_test.get('kind')!='text' or adapter_test.get('request_id')!=request_id):
        raise ValueError('Version tests must bind filehelper, text and this request ID')
    test_fields={'adapter_test':adapter_test,'to':to,'request_id':request_id} if adapter_test is not None else {}
    with closing(journal()) as con, con:
        row=con.execute('SELECT recipient,text,state,result FROM sends WHERE request_id=?',(request_id,)).fetchone()
        if row:
            if row[:2]!=(to,text):raise ValueError('Request ID already belongs to different content')
            return {'request_id':request_id,'state':row[2],'reused':True,'result':json.loads(row[3]) if row[3] else None,'resent':False}
        status=transport({'action':'status',**test_fields})
        capability_preflight(status,'text',adapter_test)
        if not status.get('logged_in'):raise RuntimeError('Test copy is not logged in: '+json.dumps(status,ensure_ascii=False))
        baseline=read_exact(to,text)
        now=int(time.time());mid=secrets.randbelow(2147483646)+1
        con.execute('INSERT INTO sends VALUES(?,?,?,?,?,?,?)',(request_id,to,text,mid,now,'submitting',json.dumps({'baseline':baseline})));con.commit()
        try:
            result=transport({'action':'send','to':to,'text':text,'request_id':request_id,'client_msg_id':mid,'create_time':now,**test_fields})
            if result.get('enqueued'):
                result.update(verify_native_record(to,text,baseline,now))
        except Exception as e:result={'error':str(e),'state':'unknown'}
        result['baseline']=baseline
        state='sent' if result.get('delivered') else 'unknown' if result.get('submitted') or result.get('state')=='unknown' else 'failed'
        con.execute('UPDATE sends SET state=?,result=? WHERE request_id=?',(state,json.dumps(result,ensure_ascii=False),request_id))
        return {'request_id':request_id,'state':state,'recipient':to,'text':text,'result':result,'resent':False}

def reconcile(request_id):
    with closing(journal()) as con, con:
        row=con.execute('SELECT recipient,text,created_at,state,result FROM sends WHERE request_id=?',(request_id,)).fetchone()
        if not row:raise ValueError('Unknown request ID')
        result=json.loads(row[4]) if row[4] else {}
        if row[3]=='sent':return {'request_id':request_id,'state':'sent','result':result,'resent':False}
        if not result.get('baseline'):raise ValueError('No persisted baseline; inspect explicitly, never resend')
        result.update(verify_native_record(row[0],row[1],result['baseline'],row[2],timeout=2))
        state='sent' if result.get('delivered') else 'unknown'
        con.execute('UPDATE sends SET state=?,result=? WHERE request_id=?',(state,json.dumps(result,ensure_ascii=False),request_id))
        return {'request_id':request_id,'state':state,'result':result,'resent':False}

def polling_interval(value):
    import math
    try:
        interval=float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a finite number >= 1 second") from None
    if not math.isfinite(interval) or interval < 1:
        raise argparse.ArgumentTypeError("interval must be a finite number >= 1 second")
    return interval

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('start');sub.add_parser('restart');sub.add_parser('restore-original')
    sub.add_parser('transport-status');sub.add_parser('transport-stop')
    from adapter_workflow import register_parser
    register_parser(sub)
    s=sub.add_parser('send');s.add_argument('--to',required=True);s.add_argument('--text',required=True);s.add_argument('--request-id')
    r=sub.add_parser('read');r.add_argument('--to',default='filehelper');r.add_argument('--limit',type=int,default=20);r.add_argument('--after',type=int);r.add_argument('--exact');r.add_argument('--account');r.add_argument('--database')
    for name in ('poll','watch'):
        command=sub.add_parser(name)
        command.add_argument('--to',required=True)
        command.add_argument('--limit',type=int,default=100)
        command.add_argument('--account')
        command.add_argument('--from-start',action='store_true')
        if name=='watch':
            command.add_argument('--interval',type=polling_interval,default=2.0)
            command.add_argument('--once',action='store_true')
    c=sub.add_parser('reconcile');c.add_argument('--request-id',required=True)
    c=sub.add_parser('contacts');c.add_argument('--query',required=True);c.add_argument('--fuzzy',action='store_true')
    c=sub.add_parser('reconcile-media');c.add_argument('--request-id',required=True)
    for name in ('send-image','send-voice'):
        c=sub.add_parser(name);c.add_argument('--to',required=True);c.add_argument('--file',required=True);c.add_argument('--request-id',required=True)
        if name=='send-voice':c.add_argument('--duration-ms',type=int,required=True)
    args=p.parse_args(argv)
    if args.command=='status':
        from adapter import scan,summary
        running=processes()
        native=transport({'action':'status'}) if 'test' in running else None
        if native is not None:
            compatibility=native.get('adapter') or {'status':'error','error':native.get('error','Native adaptation unavailable')}
        else:
            try:compatibility=summary(scan())
            except Exception as error:compatibility={'status':'error','error':str(error)}
        result={'running':running,'read_available':(CACHE/'verified-dbkeys.json').exists(),'compatibility':compatibility,'native':native}
    elif args.command in ('transport-status','transport-stop'):
        import native_client
        result=(native_client.status if args.command=='transport-status' else native_client.stop)(runtime=ROOT)
    elif args.command=='adapt':
        from adapter_workflow import run
        result=run(args)
    elif args.command in ['start','restart','restore-original']:
        result=start(restart=args.command=='restart',original=args.command=='restore-original')
    elif args.command=='read':
        from reader import WeChatReader
        result=WeChatReader().read_messages(recipient=args.to,limit=args.limit,after_local_id=args.after,exact_text=args.exact,account=args.account,database=args.database)
    elif args.command=='contacts':
        from contacts import search_contacts
        result=search_contacts(query=args.query,fuzzy=args.fuzzy)
    elif args.command in ('send-image','send-voice','reconcile-media'):
        from media import send_image,send_voice,reconcile_media
        if args.command=='reconcile-media':result=reconcile_media(args.request_id)
        elif args.command=='send-image':result=send_image(args.to,args.file,args.request_id)
        else:result=send_voice(args.to,args.file,args.request_id,args.duration_ms)
    elif args.command=='reconcile':
        result=reconcile(args.request_id)
    elif args.command in ('poll','watch'):
        from poller import poll_messages
        def poll_once():
            return poll_messages(recipient=args.to,account=args.account,limit=args.limit,from_start=args.from_start)
        if args.command=='poll':
            result=poll_once()
        else:
            try:
                while True:
                    print(json.dumps(poll_once(),ensure_ascii=False),flush=True)
                    if args.once:
                        return 0
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
    else:
        CACHE.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (CACHE/'tool-send.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=send_message(args.to,args.text,args.request_id)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except Exception as e:print(json.dumps({'error':str(e)},ensure_ascii=False));sys.exit(1)
