#!/usr/bin/env python3
from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit('SELC suite requires Python 3.11+; invoke with a supported interpreter.')
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'runtime'))
from selc.cli import main

if __name__ == '__main__':
    raise SystemExit(main(default_suite_root=ROOT, force_command='install-local'))
