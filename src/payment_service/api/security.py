"""Bearer authentication for the customer endpoints (contract security scheme ``bearer``).

``require_principal`` is installed as a router-level dependency on every customer route,
so a route cannot be added there without authentication. The gateway webhook uses its
own scheme (``X-API-Key`` + HMAC signature) and is on a separate router.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from payment_service.api.errors import ApiError
from payment_service.auth import ANONYMOUS, InvalidToken, Principal, verify_token
from payment_service.config import Settings

_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="payment-service"'}


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, headers=_CHALLENGE)


def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.auth_disabled:
        principal = ANONYMOUS
    else:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized("Your session is missing or has expired. Please return to your order and try again.")
        try:
            principal = verify_token(settings.auth_token_secret, token.strip())
        except InvalidToken as exc:
            raise _unauthorized(
                "Your session is invalid or has expired. Please return to your order and try again."
            ) from exc
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(require_principal)]
