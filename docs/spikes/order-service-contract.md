# Spike WI-002 — order-service get-by-id contract (HLD RSK-002)

**Status:** contract *assumed and pinned in code/tests*; **not yet confirmed with the order-service
owners** — nobody with access to the real service or its spec was available to this spike. The
assumption below is what payment-service now codes against; confirming it is a matter of running the
probe (below) against a real environment and answering the open questions.

## Why

`getOrder` (`GET /api/v1/orders/{orderId}`) and the amount revalidation in `createPayment` depend on
order-service returning summary, amount and currency for an order id. If the real contract differs we
want exactly one place to change.

## Isolation (the RSK-002 mitigation)

- Routes only see the `OrderService` protocol (`src/payment_service/integrations/orders.py`):
  `get_order(order_id) -> Order | None`, raising `OrderServiceError` on failure.
- `HttpOrderService` implements the assumed contract below; `parse_order_payload` is the only code that
  knows the wire shape (also used for the dev stub's `dev/orders.json`, so the stub cannot drift).
- `create_app` uses `HttpOrderService` when `ORDER_SERVICE_URL` is set, else the in-memory stub.
- Orders are projected into the local `orders` table on first sight and never re-fetched (assumed
  immutable once placed), so order-service is on the critical path only for the first view of an order.

## Assumed contract

```
GET {ORDER_SERVICE_URL}/orders/{orderId}
Accept: application/json
Authorization: Bearer {ORDER_SERVICE_API_TOKEN}     (only if set; service-to-service credential)
```

200 body (JSON object; extra fields ignored):

| field | type | rule | maps to |
|---|---|---|---|
| `id` | uuid string | must equal the requested id | `Order.id` |
| `summary` | string | non-empty | `Order.summary` |
| `amount` (alias `amount_minor`) | integer | ≥ 0, **minor units**; floats/strings rejected (catches a major-units contract) | `Order.amount_minor` |
| `currency` | string | ISO 4217 alpha-3, upper-cased | `Order.currency` |
| `created_at` | ISO 8601 string | must carry an offset (`Z` accepted) | `Order.created_at` |
| `available_payment_methods` | string list | optional (absent → upi, card, netbanking); unknown values dropped; none left → mismatch | `Order.available_payment_methods` |

## Error cases → getOrder response

| order-service behaviour | adapter result | getOrder |
|---|---|---|
| 200 + valid order | `Order` | 200 (projected and cached) |
| 404 | `None` | 404 `order_not_found` |
| any other status (400/401/403/409/5xx) | `OrderServiceError` | 502 `order_service_unavailable` |
| no answer within `ORDER_SERVICE_TIMEOUT_SECONDS` (default 3 s) | `OrderServiceError` | 502 |
| connection refused / DNS failure | `OrderServiceError` | 502 |
| 200 with non-JSON body, wrong shape, or a different order id | `OrderPayloadError` (subclass) | 502 |
| any failure, but the order is already cached | not called | 200 from the projection |

Nothing is cached from a failed fetch. An order id the bearer token is not scoped to is 404 before
order-service is called. 401/403 from order-service mean *our* credentials are wrong, so they are a 502
(our upstream problem), not a customer-facing 401/403.

Pinned by `tests/test_order_service_contract.py` (a local HTTP server plays order-service).

## Confirming against a real order-service

```
ORDER_SERVICE_URL=https://orders.example/api/v1 ORDER_SERVICE_API_TOKEN=... \
  .venv/bin/python -m payment_service.integrations.orders <order-uuid>
```

Prints `OK (getOrder -> 200): {...}`, `NOT FOUND (getOrder -> 404)` (exit 0),
`UPSTREAM FAILURE` (exit 1) or `CONTRACT MISMATCH: <which field>` (exit 2). Run it for a known order and
an unknown id; a mismatch names the field that differs, which is the change to make in
`parse_order_payload`.

## Open questions for the order-service owners

1. Real path and base URL; is it versioned (`/api/v1/orders/{id}`)?
2. Amount field name and units — minor-unit integer, or decimal major units? (A decimal would be
   rejected today, deliberately.)
3. Service-to-service auth: bearer token, mTLS, or API key header?
4. Does order-service enforce ownership (customer ↔ order), or must payment-service rely on the
   customer token's `order_id` claim as it does now (HLD RSK-004)?
5. Is there an order status (cancelled, already paid) payment-service must refuse to charge?
6. Are orders really immutable after placement (the projection is never refreshed)?
7. Does it ever return 404 for "exists but not yours" vs 403? (Both handled: 404 → 404, 403 → 502.)
8. Expected latency/SLA — is 3 s the right timeout, and should we retry idempotent GETs?
