"""Run only credential-free unit tests; never import the live Flask app."""
import sys,unittest
from pathlib import Path

ROOT=Path(__file__).parent
sys.path.insert(0,str(ROOT))
SUITES=['test_group_credit','test_cancel_adapter','test_ui','test_bottom_quick','test_text_commands']
if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(SUITES))
    raise SystemExit(not result.wasSuccessful())
