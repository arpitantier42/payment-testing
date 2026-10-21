import {
  availableMethods,
  createApi,
  createPayNowController,
  formatAmount,
  isUuid,
  validateOrder,
} from "./payment.js";

const api = createApi(window.fetch.bind(window));
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

function render(view) {
  els.button.disabled = Boolean(view.submitting) || !order;
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
    default:
      els.message.hidden = true;
  }
  // The backend answered: reload the order so a retry gets the key it now issues
  // (a fresh one if this attempt failed, the same one otherwise).
  if (view.kind === "failed" || view.kind === "api") loadOrder({ keepMessage: true });
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
    showOrderUnavailable(err.status === 404 ? "We couldn't find this order." : "We couldn't load your order. Please try again.");
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
loadOrder();
