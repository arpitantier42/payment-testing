"""HTTP routes for /api/v1 (see the OpenAPI contract in the ticket)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError
from sqlalchemy.orm import Session

from payment_service.api.errors import ApiError
from payment_service.api.security import PrincipalDep, require_principal
from payment_service.config import Settings
from payment_service.db import repository as repo
from payment_service.db.models import Order as OrderProjection
from payment_service.db.models import Payment, PaymentStatus
from payment_service.integrations.gateway import (
    GatewayError,
    PaymentGateway,
    verify_signature,
)
from payment_service.integrations.orders import Order, OrderService, OrderServiceError

# Customer endpoints: every route here requires a valid bearer token (contract scheme `bearer`).
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_principal)])
# Gateway-to-server endpoints: authenticated by X-API-Key + HMAC signature instead.
webhook_router = APIRouter(prefix="/api/v1")


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_order_service(request: Request) -> OrderService:
    return request.app.state.order_service


def get_gateway(request: Request) -> PaymentGateway:
    return request.app.state.gateway


SessionDep = Annotated[Session, Depends(get_session)]


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


def _fetch_order(orders: OrderService, order_id: uuid.UUID) -> Order:
    try:
        order = orders.get_order(order_id)
    except OrderServiceError as exc:
        raise ApiError(502, "order_service_unavailable", "Order details are unavailable right now") from exc
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    return order


def _load_or_project_order(session: Session, orders: OrderService, order_id: uuid.UUID) -> OrderProjection:
    """Return the cached projection, fetching it from order-service on a cache miss."""
    projection = repo.get_order(session, order_id)
    if projection is not None:
        return projection
    order = _fetch_order(orders, order_id)
    try:
        return repo.upsert_order_projection(
            session,
            order_id=order.id,
            summary=order.summary,
            amount_minor=order.amount_minor,
            currency=order.currency,
            available_payment_methods=list(order.available_payment_methods),
        )
    except repo.PaymentValidationError as exc:
        raise ApiError(502, "order_service_invalid", "Order details are unavailable right now") from exc


# --- orders -------------------------------------------------------------------


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    session: SessionDep,
    principal: PrincipalDep,
    orders: Annotated[OrderService, Depends(get_order_service)],
) -> dict:
    parsed = _parse_uuid(order_id)
    if parsed is None or not principal.may_access_order(parsed):
        raise ApiError(404, "order_not_found", "Order not found")
    projection = _load_or_project_order(session, orders, parsed)
    repo.ensure_payment_idempotency_key(session, projection)
    session.commit()
    return projection.to_dict()


# --- payments -------------------------------------------------------------------


class CreatePaymentRequest(BaseModel):
    """Client input for initiatePayment. Anything else (e.g. ``status``) is rejected with 400.

    The charge is always taken from the order projection; ``amount_minor``/``currency``
    are optional and, when sent, must match it (guards against a stale page).
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str | None = None
    payment_method: str | None = None
    amount_minor: StrictInt | None = None
    currency: str | None = None


def _payment_response(payment: Payment, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payment.to_dict(), status_code=status_code)


@router.post("/payments", status_code=201)
def create_payment(
    body: CreatePaymentRequest,
    session: SessionDep,
    principal: PrincipalDep,
    settings: Annotated[Settings, Depends(get_settings)],
    gateway: Annotated[PaymentGateway, Depends(get_gateway)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    try:
        order_id = repo.validate_new_payment(body.order_id, body.payment_method, idempotency_key)
    except repo.PaymentValidationError as exc:
        raise ApiError(400, "invalid_request", str(exc)) from exc
    assert idempotency_key is not None
    if not principal.may_access_order(order_id):
        raise ApiError(404, "order_not_found", "Order not found")

    # A replay of the same attempt returns the original payment without new side effects.
    existing = repo.get_payment_by_idempotency_key(session, idempotency_key)
    if existing is not None:
        if existing.order_id != order_id:
            raise ApiError(409, "idempotency_conflict", "Idempotency-Key was already used for a different payment")
        return _payment_response(existing, 200)

    order = repo.get_order(session, order_id)
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    if (body.amount_minor is not None and body.amount_minor != order.amount_minor) or (
        body.currency is not None and body.currency.upper() != order.currency
    ):
        raise ApiError(400, "amount_mismatch", "Amount or currency does not match the order")

    try:
        payment, created = repo.create_payment(
            session,
            order_id=order_id,
            payment_method=body.payment_method,
            idempotency_key=idempotency_key,
        )
    except repo.PaymentValidationError as exc:
        raise ApiError(400, "invalid_request", str(exc)) from exc
    except repo.OrderNotFound as exc:
        raise ApiError(404, "order_not_found", "Order not found") from exc
    except repo.IdempotencyConflict as exc:
        raise ApiError(409, "idempotency_conflict", str(exc)) from exc
    if not created:
        session.commit()
        return _payment_response(payment, 200)

    return_url = f"{settings.public_base_url}/pay/result.html?orderId={order_id}&paymentId={payment.id}"
    try:
        txn = gateway.create_transaction(
            payment_id=payment.id,
            amount=payment.amount_minor,
            currency=payment.currency,
            payment_method=payment.payment_method,
            return_url=return_url,
        )
    except GatewayError as exc:
        repo.update_payment_status(
            session, payment, PaymentStatus.FAILED, failure_reason=f"Gateway rejected initiation: {exc}"
        )
        session.commit()
        raise ApiError(502, "gateway_error", "The payment gateway could not start this payment") from exc

    repo.record_gateway_acceptance(
        session,
        payment,
        gateway_checkout_url=txn.checkout_url,
        gateway_session_id=txn.gateway_reference,
    )
    session.commit()
    return _payment_response(payment, 201)


@router.get("/payments/{payment_id}")
def get_payment(payment_id: str, session: SessionDep, principal: PrincipalDep) -> JSONResponse:
    payment = repo.get_payment(session, payment_id)
    if payment is None or not principal.may_access_order(payment.order_id):
        raise ApiError(404, "payment_not_found", "Payment not found")
    return _payment_response(payment)


# --- gateway webhook ----------------------------------------------------------------

EVENT_STATUS = {
    "payment.success": PaymentStatus.SUCCESS,
    "payment.failed": PaymentStatus.FAILED,
    "payment.pending": PaymentStatus.PENDING,
}


class WebhookData(BaseModel):
    payment_id: str | None = None
    gateway_reference: str | None = None  # the gateway's session id (payments.gateway_session_id)
    failure_reason: str | None = None


class WebhookBody(BaseModel):
    id: str
    type: str
    data: WebhookData


def _resolve_payment(session: Session, data: WebhookData) -> Payment | None:
    if data.payment_id:
        return repo.get_payment(session, data.payment_id)
    if data.gateway_reference:
        return repo.get_payment_by_gateway_session_id(session, data.gateway_reference)
    return None


# `/payments/webhook` is the contract path; `/payments/webhooks/gateway` is kept for
# gateways already configured with it.
@webhook_router.post("/payments/webhook")
@webhook_router.post("/payments/webhooks/gateway")
async def receive_gateway_webhook(
    request: Request,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
    signature: Annotated[str | None, Header(alias="X-Gateway-Signature")] = None,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> dict[str, Any]:
    if settings.gateway_webhook_api_key and api_key != settings.gateway_webhook_api_key:
        raise ApiError(401, "unauthorized", "Invalid API key")

    raw = await request.body()
    try:
        payload = json.loads(raw)
        event = WebhookBody.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise ApiError(400, "invalid_payload", "Webhook payload cannot be parsed") from exc

    payment = _resolve_payment(session, event.data)
    if payment is None:
        raise ApiError(400, "unknown_payment", "Webhook does not reference a known payment")

    signature_valid = verify_signature(settings.gateway_webhook_secret, raw, signature)
    stored, is_new = repo.record_webhook_event(
        session,
        payment_id=payment.id,
        gateway_event_id=event.id,
        event_type=event.type,
        signature_valid=signature_valid,
        raw_payload=payload,
    )
    session.commit()  # keep the audit record even if we stop here

    if not signature_valid:
        raise ApiError(401, "invalid_signature", "Webhook signature verification failed")
    if not is_new:
        return {"result": "duplicate"}

    new_status = EVENT_STATUS.get(event.type)
    if new_status is None:
        return {"result": "ignored", "reason": "unhandled event type"}

    failure_reason = None
    if new_status is PaymentStatus.FAILED:
        failure_reason = event.data.failure_reason or "Payment was declined by the gateway"
    try:
        repo.update_payment_status(session, payment, new_status, failure_reason=failure_reason)
    except repo.InvalidStatusTransition:
        session.rollback()
        return {"result": "ignored", "reason": f"payment already {payment.status}"}
    repo.mark_webhook_event_processed(session, stored)
    session.commit()
    return {"result": "applied", "status": payment.status}
