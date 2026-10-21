"""Runs the payment page's JavaScript tests (web/tests, node --test) as part of pytest."""

import shutil
import subprocess

import pytest

from payment_service.config import REPO_ROOT

WEB = REPO_ROOT / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_web_unit_tests_pass():
    result = subprocess.run(
        ["node", "--test", "tests/"],
        cwd=WEB,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
