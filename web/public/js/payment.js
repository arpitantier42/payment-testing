// Payment page logic, kept free of DOM access so it runs under `node --test`.
// The page never decides payment success: the final result always comes from
// GET /api/v1/payments/{paymentId}. No gateway secrets are ever needed here.

export const PAYMENT_METHODS = [
  { value: "upi", label: "UPI" },
  { value: "card", label: "Card" },
  { value: "netbanking", label: "Net banking" },
];

const METHOD_VALUES = new Set(PAYMENT_METHODS.map((m) => m.value));
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export class NetworkError extends Error {
  constructor(cause) {
    super("network error");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function isUuid(value) {
  return typeof value === "string" && UUID_RE.test(value);
}

const CURRENCY_RE = /^[A-Z]{3}$/i;

/** Format minor units with their currency; returns null when either is missing (never a bare number). */
export function formatAmount(amount, currency) {
  if (!Number.isInteger(amount) || typeof currency !== "string" || !CURRENCY_RE.test(currency)) return null;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount / 100);
  } catch {
    return `${(amount / 100).toFixed(2)} ${currency}`;
  }
}

// --- API client ----------------------------------------------------------------

export const REQUEST_TIMEOUT_MS = 15000;

/**
 * API client. A request that gets no answer within `timeoutMs` is aborted and reported
 * as a NetworkError, so no view ever waits on the network indefinitely.
 */
export function createApi(fetchImpl, baseUrl = "", { timeoutMs = REQUEST_TIMEOUT_MS, getToken = () => null } = {}) {
  async function request(path, options = {}) {
    // The customer's session token (never a gateway credential) goes in Authorization.
    const token = getToken();
    if (token) options = { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` } };
    let response;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    let timer = null;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        if (controller) controller.abort();
        reject(new Error(`no response within ${timeoutMs} ms`));
      }, timeoutMs);
    });
    try {
      response = await Promise.race([
        fetchImpl(`${baseUrl}${path}`, controller ? { ...options, signal: controller.signal } : options),
        timeout,
      ]);
    } catch (err) {
      throw new NetworkError(err);
    } finally {
      clearTimeout(timer);
    }
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok) {
      const error = body && body.error ? body.error : {};
      throw new ApiError(response.status, error.code || "http_error", error.message || `Request failed (${response.status})`);
    }
    return body;
  }

  return {
    getOrder: (orderId) => request(`/api/v1/orders/${encodeURIComponent(orderId)}`),
    getPayment: (paymentId) => request(`/api/v1/payments/${encodeURIComponent(paymentId)}`),
    createPayment: (payload, idempotencyKey) =>
      request("/api/v1/payments", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(payload),
      }),
  };
}

// --- validation (before any request is sent) --------------------------------------

/** Problems with the order as returned by GET /api/v1/orders/{orderId}; empty when displayable. */
export function validateOrder(order) {
  const errors = [];
  if (!order || !isUuid(order.id)) errors.push("The order ID is missing.");
  if (!order || !Number.isInteger(order.amount_minor) || order.amount_minor <= 0) errors.push("The payable amount is missing.");
  if (!order || typeof order.currency !== "string" || !CURRENCY_RE.test(order.currency)) errors.push("The currency is missing.");
  return errors;
}

/** Methods the backend allows for this order (all three when it does not say). */
export function availableMethods(order) {
  const allowed = order && Array.isArray(order.available_payment_methods) ? order.available_payment_methods : null;
  return PAYMENT_METHODS.filter((m) => !allowed || allowed.includes(m.value));
}

export function validatePaymentInput({ order, paymentMethod }) {
  const errors = validateOrder(order);
  if (!paymentMethod || !METHOD_VALUES.has(paymentMethod)) errors.push("Please select a payment method.");
  else if (order && !availableMethods(order).some((m) => m.value === paymentMethod)) {
    errors.push("This payment method is not available for this order.");
  }
  if (order && errors.length === 0 && !order.payment_idempotency_key) {
    errors.push("This payment can't be started yet. Please reload the page.");
  }
  return errors;
}

// --- storage keys -------------------------------------------------------------------

export const paymentIdStorageKey = (orderId) => `payment:id:${orderId}`;
export const ACCESS_TOKEN_STORAGE_KEY = "payment:access-token";

/**
 * The customer's bearer token for payment-service. Order placement opens the page with
 * `#token=...` (a fragment is never sent to servers or written to access logs); it is kept
 * in sessionStorage for this tab, so the result page after the gateway redirect has it too.
 * Returns `{ token, consumedFromHash }` so the caller can strip the fragment from the URL.
 */
export function readAccessToken({ hash, storage }) {
  const fromHash = new URLSearchParams(String(hash || "").replace(/^#/, "")).get("token");
  if (fromHash) {
    storage.setItem(ACCESS_TOKEN_STORAGE_KEY, fromHash);
    return { token: fromHash, consumedFromHash: true };
  }
  return { token: storage.getItem(ACCESS_TOKEN_STORAGE_KEY), consumedFromHash: false };
}

/** Customer-facing text for an API error (401 = the session token is missing/expired). */
export function apiErrorMessage(err, fallback) {
  if (err && err.status === 401) return "Your session has expired. Please return to your order and open the payment page again.";
  return (err && err.message) || fallback;
}

// --- Pay Now ----------------------------------------------------------------------------

/**
 * Pay Now controller.
 *
 * - Validates input and shows an error instead of calling the API when anything is missing.
 * - Allows exactly one in-flight initiation: the guard is set synchronously, so a second
 *   click (however fast) before the response returns does not send another request.
 * - Sends the Idempotency-Key the backend issued with the order (never one made up here).
 *   The backend keeps that key until the attempt made with it fails, so a retry after a
 *   network error replays the same payment; after a failure the page reloads the order
 *   to receive a fresh key.
 * - Surfaces every failed call as an error view; never proceeds silently.
 */
export const CHECKOUT_TIMEOUT_MS = 10 * 60 * 1000;

export function createPayNowController({
  api,
  storage,
  navigate,
  render,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  checkoutTimeoutMs = CHECKOUT_TIMEOUT_MS,
}) {
  let inFlight = false;
  let handoffPaymentId = null;

  /**
   * The customer is back from (or never reached) the gateway checkout: ask the backend for
   * the confirmed status and show it (Success / Pending / Failed), unlocking Pay Now unless
   * the payment succeeded. Never shows a status the backend did not confirm.
   */
  async function confirmReturn(paymentId) {
    handoffPaymentId = null;
    const result = await confirmPaymentStatus({ api, paymentId, gatewayHint: null });
    inFlight = result.kind === "success"; // a paid order stays locked
    render({ kind: "confirmed", submitting: false, locked: inFlight, result, paymentId });
    return result;
  }

  async function payNow({ order, paymentMethod }) {
    if (inFlight) return { ignored: true };

    const errors = validatePaymentInput({ order, paymentMethod });
    if (errors.length) {
      render({ kind: "validation", messages: errors, submitting: false });
      return { sent: false };
    }

    inFlight = true;
    render({ kind: "submitting", submitting: true });

    try {
      const payment = await api.createPayment(
        { order_id: order.id, payment_method: paymentMethod, amount_minor: order.amount_minor, currency: order.currency },
        order.payment_idempotency_key,
      );
      storage.setItem(paymentIdStorageKey(order.id), payment.id);

      if (payment.status === "failed" || !payment.gateway_checkout_url) {
        inFlight = false;
        render({
          kind: "failed",
          submitting: false,
          message: payment.failure_reason || "The payment could not be started.",
          paymentId: payment.id,
        });
        return { sent: true, payment };
      }
      // Stay "in flight": the page is leaving for the gateway checkout. If the page is
      // still here after the timeout (checkout blocked, abandoned, or the tab restored),
      // fall back to the confirmed status instead of spinning indefinitely (HLD NFR-006).
      render({ kind: "redirecting", submitting: true, paymentId: payment.id });
      handoffPaymentId = payment.id;
      setTimer(() => (handoffPaymentId === payment.id ? confirmReturn(payment.id) : null), checkoutTimeoutMs);
      navigate(payment.gateway_checkout_url);
      return { sent: true, payment };
    } catch (err) {
      inFlight = false;
      if (err instanceof NetworkError) {
        // Outcome unknown: the order still carries the same key, so a retry replays it.
        render({ kind: "network", submitting: false, message: "We couldn't reach the payment service. Check your connection and try again." });
      } else {
        render({ kind: "api", submitting: false, status: err.status, message: err.message || "The payment could not be started." });
      }
      return { sent: true, error: err };
    }
  }

  /** Called when the pay page is shown again (load, or restored via Back) for this order. */
  async function resume(orderId) {
    const paymentId = orderId ? storage.getItem(paymentIdStorageKey(orderId)) : null;
    if (!paymentId) {
      inFlight = false;
      handoffPaymentId = null;
      return null;
    }
    return confirmReturn(paymentId);
  }

  return { payNow, resume, confirmReturn, isInFlight: () => inFlight };
}

// --- confirmed result --------------------------------------------------------------------

const DISPLAY_STATUS = {
  success: "success",
  failed: "failed",
  pending: "pending",
  initiated: "pending",
  processing: "pending",
};

/** Normalise a gateway redirect hint ("success", "captured", "failure", ...) for comparison only. */
function normaliseGatewayHint(hint) {
  if (!hint) return null;
  const h = String(hint).toLowerCase();
  if (["success", "succeeded", "captured", "paid"].includes(h)) return "success";
  if (["failed", "failure", "declined", "cancelled", "canceled"].includes(h)) return "failed";
  return "pending";
}

/**
 * Pick the paymentId to confirm: the one stored from the initiation response wins;
 * the return URL's paymentId is only a fallback (e.g. a new tab).
 */
export function resolvePaymentId({ storage, orderId, queryPaymentId }) {
  const stored = orderId ? storage.getItem(paymentIdStorageKey(orderId)) : null;
  return stored || queryPaymentId || null;
}

/**
 * Ask the backend for the confirmed status. The gateway's redirect hint is never used
 * to decide the result; it is only compared so the page can say it was overridden.
 */
export async function confirmPaymentStatus({ api, paymentId, gatewayHint }) {
  if (!isUuid(paymentId)) {
    return { kind: "error", message: "This payment reference is missing or invalid." };
  }
  let payment;
  try {
    payment = await api.getPayment(paymentId);
  } catch (err) {
    if (err instanceof NetworkError) {
      return { kind: "network", message: "We couldn't reach the payment service to confirm your payment. Please try again." };
    }
    if (err.status === 404) {
      return { kind: "error", message: "We couldn't find this payment. It may be invalid or unknown." };
    }
    return { kind: "error", message: err.message || "We couldn't confirm your payment status." };
  }

  const status = DISPLAY_STATUS[payment.status] || "pending";
  const reported = normaliseGatewayHint(gatewayHint);
  return {
    kind: status,
    payment,
    gatewayReported: reported,
    overridden: reported !== null && reported !== status,
    message: status === "failed" ? payment.failure_reason || "Your payment failed." : undefined,
  };
}

export const POLL_ATTEMPTS = 5;
export const POLL_INTERVAL_MS = 3000;

/**
 * Confirm the status, re-asking while it is still pending, for at most `attempts` calls.
 * Every call's view is passed to `onUpdate`, and the last one is returned: the page always
 * ends on a definite Success / Failed / Pending / error view and never polls forever.
 */
export async function pollConfirmedStatus({
  api,
  paymentId,
  gatewayHint,
  onUpdate = () => {},
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
  attempts = POLL_ATTEMPTS,
  intervalMs = POLL_INTERVAL_MS,
}) {
  let view;
  for (let i = 0; i < attempts; i += 1) {
    if (i > 0) await sleep(intervalMs);
    view = await confirmPaymentStatus({ api, paymentId, gatewayHint });
    onUpdate(view);
    if (view.kind !== "pending") break;
  }
  return view;
}
