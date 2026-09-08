"""Run `scripts/backfill_variant_id_provenance_stamps.main()` with a failure injected on page 3.

NOT A TEST FILE — no `test_` prefix, so pytest never collects it. It is the subprocess entrypoint
for `tests/test_backfill_variant_id_provenance_stamps_postgres.py::
test_main_prints_the_sealed_report_with_the_cursor_when_the_run_dies_mid_scan`, and exists
because that test cannot be written any other way:

  * `main()` calls `asyncio.run()`, which refuses to nest inside the loop the async gate tests
    already run in, so main() can only be driven as a real process;
  * a real process cannot be monkeypatched from the test, and the script has NO fault-injection
    hook (deliberately — a `STAMP_FAIL_ON_PAGE` env var in a production backfill is a foot-gun
    an operator can trip by inheriting a stale shell);
  * the only main()-level failure test that existed pointed the script at an unresolvable
    DATABASE_URL, so `run()` was never entered and main()'s `{"resume_after": None,
    "sealed": False}` fallback was the CORRECT output. Mutating main() to print that fallback
    UNCONDITIONALLY (`report = None`) left all 32 tests green — the branch that carries the
    resume cursor out to the operator had no test reaching it.

So this file does in a subprocess exactly what the in-process test
`test_the_audit_row_and_the_resume_cursor_survive_an_error_mid_run` does: it replaces
`database.fetch_all` with one that raises on the THIRD page fetch, after two pages have
committed, and then hands the real argv to the real `main()`. Everything the test asserts on is
what main() actually printed and what the process actually exited with.

Usage (from the repo root; the test passes the same flags `scripts/ops/run_oneoff_job.sh` would):

    python -B tests/backfill_variant_id_provenance_stamps_fault_driver.py \
        --apply --expect-contract stamp-v1-sku-key-cursor --after 'zzz:' --page 3
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scripts.backfill_variant_id_provenance_stamps as stamps  # noqa: E402

#: Which page fetch raises. Page 3 = two pages committed behind it, rows still ahead of it: the
#: only shape in which `resume_after` is worth anything. The test asserts on this constant.
FAIL_ON_PAGE = 3
FAILURE_MESSAGE = "simulated mid-run failure (fault driver, page %d)" % FAIL_ON_PAGE

_real_fetch_all = stamps.database.fetch_all
_pages = {"n": 0}


async def _dies_on_the_third_page(query, values=None):
    _pages["n"] += 1
    if _pages["n"] == FAIL_ON_PAGE:
        raise RuntimeError(FAILURE_MESSAGE)
    return await _real_fetch_all(query, values)


stamps.database.fetch_all = _dies_on_the_third_page

if __name__ == "__main__":
    raise SystemExit(stamps.main(sys.argv[1:]))
