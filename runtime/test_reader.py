import contextlib, ctypes, hashlib, hmac, json, signal, sqlite3, struct, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import reader
KEY=b'K'*32
SALT=b'S'*16

def encrypted_page(n=1, fill=b'A'):
    clear=bytearray(fill*4096); start=16 if n==1 else 0
    if n==1:
        clear[:16]=b'SQLite format 3\0'; clear[16:18]=b'\x10\x00'; clear[20]=80
    clear[-80:]=bytes(80); iv=b'I'*16
    enc=Cipher(algorithms.AES(KEY),modes.CBC(iv)).encryptor()
    body=enc.update(bytes(clear[start:-80]))+enc.finalize()+iv
    hk=hashlib.pbkdf2_hmac('sha512',KEY,bytes(x^0x3a for x in SALT),2,32)
    mac=hmac.new(hk,body+struct.pack('<I',n),hashlib.sha512).digest()
    return (SALT if n==1 else b'')+body+mac,bytes(clear)

def wal(frames,endian='<'):
    head=struct.pack('>IIII',0x377f0682 if endian=='<' else 0x377f0683,3007000,4096,0)+b'12345678'
    state=reader.checksum(head,endian); data=head+struct.pack('>II',*state)
    for n,size,page in frames:
        prefix=struct.pack('>II',n,size); state=reader.checksum(prefix+page,endian,state)
        data+=prefix+b'12345678'+struct.pack('>II',*state)+page
    return data

class AuthTests(unittest.TestCase):
    def test_commit_and_uncommitted_tail_both_endians(self):
        base,_=encrypted_page(); committed,expected=encrypted_page(fill=b'B'); pending,_=encrypted_page(fill=b'C')
        for endian in ('<','>'):
            with tempfile.TemporaryDirectory() as td:
                source,target=Path(td)/'source',Path(td)/'plain'; source.write_bytes(base)
                Path(str(source)+'-wal').write_bytes(wal([(1,1,committed),(1,0,pending)],endian))
                stats=reader.decrypt_snapshot(source,target,KEY)
                self.assertEqual(target.read_bytes(),expected); self.assertEqual(stats['wal_tail'],'uncommitted_frames')
                self.assertEqual(stats['committed_wal_frames'],1)
    def test_tail_recovery(self):
        page,_=encrypted_page(); good=wal([(1,1,page)])
        frames,size,stats=reader.committed_wal_frames(good+b'partial')
        self.assertEqual(len(frames),1); self.assertEqual(stats['wal_tail'],'incomplete_frame')
        two=bytearray(wal([(1,1,page),(1,1,page)])); two[-1]^=1
        self.assertEqual(reader.committed_wal_frames(bytes(two))[2]['wal_tail'],'invalid_checksum')
        two=bytearray(wal([(1,1,page),(1,1,page)])); two[32+4120+8]^=1
        self.assertEqual(reader.committed_wal_frames(bytes(two))[2]['wal_tail'],'old_generation')
    def test_bad_header(self):
        page,_=encrypted_page(); data=bytearray(wal([(1,1,page)])); data[24]^=1
        for bad in (bytes(data),b'short'):
            with self.assertRaises(reader.ReaderError): reader.committed_wal_frames(bad)
    def test_all_pages_hmac_and_error_cleanup(self):
        first,_=encrypted_page(); second,_=encrypted_page(2); corrupt=bytearray(second); corrupt[0]^=1
        with tempfile.TemporaryDirectory() as td:
            source,target=Path(td)/'source',Path(td)/'plain'; source.write_bytes(first+corrupt)
            with self.assertRaisesRegex(reader.ReaderError,'page 2'): reader.decrypt_snapshot(source,target,KEY)
            self.assertFalse(target.exists())
    def test_wal_checksum_cannot_bypass_hmac(self):
        first,_=encrypted_page(); corrupt=bytearray(first); corrupt[22]^=1
        with tempfile.TemporaryDirectory() as td:
            source,target=Path(td)/'source',Path(td)/'plain'; source.write_bytes(first)
            Path(str(source)+'-wal').write_bytes(wal([(1,1,bytes(corrupt))]))
            with self.assertRaisesRegex(reader.ReaderError,'authentication'): reader.decrypt_snapshot(source,target,KEY)
            self.assertFalse(target.exists())
    def test_truncation(self):
        first,expected=encrypted_page(); second,_=encrypted_page(2)
        with tempfile.TemporaryDirectory() as td:
            source,target=Path(td)/'source',Path(td)/'plain'; source.write_bytes(first+second)
            Path(str(source)+'-wal').write_bytes(wal([(1,1,first)]))
            reader.decrypt_snapshot(source,target,KEY); self.assertEqual(target.read_bytes(),expected)
    def test_wrong_key_partial_page(self):
        first,_=encrypted_page()
        with tempfile.TemporaryDirectory() as td:
            source,target=Path(td)/'source',Path(td)/'plain'; source.write_bytes(first)
            with self.assertRaises(reader.ReaderError): reader.decrypt_snapshot(source,target,b'X'*32)
            source.write_bytes(first+b'x')
            with self.assertRaises(reader.ReaderError): reader.decrypt_snapshot(source,target,KEY)
            self.assertFalse(target.exists())

class QueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.cache=self.root/'cache'; self.cache.mkdir()
        self.db=self.root/'Documents/xwechat_files/account-a/db_storage/message/message_0.db'; self.db.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'CREATE TABLE "{reader.TABLE}" (local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, create_time INTEGER, message_content, WCDB_CT_message_content INTEGER)')
            con.executemany(f'INSERT INTO "{reader.TABLE}" VALUES(?,?,1,?,?,0)',[(i,100+i,1000+i,'target' if i==2 else f'text-{i}') for i in range(1,5)])
            con.execute('CREATE TABLE unrelated_chat (secret TEXT)'); con.execute("INSERT INTO unrelated_chat VALUES('MUST_NOT_READ')")
        (self.cache/'verified-dbkeys.json').write_text(json.dumps({'message/message_0.db':{'key':KEY.hex()}}))
        self.r=reader.WeChatReader(self.root,self.cache)
        def copy(root,rels,dest,pid):
            for rel in rels:
                (dest/rel).parent.mkdir(parents=True,exist_ok=True); (dest/rel).write_bytes((root/rel).read_bytes())
            return {'fixture':True}
        def decrypt(src,dest,key): dest.write_bytes(src.read_bytes()); return {'fixture':True}
        self.p1=patch.object(reader,'copy_snapshots',side_effect=copy); self.p2=patch.object(reader,'decrypt_snapshot',side_effect=decrypt)
        self.p1.start(); self.p2.start()
    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.temp.cleanup()
    def test_bound_cursor_exact_and_cleanup(self):
        result=self.r.read_messages(limit=2,pid=123); self.assertEqual([r['local_id'] for r in result['messages']],[4,3])
        result=self.r.read_messages(limit=2,after_local_id=0,pid=123); self.assertEqual([r['local_id'] for r in result['messages']],[2,1])
        result=self.r.read_messages(limit=5,after_local_id=2,pid=123); self.assertEqual([r['local_id'] for r in result['messages']],[4,3])
        result=self.r.read_messages(exact_text='target',pid=123); self.assertEqual([r['local_id'] for r in result['messages']],[2])
        self.assertTrue(result['temporary_plaintext_removed']); self.assertEqual(list(self.cache.glob('wechat-reader-*')),[])
        self.assertNotIn('MUST_NOT_READ',json.dumps(result))
    def test_actual_local_status_column_and_absent_fallback(self):
        result=self.r.read_messages(exact_text='target',pid=123)
        self.assertIsNone(result['messages'][0]['status'])
        self.assertFalse(result['validation'][0]['status_column_present'])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN status INTEGER')
            con.execute(f'UPDATE "{reader.TABLE}" SET status=7 WHERE local_id=2')
        result=self.r.read_messages(exact_text='target',pid=123)
        self.assertEqual(result['messages'][0]['status'],7)
        self.assertTrue(result['validation'][0]['status_column_present'])
        self.assertEqual(result['messages'][0]['content_status'],'text')
    def test_sender_mapping_uses_real_sender_id_not_status(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN real_sender_id INTEGER')
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN status INTEGER')
            con.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)')
            con.executemany('INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(?,?,1)',
                            [(7,'wxid_incoming'),(8,'wxid_self'),(99,'UNRELATED_NAME_MUST_NOT_LEAK')])
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=7,status=2 WHERE local_id=1')
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=8,status=2 WHERE local_id=2')
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=777,status=2 WHERE local_id=3')
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=NULL,status=2 WHERE local_id=4')
        result=self.r.read_messages(limit=4,pid=123)
        by_id={row['local_id']:row for row in result['messages']}
        self.assertEqual(by_id[1]['real_sender_id'],7)
        self.assertEqual(by_id[1]['sender_username'],'wxid_incoming')
        self.assertEqual(by_id[2]['sender_username'],'wxid_self')
        self.assertEqual(by_id[1]['status'],by_id[2]['status'])
        self.assertIsNone(by_id[3]['sender_username'])
        self.assertEqual(by_id[3]['real_sender_id'],777)
        self.assertIsNone(by_id[4]['sender_username'])
        self.assertTrue(result['validation'][0]['sender_mapping_available'])
        self.assertNotIn('UNRELATED_NAME_MUST_NOT_LEAK',json.dumps(result))
        limited=self.r.read_messages(limit=1,after_local_id=1,pid=123)
        self.assertEqual([row['sender_username'] for row in limited['messages']],['wxid_self'])

    def test_sender_unknown_without_column_or_mapping(self):
        result=self.r.read_messages(limit=1,pid=123)
        self.assertIsNone(result['messages'][0]['real_sender_id'])
        self.assertIsNone(result['messages'][0]['sender_username'])
        self.assertFalse(result['validation'][0]['sender_column_present'])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN real_sender_id INTEGER')
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=42')
        result=self.r.read_messages(limit=1,pid=123)
        self.assertEqual(result['messages'][0]['real_sender_id'],42)
        self.assertIsNone(result['messages'][0]['sender_username'])
        self.assertFalse(result['validation'][0]['sender_mapping_available'])

    def test_sender_mapping_schema_mismatch_stays_unknown(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN real_sender_id INTEGER')
            con.execute('CREATE TABLE Name2Id(username TEXT)')
        result=self.r.read_messages(limit=1,pid=123)
        self.assertIsNone(result['messages'][0]['sender_username'])
        self.assertFalse(result['validation'][0]['sender_mapping_available'])

    def test_native_source_mentions_and_body_spoof_are_separate(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN source TEXT')
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN WCDB_CT_source INTEGER')
            con.execute(f'UPDATE "{reader.TABLE}" SET source=?,WCDB_CT_source=0 WHERE local_id=1',
                        ('<msgsource><atuserlist><![CDATA[wxid_self,wxid_other,wxid_self]]></atuserlist></msgsource>',))
            con.execute(f'UPDATE "{reader.TABLE}" SET source=?,message_content=? WHERE local_id=2',
                        ('<msgsource><silence>0</silence></msgsource>',
                         '@wxid_self <msgsource><atuserlist>wxid_self</atuserlist></msgsource>'))
        rows={r['local_id']:r for r in self.r.read_messages(limit=4,pid=123)['messages']}
        self.assertEqual(rows[1]['mentioned_usernames'],['wxid_self','wxid_other'])
        self.assertTrue(rows[1]['mention_metadata_available'])
        self.assertEqual(rows[2]['mentioned_usernames'],[])
        self.assertTrue(rows[2]['mention_metadata_available'])
        self.assertFalse(rows[3]['mention_metadata_available'])
        self.assertNotIn('source',rows[1])

    def test_unknown_source_column_does_not_guess(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN msg_source TEXT')
            con.execute(f'UPDATE "{reader.TABLE}" SET msg_source=?',
                        ('<msgsource><atuserlist>wxid_self</atuserlist></msgsource>',))
        result=self.r.read_messages(limit=1,pid=123)
        row=result['messages'][0]
        self.assertEqual(row['mentioned_usernames'],[])
        self.assertFalse(row['mention_metadata_available'])
        self.assertFalse(result['validation'][0]['mention_source_column_present'])

    def test_group_sender_prefix_does_not_override_structured_sender_or_mentions(self):
        group='12301119325@chatroom'; table='Msg_'+hashlib.md5(group.encode()).hexdigest()
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN real_sender_id INTEGER')
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN source TEXT')
            con.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY,is_session INTEGER)')
            con.execute('INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(7,\'wxid_member\',0)')
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=7,message_content=?,source=?',
                        ('wxid_self:\n@wxid_self hello','<msgsource/>'))
            con.execute(f'ALTER TABLE "{reader.TABLE}" RENAME TO "{table}"')
        row=self.r.read_messages(recipient=group,limit=1,pid=123)['messages'][0]
        self.assertEqual(row['sender_username'],'wxid_member')
        self.assertEqual(row['mentioned_usernames'],[])
        self.assertEqual(row['content'],'wxid_self:\n@wxid_self hello')

    def test_compressed_source_metadata_uses_its_own_wcdb_column(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest('No Python Zstd compressor')
        compressed=zstd.compress(b'<msgsource><atuserlist>wxid_self</atuserlist></msgsource>')
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN source TEXT')
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN WCDB_CT_source INTEGER')
            con.execute(f'UPDATE "{reader.TABLE}" SET source=?,WCDB_CT_source=4 WHERE local_id=1',(compressed,))
            con.execute(f'UPDATE "{reader.TABLE}" SET source=?,WCDB_CT_source=2 WHERE local_id=2',(compressed,))
            con.execute(f'UPDATE "{reader.TABLE}" SET source=?,WCDB_CT_source=\'invalid\' WHERE local_id=3',(compressed,))
        rows={r['local_id']:r for r in self.r.read_messages(limit=4,pid=123)['messages']}
        self.assertEqual(rows[1]['mentioned_usernames'],['wxid_self'])
        self.assertTrue(rows[1]['mention_metadata_available'])
        self.assertEqual(rows[1]['content'],'text-1')
        self.assertFalse(rows[2]['mention_metadata_available'])
        self.assertEqual(rows[2]['mention_metadata_status'],'unsupported_zstd_dictionary')
        self.assertFalse(rows[3]['mention_metadata_available'])

    def verified_receipt(self):
        return {'state':'sent','recipient':'filehelper','request_id':'known-outgoing',
                'result':{'delivered':True,'verification':'native_database_server_id',
                          'native_record':{'database':'message/message_0.db','local_id':2,'server_id':102}}}

    def test_self_identity_requires_exact_verified_outgoing_record(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute(f'ALTER TABLE "{reader.TABLE}" ADD COLUMN real_sender_id INTEGER')
            con.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY,is_session INTEGER)')
            con.execute('INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(7,\'wxid_real_self\',0)')
            con.execute(f'UPDATE "{reader.TABLE}" SET real_sender_id=7 WHERE local_id=2')
        result=self.r.resolve_self_from_verified_send(self.verified_receipt(),pid=123)
        self.assertEqual(result['self_username'],'wxid_real_self')
        self.assertNotEqual(result['self_username'],result['account'])
        self.assertNotIn('content',json.dumps(result))
        receipt=self.verified_receipt(); receipt['result']['native_record']['server_id']=103
        with self.assertRaisesRegex(reader.ReaderError,'no longer matches'):
            self.r.resolve_self_from_verified_send(receipt,pid=123)

    def test_self_identity_missing_mapping_or_unverified_receipt_stays_unknown(self):
        with self.assertRaisesRegex(reader.ReaderError,'identity is unavailable'):
            self.r.resolve_self_from_verified_send(self.verified_receipt(),pid=123)
        for receipt in ({}, {'state':'sent','result':{'delivered':False}},
                        {'state':'unknown','result':{'delivered':True}}):
            with self.assertRaises(reader.ReaderError):
                self.r.resolve_self_from_verified_send(receipt,pid=123)
    def test_error_cleanup(self):
        with patch.object(reader,'decrypt_snapshot',side_effect=reader.ReaderError('test')):
            with self.assertRaises(reader.ReaderError): self.r.read_messages(pid=123)
        self.assertEqual(list(self.cache.glob('wechat-reader-*')),[])
    def test_explicit_recipient_does_not_return_other_conversation(self):
        result=self.r.read_messages(recipient='wxid_explicit_test',pid=123)
        self.assertEqual(result['messages'],[])
        with self.assertRaises(reader.ReaderError): self.r.read_messages(recipient='bad identifier',pid=123)
    def test_account_scope(self):
        with self.assertRaises(reader.ReaderError): self.r.read_messages(account='../account-a',pid=123)
        self.assertEqual(self.r.accounts()[0]['account'],'account-a')
    def test_resume_finally(self):
        executable=next(iter(reader.MAIN_EXECUTABLES))
        with patch.object(reader.subprocess,'check_output',side_effect=[executable,'S','T']),patch.object(reader.os,'kill') as kill:
            with self.assertRaises(RuntimeError):
                with reader.stopped_main(123): raise RuntimeError('copy failed')
            self.assertEqual(kill.call_args_list[-1].args,(123,signal.SIGCONT))
    def test_wcdb_zstd(self):
        decoder=reader.ZstdDecoder(); self.assertEqual(decoder.decode(b'abc',2)[1],'unsupported_zstd_dictionary')
        self.assertEqual(decoder.decode(b'\xff',0)[1],'unsupported_non_utf8_content')
        if decoder.lib is None: self.skipTest('No libzstd')
        lib=decoder.lib
        lib.ZSTD_compressBound.argtypes=[ctypes.c_size_t]; lib.ZSTD_compressBound.restype=ctypes.c_size_t
        lib.ZSTD_compress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]; lib.ZSTD_compress.restype=ctypes.c_size_t
        raw='REA-压缩验证'.encode(); size=lib.ZSTD_compressBound(len(raw)); output=ctypes.create_string_buffer(size)
        n=lib.ZSTD_compress(output,size,raw,len(raw),1)
        self.assertEqual(decoder.decode(output.raw[:n],4),('REA-压缩验证','zstd_text'))
        if decoder.python_zstd is not None:
            decoder.lib=None
            self.assertEqual(decoder.decode(output.raw[:n],4),('REA-压缩验证','zstd_text'))
        with contextlib.closing(sqlite3.connect(self.db)) as con, con: con.execute(f'UPDATE "{reader.TABLE}" SET message_content=?,WCDB_CT_message_content=4 WHERE local_id=2',(output.raw[:n],))
        result=self.r.read_messages(exact_text='REA-压缩验证',pid=123); self.assertEqual(result['messages'][0]['local_id'],2)

class MentionMetadataTests(unittest.TestCase):
    def test_valid_empty_and_mentions(self):
        empty=reader.mention_metadata('<msgsource/>')
        self.assertTrue(empty['mention_metadata_available'])
        self.assertEqual(empty['mentioned_usernames'],[])
        result=reader.mention_metadata('<msgsource><atuserlist> wxid_one,wxid_two,wxid_one </atuserlist></msgsource>')
        self.assertEqual(result['mentioned_usernames'],['wxid_one','wxid_two'])

    def test_invalid_xml_entities_and_nested_or_invalid_names_stay_unknown(self):
        values=['<msgsource>', '<other><atuserlist>wxid_self</atuserlist></other>',
                '<!DOCTYPE msgsource [<!ENTITY name SYSTEM "file:///etc/passwd">]><msgsource><atuserlist>&name;</atuserlist></msgsource>',
                '<msgsource><atuserlist><user>wxid_self</user></atuserlist></msgsource>',
                '<msgsource><atuserlist>wxid_self,not a username</atuserlist></msgsource>',
                '<msgsource>'+('x'*reader.MAX_MENTION_SOURCE)+'</msgsource>']
        for value in values:
            with self.subTest(value=value[:40]):
                result=reader.mention_metadata(value)
                self.assertFalse(result['mention_metadata_available'])
                self.assertEqual(result['mentioned_usernames'],[])

    def test_nested_unrelated_atuserlist_is_not_treated_as_native_metadata(self):
        result=reader.mention_metadata('<msgsource><other><atuserlist>wxid_self</atuserlist></other></msgsource>')
        self.assertTrue(result['mention_metadata_available'])
        self.assertEqual(result['mentioned_usernames'],[])

if __name__=='__main__': unittest.main()
