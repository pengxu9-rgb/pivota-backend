"""The commit sha stamped into the image outranks the deploy-time env var.

WHY THIS EXISTS. ``/health`` reports ``version.full_sha``, and the prod-deploy-drift
alarm compares exactly that against ``main``. Until the image stamped it, the only
source was ``PIVOTA_COMMIT_SHA``, set by whoever ran the deploy — so a deploy that
did not set it left the PREVIOUS value in place.

That is not hypothetical. On 2026-08-23 a manual ``gcloud run deploy`` rolled
production onto the ``eecb983b`` image while ``/health`` kept reporting
``b13b8a9b``; the running image digest was tagged ``eecb983b…`` and the env var
said otherwise. The alarm reasoned about a commit that was not serving traffic.
Worse than a wrong number: had the stale label happened to equal ``main``, the
alarm would have gone GREEN over undeployed code.

So the precedence here is the property under test, not an implementation detail.
"""
import importlib

import pytest

import config.platform as platform


@pytest.fixture(autouse=True)
def _reset_cache():
    """The baked sha is cached for the process; each test needs a clean read."""
    original_path = platform._IMAGE_COMMIT_SHA_FILE
    platform._image_commit_sha_cache = platform._UNREAD
    yield
    platform._IMAGE_COMMIT_SHA_FILE = original_path
    platform._image_commit_sha_cache = platform._UNREAD


def _stamp(tmp_path, contents):
    stamped = tmp_path / ".image_commit_sha"
    stamped.write_text(contents, encoding="utf-8")
    platform._IMAGE_COMMIT_SHA_FILE = str(stamped)
    return stamped


BAKED = "a" * 40
FROM_ENV = "b" * 40


def test_baked_sha_beats_the_deploy_time_env_var(tmp_path):
    # The exact 2026-08-23 shape: image says one thing, env var says another.
    _stamp(tmp_path, BAKED)
    assert platform.commit_sha({"PIVOTA_COMMIT_SHA": FROM_ENV}) == BAKED


def test_env_var_is_used_when_the_image_is_not_stamped(tmp_path):
    # Images built without the build arg, and every local run.
    platform._IMAGE_COMMIT_SHA_FILE = str(tmp_path / "absent")
    assert platform.commit_sha({"PIVOTA_COMMIT_SHA": FROM_ENV}) == FROM_ENV


@pytest.mark.parametrize("contents", ["", "\n", "   \n"])
def test_an_empty_stamp_falls_back_rather_than_reporting_blank(tmp_path, contents):
    # `docker build` with no --build-arg writes an empty file. Reporting "" would make
    # /health advertise an empty full_sha, which the drift alarm treats as "cannot
    # verify what production runs" and fails on - a broken deploy label taking the
    # alarm down with it.
    _stamp(tmp_path, contents)
    assert platform.commit_sha({"PIVOTA_COMMIT_SHA": FROM_ENV}) == FROM_ENV


def test_trailing_newline_is_stripped(tmp_path):
    # printf writes no newline, but a hand-edited or heredoc-written file would.
    # An unstripped value never equals the 40-char sha the alarm compares against.
    _stamp(tmp_path, BAKED + "\n")
    assert platform.commit_sha({}) == BAKED


def test_no_stamp_and_no_env_is_none_not_an_exception(tmp_path):
    platform._IMAGE_COMMIT_SHA_FILE = str(tmp_path / "absent")
    assert platform.commit_sha({}) is None


def test_unreadable_stamp_falls_back_instead_of_crashing(tmp_path):
    # A directory at the path raises IsADirectoryError, not FileNotFoundError. Any
    # OSError must degrade to the env var: this runs on the /health path, and a
    # health endpoint that raises is a worse failure than a stale label.
    (tmp_path / ".image_commit_sha").mkdir()
    platform._IMAGE_COMMIT_SHA_FILE = str(tmp_path / ".image_commit_sha")
    assert platform.commit_sha({"PIVOTA_COMMIT_SHA": FROM_ENV}) == FROM_ENV


def test_the_stamp_is_read_once_not_on_every_health_check(tmp_path):
    stamped = _stamp(tmp_path, BAKED)
    assert platform.commit_sha({}) == BAKED
    # Deleting it must not change the answer: the file cannot change without the
    # process being replaced alongside it, so /health does not re-read per request.
    stamped.unlink()
    assert platform.commit_sha({}) == BAKED
