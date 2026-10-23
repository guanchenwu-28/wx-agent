import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import wechat

class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cache=patch.object(wechat,'CACHE',Path(self.temp.name));self.cache.start()
        self.bridge=patch.object(wechat,'read_exact',return_value={'account':'fixture','messages':[]});self.bridge.start()
    def tearDown(self):self.bridge.stop();self.cache.stop();self.temp.cleanup()
    def test_duplicate_id_does_not_send_again(self):
        with patch.object(wechat,'transport',side_effect=[{'logged_in':True},{'delivered':True,'process_alive':True}]) as call:
            first=wechat.send_message('filehelper','fixture','fixed-id')
            second=wechat.send_message('filehelper','fixture','fixed-id')
        self.assertEqual(first['state'],'sent');self.assertTrue(second['reused']);self.assertFalse(second['resent']);self.assertEqual(call.call_count,2)
    def test_unknown_outcome_is_not_retried(self):
        with patch.object(wechat,'transport',side_effect=[{'logged_in':True},TimeoutError('fixture timeout')]) as call:
            first=wechat.send_message('filehelper','fixture','fixed-id')
            second=wechat.send_message('filehelper','fixture','fixed-id')
        self.assertEqual(first['state'],'unknown');self.assertEqual(second['state'],'unknown');self.assertEqual(call.call_count,2)
    def test_request_id_cannot_change_recipient_or_content(self):
        with patch.object(wechat,'transport',side_effect=[{'logged_in':True},{'delivered':True}]) as call:
            wechat.send_message('filehelper','fixture','fixed-id')
            with self.assertRaises(ValueError):wechat.send_message('filehelper','different','fixed-id')
        self.assertEqual(call.call_count,2)
    def test_not_logged_in_does_not_submit(self):
        with patch.object(wechat,'transport',return_value={'logged_in':False}) as call:
            with self.assertRaises(RuntimeError):wechat.send_message('filehelper','fixture','fixed-id')
        self.assertEqual(call.call_count,1)

if __name__=='__main__':unittest.main()
