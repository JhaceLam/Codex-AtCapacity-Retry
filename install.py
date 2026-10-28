"""Compatibility entry point for the versioned patch installer."""
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:] or ['apply']
if args[0].startswith('-'):
    args = ['apply'] + args
raise SystemExit(subprocess.call([sys.executable, str(Path(__file__).with_name('patch_manager.py'))] + args))
