import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ApiError,
  NetworkError,
  confirmPaymentStatus,
  createApi,
  availableMethods,
  createPayNowController,
  formatAmount,
  paymentIdStorageKey,
  pollConfirmedStatus,
  resolvePaymentId,
  validateOrder,
  validatePaymentInput,
} from "../public/js/payment.js";

const ORDER = {
  id: "6f1c2b8e-3d4a-4c5b-9e7f-112233445566",
  amount_minor: 349900,
  currency: "INR",
  summary: "x",
  available_payment_methods: ["upi", "card", "netbanking"],
  payment_idempotency_key: "key-1",
};
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

function setup(respond, extra = {}) {
  const { fetch, calls } = fakeFetch(respond);
  const views = [];
  const navigations = [];
  const storage = extra.storage || memoryStorage();
  const controller = createPayNowController({
    setTimer: () => {}, // no real 10-minute checkout timer in unit tests
    ...extra,
    api: createApi(fetch),
    storage,
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
    gateway_checkout_url: "https://gateway.example/checkout/abc",
    ...overrides,
  });

describe("validation before the API call (AC1)", () => {
  for (const [name, input] of [
    ["missing order id", { order: { ...ORDER, id: undefined }, paymentMethod: "upi" }],
    ["missing order", { order: null, paymentMethod: "upi" }],
    ["missing amount", { order: { id: ORDER.id }, paymentMethod: "upi" }],
    ["zero amount", { order: { ...ORDER, amount_minor: 0 }, paymentMethod: "upi" }],
    ["missing currency", { order: { ...ORDER, currency: undefined }, paymentMethod: "upi" }],
    ["method not offered for the order", { order: { ...ORDER, available_payment_methods: ["card"] }, paymentMethod: "upi" }],
    ["no backend-issued key", { order: { ...ORDER, payment_idempotency_key: null }, paymentMethod: "upi" }],
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

describe("order display (AC1, AC2, AC3, AC7, AC8)", () => {
  it("an order without an id, amount or currency is reported, never shown blank", () => {
    assert.deepEqual(validateOrder(ORDER), []);
    assert.match(validateOrder({ ...ORDER, id: undefined }).join(" "), /order ID is missing/);
    assert.match(validateOrder({ ...ORDER, currency: "" }).join(" "), /currency is missing/);
    assert.match(validateOrder({ ...ORDER, amount_minor: null }).join(" "), /amount is missing/);
    assert.ok(validateOrder(null).length >= 3);
  });

  it("formats the backend amount exactly and never without a currency", () => {
    assert.match(formatAmount(349900, "INR"), /3,?499\.00/);
    assert.equal(formatAmount(349900, undefined), null);
    assert.equal(formatAmount(349900, ""), null);
    assert.equal(formatAmount(undefined, "INR"), null);
  });

  it("offers UPI, card and net banking as distinct methods, filtered by the order", () => {
    assert.deepEqual(availableMethods(ORDER).map((m) => m.value), ["upi", "card", "netbanking"]);
    assert.deepEqual(availableMethods({ ...ORDER, available_payment_methods: ["card"] }).map((m) => m.value), ["card"]);
  });
});

describe("initiation request", () => {
  it("POSTs order, method, amount and the backend-issued Idempotency-Key, then goes to checkout", async () => {
    const { controller, calls, navigations, storage } = setup(() => created());
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/v1/payments");
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.headers["Idempotency-Key"], "key-1");
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      order_id: ORDER.id,
      payment_method: "upi",
      amount_minor: 349900,
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

  it("a network failure shows a distinct error and a retry reuses the same key", async () => {
    const { controller, calls, views } = setup((url, opts, n) =>
      n === 1 ? Promise.reject(new TypeError("Failed to fetch")) : created(),
    );
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(views.at(-1).kind, "network");

    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    assert.equal(calls.length, 2);
    assert.equal(calls[1].options.headers["Idempotency-Key"], "key-1");
  });

  it("a retry after an answered error uses the key from the reloaded order", async () => {
    const { controller, calls } = setup((url, opts, n) =>
      n === 1 ? jsonResponse(502, { error: { message: "gateway down" } }) : created(),
    );
    await controller.payNow({ order: ORDER, paymentMethod: "card" });
    // The page reloads the order; the backend rotated the key because that attempt failed.
    await controller.payNow({ order: { ...ORDER, payment_idempotency_key: "key-2" }, paymentMethod: "card" });
    assert.equal(calls[0].options.headers["Idempotency-Key"], "key-1");
    assert.equal(calls[1].options.headers["Idempotency-Key"], "key-2");
  });

  it("a payment the gateway refused is shown as failed, not redirected", async () => {
    const { controller, views, navigations } = setup(() =>
      created({ status: "failed", gateway_checkout_url: null, failure_reason: "Gateway rejected initiation" }),
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

describe("after the gateway checkout: abandoned or returned (REQ-009 AC2, REQ-010, REQ-011, REQ-013)", () => {
  const paymentBody = (status, extra = {}) => jsonResponse(200, { id: PAYMENT_ID, status, ...extra });

  /** POST answers `created()`, every GET /payments answers `statusFor(n)`. */
  function handoffSetup(statusFor) {
    const timers = [];
    let gets = 0;
    const ctx = setup(
      (url, opts) => (opts.method === "POST" ? created() : statusFor(++gets)),
      { setTimer: (fn, ms) => timers.push({ fn, ms }), checkoutTimeoutMs: 1234 },
    );
    return { ...ctx, timers };
  }

  it("arms a checkout timeout only after a successful initiation", async () => {
    const { controller, timers } = handoffSetup(() => paymentBody("processing"));
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(timers.length, 1);
    assert.equal(timers[0].ms, 1234);

    const failing = setup(() => jsonResponse(502, { error: { message: "down" } }), { setTimer: (fn) => timers.push({ fn }) });
    await failing.controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.equal(timers.length, 1);
    assert.deepEqual(failing.navigations, []);
  });

  for (const [backend, shown] of [["processing", "pending"], ["pending", "pending"], ["failed", "failed"]]) {
    it(`abandoned checkout (backend ${backend}) ends on ${shown} instead of hanging, and unlocks Pay Now`, async () => {
      const { controller, calls, views, timers } = handoffSetup(() =>
        paymentBody(backend, { failure_reason: backend === "failed" ? "Checkout abandoned" : null }),
      );
      await controller.payNow({ order: ORDER, paymentMethod: "upi" });
      assert.equal(controller.isInFlight(), true);

      await timers[0].fn(); // customer never came back from the gateway in time
      const last = views.at(-1);
      assert.equal(last.kind, "confirmed");
      assert.equal(last.result.kind, shown);
      assert.equal(last.submitting, false);
      assert.equal(calls.at(-1).url, `/api/v1/payments/${PAYMENT_ID}`); // the id from initiation
      assert.equal(controller.isInFlight(), false);
      if (shown === "failed") assert.equal(last.result.message, "Checkout abandoned");
    });
  }

  it("a success confirmed after return keeps Pay Now locked (no second charge)", async () => {
    const storage = memoryStorage();
    storage.setItem(paymentIdStorageKey(ORDER.id), PAYMENT_ID);
    const ctx = setup((url, opts) => (opts.method === "POST" ? created() : paymentBody("success")), { storage });
    const result = await ctx.controller.resume(ORDER.id);
    assert.equal(result.kind, "success");
    assert.equal(ctx.views.at(-1).locked, true);
    const again = await ctx.controller.payNow({ order: ORDER, paymentMethod: "upi" });
    assert.deepEqual(again, { ignored: true });
    assert.equal(ctx.calls.filter((c) => c.options.method === "POST").length, 0);
  });

  it("returning via Back (resume) confirms the stored payment and cancels the pending timeout check", async () => {
    const { controller, calls, views, timers } = handoffSetup(() => paymentBody("pending"));
    await controller.payNow({ order: ORDER, paymentMethod: "upi" });
    await controller.resume(ORDER.id);
    assert.equal(views.at(-1).result.kind, "pending");
    assert.equal(controller.isInFlight(), false);
    const getsBefore = calls.length;
    await timers[0].fn(); // already handled: no second status call
    assert.equal(calls.length, getsBefore);
  });

  it("resume without a previous attempt sends nothing and leaves Pay Now usable", async () => {
    const { controller, calls } = setup(() => created());
    assert.equal(await controller.resume(ORDER.id), null);
    assert.equal(calls.length, 0);
    assert.equal(controller.isInFlight(), false);
  });

  it("an unknown paymentId on return is shown as an error, never as success", async () => {
    const storage = memoryStorage();
    storage.setItem(paymentIdStorageKey(ORDER.id), PAYMENT_ID);
    const ctx = setup(() => jsonResponse(404, { error: { code: "payment_not_found", message: "Payment not found" } }), { storage });
    const result = await ctx.controller.resume(ORDER.id);
    assert.equal(result.kind, "error");
    assert.equal(ctx.controller.isInFlight(), false);
  });

  it("result page polling re-checks while pending and stops at the confirmed result", async () => {
    const statuses = ["pending", "processing", "success"];
    const { fetch, calls } = fakeFetch((u, o, n) => paymentBody(statuses[n - 1]));
    const seen = [];
    const sleeps = [];
    const view = await pollConfirmedStatus({
      api: createApi(fetch), paymentId: PAYMENT_ID, gatewayHint: "failed",
      onUpdate: (v) => seen.push(v.kind), sleep: async (ms) => sleeps.push(ms), intervalMs: 7,
    });
    assert.equal(view.kind, "success");
    assert.equal(view.overridden, true); // the backend wins over the redirect hint
    assert.deepEqual(seen, ["pending", "pending", "success"]);
    assert.equal(calls.length, 3);
    assert.deepEqual(sleeps, [7, 7]);
  });

  it("result page polling is bounded: a status that stays pending ends on Pending", async () => {
    const { fetch, calls } = fakeFetch(() => paymentBody("processing"));
    const view = await pollConfirmedStatus({ api: createApi(fetch), paymentId: PAYMENT_ID, sleep: async () => {}, attempts: 4 });
    assert.equal(view.kind, "pending");
    assert.equal(calls.length, 4);
  });

  it("result page polling stops at once on an error or failure", async () => {
    for (const response of [jsonResponse(404, {}), paymentBody("failed", { failure_reason: "Declined" })]) {
      const { fetch, calls } = fakeFetch(() => response);
      const view = await pollConfirmedStatus({ api: createApi(fetch), paymentId: PAYMENT_ID, sleep: async () => {} });
      assert.ok(["error", "failed"].includes(view.kind));
      assert.equal(calls.length, 1);
    }
  });

  it("a status request that never answers times out into a network error view", async () => {
    let signal;
    const hanging = (url, options) => {
      signal = options.signal;
      return new Promise(() => {});
    };
    const view = await confirmPaymentStatus({ api: createApi(hanging, "", { timeoutMs: 20 }), paymentId: PAYMENT_ID });
    assert.equal(view.kind, "network");
    assert.equal(signal.aborted, true);
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
