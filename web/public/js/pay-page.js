import {
  PAYMENT_METHODS,
  createApi,
  createPayNowController,
  formatAmount,
} from "./payment.js";

const api = createApi(window.fetch.bind(window));
const params = new URLSearchParams(window.location.search);
const orderId = params.get("orderId");

const els = {
  summary: document.getElementById("order-summary"),
  amount: document.getElementById("order-amount"),
  methods: document.getElementById("methods"),
  form: document.getElementById("pay-form"),
  button: document.getElementById("pay-now"),
  message: document.getElementById("message"),
};

// Order details shown on the page; amount/currency come only from the backend.
let order = orderId ? { id: orderId } : null;

for (const m of PAYMENT_METHODS) {
  const label = document.createElement("label");
  const input = document.createElement("input");
  input.type = "radio";
  input.name = "payment_method";
  input.value = m.value;
  input.dataset.testid = `method-${m.value}`;
  label.append(input, ` ${m.label}`);
  els.methods.append(label);
}

function showMessage(kind, text) {
  els.message.hidden = false;
  els.message.className = `message message-${kind}`;
  els.message.dataset.testid = `message-${kind}`;
  els.message.textContent = text;
}

function render(view) {
  els.button.disabled = Boolean(view.submitting);
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
}

const controller = createPayNowController({
  api,
  storage: window.sessionStorage,
  newKey: () => window.crypto.randomUUID(),
  navigate: (url) => window.location.assign(url),
  render,
});

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const selected = els.form.querySelector("input[name=payment_method]:checked");
  controller.payNow({ order, paymentMethod: selected ? selected.value : null });
});

async function loadOrder() {
  if (!orderId) {
    els.summary.textContent = "No order selected.";
    return;
  }
  try {
    order = await api.getOrder(orderId);
    els.summary.textContent = order.summary;
    els.amount.textContent = formatAmount(order.amount, order.currency);
  } catch (err) {
    els.summary.textContent = "Order details are unavailable.";
    showMessage("error", err.status === 404 ? "We couldn't find this order." : "We couldn't load your order. Please try again.");
  }
}

loadOrder();
