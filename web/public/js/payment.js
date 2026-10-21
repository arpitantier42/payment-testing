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

export function formatAmount(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount / 100);
  } catch {
    return `${(amount / 100).toFixed(2)} ${currency}`;
  }
}

// --- API client ----------------------------------------------------------------

export function createApi(fetchImpl, baseUrl = "") {
  async function request(path, options = {}) {
    let response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, options);
    } catch (err) {
      throw new NetworkError(err);
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

export function validatePaymentInput({ order, paymentMethod }) {
  const errors = [];
  if (!order || !isUuid(order.id)) errors.push("The order ID is missing.");
  if (!order || !Number.isInteger(order.amount) || order.amount <= 0) errors.push("The payable amount is missing.");
  if (!paymentMethod || !METHOD_VALUES.has(paymentMethod)) errors.push("Please select a payment method.");
  return errors;
}

// --- storage keys -------------------------------------------------------------------

export const idempotencyStorageKey = (orderId) => `payment:idempotency-key:${orderId}`;
export const paymentIdStorageKey = (orderId) => `payment:id:${orderId}`;

// --- Pay Now ----------------------------------------------------------------------------

/**
 * Pay Now controller.
 *
 * - Validates input and shows an error instead of calling the API when anything is missing.
 * - Allows exactly one in-flight initiation: the guard is set synchronously, so a second
 *   click (however fast) before the response returns does not send another request.
 * - Reuses the Idempotency-Key for the same attempt when the outcome is unknown (network
 *   error), and starts a fresh key once the backend has answered.
 * - Surfaces every failed call as an error view; never proceeds silently.
 */
export function createPayNowController({ api, storage, newKey, navigate, render }) {
  let inFlight = false;

  async function payNow({ order, paymentMethod }) {
    if (inFlight) return { ignored: true };

    const errors = validatePaymentInput({ order, paymentMethod });
    if (errors.length) {
      render({ kind: "validation", messages: errors, submitting: false });
      return { sent: false };
    }

    inFlight = true;
    render({ kind: "submitting", submitting: true });

    const keyName = idempotencyStorageKey(order.id);
    let key = storage.getItem(keyName);
    if (!key) {
      key = newKey();
      storage.setItem(keyName, key);
    }

    try {
      const payment = await api.createPayment(
        { order_id: order.id, payment_method: paymentMethod, amount: order.amount, currency: order.currency },
        key,
      );
      storage.removeItem(keyName);
      storage.setItem(paymentIdStorageKey(order.id), payment.id);

      if (payment.status === "failed" || !payment.checkout_url) {
        inFlight = false;
        render({
          kind: "failed",
          submitting: false,
          message: payment.failure_reason || "The payment could not be started.",
          paymentId: payment.id,
        });
        return { sent: true, payment };
      }
      // Stay "in flight": the page is leaving for the gateway checkout.
      render({ kind: "redirecting", submitting: true, paymentId: payment.id });
      navigate(payment.checkout_url);
      return { sent: true, payment };
    } catch (err) {
      inFlight = false;
      if (err instanceof NetworkError) {
        // Outcome unknown: keep the key so a retry cannot create a second payment.
        render({ kind: "network", submitting: false, message: "We couldn't reach the payment service. Check your connection and try again." });
      } else {
        storage.removeItem(keyName);
        render({ kind: "api", submitting: false, status: err.status, message: err.message || "The payment could not be started." });
      }
      return { sent: true, error: err };
    }
  }

  return { payNow, isInFlight: () => inFlight };
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
