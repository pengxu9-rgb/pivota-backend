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


def broken_files(paths):
    """The sweep itself, over ANY iterable of paths.

    A FUNCTION, so a test can hand it a file it knows is broken and watch it report one. When the
    loop was inlined into the test, making its `except SyntaxError` swallow left this module fully
    green — the mechanism could be deleted and nothing noticed, which is precisely the failure
    class this file exists to close."""
    broken = []
    for path in paths:
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            broken.append("%s: %s" % (path, exc.msg))
    return broken


def test_every_python_file_compiles():
    broken = broken_files(_python_files())
    assert not broken, "these files cannot be compiled:\n  " + "\n  ".join(sorted(broken))


def test_the_sweep_REPORTS_a_broken_file(tmp_path):
    """Hand it a real file that cannot compile. Killing the `except` branch now fails here."""
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n    pass\n")
    good = tmp_path / "good.py"
    good.write_text("x = 1\n")

    reported = broken_files([bad, good])
    assert len(reported) == 1, reported
    assert "bad.py" in reported[0]

    # the control: a tree of only-valid files reports nothing, so "always reports one" fails too
    assert broken_files([good]) == []


def test_the_sweep_catches_the_shape_that_shipped(tmp_path):
    """A late `from __future__` import — seven of these merged past the linter in this same PR."""
    bad = tmp_path / "late_future.py"
    bad.write_text("from typing import List\nfrom __future__ import annotations\n")
    assert len(broken_files([bad])) == 1


def test_the_sweep_actually_found_files():
    """The control. A glob that matched nothing would make the test above pass forever — the
    'absence of a mechanism satisfies the assertion' shape this repo keeps hitting.

    Pinned near the real count (2,418 on 2026-09-10), not at a token floor: `tests/` alone holds
    1,233, so a walk that dropped every production tree and kept only tests would clear 500."""
    count = sum(1 for _ in _python_files())
    assert count > 2000, "only %d python files found; the walk is broken, not the repo clean" % count
    # ...and the production trees specifically, since the count alone cannot tell which survived.
    for tree in ("routes", "services", "db", "jobs", "scripts"):
        assert any(p.parts[len(_ROOT.parts)] == tree for p in _python_files()), (
            "no files found under %s/ — the walk is skipping production code" % tree
        )


