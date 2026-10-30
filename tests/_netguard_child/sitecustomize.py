"""Installs the loopback-only network guard in a Python child process of a guarded test run.

tests/net_guard.py puts this directory first on PYTHONPATH for the run, so any `python` a test
starts (subprocess, a script under test) imports this at startup. The child refuses the same
traffic and appends each refusal to the run directory, where the parent reads it back and
fails the test that started the child. Outside a guarded run (no NETGUARD_RUN_DIR) it installs
nothing.

Being first on the path, this file shadows any other sitecustomize (a safety-net guard, a
coverage hook): it runs the first one after it in path order -- skipping its own directory
under any spelling and any other copy of this shim -- so this guard is installed on top of it
and sees every call first. If this guard cannot be installed in a guarded run, the child exits
rather than run unguarded.
"""

import importlib.machinery as _machinery
import importlib.util as _util
import os as _os
import sys as _sys

_HERE = _os.path.realpath(_os.path.dirname(_os.path.abspath(__file__)))
#: Names a file as this shim, so a second copy on the path (another checkout, a symlinked
#: spelling) is never chained to: two copies would chain to each other without end.
_PIVOTA_NET_GUARD_SHIM = True


def _is_shim(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "_PIVOTA_NET_GUARD_SHIM = True" in fh.read()
    except OSError:
        return False


def _run_shadowed() -> None:
    if getattr(_sys, "_pivota_net_guard_shim_chaining", False):
        return  # re-entered through a chained file: the outer run of this shim does the chaining
    _sys._pivota_net_guard_shim_chaining = True
    try:
        for entry in _sys.path:
            if _os.path.realpath(entry or _os.curdir) == _HERE:
                continue
            spec = _machinery.PathFinder.find_spec("sitecustomize", [entry])
            if spec is None or spec.loader is None or spec.origin is None or _is_shim(spec.origin):
                continue
            module = _util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # what site.py does for a failing sitecustomize
                print(f"Error in sitecustomize; set PYTHONVERBOSE for traceback:\n{type(exc).__name__}: {exc}", file=_sys.stderr)
            return
    finally:
        _sys._pivota_net_guard_shim_chaining = False


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
