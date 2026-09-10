"""Every Python file in the repository must compile. No linter config can switch this off.

WHY THIS EXISTS SEPARATELY FROM RUFF. The PR that introduced the repository's first linter also
introduced SEVEN `SyntaxError`s — `from typing import X` inserted ABOVE
`from __future__ import annotations` — one of them in `routes/agent_center_bd_routes.py`, which
`main.py` imports, so the application could not boot. **Ruff said "All checks passed!"**

Two reasons it did:
  * a misplaced `__future__` import is not a parse error to ruff; it parses fine and the rule that
    objects is F404, which was not selected;
  * the config claimed `E9` covered "a file that cannot be parsed", and E999 is not a selectable
    rule in ruff 0.16 at all.

Both are now fixed in ruff.toml. This file exists because both were CONFIGURATION, and a gate whose
coverage depends on remembering to select the right rule is a gate that will be wrong again. Here
the question is asked directly, of the interpreter, about every file: does it compile? A `select`
list cannot narrow that and an `exclude` cannot hide it.
"""

from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKIP = {".venv", "venv", "node_modules", ".git", "__pycache__", "build", "dist", ".claude"}


def _python_files():
    for path in _ROOT.rglob("*.py"):
        if any(part in _SKIP for part in path.parts):
            continue
        yield path


def test_every_python_file_compiles():
    broken = []
    for path in _python_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            broken.append("%s:%s: %s" % (path.relative_to(_ROOT), exc.lineno, exc.msg))
    assert not broken, "these files cannot be compiled:\n  " + "\n  ".join(sorted(broken))


def test_the_sweep_actually_found_files():
    """The control. A glob that matched nothing would make the test above pass forever — the
    'absence of a mechanism satisfies the assertion' shape this repo keeps hitting."""
    count = sum(1 for _ in _python_files())
    assert count > 500, "only %d python files found; the walk is broken, not the repo clean" % count
