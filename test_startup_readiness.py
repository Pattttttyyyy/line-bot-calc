"""Real local Gunicorn startup tests using a fake WSGI app, never app.py."""
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


FIXTURE = '''import os,time
from pathlib import Path
Path('import_started').touch()
while not Path('release_import').exists():time.sleep(0.01)
if os.environ.get('FAIL_IMPORT')=='1':raise RuntimeError('fixture initialization failed')
def app(environ,start_response):
    start_response('200 OK',[('Content-Type','text/plain')])
    return [b'fixture ready']
'''


class StartupReadinessTests(unittest.TestCase):
    def start_server(self, *, legacy=False, fail=False):
        tmp=tempfile.TemporaryDirectory(prefix='bot-startup-test-')
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        (root/'fixture_application.py').write_text(FIXTURE)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port=s.getsockname()[1]
        cmd=next(line for line in (Path(__file__).parent/'Dockerfile').read_text().splitlines()
                 if line.startswith('CMD '))
        args=shlex.split(cmd.removeprefix('CMD exec '))
        self.assertEqual(args[0],'gunicorn')
        self.assertIn('--preload',args)
        args=[a.replace(':$PORT',f'127.0.0.1:{port}').replace('app:app','fixture_application:app') for a in args[1:]]
        if legacy:args.remove('--preload')
        env={'PATH':os.environ.get('PATH',''),'FAIL_IMPORT':'1' if fail else '0'}
        proc=subprocess.Popen([sys.executable,'-m','gunicorn',*args],cwd=root,env=env,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=4)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
        self.addCleanup(stop)
        deadline=time.monotonic()+8
        while not (root/'import_started').exists():
            if proc.poll() is not None:self.fail('Fixture server exited before initialization')
            if time.monotonic()>deadline:self.fail('Fixture initialization did not start')
            time.sleep(0.01)
        return root,port,proc

    @staticmethod
    def listening(port):
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=0.1):return True
        except OSError:return False

    def test_production_command_waits_for_app_then_serves_first_request(self):
        root,port,proc=self.start_server()
        self.assertFalse(self.listening(port),'Not ready: startup probe must not pass')
        (root/'release_import').touch()
        deadline=time.monotonic()+8
        while not self.listening(port):
            if proc.poll() is not None:self.fail('Server failed to start')
            if time.monotonic()>deadline:self.fail('Server did not listen after initialization')
            time.sleep(0.01)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=2) as response:
            self.assertEqual(response.status,200)
            self.assertEqual(response.read(),b'fixture ready')

    def test_initialization_failure_never_exposes_listening_socket(self):
        root,port,proc=self.start_server(fail=True)
        self.assertFalse(self.listening(port))
        (root/'release_import').touch()
        self.assertNotEqual(proc.wait(timeout=8),0)
        self.assertFalse(self.listening(port))

    def test_previous_command_reproduces_premature_tcp_readiness(self):
        root,port,_=self.start_server(legacy=True)
        self.assertTrue(self.listening(port),'Old listener opens while app initialization is blocked')
        self.assertFalse((root/'release_import').exists())
        (root/'release_import').touch()


if __name__=='__main__':unittest.main()
