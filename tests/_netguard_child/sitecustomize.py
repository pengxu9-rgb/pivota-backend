"""Installs the loopback-only network guard in a Python child process of a guarded test run.

tests/net_guard.py puts this directory first on PYTHONPATH for the run, so any `python` a test
starts (subprocess, a script under test) imports this at startup. The child refuses the same
traffic and appends each refusal to the run directory, where the parent reads it back and
fails the test that started the child. Outside a guarded run (no NETGUARD_RUN_DIR) it installs
nothing.

Being first on the path, this file shadows any other sitecustomize (a safety-net guard, a
coverage hook): it runs that one first, from where it sits in path order, so this guard is
installed on top of it and sees every call first. If this guard cannot be installed in a
guarded run, the child exits rather than run unguarded.
"""

import importlib.machinery as _machinery
import importlib.util as _util
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))


def _run_shadowed() -> None:
    rest = [p for p in _sys.path if _os.path.abspath(p or _os.curdir) != _HERE]
    spec = _machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = _util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # what site.py does for a failing sitecustomize
        print(f"Error in sitecustomize; set PYTHONVERBOSE for traceback:\n{type(exc).__name__}: {exc}", file=_sys.stderr)


def _install() -> None:
    path = _os.path.join(_os.path.dirname(_HERE), "net_guard_core.py")
    spec = _util.spec_from_file_location("_pivota_net_guard_child", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_in_child()


_run_shadowed()
if _os.environ.get("NETGUARD_RUN_DIR"):
    try:
        _install()
    except BaseException as exc:
        print(f"[net_guard] could not install the network guard in this child ({type(exc).__name__}: {exc}); "
              "exiting rather than running unguarded", file=_sys.stderr)
        _os._exit(70)
