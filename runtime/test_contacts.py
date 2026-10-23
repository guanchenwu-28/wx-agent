import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import contacts


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.source = self.root / 'Documents/xwechat_files/account-a/db_storage/contact/contact.db'
        self.source.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.source)) as con, con:
            con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,local_type INTEGER)')
            con.executemany('INSERT INTO contact VALUES(?,?,?,?)',[
                ('wxid_exact','目标甲','备注甲',1),('wxid_remark','另一个昵称','目标甲',1),
                ('123@chatroom','目标甲讨论组','',2),('wxid_literal','100%_目标',None,1),
                ('wxid_secret','未授权其他人','隐藏备注',1),('wxid_sql',"Robert'); DROP TABLE contact;--",None,1)])
            con.execute('CREATE TABLE chatroom_member(secret TEXT)')
            con.execute("INSERT INTO chatroom_member VALUES('MEMBERS_MUST_NOT_BE_READ')")
        (self.cache/'verified-dbkeys.json').write_text(json.dumps({contacts.CONTACT_DATABASE:{'key':'42'*32}}))
        self.reader = contacts.ContactReader(root=self.root,cache=self.cache)
        self.queries = []
        def copy(root, rels, target, pid):
            for relative in rels:
                path=target/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((root/relative).read_bytes())
            return {'fixture':True}
        def decrypt(source,target,key):
            target.write_bytes(source.read_bytes())
            return {'page_hmac_verified':True}
        self.copy=patch.object(contacts,'copy_snapshots',side_effect=copy)
        self.decrypt=patch.object(contacts,'decrypt_snapshot',side_effect=decrypt)
        original_connect=sqlite3.connect
        def connect(*args,**kwargs):
            con=original_connect(*args,**kwargs);con.set_trace_callback(self.queries.append);return con
        self.connect=patch.object(contacts.sqlite3,'connect',side_effect=connect)
        self.copy.start();self.decrypt.start();self.connect.start()

    def tearDown(self):
        self.copy.stop();self.decrypt.stop();self.connect.stop();self.temp.cleanup()

    def search(self,query,**kwargs):
        return self.reader.search(query,pid=123,**kwargs)

    def test_exact_nickname_or_remark_only(self):
        result=self.search('目标甲')
        self.assertEqual([r['wxid'] for r in result['contacts']],['wxid_exact','wxid_remark'])
        self.assertEqual(result['match_mode'],'exact_name')
        self.assertTrue(result['temporary_plaintext_removed'])
        self.assertNotIn('MEMBERS_MUST_NOT_BE_READ',json.dumps(result))
        self.assertFalse(any('FROM chatroom_member' in q for q in self.queries))
        self.assertEqual(self.search('wxid_exact')['contacts'],[])
        self.assertEqual(list(self.cache.glob('wechat-contacts-*')),[])

    def test_fuzzy_is_explicit_and_group_classification(self):
        self.assertEqual(self.search('讨论')['contacts'],[])
        result=self.search('讨论',fuzzy=True)
        self.assertEqual(result['contacts'][0]['wxid'],'123@chatroom')
        self.assertEqual(result['contacts'][0]['type'],'group')
        self.assertTrue(result['contacts'][0]['is_group'])

    def test_fuzzy_metacharacters_are_literal(self):
        result=self.search('%_',fuzzy=True)
        self.assertEqual([r['wxid'] for r in result['contacts']],['wxid_literal'])
        self.assertEqual(self.search('%',fuzzy=False)['contacts'],[])

    def test_limit_and_has_more(self):
        result=self.search('目标甲',limit=1)
        self.assertEqual(len(result['contacts']),1)
        self.assertTrue(result['has_more'])
        with self.assertRaises(contacts.ReaderError):self.search('目标',limit=11)

    def test_empty_query_rejected_before_snapshot(self):
        with patch.object(contacts,'copy_snapshots') as copy:
            for query in ('','   ',None):
                with self.assertRaises(contacts.ReaderError):self.search(query)
            copy.assert_not_called()

    def test_bound_sql_parameters(self):
        result=self.search("Robert'); DROP TABLE contact;--")
        self.assertEqual(result['contacts'][0]['wxid'],'wxid_sql')
        self.assertEqual(len(self.search('目标甲')['contacts']),2)

    def test_cleanup_on_decryption_failure(self):
        with patch.object(contacts,'decrypt_snapshot',side_effect=contacts.ReaderError('fixture')):
            with self.assertRaises(contacts.ReaderError):self.search('目标甲')
        self.assertEqual(list(self.cache.glob('wechat-contacts-*')),[])

    def test_missing_key_fails_without_snapshot(self):
        (self.cache/'verified-dbkeys.json').write_text('{}')
        with patch.object(contacts,'copy_snapshots') as copy:
            with self.assertRaises(contacts.ReaderError):self.search('目标甲')
            copy.assert_not_called()

    def test_cli_forwards_exact_default(self):
        with patch.object(contacts,'search_contacts',return_value={'contacts':[]}) as search,contextlib.redirect_stdout(io.StringIO()) as output:
            contacts.main(['--query','目标甲','--limit','3','--account','account-a'])
        search.assert_called_once_with('目标甲',fuzzy=False,limit=3,account='account-a')
        self.assertEqual(json.loads(output.getvalue()),{'contacts':[]})

if __name__=='__main__':unittest.main()
