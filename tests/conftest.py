"""Shared pytest setup for the Aime test suite.

The project isn't pip-installed; modules live under ``src/`` and runtime code
puts that directory on ``sys.path`` itself (e.g. ``web_app.py``). Tests do the
same one thing here so ``import aime...`` resolves no matter where pytest is
invoked from.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
