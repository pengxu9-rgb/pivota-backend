"""Repo-root conftest: loads plugins that must cover every test directory (tests/ and
readiness/tests/), before either directory's own conftest or any test module is imported."""

pytest_plugins = ("tests.net_guard",)
