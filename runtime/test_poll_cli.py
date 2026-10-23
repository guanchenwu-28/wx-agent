import contextlib
import io
import json
import unittest
from unittest.mock import patch

import wechat


class PollCliTests(unittest.TestCase):
    def setUp(self):
        self.result = {'recipient':'filehelper','account':'fixture','messages':[],
                       'cursor':{},'has_more':False,'initialized':True,'unsupported':[]}

    def test_poll_single_json_forwards_options(self):
        output=io.StringIO()
        with patch('poller.poll_messages',return_value=self.result) as poll, contextlib.redirect_stdout(output):
            wechat.main(['poll','--to','filehelper','--limit','10','--account','fixture','--from-start'])
        self.assertEqual(json.loads(output.getvalue()),self.result)
        poll.assert_called_once_with(recipient='filehelper',account='fixture',limit=10,from_start=True)

    def test_watch_once_one_json_line_no_sleep(self):
        output=io.StringIO()
        with patch('poller.poll_messages',return_value=self.result) as poll, patch.object(wechat.time,'sleep') as sleep, contextlib.redirect_stdout(output):
            self.assertEqual(wechat.main(['watch','--to','filehelper','--once','--interval','1']),0)
        self.assertEqual(len(output.getvalue().splitlines()),1)
        self.assertEqual(json.loads(output.getvalue()),self.result)
        self.assertEqual(poll.call_count,1)
        sleep.assert_not_called()

    def test_watch_ctrl_c_from_sleep_is_clean(self):
        output=io.StringIO()
        with patch('poller.poll_messages',return_value=self.result), patch.object(wechat.time,'sleep',side_effect=KeyboardInterrupt) as sleep, contextlib.redirect_stdout(output):
            self.assertEqual(wechat.main(['watch','--to','filehelper','--interval','1.5']),0)
        self.assertEqual(len(output.getvalue().splitlines()),1)
        sleep.assert_called_once_with(1.5)

    def test_watch_ctrl_c_during_poll_is_clean(self):
        output=io.StringIO()
        with patch('poller.poll_messages',side_effect=KeyboardInterrupt), contextlib.redirect_stdout(output):
            self.assertEqual(wechat.main(['watch','--to','filehelper']),0)
        self.assertEqual(output.getvalue(),'')

    def test_watch_multiple_cycles_are_jsonl(self):
        output=io.StringIO()
        with patch('poller.poll_messages',return_value=self.result) as poll, patch.object(wechat.time,'sleep',side_effect=[None,KeyboardInterrupt]), contextlib.redirect_stdout(output):
            wechat.main(['watch','--to','filehelper'])
        self.assertEqual(poll.call_count,2)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],[self.result,self.result])

    def test_interval_rejects_small_nan_infinite(self):
        for value in ('0','-1','0.99','nan','inf','-inf','abc'):
            with self.subTest(value=value), patch('poller.poll_messages') as poll, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    wechat.main(['watch','--to','filehelper','--interval='+value])
                self.assertEqual(raised.exception.code,2)
                poll.assert_not_called()

    def test_poll_and_watch_require_explicit_recipient(self):
        for command in ('poll','watch'):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): wechat.main([command])

    def test_read_database_option(self):
        output=io.StringIO()
        with patch('reader.WeChatReader') as factory, contextlib.redirect_stdout(output):
            factory.return_value.read_messages.return_value={'messages':[]}
            wechat.main(['read','--to','filehelper','--after','4','--database','message/message_0.db'])
        self.assertEqual(factory.return_value.read_messages.call_args.kwargs['database'],'message/message_0.db')
        self.assertEqual(factory.return_value.read_messages.call_args.kwargs['after_local_id'],4)

if __name__=='__main__': unittest.main()
