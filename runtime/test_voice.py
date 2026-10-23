import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import media

class VoiceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.file=self.root/'test.silk';self.file.write_bytes(b'\x02#!SILK_V3'+b'test-payload')
  self.cache=patch.object(media.wechat,'CACHE',self.root/'cache');self.cache.start()
 def tearDown(self):self.cache.stop();self.tmp.cleanup()
 def test_rejects_audio_file_attachment_as_voice(self):
  self.file.write_bytes(b'FORM-not-a-silk-file')
  with patch.object(media.wechat,'transport') as t:
   with self.assertRaises(ValueError):media.send_voice('filehelper',self.file,'x',4240)
   t.assert_not_called()
 def test_invalid_duration_rejected_before_send(self):
  for duration in [None,0,999,60001]:
   with self.assertRaises(ValueError):media.send_voice('filehelper',self.file,'x',duration)
 def test_repeated_voice_request_is_not_sent_twice(self):
  with patch.object(media.wechat,'transport',side_effect=[{'logged_in':True},{'enqueued':True,'submitted':True}]) as transport,patch.object(media,'baseline_for',return_value={'account':'a','cursors':{'message/message_0.db':1}}),patch.object(media,'verify',return_value={'delivered':True}):
   first=media.send_voice('filehelper',self.file,'v1',4240);again=media.send_voice('filehelper',self.file,'v1',4240)
   self.assertEqual(first['state'],'sent');self.assertTrue(again['reused']);self.assertEqual(transport.call_count,2)
   with self.assertRaises(ValueError):media.send_voice('filehelper',self.file,'v1',5000)
 def check_record(self,length=8553,duration=4240):
  row={'database':'message/message_0.db','local_id':10,'create_time':123,'local_type':34,'server_id':999,'content':f'<msg><voicemsg voiceformat="4" voicelength="{duration}" length="{length}" /></msg>'}
  identity={'count':1,'strings':['filehelper','uuid'],'words':[str((10<<32)|123),'123000']}
  with patch.object(media,'WeChatReader') as reader,patch.object(media.time,'sleep'),patch.object(media.time,'monotonic',side_effect=[0,0,2]):
   reader.return_value.read_messages.return_value={'messages':[row],'missing_key_databases':[],'result_limit_reached':False,'scan_limit_reached':False}
   return media.verify('filehelper',{'account':'a','cursors':{'message/message_0.db':9}},34,timeout=1,native_identity=identity,voice_metadata={'duration_ms':4240,'payload_bytes':8553})
 def test_correct_voice_metadata_and_identity(self):self.assertTrue(self.check_record()['delivered'])
 def test_wrong_duration_not_confirmed(self):self.assertFalse(self.check_record(duration=5000)['delivered'])
 def test_wrong_byte_length_not_confirmed(self):self.assertFalse(self.check_record(length=100)['delivered'])

if __name__=='__main__':unittest.main()
