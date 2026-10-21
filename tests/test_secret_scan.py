"""REQ-017: the payment page artifact carries no gateway secrets (CI secret scan)."""

import pytest

from payment_service.config import REPO_ROOT
from payment_service.secret_scan import main, scan


def test_shipped_payment_page_has_no_secrets(monkeypatch):
    monkeypatch.setenv("GATEWAY_WEBHOOK_SECRET", "a-real-looking-webhook-secret")
    assert scan(REPO_ROOT / "web" / "public") == []
    assert main([str(REPO_ROOT / "web" / "public")]) == 0


@pytest.mark.parametrize(
    "content, reason",
    [
        ("const k = 'sk_live_abcdefghijklmnop';", "stripe"),
        ("whsec_0123456789abcdef", "webhook secret"),
        ("key: 'rzp_test_ABCDEFGHIJKL'", "razorpay"),
        ("-----BEGIN PRIVATE KEY-----", "private key"),
        ("fetch('/x?s=' + GATEWAY_WEBHOOK_SECRET)", "GATEWAY_WEBHOOK_SECRET"),
        ("const secret = 'value-from-the-env-123';", "contains the value of GATEWAY_API_KEY"),
    ],
)
def test_scan_flags_secrets(tmp_path, content, reason):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "app.js").write_text(f"// ok\n{content}\n")
    findings = scan(tmp_path, env={"GATEWAY_API_KEY": "value-from-the-env-123"})
    assert findings and findings[0].line == 2
    assert any(reason in f.reason for f in findings)
    assert main([str(tmp_path)]) == 1 or reason.startswith("contains")


def test_missing_directory_is_an_error(tmp_path):
    assert main([str(tmp_path / "nope")]) == 2
