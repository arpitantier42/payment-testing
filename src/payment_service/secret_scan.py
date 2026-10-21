"""Secret scan over the payment page artifact (HLD NFR-003, REQ-017).

Fails when anything under the web directory looks like a gateway credential or names a
server-side secret setting, or contains the actual value of one of those settings from
the environment. Run in CI: ``python -m payment_service.secret_scan [web_dir]``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from payment_service.config import REPO_ROOT

# Server-side settings whose names (and values) must never appear in the frontend.
SECRET_SETTINGS = (
    "GATEWAY_WEBHOOK_SECRET",
    "GATEWAY_WEBHOOK_API_KEY",
    "GATEWAY_API_KEY",
    "AUTH_TOKEN_SECRET",
    "DATABASE_URL",
)

# Common gateway credential shapes (Stripe, Razorpay, PayPal/Braintree, Adyen, generic PEM).
KEY_PATTERNS = {
    "stripe secret/restricted key": re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}"),
    "stripe webhook secret": re.compile(r"\bwhsec_[0-9A-Za-z]{10,}"),
    "razorpay key": re.compile(r"\brzp_(?:live|test)_[0-9A-Za-z]{10,}"),
    "braintree access token": re.compile(r"access_token\$(?:production|sandbox)\$"),
    "adyen api key": re.compile(r"\bAQE[0-9A-Za-z+/=]{40,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
}

TEXT_SUFFIXES = {".html", ".js", ".mjs", ".css", ".json", ".txt", ".map", ".svg"}
# Env values shorter than this are not searched for (too likely to match by chance).
MIN_VALUE_LENGTH = 8


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def scan(web_dir: Path, env: dict[str, str] | None = None) -> list[Finding]:
    env = os.environ if env is None else env
    values = {
        name: env[name] for name in SECRET_SETTINGS if len(env.get(name, "")) >= MIN_VALUE_LENGTH
    }
    findings = []
    for path in sorted(p for p in web_dir.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES):
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for name in SECRET_SETTINGS:
                if name in line:
                    findings.append(Finding(path, lineno, f"references server-side setting {name}"))
                if name in values and values[name] in line:
                    findings.append(Finding(path, lineno, f"contains the value of {name}"))
            for label, pattern in KEY_PATTERNS.items():
                if pattern.search(line):
                    findings.append(Finding(path, lineno, f"looks like a {label}"))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    web_dir = Path(args[0]) if args else REPO_ROOT / "web" / "public"
    if not web_dir.is_dir():
        print(f"secret scan: {web_dir} is not a directory", file=sys.stderr)
        return 2
    findings = scan(web_dir)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"secret scan: {len(findings)} finding(s) in {web_dir}", file=sys.stderr)
        return 1
    print(f"secret scan: no gateway secrets found in {web_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
