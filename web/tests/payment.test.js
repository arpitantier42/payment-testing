import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ApiError,
  NetworkError,
  confirmPaymentStatus,
  createApi,
  createPayNowController,
  idempotencyStorageKey,
  paymentIdStorageKey,
  resolvePaymentId,
  validatePaymentInput,
} from "../public/js/payment.js";

const ORDER = { id: "6f1c2b8e-3d4a-4c5b-9e7f-112233445566", amount: 349900, currency: "INR", summary: "x" };
const PAYMENT_ID = "0b6e5d1a-7c2f-4e8b-9a3d-5f6e7a8b9c0d";

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

/** fetch double that records calls; `respond` decides each response. */
function fakeFetch(respond) {
  const calls = [];
  const fetch = (url, options = {}) => {
    calls.push({ url, options });
    return respond(url, options, calls.length);
  };
  return { fetch, calls };
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}

function setup(respond) {
  const { fetch, calls } = fakeFetch(respond);
  const views = [];
  const navigations = [];
  const storage = memoryStorage();
  let keys = 0;
  const controller = createPayNowController({
    api: createApi(fetch),
    storage,
    newKey: () => `key-${++keys}`,
    navigate: (url) => navigations.push(url),
    render: (view) => views.push(view),
  });
  return { controller, calls, views, navigations, storage };
}

const created = (overrides = {}) =>
  jsonResponse(201, {
    id: PAYMENT_ID,
    order_id: ORDER.id,
    status: "processing",
    checkout_url: "https://gateway.example/checkout/abc",
    ...overrides,
  });

describe("validation before the API call (AC1)", () => {
  for (const [name, input] of [
    ["missing order id", { order: { ...ORDER, id: undefined }, paymentMethod: "upi" }],
    ["missing order", { order: null, paymentMethod: "upi" }],
    ["missing amount", { order: { id: ORDER.id }, paymentMethod: "upi" }],
    ["zero amount", { order: { ...ORDER, amount: 0 }, paymentMethod: "upi" }],
    ["missing payment method", { order: ORDER, paymentMethod: null }],
    ["unknown payment method", { order: ORDER, paymentMethod: "cash" }],
  ]) {
    it(`${name}: shows an error and sends no request`, async () => {
      const { controller, calls, views } = setup(() => created());
      const result = await controller.payNow(input);
      assert.equal(result.sent, false);
      assert.equal(calls.length, 0);
      assert.equal(views.at(-1).kind, "validation");
      assert.ok(views.at(-1).messages.length > 0);
    });
  }

  it("valid input yields no errors", () => {
    assert.deepEqual(validatePaymentInput({ order: ORDER, paymentMethod: "card" }), []);
  });
});

describe("initiation request", () => {
  it("POSTs order, method, amount and an Idempotency-Key, then goes to checkout", async () => {
    const { controller, calls, navigations, storage } = setup(() => created());
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/v1/payments");
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.headers["Idempotency-Key"], "key-1");
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      order_id: ORDER.id,
      payment_method: "upi",
      amount: 349900,
      currency: "INR",
    });
    assert.deepEqual(navigations, ["https://gateway.example/checkout/abc"]);
    assert.equal(storage.getItem(paymentIdStorageKey(ORDER.id)), PAYMENT_ID);
  });

  it("never sends a status field", async () => {
    const { controller, calls } = setup(() => created());
    await controller.payNow({ order: { ...ORDER, status: "success" }, paymentMethod: "upi" });
    assert.equal("status" in JSON.parse(calls[0].options.body), false);
  });
});

describe("failed API calls are surfaced (AC2)", () => {
  for (const status of [400, 404, 409, 500, 502]) {
    it(`HTTP ${status} shows an error and does not navigate`, async () => {
      const { controller, views, navigations } = setup(() =>
        jsonResponse(status, { error: { code: "x", message: `server said ${status}` } }),
      );
      await controller.payNow({ order: ORDER, paymentMethod: "card" });
      assert.equal(views.at(-1).kind, "api");
      assert.equal(views.at(-1).message, `server said ${status}`);
      assert.equal(views.at(-1).submitting, false);
      assert.deepEqual(navigations, []);
    });
  }

  it("a non-JSON error response still produces an error message", async () => {
    const { controller, views } = setup(() => ({ ok: false, status: 503, json: async () => { throw new Error("html"); } }));
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(views.at(-1).kind, "api");
    assert.match(views.at(-1).message, /503/);
  });

  it("a network failure shows a distinct error and keeps the key for a safe retry", async () => {
    const { controller, calls, views, storage } = setup((url, opts, n) =>
      n === 1 ? Promise.reject(new TypeError("Failed to fetch")) : created(),
    );
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(views.at(-1).kind, "network");
    assert.equal(storage.getItem(idempotencyStorageKey(ORDER.id)), "key-1");

    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(calls.length, 2);
    assert.equal(calls[1].options.headers["Idempotency-Key"], "key-1");
  });

  it("an answered error starts a new attempt key next time", async () => {
    const { controller, calls } = setup((url, opts, n) =>
      n === 1 ? jsonResponse(502, { error: { message: "gateway down" } }) : created(),
    );
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(calls[0].options.headers["Idempotency-Key"], "key-1");
    assert.equal(calls[1].options.headers["Idempotency-Key"], "key-2");
  });

  it("a payment the gateway refused is shown as failed, not redirected", async () => {
    const { controller, views, navigations } = setup(() =>
      created({ status: "failed", checkout_url: null, failure_reason: "Gateway rejected initiation" }),
    );
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(views.at(-1).kind, "failed");
    assert.equal(views.at(-1).message, "Gateway rejected initiation");
    assert.deepEqual(navigations, []);
  });
});

describe("duplicate submissions (AC7, AC8)", () => {
  it("a second click while the first request is in flight sends nothing", async () => {
    const pending = deferred();
    const { controller, calls, views } = setup(() => pending.promise);
    const first = controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(views.at(-1).submitting, true); // button disabled immediately

    const second = await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.deepEqual(second, { ignored: true });
    assert.equal(calls.length, 1);

    pending.resolve(created());
    await first;
    assert.equal(calls.length, 1);
  });

  it("rapid double (and triple) clicks before any response send exactly one request", async () => {
    const pending = deferred();
    const { controller, calls } = setup(() => pending.promise);
    const clicks = [1, 2, 3].map(() => controller.payNow({ order: ORDER, paymentMethod: "netbanking" }));
    pending.resolve(created());
    await Promise.all(clicks);
    assert.equal(calls.length, 1);
  });

  it("stays locked after a successful initiation while navigating to checkout", async () => {
    const { controller, calls } = setup(() => created());
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(calls.length, 1);
    assert.equal(controller.isInFlight(), true);
  });

  it("unlocks after an error so the customer can retry", async () => {
    const { controller, calls } = setup((u, o, n) => (n === 1 ? jsonResponse(500, {}) : created()));
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(controller.isInFlight(), false);
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(calls.length, 2);
  });
});

describe("confirmed status from the backend (AC3, AC4, AC5, AC6, AC9)", () => {
  const paymentBody = (status, extra = {}) => jsonResponse(200, { id: PAYMENT_ID, status, ...extra });

  it("calls GET /api/v1/payments/{paymentId} with the given id", async () => {
    const { fetch, calls } = fakeFetch(() => paymentBody("success"));
    const view = await confirmPaymentStatus({ api: createApi(fetch), paymentId: PAYMENT_ID });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `/api/v1/payments/${PAYMENT_ID}`);
    assert.equal(calls[0].options.method, undefined); // GET
    assert.equal(view.kind, "success");
  });

  it("uses the paymentId stored from initiation rather than one from the URL", async () => {
    const { controller, storage } = setup(() => created());
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    const other = "11111111-2222-4333-8444-555555555555";
    assert.equal(resolvePaymentId({ storage, orderId: ORDER.id, queryPaymentId: other }), PAYMENT_ID);
    assert.equal(resolvePaymentId({ storage: memoryStorage(), orderId: ORDER.id, queryPaymentId: other }), other);
  });

  for (const [gateway, backend, shown] of [
    ["success", "failed", "failed"],
    ["success", "pending", "pending"],
    ["success", "processing", "pending"],
    ["failed", "success", "success"],
  ]) {
    it(`gateway said ${gateway}, backend says ${backend}: shows ${shown}`, async () => {
      const { fetch } = fakeFetch(() => paymentBody(backend, { failure_reason: backend === "failed" ? "Card declined" : null }));
      const view = await confirmPaymentStatus({ api: createApi(fetch), paymentId: PAYMENT_ID, gatewayHint: gateway });
      assert.equal(view.kind, shown);
      assert.equal(view.overridden, true);
      if (shown === "failed") assert.equal(view.message, "Card declined");
    });
  }

  it("matching gateway hint is not flagged as overridden", async () => {
    const { fetch } = fakeFetch(() => paymentBody("success"));
    const view = await confirmPaymentStatus({ api: createApi(fetch), paymentId: PAYMENT_ID, gatewayHint: "success" });
    assert.equal(view.kind, "success");
    assert.equal(view.overridden, false);
  });

  it("an unknown paymentId (404) shows an error state", async () => {
    const { fetch } = fakeFetch(() => jsonResponse(404, { error: { code: "payment_not_found", message: "Payment not found" } }));
    const view = await confirmPaymentStatus({ api: createApi(fetch), paymentId: PAYMENT_ID, gatewayHint: "success" });
    assert.equal(view.kind, "error");
    assert.match(view.message, /couldn't find this payment/);
  });

  for (const bad of [null, "", "not-a-uuid", "123"]) {
    it(`an invalid paymentId (${JSON.stringify(bad)}) shows an error without trusting the gateway`, async () => {
      const { fetch, calls } = fakeFetch(() => paymentBody("success"));
      const view = await confirmPaymentStatus({ api: createApi(fetch), paymentId: bad, gatewayHint: "success" });
      assert.equal(view.kind, "error");
      assert.equal(calls.length, 0);
    });
  }

  it("a network failure while confirming is an error, never a success inferred from the redirect", async () => {
    const { fetch } = fakeFetch(() => Promise.reject(new TypeError("offline")));
    const view = await confirmPaymentStatus({ api: createApi(fetch), paymentId: PAYMENT_ID, gatewayHint: "success" });
    assert.equal(view.kind, "network");
  });
});

describe("api client", () => {
  it("wraps transport failures and HTTP errors in distinct types", async () => {
    const down = createApi(() => Promise.reject(new Error("x")));
    await assert.rejects(down.getPayment(PAYMENT_ID), NetworkError);
    const notFound = createApi(() => jsonResponse(404, { error: { code: "payment_not_found", message: "nope" } }));
    await assert.rejects(notFound.getPayment(PAYMENT_ID), (err) => err instanceof ApiError && err.status === 404);
  });
});
