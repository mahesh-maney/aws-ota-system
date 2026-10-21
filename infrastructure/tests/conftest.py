"""
conftest.py — shared pytest configuration for OTA Lambda tests.

Adds 06_lambdas/ to sys.path so that `import _notif` (and any other
shared helpers copied into Lambda directories) resolve correctly when
Lambda source files are loaded via importlib during tests.
"""
import os
import sys

_lambdas_dir = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "06_lambdas")
)
if _lambdas_dir not in sys.path:
    sys.path.insert(0, _lambdas_dir)
