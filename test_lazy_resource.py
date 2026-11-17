"""Resource deferral and real app import with all external services blocked."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock

from lazy_resource import LazyResource


class LazyResourceTests(unittest.TestCase):
    def test_factory_runs_only_on_first_attribute_access(self):
        handle=Mock();handle.title='fixture workbook'
        factory=Mock(return_value=handle)
        resource=LazyResource(factory)
        factory.assert_not_called()
        self.assertEqual(resource.title,'fixture workbook')
        self.assertIs(resource.worksheet,handle.worksheet)
        factory.assert_called_once_with()

    def test_concurrent_first_users_share_one_complete_handle(self):
        started=threading.Event();release=threading.Event()
        handle=Mock();handle.title='ready'
        def factory():
            started.set()
            if not release.wait(3):raise RuntimeError('fixture wait expired')
            return handle
        build=Mock(side_effect=factory)
        resource=LazyResource(build)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(lambda:resource.title)
            self.assertTrue(started.wait(2))
            second=pool.submit(lambda:resource.title)
            release.set()
            self.assertEqual(first.result(timeout=3),'ready')
            self.assertEqual(second.result(timeout=3),'ready')
        build.assert_called_once_with()

    def test_failed_lookup_does_not_poison_later_retry(self):
        handle=Mock();handle.title='recovered'
        build=Mock(side_effect=[TimeoutError('fixture lookup failure'),handle])
        resource=LazyResource(build)
        with self.assertRaises(TimeoutError):_ = resource.title
        self.assertEqual(resource.title,'recovered')
        self.assertEqual(build.call_count,2)

    def test_real_application_loads_without_google_db_or_line_network(self):
        # This subprocess imports the actual application only under explicit
        # fake credentials, a fake Google client, and network/DB deny mocks.
        script='''
import os,sys
from unittest.mock import Mock,patch
sys.path.insert(0,sys.argv[1])
client=Mock()
client.open_by_key.side_effect=AssertionError('Google workbook opened during startup')
with patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'FAKE_SECRET',
        'LINE_CHANNEL_ACCESS_TOKEN':'FAKE_TOKEN','GOOGLE_SHEET_ID':'FAKE_SHEET'},clear=True),\
     patch('google.oauth2.service_account.Credentials.from_service_account_file',return_value=Mock()),\
     patch('gspread.authorize',return_value=client),\
     patch('psycopg2.connect',side_effect=AssertionError('Database accessed during startup')),\
     patch('requests.sessions.Session.request',side_effect=AssertionError('Network accessed during startup')),\
     patch('socket.socket.connect',side_effect=AssertionError('Socket accessed during startup')):
    import app
    client.open_by_key.assert_not_called()
    with app.app.test_client() as web:
        assert web.get('/').status_code==200
    assert '/callback' in [rule.rule for rule in app.app.url_map.iter_rules()]
    book=Mock();book.title='fixture workbook'
    account=Mock();account.title='fixture account'
    book.worksheet.return_value=account
    client.open_by_key.side_effect=None
    client.open_by_key.return_value=book
    assert app.account_sheet.title=='fixture account'
    assert app.spreadsheet.title=='fixture workbook'
    client.open_by_key.assert_called_once_with('FAKE_SHEET')
    book.worksheet.assert_called_once_with('帳務表')
'''
        result=subprocess.run([sys.executable,'-I','-c',script,str(Path(__file__).parent)],
            capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('BOT_APPLICATION_READY_MS ',result.stdout)


if __name__=='__main__':unittest.main()
