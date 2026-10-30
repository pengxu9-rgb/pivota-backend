"""Installs the loopback-only network guard in a Python child process of a guarded test run.

tests/net_guard.py puts this directory first on PYTHONPATH for the run, so any `python` a test
starts (subprocess, a script under test) imports this at startup. The child refuses the same
traffic and appends each refusal to the run directory, where the parent reads it back and
fails the test that started the child. Outside a guarded run (no NETGUARD_RUN_DIR) it does
nothing.
"""

import importlib.util as _util
import os as _os

if _os.environ.get("NETGUARD_RUN_DIR"):
    _path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "net_guard_core.py")
    _spec = _util.spec_from_file_location("_pivota_net_guard_child", _path)
    if _spec is not None and _spec.loader is not None:
        _module = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        _module.install_in_child()
