import json
import unittest
from unittest.mock import patch
import wechat
import test_cli

def record(local_id=5,server_id=123):
    return {'database':'message/message_0.db','local_id':local_id,'server_id':server_id,'create_time':10,'content':'fixture','status':2}

class NativeVerificationTests(unittest.TestCase):
    def test_server_clock_skew_does_not_hide_new_record(self):
        with patch.object(wechat,'read_exact',return_value={'account':'a','messages':[record()]}):
            result=wechat.verify_native_record('filehelper','fixture',{'account':'a','messages':[]},created_at=9999)
        self.assertTrue(result['delivered'])
    def test_multiple_new_identical_rows_are_not_assumed_delivered(self):
        with patch.object(wechat,'read_exact',return_value={'account':'a','messages':[record(),record(6)]}):
            result=wechat.verify_native_record('filehelper','fixture',{'account':'a','messages':[]},created_at=1)
        self.assertFalse(result['delivered']);self.assertEqual(result['state'],'unknown')
    def test_old_identical_row_does_not_confirm_new_send(self):
        baseline={'account':'a','messages':[record()]}
        with patch.object(wechat,'read_exact',return_value=baseline),patch.object(wechat.time,'monotonic',side_effect=[0,0,20]),patch.object(wechat.time,'sleep'):
            result=wechat.verify_native_record('filehelper','fixture',baseline,created_at=1)
        self.assertFalse(result['delivered'])
    def test_account_switch_rejected(self):
        with patch.object(wechat,'read_exact',return_value={'account':'other','messages':[record()]}):
            with self.assertRaises(RuntimeError):wechat.verify_native_record('filehelper','fixture',{'account':'a','messages':[]},1)

class ReconcileTests(unittest.TestCase):
    setUp=test_cli.OutboxTests.setUp
    tearDown=test_cli.OutboxTests.tearDown
    def test_unknown_can_be_verified_without_network_send(self):
        with patch.object(wechat,'transport',side_effect=[{'logged_in':True},TimeoutError()]):
            wechat.send_message('filehelper','fixture','reconcile-fixture')
        with patch.object(wechat,'transport') as transport,patch.object(wechat,'verify_native_record',return_value={'delivered':True,'native_record':record()}):
            result=wechat.reconcile('reconcile-fixture')
        transport.assert_not_called();self.assertEqual(result['state'],'sent');self.assertFalse(result['resent'])

if __name__=='__main__':unittest.main()
