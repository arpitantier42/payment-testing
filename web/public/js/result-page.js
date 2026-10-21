import { confirmPaymentStatus, createApi, resolvePaymentId } from "./payment.js";

const api = createApi(window.fetch.bind(window));
const params = new URLSearchParams(window.location.search);
const orderId = params.get("orderId");
// Gateway redirect parameters are a hint only; they never decide the shown result.
const gatewayHint = params.get("gateway_status") || params.get("status");
const paymentId = resolvePaymentId({
  storage: window.sessionStorage,
  orderId,
  queryPaymentId: params.get("paymentId"),
});

const els = {
  result: document.getElementById("result"),
  note: document.getElementById("note"),
  checkAgain: document.getElementById("check-again"),
  retry: document.getElementById("retry"),
};

const TEXT = {
  success: "Payment successful. Thank you!",
  pending: "Your payment is pending confirmation. We'll update this page once it is confirmed.",
};

function render(view) {
  els.result.className = `result result-${view.kind}`;
  els.result.dataset.testid = `result-${view.kind}`;
  els.result.textContent = TEXT[view.kind] || view.message;

  els.note.hidden = !view.overridden;
  if (view.overridden) {
    els.note.textContent = `The payment provider reported "${view.gatewayReported}", but the confirmed status is shown above.`;
  }
  els.checkAgain.hidden = !["pending", "network"].includes(view.kind);
  const canRetry = orderId && ["failed", "pending"].includes(view.kind);
  els.retry.hidden = !canRetry;
  if (canRetry) els.retry.href = `index.html?orderId=${encodeURIComponent(orderId)}`;
}

async function refresh() {
  els.checkAgain.disabled = true;
  render(await confirmPaymentStatus({ api, paymentId, gatewayHint }));
  els.checkAgain.disabled = false;
}

els.checkAgain.addEventListener("click", refresh);
refresh();
