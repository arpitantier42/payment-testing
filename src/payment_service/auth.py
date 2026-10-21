"""Customer bearer tokens (HLD RSK-004: the bearer scheme is a placeholder until the
order-placement session issuer is confirmed).

A token is ``<claims>.<signature>``: base64url (unpadded) JSON claims and the base64url
HMAC-SHA256 of the claims part, keyed with ``AUTH_TOKEN_SECRET``. Claims:

- ``sub``      (required) who the token was issued to (customer / session id)
- ``exp``      (required) expiry, unix seconds
- ``order_id`` (optional) restricts the token to one order: other orders and their
               payments look "not found" to it

Dev tokens: ``python -m payment_service.auth --order-id <uuid>`` (reads AUTH_TOKEN_SECRET)
prints a token and the pay-page URL that hands it to the page in the URL fragment.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass


class InvalidToken(Exception):
    """The bearer token is malformed, badly signed or expired (message is safe to show)."""


@dataclass(frozen=True)
class Principal:
    subject: str
    order_id: uuid.UUID | None = None

    def may_access_order(self, order_id: uuid.UUID) -> bool:
        return self.order_id is None or self.order_id == order_id


# Used when AUTH_DISABLED is set (local development only).
ANONYMOUS = Principal(subject="anonymous")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, message: str) -> str:
    return _b64encode(hmac.new(secret.encode(), message.encode("ascii"), hashlib.sha256).digest())


def issue_token(
    secret: str,
    *,
    subject: str,
    order_id: uuid.UUID | str | None = None,
    ttl_seconds: int = 3600,
    now: float | None = None,
) -> str:
    if not secret:
        raise ValueError("a signing secret is required")
    claims: dict = {"sub": subject, "exp": int((now if now is not None else time.time()) + ttl_seconds)}
    if order_id is not None:
        claims["order_id"] = str(uuid.UUID(str(order_id)))
    body = _b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(secret, body)}"


def verify_token(secret: str, token: str, *, now: float | None = None) -> Principal:
    if not secret:
        raise InvalidToken("Authentication is not configured")
    body, sep, signature = (token or "").partition(".")
    if not sep or not body or not signature:
        raise InvalidToken("Malformed access token")
    try:
        expected = _sign(secret, body)
    except UnicodeEncodeError as exc:
        raise InvalidToken("Malformed access token") from exc
    if not hmac.compare_digest(expected.encode(), signature.encode("utf-8", "replace")):
        raise InvalidToken("Invalid access token")
    try:
        claims = json.loads(_b64decode(body))
    except (ValueError, binascii.Error) as exc:
        raise InvalidToken("Malformed access token") from exc
    if not isinstance(claims, dict):
        raise InvalidToken("Malformed access token")

    subject, exp, order_id = claims.get("sub"), claims.get("exp"), claims.get("order_id")
    if not isinstance(subject, str) or not subject:
        raise InvalidToken("Malformed access token")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise InvalidToken("Malformed access token")
    if exp <= (now if now is not None else time.time()):
        raise InvalidToken("Access token has expired")
    scoped = None
    if order_id is not None:
        try:
            scoped = uuid.UUID(str(order_id))
        except ValueError as exc:
            raise InvalidToken("Malformed access token") from exc
    return Principal(subject=subject, order_id=scoped)


def main(argv: list[str] | None = None) -> None:
    from payment_service.config import Settings

    parser = argparse.ArgumentParser(description="Mint a customer bearer token (development only).")
    parser.add_argument("--order-id", help="restrict the token to this order")
    parser.add_argument("--subject", default="dev-customer")
    parser.add_argument("--ttl", type=int, help="lifetime in seconds (default AUTH_TOKEN_TTL_SECONDS)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.auth_token_secret:
        parser.error("AUTH_TOKEN_SECRET is not set")
    token = issue_token(
        settings.auth_token_secret,
        subject=args.subject,
        order_id=args.order_id,
        ttl_seconds=args.ttl or settings.auth_token_ttl_seconds,
    )
    print(token)
    if args.order_id:
        print(f"{settings.public_base_url}/pay/?orderId={args.order_id}#token={token}")


if __name__ == "__main__":
    main()
