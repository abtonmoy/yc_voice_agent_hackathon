"""Pytest config: put ``server/`` on sys.path so ``mock_backend`` / ``tools``
import cleanly regardless of the invocation cwd."""

import os
import sys

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)
