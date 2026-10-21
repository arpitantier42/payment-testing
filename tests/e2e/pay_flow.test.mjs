// WI-2 acceptance: the real payment page logic (web/public/js/payment.js) against a live
// payment-service (bearer auth on, FakeGateway, migrated SQLite). Started by
// tests/test_e2e_payment_flow.py, which passes the server URL, tokens and order ids in the
// environment and checks the database afterwards. Each test uses its own order.
//
// This file plays the gateway too (it signs webhooks), so it lives outside web/.

import assert from "node:assert/strict";
import { createHmac, randomUUID } from "node:crypto";
import { describe, it } from "node:test";

import {
  ACCESS_TOKEN_STORAGE_KEY,
  ApiError,
  createApi,
  createPayNowController,
  formatAmount,
  pollConfirmedStatus,
  readAccessToken,
  resolvePaymentId,
  validateOrder,
} from "../../web/public/js/payment.js";

const env = process.env;
const BASE_URL = env.E2E_BASE_URL;
const TOKEN = env.E2E_TOKEN;
const OTHER_ORDER_TOKEN = env.E2E_OTHER_ORDER_TOKEN; // scoped to an order that is not this customer's
const WEBHOOK_SECRET = env.E2E_WEBHOOK_SECRET;
const ORDERS = JSON.parse(env.E2E_ORDERS || "{}");
const SERVER_SECRETS = JSON.parse(env.E2E_SERVER_SECRETS || "[]");
const ALLOWED_REQUEST_HEADERS = new Set(["Authorization", "Content-Type", "Idempotency-Key"]);

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

/** One browser tab on the pay page: real HTTP, recorded requests, captured views/navigation. */
function openPayPage({ token = TOKEN, storage = memoryStorage() } = {}) {
  // The page is opened with #token=...; readAccessToken keeps it in sessionStorage for the tab.
  const { token: sessionToken } = readAccessToken({ hash: token ? `#token=${token}` : "", storage });
  const requests = [];
  const recordingFetch = (url, options = {}) => {
    requests.push({ url, method: options.method || "GET", headers: { ...(options.headers || {}) }, body: options.body });
    return fetch(url, options);
  };
  const api = createApi(recordingFetch, BASE_URL, { getToken: () => sessionToken });
  const views = [];
  const navigations = [];
  const timers = [];
  const controller = createPayNowController({
    api,
    storage,
    navigate: (url) => navigations.push(url),
    render: (view) => views.push(view),
    setTimer: (fn, ms) => timers.push({ fn, ms }),
  });
  return { api, controller, requests, views, navigations, timers, storage };
}

/** What result-page.js does after the gateway redirect: hint from the URL, id from storage. */
async function landOnResultPage(tab, checkoutUrl) {
  const url = new URL(checkoutUrl);
  const paymentId = resolvePaymentId({
    storage: tab.storage,
    orderId: url.searchParams.get("orderId"),
    queryPaymentId: url.searchParams.get("paymentId"),
  });
  return pollConfirmedStatus({
    api: tab.api,
    paymentId,
    gatewayHint: url.searchParams.get("gateway_status"),
    sleep: async () => {},
    attempts: 2,
  });
}

/** The gateway delivers a signed outcome webhook (server-to-server, not the page). */
async function gatewayWebhook(paymentId, type, data = {}) {
  const body = JSON.stringify({ id: `evt_${randomUUID()}`, type, data: { payment_id: paymentId, ...data } });
  const signature = createHmac("sha256", WEBHOOK_SECRET).update(body).digest("hex");
  const response = await fetch(`${BASE_URL}/api/v1/payments/webhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Gateway-Signature": signature },
    body,
  });
  assert.equal(response.status, 200, await response.text());
}

const posts = (tab) => tab.requests.filter((r) => r.method === "POST");

describe("WI-2 acceptance against a live payment-service", () => {
  it("AC1: a failed order fetch is an error, never a blank or stale amount", async () => {
    const tab = openPayPage();
    // order-service down and no cached projection -> 502
    await assert.rejects(tab.api.getOrder(ORDERS.broken), (err) => err instanceof ApiError && err.status === 502);
    // unknown order -> 404
    await assert.rejects(tab.api.getOrder(ORDERS.unknown), (err) => err instanceof ApiError && err.status === 404);
    // nothing loaded means nothing displayable, and Pay Now refuses to send
    assert.ok(validateOrder(null).length > 0);
    const result = await tab.controller.payNow({ order: null, paymentMethod: "upi" });
    assert.equal(result.sent, false);
    assert.equal(posts(tab).length, 0);
  });

  it("AC2: the page renders exactly the backend's amount and charges that amount", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.display);
    assert.equal(order.amount_minor, 349900);
    assert.equal(order.currency, "INR");
    assert.equal(formatAmount(order.amount_minor, order.currency), formatAmount(349900, "INR"));
    await tab.controller.payNow({ order, paymentMethod: "card" });
    const payment = await tab.api.getPayment(tab.storage.getItem(`payment:id:${order.id}`));
    assert.equal(payment.amount_minor, order.amount_minor);
    assert.equal(payment.currency, order.currency);
  });

  it("AC3: missing order id, amount or method sends no API call and shows an error", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.validation);
    for (const input of [
      { order: { ...order, id: undefined }, paymentMethod: "upi" },
      { order: { ...order, amount_minor: undefined }, paymentMethod: "upi" },
      { order, paymentMethod: null },
    ]) {
      const result = await tab.controller.payNow(input);
      assert.equal(result.sent, false);
      assert.equal(tab.views.at(-1).kind, "validation");
    }
    assert.equal(posts(tab).length, 0);
  });

  it("AC4, AC10: a non-success initiation response is an error view and never a redirect", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.apiError);
    // A key the backend never issued -> 400
    await tab.controller.payNow({ order: { ...order, payment_idempotency_key: "not-issued" }, paymentMethod: "upi" });
    assert.equal(tab.views.at(-1).kind, "api");
    assert.equal(tab.views.at(-1).status, 400);
    assert.ok(tab.views.at(-1).message);
    // A session for another order -> 404; no session -> 401
    for (const [token, status] of [[OTHER_ORDER_TOKEN, 404], [null, 401]]) {
      const other = openPayPage({ token });
      await other.controller.payNow({ order, paymentMethod: "upi" });
      assert.equal(other.views.at(-1).kind, "api");
      assert.equal(other.views.at(-1).status, status);
      assert.deepEqual(other.navigations, []);
    }
    assert.deepEqual(tab.navigations, []);
    assert.equal(tab.controller.isInFlight(), false);
  });

  it("AC5, AC9: checkout opens only after a successful POST /api/v1/payments, the sole initiation call", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.redirect);
    await tab.controller.payNow({ order, paymentMethod: "upi" });
    assert.equal(posts(tab).length, 1);
    assert.equal(posts(tab)[0].url, `${BASE_URL}/api/v1/payments`);
    const paymentId = tab.storage.getItem(`payment:id:${order.id}`);
    const payment = await tab.api.getPayment(paymentId);
    assert.equal(tab.views.at(-1).kind, "redirecting");
    assert.deepEqual(tab.navigations, [payment.gateway_checkout_url]);
    // every request the page made is to the Payment Service REST API
    for (const r of tab.requests) assert.ok(r.url.startsWith(`${BASE_URL}/api/v1/`), r.url);
  });

  it("AC6: an abandoned checkout ends on Pending or Failed instead of hanging", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.abandon);
    await tab.controller.payNow({ order, paymentMethod: "netbanking" });
    assert.equal(tab.timers.length, 1); // armed at hand-off
    await tab.timers[0].fn(); // the customer never returns from the gateway
    assert.equal(tab.views.at(-1).kind, "confirmed");
    assert.equal(tab.views.at(-1).result.kind, "pending");
    assert.equal(tab.controller.isInFlight(), false);

    const paymentId = tab.storage.getItem(`payment:id:${order.id}`);
    await gatewayWebhook(paymentId, "payment.failed", { failure_reason: "Checkout abandoned" });
    const back = await tab.controller.resume(order.id); // returning via Back
    assert.equal(back.kind, "failed");
    assert.equal(back.message, "Checkout abandoned");
  });

  it("AC7, AC8, AC11, AC18: the shown status is the backend's, fetched by the initiation paymentId", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.confirm);
    await tab.controller.payNow({ order, paymentMethod: "card" });
    const checkoutUrl = tab.navigations[0];
    assert.equal(new URL(checkoutUrl).searchParams.get("gateway_status"), "success"); // the redirect claims success

    const paymentId = tab.storage.getItem(`payment:id:${order.id}`);
    const before = tab.requests.length;
    const interim = await landOnResultPage(tab, checkoutUrl);
    // No verified webhook yet: the backend says pending, so pending is shown (redirect overridden).
    assert.equal(interim.kind, "pending");
    assert.equal(interim.overridden, true);
    const gets = tab.requests.slice(before);
    assert.ok(gets.length >= 1);
    for (const r of gets) {
      assert.equal(r.method, "GET");
      assert.equal(r.url, `${BASE_URL}/api/v1/payments/${paymentId}`);
    }

    await gatewayWebhook(paymentId, "payment.failed", { failure_reason: "Card declined" });
    const final = await landOnResultPage(tab, checkoutUrl);
    assert.equal(final.kind, "failed");
    assert.equal(final.message, "Card declined");
    assert.equal(final.overridden, true);
  });

  it("AC12: an unknown or invalid paymentId is an error state", async () => {
    const tab = openPayPage();
    for (const paymentId of [randomUUID(), "not-a-uuid"]) {
      const view = await pollConfirmedStatus({ api: tab.api, paymentId, gatewayHint: "success", sleep: async () => {} });
      assert.equal(view.kind, "error");
      assert.ok(view.message);
    }
    // another customer's payment looks unknown too
    const owner = openPayPage();
    const order = await owner.api.getOrder(ORDERS.foreign);
    await owner.controller.payNow({ order, paymentMethod: "upi" });
    const stranger = openPayPage({ token: OTHER_ORDER_TOKEN });
    const view = await pollConfirmedStatus({
      api: stranger.api,
      paymentId: owner.storage.getItem(`payment:id:${order.id}`),
      sleep: async () => {},
    });
    assert.equal(view.kind, "error");
  });

  it("AC13, AC14: double clicks while in flight send exactly one initiation request", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.doubleClick);
    const clicks = [1, 2, 3].map(() => tab.controller.payNow({ order, paymentMethod: "upi" }));
    const results = await Promise.all(clicks);
    assert.equal(posts(tab).length, 1);
    assert.deepEqual(results.slice(1), [{ ignored: true }, { ignored: true }]);
    await tab.controller.payNow({ order, paymentMethod: "upi" }); // still locked while leaving for checkout
    assert.equal(posts(tab).length, 1);
    // a second tab replaying the same key gets the same payment, not a new one
    const second = openPayPage();
    await second.controller.payNow({ order, paymentMethod: "upi" });
    assert.equal(second.storage.getItem(`payment:id:${order.id}`), tab.storage.getItem(`payment:id:${order.id}`));
  });

  it("AC15, AC16: retry after Failed creates a new attempt; after Success no second charge", async () => {
    const tab = openPayPage();
    let order = await tab.api.getOrder(ORDERS.retry);
    await tab.controller.payNow({ order, paymentMethod: "upi" });
    const firstId = tab.storage.getItem(`payment:id:${order.id}`);
    await gatewayWebhook(firstId, "payment.failed", { failure_reason: "Insufficient funds" });
    const failed = await tab.controller.resume(order.id);
    assert.equal(failed.kind, "failed");
    assert.equal(tab.controller.isInFlight(), false); // retry is available

    // Retry: the page reloads the order and gets a rotated key.
    const firstKey = order.payment_idempotency_key;
    order = await tab.api.getOrder(ORDERS.retry);
    assert.notEqual(order.payment_idempotency_key, firstKey);
    await tab.controller.payNow({ order, paymentMethod: "card" });
    const secondId = tab.storage.getItem(`payment:id:${order.id}`);
    assert.notEqual(secondId, firstId);
    await gatewayWebhook(secondId, "payment.success");
    const paid = await tab.controller.resume(order.id);
    assert.equal(paid.kind, "success");
    assert.deepEqual(await tab.controller.payNow({ order, paymentMethod: "card" }), { ignored: true });

    // Even a fresh tab cannot charge again: the key is not rotated after success.
    const fresh = openPayPage();
    const again = await fresh.api.getOrder(ORDERS.retry);
    assert.equal(again.payment_idempotency_key, order.payment_idempotency_key);
    await fresh.controller.payNow({ order: again, paymentMethod: "upi" });
    assert.equal(fresh.storage.getItem(`payment:id:${order.id}`), secondId); // replay of the paid attempt
    assert.deepEqual(fresh.navigations, []); // not sent to the gateway again
    assert.equal(fresh.views.at(-1).kind, "confirmed");
    assert.equal(fresh.views.at(-1).result.kind, "success");
    assert.equal(fresh.controller.isInFlight(), true); // Pay Now stays locked
  });

  it("AC17: no gateway secret or credential in requests or storage made by the page", async () => {
    const tab = openPayPage();
    const order = await tab.api.getOrder(ORDERS.secrets);
    await tab.controller.payNow({ order, paymentMethod: "upi" });
    await landOnResultPage(tab, tab.navigations[0]);
    assert.ok(tab.requests.length >= 3);
    for (const r of tab.requests) {
      for (const name of Object.keys(r.headers)) assert.ok(ALLOWED_REQUEST_HEADERS.has(name), name);
      assert.equal(r.headers.Authorization, `Bearer ${TOKEN}`); // the customer's own session only
      assert.equal("X-Gateway-Signature" in r.headers || "X-API-Key" in r.headers, false);
    }
    const sent = JSON.stringify(tab.requests);
    const stored = JSON.stringify([...tab.storage.map.entries()]);
    for (const secret of SERVER_SECRETS) {
      assert.equal(sent.includes(secret), false);
      assert.equal(stored.includes(secret), false);
    }
    assert.deepEqual(
      [...tab.storage.map.keys()].sort(),
      [ACCESS_TOKEN_STORAGE_KEY, `payment:id:${order.id}`].sort(),
    );
  });
});
