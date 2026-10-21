import {
  apiErrorMessage,
  availableMethods,
  createApi,
  createPayNowController,
  formatAmount,
  isUuid,
  readAccessToken,
  validateOrder,
} from "./payment.js";

const { token, consumedFromHash } = readAccessToken({ hash: window.location.hash, storage: window.sessionStorage });
// Drop the token from the address bar so it is not bookmarked, shared or kept in history.
if (consumedFromHash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
const api = createApi(window.fetch.bind(window), "", { getToken: () => token });
const params = new URLSearchParams(window.location.search);
const orderId = params.get("orderId");

const els = {
  orderId: document.getElementById("order-id"),
  summary: document.getElementById("order-summary"),
  amount: document.getElementById("order-amount"),
  methods: document.getElementById("methods"),
  form: document.getElementById("pay-form"),
  button: document.getElementById("pay-now"),
  message: document.getElementById("message"),
};

// Order as returned by the backend: amount/currency/methods and the Idempotency-Key
// for the next attempt all come from GET /api/v1/orders/{orderId}, never from here.
let order = null;

function renderMethods(methods) {
  els.methods.replaceChildren();
  for (const m of methods) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio"; // one radio group: only one method can be selected
    input.name = "payment_method";
    input.value = m.value;
    input.dataset.testid = `method-${m.value}`;
    label.append(input, ` ${m.label}`);
    els.methods.append(label);
  }
}

function showMessage(kind, text) {
  els.message.hidden = false;
  els.message.className = `message message-${kind}`;
  els.message.dataset.testid = `message-${kind}`;
  els.message.textContent = text;
}

function showOrderUnavailable(text) {
  order = null;
  els.orderId.textContent = "—";
  els.summary.textContent = "Order details are unavailable.";
  els.amount.textContent = "—";
  els.button.disabled = true;
  renderMethods([]);
  showMessage("error", text);
}

const RETURN_TEXT = {
  success: "Your payment was successful. No further payment is needed.",
  pending: "Your payment is pending confirmation. We'll show the final result once the payment provider confirms it.",
};

function resultLink(paymentId) {
  return `result.html?orderId=${encodeURIComponent(orderId)}&paymentId=${encodeURIComponent(paymentId)}`;
}

// Back from (or never reached) the gateway checkout: show the backend-confirmed status.
function renderConfirmed({ result, paymentId }) {
  const kind = ["success", "pending", "failed"].includes(result.kind) ? result.kind : "error";
  showMessage(kind, RETURN_TEXT[kind] || result.message);
  if (kind === "success" || kind === "pending") {
    const link = document.createElement("a");
    link.href = resultLink(paymentId);
    link.textContent = " View payment status";
    els.message.append(link);
  }
}

function render(view) {
  els.button.disabled = Boolean(view.submitting) || Boolean(view.locked) || !order;
  els.button.textContent = view.submitting ? "Processing…" : "Pay Now";
  switch (view.kind) {
    case "validation":
      showMessage("validation", view.messages.join(" "));
      break;
    case "network":
    case "api":
    case "failed":
      showMessage(view.kind === "failed" ? "failed" : "error", view.message);
      break;
    case "redirecting":
      showMessage("info", "Taking you to the secure checkout…");
      break;
    case "confirmed":
      renderConfirmed(view);
      break;
    default:
      els.message.hidden = true;
  }
  // The backend answered: reload the order so a retry gets the key it now issues
  // (a fresh one if this attempt failed, the same one otherwise).
  if (view.kind === "failed" || view.kind === "api" || (view.kind === "confirmed" && view.result.kind === "failed")) {
    loadOrder({ keepMessage: true });
  }
}

const controller = createPayNowController({
  api,
  storage: window.sessionStorage,
  navigate: (url) => window.location.assign(url),
  render,
});

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const selected = els.form.querySelector("input[name=payment_method]:checked");
  controller.payNow({ order, paymentMethod: selected ? selected.value : null });
});

async function loadOrder({ keepMessage = false } = {}) {
  // The page only works for an order that was placed: no valid order id, no page.
  if (!isUuid(orderId)) {
    showOrderUnavailable("No valid order was found. Please return to your order and try again.");
    return;
  }
  let loaded;
  try {
    loaded = await api.getOrder(orderId);
  } catch (err) {
    showOrderUnavailable(
      err.status === 404
        ? "We couldn't find this order."
        : err.status === 401
          ? apiErrorMessage(err)
          : "We couldn't load your order. Please try again.",
    );
    return;
  }
  const problems = validateOrder(loaded);
  if (problems.length) {
    showOrderUnavailable(`${problems.join(" ")} Please try again later.`);
    return;
  }
  const selected = els.form.querySelector("input[name=payment_method]:checked");
  order = loaded;
  els.orderId.textContent = order.id;
  els.summary.textContent = order.summary;
  els.amount.textContent = formatAmount(order.amount_minor, order.currency);
  renderMethods(availableMethods(order));
  if (selected) {
    const again = els.form.querySelector(`input[name=payment_method][value="${selected.value}"]`);
    if (again) again.checked = true;
  }
  if (!controller.isInFlight()) els.button.disabled = false;
  if (!keepMessage) els.message.hidden = true;
}

els.button.disabled = true;
loadOrder().then(() => {
  // A previous attempt for this order (e.g. checkout abandoned and the page reopened):
  // show its confirmed status rather than a blank form or an endless "Processing…".
  if (order) controller.resume(orderId);
});

// Back button from the gateway restores this page from the back/forward cache with
// Pay Now still "Processing…": confirm the status with the backend instead of hanging.
window.addEventListener("pageshow", (event) => {
  if (event.persisted && order) controller.resume(orderId);
});
