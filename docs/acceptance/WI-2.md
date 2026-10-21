# WI-2 acceptance criteria → code and tests

Much of the page and API behaviour these criteria cover was built before WI-2 (WI-1), so it
is not in the WI-2 diff. WI-2 adds bearer auth in front of it. The end-to-end test
`tests/test_e2e_payment_flow.py` runs the real page logic (`web/public/js/payment.js`)
against a live payment-service with auth on, one Node test per criterion
(`tests/e2e/pay_flow.test.mjs`, names start with `ACn:`), and then checks the database.
Unit tests: `web/tests/payment.test.js` (page logic) and `tests/test_api.py` / `tests/test_auth.py` (API).

| AC | Behaviour | Code | Tests |
|---|---|---|---|
| 1 | Failed order fetch → error, no blank/stale amount | `pay-page.js` `loadOrder` → `showOrderUnavailable`; `payment.js` `validateOrder`, `formatAmount` (null without currency) | e2e AC1 (502 + 404); unit "order display" |
| 2 | Show the backend's exact amount | `formatAmount(order.amount_minor, order.currency)`; server copies amount from projection (`routes.create_payment`) | e2e AC2; `test_amount_is_taken_from_the_order_when_not_sent`, `test_amount_must_match_order` |
| 3 | Missing order id / amount / method → no call, error shown | `validatePaymentInput` in `createPayNowController.payNow` (before `inFlight`) | e2e AC3; unit "validation before the API call" |
| 4, 10 | Non-success response → error view, no redirect | `payNow` `catch` → `api` / `network` views; `navigate` only on the success path | e2e AC4/AC10 (400, 404, 401); unit "failed API calls are surfaced"; 401 unit test |
| 5 | Checkout only after a successful initiation | `payNow`: `navigate(payment.gateway_checkout_url)` after `createPayment` resolves; refused (failed) payment is not redirected | e2e AC5; unit "a payment the gateway refused is shown as failed" |
| 6 | Abandoned checkout → Pending/Failed, never hangs | `CHECKOUT_TIMEOUT_MS` timer → `confirmReturn`; `resume` on load/`pageshow`; `REQUEST_TIMEOUT_MS`; bounded `pollConfirmedStatus` | e2e AC6; unit "after the gateway checkout" |
| 7, 18 | Shown status = backend's, overriding the redirect | `confirmPaymentStatus` (redirect hint only compared → `overridden`); status changes only via verified webhook | e2e AC7/AC18 (redirect says success, shown pending, then failed); `test_verified_webhook_sets_final_status`, `test_no_api_operation_accepts_a_status_change_from_the_client` |
| 8, 11 | `GET /api/v1/payments/{paymentId}` with the id from initiation | `api.getPayment`; `resolvePaymentId` prefers the id stored from the POST response | e2e AC8/AC11 (every status call uses that id); unit "uses the paymentId stored from initiation" |
| 9 | `POST /api/v1/payments` is the only way to initiate | `api.createPayment` is the only POST the page makes | e2e AC9 (one POST, all requests to `/api/v1/`) |
| 12 | Unknown/invalid paymentId → error state | `confirmPaymentStatus` (invalid id: no call; 404: error); API 404, also for another customer's payment | e2e AC12; `test_unknown_or_invalid_payment_id_is_404`, `test_token_scoped_to_another_order_sees_not_found` |
| 13, 14 | Second/rapid clicks while in flight → exactly one request | synchronous `inFlight` flag in `payNow` + disabled button (`pay-page.js` render); server-issued key + unique index | e2e AC13/AC14 (3 clicks + second tab → 1 row); unit "duplicate submissions" |
| 15 | Retry after Failed | `confirmReturn` unlocks unless success; pay page reloads the order for the rotated key; result page "retry" link | e2e AC15 |
| 16 | Retry never double-charges | `ensure_payment_idempotency_key` rotates only after `failed`; replay returns the original payment; a replayed *successful* payment is shown as success, not redirected | e2e AC16 + DB checks (≤1 success per order, replays never call the gateway); `test_key_is_not_rotated_after_success` |
| 17 | No gateway secrets in source, storage or requests | secrets server-side only (`config.py`, `repr=False`); `secret_scan` over `web/public` (CI); the page only sends its own bearer token | e2e AC17 (request headers allow-list, no secret in requests/storage); `tests/test_secret_scan.py`, `test_payment_pages_are_served_without_secrets` |
