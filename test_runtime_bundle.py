"""Test the image's actual file allowlist, without importing the live app."""
import ast
import fnmatch
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).parent

def included(path):
    allowed=True
    for pattern in (ROOT/'.dockerignore').read_text().splitlines():
        if not pattern or pattern.startswith('#'):
            continue
        exclude=not pattern.startswith('!')
        pattern=pattern.lstrip('!')
        if fnmatch.fnmatch(path,pattern):
            allowed=not exclude
    return allowed

class RuntimeBundleTests(unittest.TestCase):
    def test_every_local_runtime_dependency_is_shipped(self):
        pending=['app','daily_report'];seen=set()
        while pending:
            name=pending.pop()
            if name in seen:
                continue
            seen.add(name)
            path=ROOT/(name+'.py')
            self.assertTrue(included(path.name),f'Runtime dependency missing from image: {path.name}')
            for node in ast.walk(ast.parse(path.read_text())):
                modules=([node.module] if isinstance(node,ast.ImportFrom) else
                         [a.name for a in node.names] if isinstance(node,ast.Import) else [])
                for module in modules:
                    if module and (ROOT/(module+'.py')).exists():
                        pending.append(module)

    def test_staged_image_can_load_the_sync_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in ROOT.glob('*.py'):
                if included(path.name):
                    shutil.copyfile(path,Path(tmp)/path.name)
            result=subprocess.run([sys.executable,'-I','-c',
                'import sys;sys.path.insert(0,sys.argv[1]);import group_sheet_queue;'
                'from group_credit_handler import try_fast_group_sale',tmp],
                capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_private_files_and_disabled_payment_draft_stay_excluded(self):
        for path in ('credentials.private.json','.env','test_postgres.py','mycard_draft/provider.py'):
            self.assertFalse(included(path))
